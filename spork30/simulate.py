"""Simulation driver: the real engine, real public taker prints, modelled fills.

The previous simulation lifted 5 shares every 20 s on whichever side BTC was
moving toward. Every fill was adversely selected by construction and the PnL
it produced measured that rule, not the strategy (266 cycles, -158.50, 66% of
sold shares were the eventual winner).

This driver runs the same inputs the live loop sees:

* spot, price to beat and realised vol from the Binance `SpotFeed`;
* the live window's real CLOB top of book, polled once a second (the public
  print tape is indexed 15 s to minutes late, so a touch rebuilt from prints
  is stale; prints are kept for fills, and stand in for the touch only when
  the book poll fails);
* fills only when a taker print trades through a resting ask, after
  `SIM_QUEUE_AHEAD` shares at the same level (default 200, the queue that
  reproduces the reference account's volume), through the same `FillModel`
  as the backtest.

Nothing here signs or sends. Prints and books are public and fetched read-only.
"""

from __future__ import annotations

import os
import threading
import time
from decimal import Decimal
from pathlib import Path

from spork30.backtest import fetch_spot
from spork30.config import AppConfig
from spork30.execution import ClipSchedule
from spork30.fillmodel import FillModel, Print, print_from_trade
from spork30.models import BookTop
from spork30.portfolio import Engine
from spork30.spot import SpotCache
from spork30.spotfeed import SpotFeed

POLL_SECONDS = 2.0
# Prints are re-fetched from this many seconds before the last one seen so a
# late-indexed trade is not missed; duplicates are dropped by hash.
REFETCH_OVERLAP_SECONDS = 5
# Calibrated against the reference account 0x1dd2…51c2 on the 384 overlap
# windows (Oct 11-13 2026): a touch quoter with lean cap 6 replayed at queue
# 200 sells 150.0 sh/w in 20.7 fills, the account sold 153.8 in 23.5
# (q100 282 sh/w, q250 116). `python -m spork30.audit calibrate`.
DEFAULT_QUEUE_AHEAD = Decimal("200")


def queue_ahead_from_env() -> Decimal:
    raw = os.environ.get("SIM_QUEUE_AHEAD", "").strip()
    return Decimal(raw) if raw else DEFAULT_QUEUE_AHEAD


class PrintTape:
    """Background poller for one window's public taker prints.

    `follow(slug, start)` resolves the market once and fetches its history
    from 300 s before the window; afterwards every poll fetches the prints
    since the last one seen. `drain()` hands new prints to the caller in
    time order.
    """

    def __init__(self, client_factory=None, *, poll_seconds: float = POLL_SECONDS, start: bool = True) -> None:
        self._factory = client_factory or _public_client
        self._client = None
        self._poll = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._slug: str | None = None
        self._start: int | None = None
        self._condition: str | None = None
        self._labels: dict[str, str] = {}
        self._seen: set[tuple] = set()
        self._pending: list[Print] = []
        self._last_ts: int | None = None
        self.count = 0
        self.error: str | None = None
        self.last_poll: float = 0.0
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, name="poly-prints", daemon=True)
            self._thread.start()

    def follow(self, slug: str, start: int) -> None:
        with self._lock:
            if slug == self._slug:
                return
            self._slug = slug
            self._start = start
            self._condition = None
            self._labels = {}
            self._seen = set()
            self._pending = []
            self._last_ts = None
            self.count = 0

    def drain(self) -> list[Print]:
        with self._lock:
            out = self._pending
            self._pending = []
        out.sort(key=lambda p: p.ts)
        return out

    def push(self, prints: list[Print]) -> None:
        """Feed prints directly. Tests and the catch-up path use this."""
        with self._lock:
            self._add(prints)

    def health(self) -> dict:
        return {"prints": self.count, "tape_error": self.error,
                "tape_age_s": round(time.time() - self.last_poll, 1) if self.last_poll else None}

    def close(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        with self._lock:
            slug, start, condition = self._slug, self._start, self._condition
            last = self._last_ts
        if slug is None or start is None:
            return
        if self._client is None:
            self._client = self._factory()
        if condition is None:
            market = self._client.get_market(slug=slug)
            labels = {str(market.outcomes.yes.token_id): str(market.outcomes.yes.label),
                      str(market.outcomes.no.token_id): str(market.outcomes.no.label)}
            with self._lock:
                if self._slug != slug:
                    return
                self._condition = condition = str(market.condition_id)
                self._labels = labels
        since = (last - REFETCH_OVERLAP_SECONDS) if last is not None else start - 300
        fresh: list[Print] = []
        for page in self._client.list_trades(condition_id=condition, taker_only=True, start=since, page_size=500):
            for trade in page.items:
                p = print_from_trade(trade, self._labels)
                if p is not None:
                    fresh.append((p, str(getattr(trade, "transaction_hash", "") or "")))
            if last is not None:
                break  # one newest-first page covers a 2 s poll many times over
        with self._lock:
            if self._slug != slug:
                return
            self._add_keyed(fresh)
        self.last_poll = time.time()
        self.error = None

    def _add(self, prints: list[Print]) -> None:
        self._add_keyed([(p, "") for p in prints])

    def _add_keyed(self, keyed: list[tuple[Print, str]]) -> None:
        for p, tx in keyed:
            key = (tx, p.ts, p.outcome, p.side, str(p.price), str(p.size))
            if key in self._seen:
                continue
            self._seen.add(key)
            self._pending.append(p)
            self.count += 1
            if self._last_ts is None or p.ts > self._last_ts:
                self._last_ts = p.ts

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # network: keep polling
                self.error = f"{type(exc).__name__}: {exc}"[:120]
            self._stop.wait(self._poll)


def _public_client():
    from polymarket import PublicClient

    return PublicClient()


# A CLOB snapshot older than this is not used as the touch; the print-implied
# book stands in (and the engine's own stale-book gate applies after 5 s).
BOOK_MAX_AGE_SECONDS = 3.0


class BookPoller:
    """Background poller for the live window's real CLOB top of book.

    The public print tape is indexed with a lag of 15 s to over 2 min (Oct 15
    2026: no new print for 105 s while the real Up ask went 0.37 to 0.84), so
    a touch rebuilt from prints is minutes old while the fair value is live.
    The live loop reads the order book directly; the simulation does the same
    here, once a second for the Up token, and keeps prints for fills only.
    """

    def __init__(self, client_factory=None, *, poll_seconds: float = 1.0, start: bool = True) -> None:
        self._factory = client_factory or _public_client
        self._client = None
        self._poll = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._slug: str | None = None
        self._token: str | None = None
        self._top: BookTop | None = None
        self._asof: float = 0.0
        self.polls = 0
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._run, name="poly-book", daemon=True)
            self._thread.start()

    def follow(self, slug: str) -> None:
        with self._lock:
            if slug == self._slug:
                return
            self._slug = slug
            self._token = None
            self._top = None
            self._asof = 0.0

    def push(self, top: BookTop, asof: float | None = None) -> None:
        """Feed a snapshot directly (tests)."""
        with self._lock:
            self._top = top
            self._asof = asof if asof is not None else time.time()

    def top(self, now: int, *, wall: float | None = None, max_age: float = BOOK_MAX_AGE_SECONDS) -> BookTop | None:
        with self._lock:
            top, asof = self._top, self._asof
        if top is None or (wall if wall is not None else time.time()) - asof > max_age:
            return None
        return BookTop(bid=top.bid, ask=top.ask, observed_ts=now, tick=top.tick)

    def health(self) -> dict:
        with self._lock:
            asof = self._asof
        return {"book_source": "clob" if asof and time.time() - asof <= BOOK_MAX_AGE_SECONDS else "prints",
                "book_age_s": round(time.time() - asof, 1) if asof else None, "book_error": self.error}

    def close(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        with self._lock:
            slug, token = self._slug, self._token
        if slug is None:
            return
        if self._client is None:
            self._client = self._factory()
        if token is None:
            market = self._client.get_market(slug=slug)
            token = str(market.outcomes.yes.token_id)
            with self._lock:
                if self._slug != slug:
                    return
                self._token = token
        book = self._client.get_order_book(token_id=token)
        asks = [Decimal(str(level.price)) for level in book.asks]
        bids = [Decimal(str(level.price)) for level in book.bids]
        ask = min(asks) if asks else None
        if ask is None:
            return
        bid = max(bids) if bids else None
        if bid is not None and bid >= ask:
            bid = None
        tick = None
        raw_tick = getattr(book, "tick_size", None)
        if raw_tick is not None:
            try:
                tick = Decimal(str(raw_tick))
            except ArithmeticError:
                tick = None
        with self._lock:
            if self._slug != slug:
                return
            self._top = BookTop(bid=bid, ask=ask, observed_ts=int(time.time()), tick=tick)
            self._asof = time.time()
        self.polls += 1
        self.error = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # network: keep polling
                self.error = f"{type(exc).__name__}: {exc}"[:120]
            self._stop.wait(self._poll)


class Simulation:
    """Step the engine once per second with real spot and real prints."""

    def __init__(
        self,
        config: AppConfig,
        *,
        chainlink: SpotCache | None = None,
        spot_feed: SpotFeed | None = None,
        tape: PrintTape | None = None,
        books: BookPoller | None = None,
        queue_ahead: Decimal | None = None,
        seed_spot: bool = True,
    ) -> None:
        self.config = config
        self.engine = Engine(config=config, clips=ClipSchedule(config.strategy, seed=30))
        self.chainlink = chainlink
        # The feed is started after the first seed so live prints cannot land
        # ahead of the history they are meant to follow.
        self.feed = spot_feed if spot_feed is not None else SpotFeed(start=False)
        self._start_feed = spot_feed is None
        self.tape = tape if tape is not None else PrintTape()
        # A caller that hands in a stopped tape (tests, --once) gets a stopped
        # book poller too; the print-implied touch then stands in.
        self.books = books if books is not None else BookPoller(start=tape is None or tape._thread is not None)
        self.queue_ahead = queue_ahead if queue_ahead is not None else queue_ahead_from_env()
        self.models: dict[str, FillModel] = {}
        self._seeded: set[int] = set()
        self._primed: set[int] = set()
        self._seed_spot = seed_spot
        self._last_health: int | None = None
        # Per live window: seconds with at least one resting ask, seconds the
        # window has been open, risk refusals and quote pulls. "Quoted" means
        # an ask was resting, not that one was attempted.
        self._uptime_window: int | None = None
        self._quoted_seconds = 0
        self._open_seconds = 0
        self._refusals = 0
        self._pulls = 0
        self._holds = 0

    # --- inputs ------------------------------------------------------------

    def seed_spot(self, window: int) -> None:
        """Load Binance 1-s closes for the five minutes before `window` and the
        window itself, so the beat and realised vol exist from the first tick."""
        if not self._seed_spot or window in self._seeded:
            return
        self._seeded.add(window)
        try:
            for ts, price in fetch_spot(window):
                self.feed.record(price, ts + 0.999)
            if self.feed.source == "none":
                self.feed.source = "binance-1s-klines"
        except Exception as exc:  # network: the feed fills in as it runs
            self.feed.error = f"{type(exc).__name__}: {exc}"[:120]
        if self._start_feed:
            self.feed.start()

    def prime_chainlink(self, window: int) -> None:
        """Fetch the Polymarket beat for this window and the previous one, which
        the previous window's settlement is read against."""
        if self.chainlink is None or window in self._primed:
            return
        self._primed.add(window)
        from spork30.spot import fetch_price_to_beat

        self.chainlink.follow(window)
        for w in (window, window - 300):
            try:
                self.chainlink.remember(w, fetch_price_to_beat(w))
            except Exception:
                pass
        deadline = time.time() + 2.0
        while self.chainlink.snapshot()[0] is None and time.time() < deadline:
            time.sleep(0.05)

    def model_for(self, slug: str, start: int) -> FillModel:
        model = self.models.get(slug)
        if model is None:
            model = FillModel(self.engine, slug, queue=self.queue_ahead, window_start=start)
            self.models[slug] = model
            for old in [s for s, m in self.models.items() if m.window_start < start - 2 * 300]:
                del self.models[old]
        return model

    def previous_resolution(self, current: int, now: int) -> tuple[bool, str | None]:
        """Settlement of the previous window from the Chainlink TWAP the market
        resolves on (the screen's feed), ~65 s after it ends."""
        previous = current - 300
        if self.chainlink is None or now < previous + 365:
            return False, None
        beat = self.chainlink.price_for(previous)
        end_price = self.chainlink.price_for(current)
        if end_price is None:
            end_price = self.chainlink.snapshot()[0]
        if beat is None or end_price is None:
            return False, None
        return True, "Up" if end_price >= beat else "Down"

    # --- stepping ------------------------------------------------------------

    def step(self, now: int, prints: list[Print], *, wall: float | None = None, replay: bool = False) -> None:
        """One engine second. `replay` reads spot as the close of the second
        before `now` (the backtest's 1 s lag); otherwise the freshest print
        against the wall clock, as the live loop does."""
        window = self.config.strategy.window_seconds
        current = now - (now % window)
        slug = f"{self.config.strategy.slug_prefix}-{current}"
        model = self.model_for(slug, current)
        if self.chainlink is not None:
            self.chainlink.follow(current)
        # Prints before this second (catch-up) update the book before the quote.
        early = [p for p in prints if p.ts < now]
        late = [p for p in prints if p.ts >= now]
        for p in early:
            model.on_print(p, quoting=current <= p.ts < current + window)
        if replay:
            spot = self.feed.price_before(now)
        else:
            spot = self.feed.fresh_price(now=wall if wall is not None else now)
        beat = self.feed.beat_for(current)
        resolved, winner = self.previous_resolution(current, now)
        # The real top of book when a fresh snapshot exists; otherwise the
        # print-implied one (catch-up, or the poll failing).
        top = None
        if current <= now < current + window:
            top = None if replay else self.books.top(now, wall=wall)
            if top is None:
                top = model.book.top(now)
        actions = self.engine.step(
            now=now,
            book=top,
            last_up_trade=None,
            resolved=resolved,
            winner=winner,
            emergency_stop=False,
            spot=spot,
            beat=beat,
            sigma=self.feed.sigma(),
        )
        model.on_actions(actions, now, top=top)
        model.sweep(now)
        for p in late:
            model.on_print(p, quoting=current <= p.ts < current + window)
        if self._uptime_window != current:
            self._uptime_window = current
            self._quoted_seconds = self._open_seconds = self._refusals = self._pulls = self._holds = 0
        if current <= now < current + window:
            self._open_seconds += 1
            market = self.engine.markets.get(slug)
            if market is not None and any(o.live for o in market.orders.values()):
                self._quoted_seconds += 1
        for action in actions:
            if action.slug != slug:
                continue
            if action.kind == "RISK_REFUSE":
                self._refusals += 1
            elif action.kind == "QUOTES_PULLED":
                self._pulls += 1
            elif action.kind == "QUOTES_HELD":
                self._holds += 1
        if self._last_health is None or now - self._last_health >= 5:
            self._last_health = now
            self.engine._log({"event": "health", "slug": "", "ts": now, "sim": True,
                              "fill_model": "prints", "queue_ahead": str(self.queue_ahead),
                              "live_orders": len(self.engine.live_orders()),
                              "quoted_seconds": self._quoted_seconds, "open_seconds": self._open_seconds,
                              "refusals": self._refusals, "pulls": self._pulls, "holds": self._holds,
                              "places": model.places, "window_fills": model.fills, "late_fills": model.late_fills,
                              **self.feed.health(), **self.tape.health(), **self.books.health()})

    def catch_up(self, begin: int, now: int, *, pace: float = 0.0) -> None:
        """Replay from `begin` to `now` using the prints fetched so far,
        sleeping `pace` seconds per replayed second so the screen can show it."""
        window = self.config.strategy.window_seconds
        current = now - (now % window)
        self.prime_chainlink(current)
        self.seed_spot(current)
        self.tape.follow(f"{self.config.strategy.slug_prefix}-{current}", current)
        try:
            self.tape.poll_once()
        except Exception as exc:
            self.tape.error = f"{type(exc).__name__}: {exc}"[:120]
        by_ts: dict[int, list[Print]] = {}
        for p in self.tape.drain():
            by_ts.setdefault(max(p.ts, begin), []).append(p)
        for ts in range(begin, now + 1):
            self.step(ts, by_ts.get(ts, []), replay=True)
            if pace > 0:
                time.sleep(pace)

    def follow_window(self, now: int) -> None:
        window = self.config.strategy.window_seconds
        current = now - (now % window)
        self.prime_chainlink(current)
        self.seed_spot(current)
        slug = f"{self.config.strategy.slug_prefix}-{current}"
        self.tape.follow(slug, current)
        self.books.follow(slug)

    def close(self) -> None:
        self.tape.close()
        self.books.close()
        self.feed.close()
        self.engine.save()


def run_wall_clock(stop: threading.Event, chainlink: SpotCache, config: AppConfig, *, pace: float = 0.0) -> None:
    """Follow the current 5-minute window on the wall clock until stopped.

    `pace` slows the initial catch-up (seconds per replayed second); the
    seconds that pass meanwhile are replayed too before live stepping starts.
    """
    sim = Simulation(config, chainlink=chainlink)
    try:
        now = int(time.time())
        window = now - (now % config.strategy.window_seconds)
        begin = window - config.strategy.window_seconds - config.strategy.split_lead_seconds - 2
        sim.catch_up(begin, now, pace=pace)
        last = now
        while not stop.is_set():
            ts = int(time.time())
            if ts != last:
                sim.follow_window(ts)
                prints = sim.tape.drain()
                for missed in range(last + 1, ts + 1):
                    sim.step(missed, prints if missed == ts else [], wall=time.time())
                last = ts
            stop.wait(0.05)
    finally:
        sim.close()


def stop_file_for(config: AppConfig) -> Path:
    """`<BOT_DATA_DIR>/STOP`: touching it ends a headless run cleanly."""
    return Path(config.state_path).parent.parent / "STOP"


def main(argv: list[str] | None = None) -> None:
    """Headless simulation, so several variants can run side by side, each
    with its own BOT_DATA_DIR and knobs, and be scored with
    `python -m spork30.reference simcompare`."""
    import argparse

    from spork30.config import load_config, reset_runtime

    parser = argparse.ArgumentParser(description="Run the simulation without a screen")
    parser.add_argument("--hours", type=float, default=0, help="stop after this long; 0 runs until the STOP file")
    parser.add_argument("--init", action="store_true", help="clear this data dir's decision log and state first")
    parser.add_argument("--stop-file", default="", help="default <BOT_DATA_DIR>/STOP")
    args = parser.parse_args(argv)
    config = load_config()
    if config.live_enabled:
        raise SystemExit("spork30.simulate runs in SIMULATION only; set TRADING_MODE=SIMULATION")
    if args.init:
        reset_runtime(config)
    stop_file = Path(args.stop_file) if args.stop_file else stop_file_for(config)
    if stop_file.exists():
        stop_file.unlink()
    s = config.strategy
    print(f"simulation -> {config.decision_log_path}  mode {s.quote_mode} fair_edge {s.fair_edge} "
          f"lean {s.max_lean_shares} clamp {int(s.lean_size_clamp)} rebalance {int(s.rebalance_at_touch)} "
          f"requote {s.requote_min_seconds}s/{s.requote_threshold_ticks}t sticky {int(s.sticky_clip)} "
          f"underdog {s.underdog_min_price} jump_pause {config.risk.jump_pause_seconds}s "
          f"queue {queue_ahead_from_env()}", flush=True)
    deadline = time.time() + args.hours * 3600 if args.hours > 0 else None
    stop = threading.Event()
    chainlink = SpotCache()
    errors: list[str] = []

    def work() -> None:
        try:
            run_wall_clock(stop, chainlink, config)
        except Exception as exc:  # reported below; the run ends
            errors.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=work, name="simulation", daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            if stop_file.exists() or (deadline is not None and time.time() >= deadline):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop.set()
    worker.join(timeout=15)
    chainlink.close()
    print(f"stopped: {errors[0]}" if errors else "done", flush=True)


def advance_to_now(chainlink: SpotCache, config: AppConfig) -> None:
    """One catch-up to the current second, for a single printed frame."""
    sim = Simulation(config, chainlink=chainlink, spot_feed=SpotFeed(start=False), tape=PrintTape(start=False),
                     books=BookPoller(start=False))
    try:
        now = int(time.time())
        window = now - (now % config.strategy.window_seconds)
        begin = window - config.strategy.window_seconds - config.strategy.split_lead_seconds - 2
        sim.catch_up(begin, now)
    finally:
        sim.close()


if __name__ == "__main__":
    main()
