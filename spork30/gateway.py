"""Polymarket venue. Orders are post-only. Splits use the gasless relayer.

Nothing in this module runs unless the live loop constructs it.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

from spork30.config import AppConfig
from spork30.live import _redact
from spork30.models import BookTop, window_start_for
from spork30.portfolio import Engine

# Gnosis Conditional Tokens on Polygon. payoutDenominator(conditionId) turns
# non-zero the moment the oracle reports, which for btc-updown-5m is about a
# minute after the window closes. This is the signal redeemPositions needs;
# gamma's umaResolutionStatus stays empty for these markets and the CLOB
# `winner` flag lags by a quarter of an hour.
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_SEL_PAYOUT_DENOMINATOR = "dd34de67"
_SEL_PAYOUT_NUMERATORS = "0504c814"
# Give up polling a dead order after this many failed reads.
MAX_SYNC_ATTEMPTS = 5
# Minimum spacing between payout reads for a window that is not resolved yet.
RESOLUTION_POLL_SECONDS = 5.0
# REST poll spacing per order while the user websocket is delivering events.
# Without the feed (or when it is down) every unreconciled order is polled
# each tick, as before.
SLOW_POLL_SECONDS = 10
# How long run_loop waits for in-flight relayer transactions on exit.
CHAIN_DRAIN_SECONDS = 30.0


class GaslessNotConfigured(RuntimeError):
    pass


class ChainWorker:
    """Runs relayer transactions off the loop thread, one at a time, in order.

    SPLIT / MERGE / REDEEM used to call handle.wait() inline, which stalled the
    one-second loop for the life of the transaction (no quote refresh, no
    fill sync) while asks were resting. submit() now returns a ticket at once;
    the loop picks the terminal outcome up from finished() on a later tick and
    rolls the engine back if the transaction failed. Jobs run strictly in
    submission order so a MERGE never races a REDEEM on the same condition.
    """

    def __init__(self, run: Callable[[dict], str]) -> None:
        self._run = run
        self._jobs: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self._done: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._seq = 0
        self._thread: threading.Thread | None = None

    def submit(self, payload: dict) -> str:
        with self._lock:
            self._seq += 1
            ticket = f"tx-{self._seq}"
            self._pending[ticket] = payload
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="chain-worker", daemon=True)
                self._thread.start()
        self._jobs.put((ticket, payload))
        return ticket

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def finished(self) -> list[dict]:
        out: list[dict] = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out

    def wait(self, timeout: float) -> bool:
        """Block until every submitted job has a result, or `timeout` passes."""
        deadline = time.monotonic() + timeout
        while self.pending() and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.pending()

    def _loop(self) -> None:
        # Once a transaction on a market fails, later ones queued for the same
        # market are not sent. A REDEEM after a failed MERGE would redeem the
        # un-merged pairs' winner side too, and the engine's rollback of the
        # merge would then disagree with the chain. Reporting the skip as a
        # failure rolls both back, so local state matches the chain exactly.
        failed: set[str] = set()
        while True:
            ticket, payload = self._jobs.get()
            slug = str(payload.get("slug") or "")
            if slug in failed:
                result = {"ticket": ticket, "payload": payload, "ok": False,
                          "error": "skipped: an earlier transaction on this market failed"}
            else:
                try:
                    tx_hash = self._run(payload)
                    result = {"ticket": ticket, "payload": payload, "ok": True, "hash": tx_hash}
                except Exception as exc:  # the loop thread decides what a failure means
                    failed.add(slug)
                    result = {"ticket": ticket, "payload": payload, "ok": False,
                              "error": _redact(f"{type(exc).__name__}: {exc}")}
            with self._lock:
                self._pending.pop(ticket, None)
            self._done.put(result)


@dataclass
class OrderUpdate:
    """One user-channel observation about an order we placed.

    `size_matched` is cumulative, as the exchange reports it, or None when the
    event only says "something happened to this order" (a trade event naming
    one of our maker orders), in which case the order is polled immediately.
    """

    exchange_id: str
    size_matched: Decimal | None
    status: str
    ts: float


class UserFeed:
    """CLOB user channel on a background thread; the loop drains it in sync_fills.

    `connect` returns an authenticated async client (the private key stays in
    the caller's closure). The SDK reconnects on its own inside one client;
    this wrapper re-creates the client if that gives up. Events are only
    queued here; nothing touches the engine from this thread.
    """

    def __init__(self, connect: Callable[[], Awaitable[Any]], *, backoff: float = 5.0) -> None:
        self._connect = connect
        self._backoff = backoff
        self._queue: "queue.Queue[OrderUpdate]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: Any = None
        self._task: Any = None
        self.connected = False
        self.last_event_at = 0.0
        self.errors: list[str] = []
        self.events_seen = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="user-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def healthy(self) -> bool:
        """Connected on a live thread. The SDK's own heartbeat drops the socket
        when the server goes quiet, which clears `connected` here."""
        alive = self._thread is not None and self._thread.is_alive()
        return bool(alive and self.connected)

    def drain(self) -> dict[str, OrderUpdate]:
        """Latest update per order id since the previous drain."""
        latest: dict[str, OrderUpdate] = {}
        while True:
            try:
                update = self._queue.get_nowait()
            except queue.Empty:
                return latest
            previous = latest.get(update.exchange_id)
            # A trade hint must not overwrite a cumulative size from the same batch.
            if previous is not None and update.size_matched is None and previous.size_matched is not None:
                continue
            latest[update.exchange_id] = update

    def push_event(self, event: Any) -> None:
        """Translate an SDK user event. Also called directly by tests."""
        now = time.monotonic()
        self.last_event_at = now
        self.events_seen += 1
        kind = getattr(event, "type", "")
        payload = getattr(event, "payload", None)
        if payload is None:
            return
        if kind == "order":
            status = str(getattr(payload, "status", "") or "").lower()
            if str(getattr(payload, "order_event_type", "")).upper() == "CANCELLATION":
                status = "canceled"
            self._queue.put(
                OrderUpdate(str(payload.id), Decimal(str(payload.size_matched)), status, now)
            )
        elif kind == "trade":
            for maker in getattr(payload, "maker_orders", None) or ():
                self._queue.put(OrderUpdate(str(maker.order_id), None, "", now))

    def _run(self) -> None:
        import asyncio

        asyncio.run(self._main())

    async def _main(self) -> None:
        import asyncio

        from polymarket.streams import UserSpec

        self._loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            client = None
            try:
                client = await self._connect()
                handle = await client.subscribe(UserSpec())
                self.connected = True
                self.last_event_at = time.monotonic()
                self._task = asyncio.current_task()
                async for event in handle:
                    self.push_event(event)
                    if self._stop.is_set():
                        break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.errors.append(_redact(f"{type(exc).__name__}: {exc}"))
            finally:
                self.connected = False
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(self._backoff)


class Legs:
    """Token ids for one market, plus which payout slot is Up."""

    __slots__ = ("condition", "up", "down", "up_index")

    def __init__(self, condition: str, up: str, down: str, up_index: int) -> None:
        self.condition = condition
        self.up = up
        self.down = down
        self.up_index = up_index

    def as_tuple(self) -> tuple[str, str, str]:
        return self.condition, self.up, self.down


class PolymarketVenue:
    """Signer plus book and resolution reads for the live loop."""

    def __init__(
        self,
        client,
        *,
        rpc: str = "",
        cache_path: Path | None = None,
        chain: ChainWorker | None = None,
        feed: UserFeed | None = None,
    ) -> None:
        self.client = client
        self.rpc = rpc
        self.cache_path = cache_path
        self.chain = chain if chain is not None else ChainWorker(self._run_chain)
        self.feed = feed
        self._tokens: dict[str, Legs] = {}
        # A resolved payout never changes; an unresolved one is re-read at most
        # every RESOLUTION_POLL_SECONDS. Three eth_calls per read otherwise
        # land on every tick of the loop.
        self._resolved: dict[int, tuple[bool, str | None]] = {}
        self._last_resolution_poll: dict[int, float] = {}
        self._last_order_poll: dict[str, int] = {}
        self._load_cache()

    @classmethod
    def from_env(cls, config: AppConfig) -> "PolymarketVenue":
        from polymarket import BuilderApiKey, RelayerApiKey, SecureClient

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY") or ""
        if not private_key or not config.funder:
            raise GaslessNotConfigured("POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER are required")
        api_key = load_or_mint_api_key(
            BuilderApiKey,
            RelayerApiKey,
            lambda: _mint_builder_key(SecureClient, private_key, config.funder),
        )
        client = SecureClient.create(
            private_key=private_key,
            wallet=config.funder,
            api_key=api_key,
        )
        if not client.is_gasless_ready():
            raise GaslessNotConfigured("the relayer rejected this signer; no order will be sent")
        cache = Path(config.state_path).parent / "markets.json"
        feed = None
        if os.environ.get("USER_FEED", "1").strip().lower() not in {"0", "false", "no", "off"}:
            feed = UserFeed(_async_client_factory(private_key, config.funder, client.credentials))
            feed.start()
        return cls(client, rpc=config.polygon_rpc, cache_path=cache, feed=feed)

    def close(self) -> None:
        if self.feed is not None:
            self.feed.stop()

    def health(self) -> dict:
        """Observational snapshot for the health record the loop writes."""
        return {
            "feed": None if self.feed is None else self.feed.healthy(),
            "feed_events": 0 if self.feed is None else self.feed.events_seen,
            "chain_pending": self.chain.pending(),
            "rpc": bool(self.rpc),
        }

    # --- chain transactions ---------------------------------------------------

    def finished_transactions(self) -> list[dict]:
        """Terminal outcomes of SPLIT / MERGE / REDEEM submitted on earlier ticks."""
        return self.chain.finished()

    def wait_for_chain(self, timeout: float = CHAIN_DRAIN_SECONDS) -> bool:
        return self.chain.wait(timeout)

    def _run_chain(self, payload: dict) -> str:
        """Worker thread: submit the relayer transaction and wait for it to land."""
        kind = payload["kind"]
        condition = payload["condition"]
        if kind == "SPLIT":
            handle = self.client.split_position(condition_id=condition, amount=_base_units(payload["size"]))
        elif kind == "MERGE":
            handle = self.client.merge_positions(condition_id=condition, amount=_base_units(payload["size"]))
        elif kind == "REDEEM":
            handle = self.client.redeem_positions(condition_id=condition)
        else:
            raise RuntimeError(f"unsupported action {kind}")
        outcome = handle.wait()
        return str(outcome.transaction_hash)

    def book(self, now: int) -> BookTop | None:
        start = window_start_for(now)
        try:
            legs = self._legs(f"btc-updown-5m-{start}")
            book = self.client.get_order_book(token_id=legs.up)
        except Exception:
            return None
        ask = _best(book.asks, lowest=True)
        if ask is None:
            ask = book.last_trade_price
        if ask is None:
            return None
        # No bid is reported as None. The old fallback of ask - 0.01 invented a
        # bid, and the Down quote is priced off it.
        bid = _best(book.bids, lowest=False)
        observed = now
        if isinstance(book.timestamp, datetime):
            observed = int(book.timestamp.timestamp())
        tick = None
        raw_tick = getattr(book, "tick_size", None)
        if raw_tick is not None:
            try:
                tick = Decimal(str(raw_tick))
            except ArithmeticError:
                tick = None
        return BookTop(bid=bid, ask=ask, observed_ts=observed, tick=tick)

    def resolution(self, now: int) -> tuple[bool, str | None]:
        return self.resolution_for(window_start_for(now) - 300)

    def resolution_for(self, start: int) -> tuple[bool, str | None]:
        """Oracle status for one window, read from the CTF contract.

        Used for the previous window every tick and for any older market the
        engine still holds tokens in. Falls back to gamma's closed-market
        prices when no RPC is configured.
        """
        if start <= 0:
            return False, None
        known = self._resolved.get(start)
        if known is not None:
            return known
        clock = self._monotonic()
        last = self._last_resolution_poll.get(start)
        if last is not None and clock - last < RESOLUTION_POLL_SECONDS:
            return False, None
        self._last_resolution_poll[start] = clock
        slug = f"btc-updown-5m-{start}"
        try:
            legs = self._legs(slug)
        except Exception:
            return False, None
        if self.rpc:
            try:
                result = self._payout(legs)
            except Exception:
                return False, None
        else:
            result = self._gamma_resolution(slug)
        if result[0]:
            self._resolved[start] = result
            self._last_resolution_poll.pop(start, None)
        return result

    @staticmethod
    def _monotonic() -> float:
        import time

        return time.monotonic()

    def _payout(self, legs: Legs) -> tuple[bool, str | None]:
        condition = legs.condition[2:] if legs.condition.startswith("0x") else legs.condition
        condition = condition.rjust(64, "0")
        denominator = _eth_call_uint(self.rpc, _SEL_PAYOUT_DENOMINATOR + condition)
        if denominator == 0:
            return False, None
        numerators = [
            _eth_call_uint(self.rpc, _SEL_PAYOUT_NUMERATORS + condition + hex(i)[2:].rjust(64, "0"))
            for i in (0, 1)
        ]
        up_won = numerators[legs.up_index] > 0
        down_won = numerators[1 - legs.up_index] > 0
        if up_won == down_won:
            # Both or neither: a 50/50 payout or an unexpected shape. Not a
            # redeem-the-winner outcome; leave the market waiting.
            return False, None
        return True, "Up" if up_won else "Down"

    def _gamma_resolution(self, slug: str) -> tuple[bool, str | None]:
        try:
            market = self.client.get_market(slug=slug)
        except Exception:
            market = _first(self.client.list_markets(slug=slug, closed=True, page_size=1))
        if market is None:
            return False, None
        state = getattr(market, "state", None)
        if state is None or not getattr(state, "closed", False):
            return False, None
        for leg in (market.outcomes.yes, market.outcomes.no):
            if Decimal(str(leg.price)) >= Decimal("0.99"):
                return True, str(leg.label)
        return False, None

    def sync_fills(self, engine: Engine, now: int) -> None:
        """Book size_matched for every order the exchange may still know about.

        The user websocket, when connected, delivers cumulative size_matched
        per order; those are applied directly. REST get_order is the fallback:
        every tick for an order the feed has not covered while the feed is
        down, every SLOW_POLL_SECONDS otherwise, and at once when a trade
        event names the order. Dead orders with an exchange id keep being
        reconciled until the exchange confirms a terminal state, so a fill that
        landed between our last poll and our cancel is still booked.
        """
        updates = self.feed.drain() if self.feed is not None else {}
        healthy = self.feed is not None and self.feed.healthy()
        for market in engine.markets.values():
            for order in list(market.orders.values()):
                if not order.exchange_id or order.reconciled:
                    continue
                update = updates.get(order.exchange_id)
                if update is not None and update.size_matched is not None:
                    matched, status = update.size_matched, update.status
                else:
                    due = (not healthy) or update is not None or (
                        now - self._last_order_poll.get(order.exchange_id, -SLOW_POLL_SECONDS) >= SLOW_POLL_SECONDS
                    )
                    if not due:
                        continue
                    self._last_order_poll[order.exchange_id] = now
                    try:
                        remote = self.client.get_order(order_id=order.exchange_id)
                    except Exception:
                        if not order.live:
                            order.sync_attempts += 1
                            if order.sync_attempts >= MAX_SYNC_ATTEMPTS:
                                order.reconciled = True
                                order.cancel_sent = True
                        continue
                    matched = Decimal(str(remote.size_matched))
                    status = str(getattr(remote, "status", "") or "").lower()
                delta = matched - order.filled
                if delta > 0:
                    # Clamp to what is left locally. A remote figure above the
                    # order size would otherwise raise out of the loop.
                    engine.apply_fill(
                        order.order_id, min(delta, order.remaining), order.price, ts=now, late=not order.live
                    )
                if status in {"canceled", "cancelled", "matched"}:
                    order.live = False
                    order.cancel_sent = True
                    order.reconciled = True
                elif not order.live and order.cancel_sent and update is None:
                    # Our cancel went through and this REST poll came after it;
                    # the exchange has nothing further to report.
                    order.reconciled = True
                if order.reconciled:
                    self._last_order_poll.pop(order.exchange_id, None)

    def __call__(self, payload: dict) -> str:
        kind = payload["kind"]
        if kind == "PLACE":
            return self._place(payload)
        if kind == "CANCEL":
            self.client.cancel_order(order_id=payload["exchange_id"])
            return payload["exchange_id"]
        if kind not in {"SPLIT", "MERGE", "REDEEM"}:
            raise RuntimeError(f"unsupported action {kind}")
        # Token lookup stays synchronous (cached after the first call) so an
        # unknown market fails here, where _send rolls the engine back at once.
        condition, _up, _down = self._legs(payload["slug"]).as_tuple()
        return self.chain.submit({**payload, "condition": condition})

    def _place(self, payload: dict) -> str:
        legs = self._legs(payload["slug"])
        token = legs.up if payload["outcome"] == "Up" else legs.down
        response = self.client.place_limit_order(
            token_id=token,
            side="SELL",
            price=payload["price"],
            size=payload["size"],
            post_only=True,
        )
        if not getattr(response, "ok", False):
            message = getattr(response, "message", "order rejected")
            raise RuntimeError(str(message))
        return str(response.order_id)

    def _legs(self, slug: str) -> Legs:
        cached = self._tokens.get(slug)
        if cached is not None:
            return cached
        try:
            market = self.client.get_market(slug=slug)
        except Exception:
            # Finished markets drop out of the plain slug lookup within ~10
            # minutes of close; they are still there with closed=true.
            market = _first(self.client.list_markets(slug=slug, closed=True, page_size=1))
            if market is None:
                raise
        up = down = ""
        up_index = -1
        for index, leg in enumerate((market.outcomes.yes, market.outcomes.no)):
            if leg.label == "Up":
                up = str(leg.token_id)
                up_index = index
            elif leg.label == "Down":
                down = str(leg.token_id)
        if not up or not down or up_index < 0:
            raise RuntimeError(f"{slug} has no Up and Down tokens")
        legs = Legs(str(market.condition_id), up, down, up_index)
        self._tokens[slug] = legs
        self._save_cache()
        return legs

    def _load_cache(self) -> None:
        if self.cache_path is None or not self.cache_path.exists():
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for slug, row in payload.items():
            try:
                self._tokens[slug] = Legs(str(row["condition"]), str(row["up"]), str(row["down"]), int(row["up_index"]))
            except (KeyError, TypeError, ValueError):
                continue

    def _save_cache(self) -> None:
        """Token ids and condition ids outlive the gamma slug lookup. Persisting
        them lets a restarted process redeem a market gamma no longer lists."""
        if self.cache_path is None:
            return
        keep = sorted(self._tokens.items(), key=lambda kv: kv[0])[-200:]
        payload = {
            slug: {"condition": legs.condition, "up": legs.up, "down": legs.down, "up_index": legs.up_index}
            for slug, legs in keep
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass


_BUILDER_ENV = (
    "POLYMARKET_BUILDER_API_KEY",
    "POLYMARKET_BUILDER_SECRET",
    "POLYMARKET_BUILDER_PASSPHRASE",
)


def load_or_mint_api_key(builder_type, relayer_type, mint, env_path: Path | None = None):
    """Use a saved gasless key, or create a builder key from the signed account.

    The CLOB api key is derived inside SecureClient from the private key and is
    not stored. A builder key's secret is returned only when it is created, so
    the first live start writes it into spork30/.env and later starts reuse it.
    """
    existing = _api_key(builder_type, relayer_type)
    if existing is not None:
        return existing
    try:
        minted = mint()
    except GaslessNotConfigured:
        raise
    except Exception as exc:
        raise GaslessNotConfigured(
            f"could not create a builder key from the private key: {_redact(str(exc))}"
        ) from exc
    key = str(getattr(minted, "key", "") or "")
    secret = str(getattr(minted, "secret", "") or "")
    passphrase = str(getattr(minted, "passphrase", "") or "")
    if not key or not secret or not passphrase:
        raise GaslessNotConfigured("builder key response did not include key, secret, and passphrase")
    persist_builder_credentials(key, secret, passphrase, path=env_path)
    return builder_type(key=key, secret=secret, passphrase=passphrase)


def persist_builder_credentials(
    key: str, secret: str, passphrase: str, path: Path | None = None
) -> None:
    values = {
        "POLYMARKET_BUILDER_API_KEY": key,
        "POLYMARKET_BUILDER_SECRET": secret,
        "POLYMARKET_BUILDER_PASSPHRASE": passphrase,
    }
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise GaslessNotConfigured("refusing to store a builder credential that contains a newline")
    for name, value in values.items():
        os.environ[name] = value
    env_path = path or Path(__file__).resolve().parent / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        name = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if name in values:
            out.append(f"{name}={values[name]}")
            seen.add(name)
        else:
            out.append(line)
    missing = [name for name in _BUILDER_ENV if name not in seen]
    if missing and out and out[-1] != "":
        out.append("")
    for name in missing:
        out.append(f"{name}={values[name]}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _async_client_factory(private_key: str, funder: str, credentials) -> Callable[[], Awaitable[Any]]:
    """Closure for the user feed. The key is captured here, not stored on the feed."""

    async def connect():
        from polymarket import AsyncSecureClient

        return await AsyncSecureClient.create(private_key=private_key, wallet=funder, credentials=credentials)

    return connect


def _mint_builder_key(client_type, private_key: str, funder: str):
    bootstrap = client_type.create(private_key=private_key, wallet=funder)
    try:
        return bootstrap.create_builder_api_key()
    finally:
        close = getattr(bootstrap, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


def _api_key(builder_type, relayer_type):
    builder_key = os.environ.get("POLYMARKET_BUILDER_API_KEY") or ""
    builder_secret = os.environ.get("POLYMARKET_BUILDER_SECRET") or ""
    builder_pass = os.environ.get("POLYMARKET_BUILDER_PASSPHRASE") or ""
    if builder_key and builder_secret and builder_pass:
        return builder_type(key=builder_key, secret=builder_secret, passphrase=builder_pass)
    relayer_key = os.environ.get("POLYMARKET_RELAYER_API_KEY") or ""
    relayer_address = os.environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS") or ""
    if relayer_key and relayer_address:
        return relayer_type(key=relayer_key, address=relayer_address)
    return None


def _first(paginator):
    try:
        for item in paginator:
            return item
    except Exception:
        return None
    return None


def _eth_call_uint(rpc: str, data_hex: str) -> int:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": CTF_ADDRESS, "data": "0x" + data_hex}, "latest"]}
    ).encode("utf-8")
    request = urllib.request.Request(rpc, data=body, headers={"Content-Type": "application/json", "User-Agent": "spork30"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    result = str(payload.get("result") or "0x0")
    return int(result, 16) if result not in ("0x", "") else 0


def _best(levels, *, lowest: bool) -> Decimal | None:
    if not levels:
        return None
    prices = [Decimal(str(level.price)) for level in levels]
    return min(prices) if lowest else max(prices)


def _base_units(size: str) -> int:
    units = Decimal(size) * Decimal("1000000")
    if units != units.to_integral_value():
        raise RuntimeError(f"size {size} is not a whole pUSD amount")
    return int(units)
