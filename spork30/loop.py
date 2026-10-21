"""One-second live loop. The venue signs. This module does not.

Simulation builds the same actions and stops before submit. A live run
passes a venue that talks to Polymarket. Tests pass a fake venue.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Callable, Protocol

from spork30.config import AppConfig, load_config
from spork30.execution import ClipSchedule
from spork30.live import LiveTradingDisabled, _redact, assert_live_allowed, live_preflight, read_collateral, submit
from spork30.models import BookTop, Phase
from spork30.portfolio import Engine
from spork30.spotfeed import SpotFeed


class Venue(Protocol):
    def __call__(self, payload: dict) -> str: ...

    def book(self, now: int) -> BookTop | None: ...

    def resolution(self, now: int) -> tuple[bool, str | None]: ...

    def sync_fills(self, engine: Engine, now: int) -> None: ...


# How many consecutive failed ticks before the loop stops trying and pulls
# its orders. One failure is a transient; a run of them is an outage.
MAX_CONSECUTIVE_TICK_ERRORS = 30
# Spacing of the observational `health` record (feed state, pending chain
# transactions, tick duration) the dashboard reads.
HEALTH_EVERY_SECONDS = 5


def run_loop(
    config: AppConfig,
    engine: Engine,
    venue: Venue,
    *,
    now_fn: Callable[[], int] | None = None,
    sleep: Callable[[float], None] | None = None,
    cycles: int | None = None,
    stop: threading.Event | None = None,
    quiet: bool = False,
    cancel_on_exit: bool = True,
    spot_feed: SpotFeed | None = None,
) -> None:
    """Step the strategy and submit every split, quote, cancel, merge, and redeem.

    A tick that raises is logged as `loop_error` and the loop continues; the
    previous version let any exception out of `engine.step` or `sync_fills`
    kill the process with asks still resting on the exchange. On any exit
    (stop event, Ctrl+C, too many consecutive errors) the loop cancels every
    order it believes is live, so nothing keeps filling unattended.

    `spot_feed` supplies the spot-referenced fair value (U6): the fresh spot,
    the feed's own TWAP-60 price to beat for the live window, and realised
    vol. Without it (or with a stale feed) the engine pulls its quotes while
    `fair_edge` > 0 rather than quote blind off the touch.
    """
    assert_live_allowed(config)
    clock = now_fn or (lambda: int(time.time()))
    pause = sleep or time.sleep
    # A venue may know how to resolve an arbitrary window; older venues only
    # answer for the previous one.
    resolve = getattr(venue, "resolution_for", None)
    health = getattr(venue, "health", None)
    window_seconds = config.strategy.window_seconds
    completed = 0
    errors = 0
    last_health = None
    try:
        while cycles is None or completed < cycles:
            if stop is not None and stop.is_set():
                return
            now = clock()
            began = time.monotonic()
            try:
                _reconcile_chain(engine, venue, quiet=quiet)
                venue.sync_fills(engine, now)
                resolved, winner = venue.resolution(now)
                spot = beat = sigma = None
                if spot_feed is not None:
                    # Spot age is measured against the wall clock: the
                    # whole-second tick can be up to a second behind it.
                    spot = spot_feed.fresh_price(now=time.time() if now_fn is None else now)
                    beat = spot_feed.beat_for(now - (now % window_seconds))
                    sigma = spot_feed.sigma()
                actions = engine.step(
                    now=now,
                    book=venue.book(now),
                    last_up_trade=None,
                    resolved=resolved,
                    winner=winner,
                    emergency_stop=False,
                    resolve=resolve,
                    spot=spot,
                    beat=beat,
                    sigma=sigma,
                )
                _dispatch(config, engine, venue, actions, quiet=quiet)
                _dispatch_resting_cancels(config, engine, venue, quiet=quiet)
                errors = 0
                if health is not None and (last_health is None or now - last_health >= HEALTH_EVERY_SECONDS):
                    last_health = now
                    extra = spot_feed.health() if spot_feed is not None else {}
                    engine._log({"event": "health", "slug": "", "ts": now,
                                 "tick_ms": int((time.monotonic() - began) * 1000),
                                 "live_orders": len(engine.live_orders()), **health(), **extra})
            except KeyboardInterrupt:
                return
            except Exception as exc:
                errors += 1
                message = _redact(f"{type(exc).__name__}: {exc}")
                engine._log({"event": "loop_error", "slug": "", "ts": now, "error": message, "consecutive": errors})
                if not quiet:
                    print(f"tick failed ({errors}): {message}", flush=True)
                if errors >= MAX_CONSECUTIVE_TICK_ERRORS:
                    engine._log({"event": "loop_abort", "slug": "", "ts": now, "reason": "consecutive_errors"})
                    return
            finally:
                engine.save()
            completed += 1
            if cycles is None:
                if stop is not None:
                    if stop.wait(1):
                        return
                else:
                    pause(1)
    finally:
        if cancel_on_exit:
            cancel_all_live(config, engine, venue, quiet=quiet)
        # Relayer transactions submitted on the last ticks may still be in
        # flight. Wait for them (bounded) so a failure is rolled back into the
        # saved state instead of being lost with the process.
        drain = getattr(venue, "wait_for_chain", None)
        if drain is not None:
            try:
                drain()
            except Exception:
                pass
        _reconcile_chain(engine, venue, quiet=quiet)
        engine.save()


def _reconcile_chain(engine: Engine, venue: Venue, quiet: bool = False) -> None:
    """Apply terminal outcomes of SPLIT / MERGE / REDEEM sent on earlier ticks.

    The engine books a mint, merge, or redeem when it is sent. A relayer
    failure reported later is handled exactly like a synchronous send
    failure: same `send_fail` event, same rollback, same halt.
    """
    finished = getattr(venue, "finished_transactions", None)
    if finished is None:
        return
    for result in finished():
        payload = result.get("payload") or {}
        kind = str(payload.get("kind") or "")
        slug = str(payload.get("slug") or "")
        if result.get("ok"):
            engine._log({"event": "tx_confirmed", "slug": slug, "kind": kind,
                         "ticket": result.get("ticket", ""), "hash": result.get("hash", "")})
            if not quiet:
                print(f"confirmed {kind} {slug} {result.get('hash', '')}", flush=True)
            continue
        message = _redact(str(result.get("error") or "relayer failure"))
        engine._log({"event": "send_fail", "slug": slug, "kind": kind, "order_id": "",
                     "ticket": result.get("ticket", ""), "error": message})
        if not quiet:
            print(f"failed {kind} {slug}: {message}", flush=True)
        if kind == "SPLIT":
            _undo_split(engine, payload)
        elif kind == "MERGE":
            _undo_merge(engine, payload)
        elif kind == "REDEEM":
            _undo_redeem(engine, payload)


def cancel_all_live(config: AppConfig, engine: Engine, venue: Venue, quiet: bool = False) -> int:
    """Best-effort cancel of every order the engine believes is resting."""
    sent = 0
    for market, order in engine.live_orders():
        order.live = False
        engine._log({"event": "cancel", "slug": market.slug, "order_id": order.order_id, "reason": "shutdown"})
        if not order.exchange_id or order.cancel_sent:
            continue
        _send(
            config,
            engine,
            venue,
            {"kind": "CANCEL", "slug": market.slug, "order_id": order.order_id, "exchange_id": order.exchange_id},
            quiet=quiet,
        )
        sent += 1
    return sent


def run_live(stop: threading.Event | None = None, quiet: bool = False) -> None:
    """Open the Polymarket client and run until the process is stopped."""
    from spork30.gateway import PolymarketVenue

    config = load_config()
    assert_live_allowed(config)
    engine = Engine(config=config, clips=ClipSchedule(config.strategy, seed=30))
    engine.load()
    try:
        collateral = read_collateral(config)
    except Exception as exc:
        collateral = None
        if not quiet:
            print(f"collateral check skipped: {_redact(str(exc))}", flush=True)
    problems = live_preflight(config, collateral=collateral, open_markets=engine.open_count())
    if problems:
        raise LiveTradingDisabled("; ".join(problems))
    venue = PolymarketVenue.from_env(config)
    # The fair value needs ~60 s of spot before a window to measure its beat,
    # so the feed is opened before the loop; the first window after start
    # quotes only once that history exists.
    strategy = config.strategy
    wants_fair = strategy.fair_edge > 0 or (strategy.quote_mode == "touch" and strategy.fair_guard_ticks > 0)
    spot_feed = SpotFeed() if wants_fair else None
    if not quiet:
        print(
            f"live loop split {strategy.split_size} "
            f"lead {strategy.split_lead_seconds}s "
            f"redeem {strategy.redeem_delay_seconds}s "
            f"merge {strategy.flatten_delay_seconds}s "
            f"mode {strategy.quote_mode} fair_edge {strategy.fair_edge} lean_cap {strategy.max_lean_shares}",
            flush=True,
        )
    try:
        run_loop(config, engine, venue, stop=stop, quiet=quiet, spot_feed=spot_feed)
    finally:
        if spot_feed is not None:
            spot_feed.close()
        venue.close()


def _dispatch(config: AppConfig, engine: Engine, venue: Venue, actions: list, quiet: bool = False) -> None:
    for action in actions:
        if action.kind not in {"PLACE", "CANCEL", "SPLIT", "MERGE", "REDEEM"}:
            continue
        payload = _payload(engine, action)
        if payload is None:
            continue
        _send(config, engine, venue, payload, quiet=quiet)


def _dispatch_resting_cancels(config: AppConfig, engine: Engine, venue: Venue, quiet: bool = False) -> None:
    """Flatten and stale-book pulls mark orders dead without a CANCEL action."""
    for market in engine.markets.values():
        for order in market.orders.values():
            if not order.exchange_id or order.live or order.cancel_sent:
                continue
            if order.remaining <= Decimal("0.000001"):
                # Fully filled: nothing rests on the exchange to cancel.
                order.cancel_sent = True
                continue
            _send(
                config,
                engine,
                venue,
                {
                    "kind": "CANCEL",
                    "slug": market.slug,
                    "order_id": order.order_id,
                    "exchange_id": order.exchange_id,
                },
                quiet=quiet,
            )


def _send(config: AppConfig, engine: Engine, venue: Venue, payload: dict, quiet: bool = False) -> None:
    try:
        external_id = submit(config, payload, venue)
    except Exception as exc:
        message = _redact(str(exc))
        engine._log(
            {
                "event": "send_fail",
                "slug": payload["slug"],
                "kind": payload["kind"],
                "order_id": payload.get("order_id", ""),
                "error": message,
            }
        )
        if not quiet:
            print(f"send failed {payload['kind']} {payload['slug']}: {message}", flush=True)
        if payload["kind"] == "PLACE":
            _drop_local_order(engine, payload.get("order_id", ""))
        elif payload["kind"] == "SPLIT":
            _undo_split(engine, payload)
        elif payload["kind"] == "MERGE":
            _undo_merge(engine, payload)
        elif payload["kind"] == "REDEEM":
            _undo_redeem(engine, payload)
        return
    if payload["kind"] == "PLACE":
        order = _find_order(engine, payload["order_id"])
        if order is not None:
            order.exchange_id = external_id
    if payload["kind"] == "CANCEL":
        order = _find_order(engine, payload.get("order_id", ""))
        if order is not None:
            order.cancel_sent = True
    engine._log(
        {
            "event": "send",
            "slug": payload["slug"],
            "kind": payload["kind"],
            "external_id": external_id,
            "order_id": payload.get("order_id", ""),
        }
    )
    if not quiet:
        print(f"sent {payload['kind']} {payload['slug']} {external_id}", flush=True)


def _payload(engine: Engine, action) -> dict | None:
    detail = action.detail
    base = {"kind": action.kind, "slug": action.slug}
    if action.kind == "PLACE":
        return {
            **base,
            "order_id": detail["order_id"],
            "outcome": detail["outcome"],
            "side": detail["side"],
            "price": detail["price"],
            "size": detail["size"],
        }
    if action.kind == "CANCEL":
        order = _find_order(engine, detail.get("order_id", ""))
        if order is None or not order.exchange_id:
            return None
        return {**base, "order_id": order.order_id, "exchange_id": order.exchange_id}
    if action.kind == "SPLIT":
        return {**base, "size": detail["size"]}
    if action.kind == "MERGE":
        if Decimal(detail.get("size") or "0") <= 0:
            return None
        return {**base, "size": detail["size"]}
    if action.kind == "REDEEM":
        # size and winner ride along so a failed send can be rolled back.
        return {**base, "size": detail.get("size", "0"), "winner": detail.get("winner", "")}
    return None


def _undo_split(engine: Engine, payload: dict) -> None:
    """The engine credits a mint before the chain accepts it. A revert undoes that."""
    market = engine.markets.get(payload["slug"])
    if market is None:
        return
    size = Decimal(str(payload.get("size") or "0"))
    if market.up >= size:
        market.up -= size
    if market.down >= size:
        market.down -= size
    market.cash += size
    market.split_size = max(market.split_size - size, Decimal("0"))
    if market.phase not in {Phase.HALTED, Phase.CLOSED}:
        engine.halt(market, "SPLIT_FAILED")


def _undo_merge(engine: Engine, payload: dict) -> None:
    """The engine books a merge before the chain confirms it. Put the pairs back
    and halt the market so the stranded tokens are visible, not reported as cash."""
    market = engine.markets.get(payload["slug"])
    if market is None:
        return
    size = Decimal(str(payload.get("size") or "0"))
    market.up += size
    market.down += size
    market.cash -= size
    market.merged -= size
    market.flatten_done = False
    engine.halt(market, "MERGE_FAILED")


def _undo_redeem(engine: Engine, payload: dict) -> None:
    """Same for a redeem: restore the winner tokens, drop the booked PnL, halt."""
    market = engine.markets.get(payload["slug"])
    if market is None:
        return
    size = Decimal(str(payload.get("size") or "0"))
    winner = str(payload.get("winner") or "")
    booked = market.cash
    if winner in {"Up", "Down"} and size > 0:
        market.add(winner, size)
        market.cash -= size
        market.redeemed -= size
    if market.pnl_booked:
        # _book_cycle counted this market's post-redeem cash; take it back out.
        engine.risk_state.realized_pnl_today -= booked
        market.pnl_booked = False
    market.flatten_done = False
    engine.halt(market, "REDEEM_FAILED")


def _find_order(engine: Engine, order_id: str):
    if not order_id:
        return None
    for market in engine.markets.values():
        order = market.orders.get(order_id)
        if order is not None:
            return order
    return None


def _drop_local_order(engine: Engine, order_id: str) -> None:
    order = _find_order(engine, order_id)
    if order is not None:
        order.live = False
        order.cancel_sent = True
