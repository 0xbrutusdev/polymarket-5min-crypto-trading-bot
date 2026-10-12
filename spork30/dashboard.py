"""Fullscreen terminal dashboard for simulation and live activity.

Simulation follows the current 5-minute window on the wall clock. Live mode
starts the order loop in this process and draws it. `--follow` only tails a
loop that is already running. Rendering lives in spork30.screen; this module
owns the process plumbing (log tail, sim thread, keys, redraw).

    python -m spork30.dashboard
    python -m spork30.dashboard --follow
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from decimal import Decimal 
from pathlib import Path

from rich.console import Console

from spork30.activity import Book, LogTail
from spork30.config import TradingMode, load_config, reset_runtime
from spork30.execution import ClipSchedule
from spork30.models import BookTop
from spork30.portfolio import Engine
from spork30.screen import build
from spork30.spot import SpotCache

def trading_notice(book: Book, log_path: str, now: int) -> str:
    """Why the live screen has no orders. Empty when this window is being quoted."""
    path = Path(log_path)
    if not path.exists() or path.stat().st_size == 0:
        return "order loop is not running. Start it with: python -m spork30.live --run"
    window = now - (now % 300)
    market = book.active(now)
    if market is not None and market.start == window and market.phase != "HALTED":
        return ""
    for line in reversed(book.tape):
        if str(line.slug).endswith(str(window)) and line.event in {"reject", "halt"}:
            return line.detail
    return "no order has been sent for this window"


def live_status() -> str:
    confirm = os.environ.get("LIVE_TRADING_CONFIRM") == "YES"
    key = bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))
    funder = bool(os.environ.get("POLYMARKET_FUNDER"))
    signature = os.environ.get("POLYMARKET_SIGNATURE_TYPE", "3")
    parts = [
        f"confirm {'yes' if confirm else 'no'}",
        f"key {'set' if key else 'missing'}",
        f"funder {'set' if funder else 'missing'}",
        f"sig {signature}",
    ]
    return "  ".join(parts)


def run_wall_clock(stop: threading.Event, spot: SpotCache) -> None:
    """Follow the current 5-minute window on the wall clock until stopped.

    The Up reference is a simulation of the book from BTC versus that window's
    open. It is not a new strategy rule. Demo fills are local tape, not the
    target account.
    """
    config = load_config()
    engine = Engine(config=config, clips=ClipSchedule(config.strategy, seed=30))
    now = int(time.time())
    window = now - (now % config.strategy.window_seconds)
    _prime(spot, window)
    begin = window - config.strategy.window_seconds - config.strategy.split_lead_seconds - 2
    previous_ask: Decimal | None = None
    for ts in range(begin, now + 1):
        if stop.is_set():
            return
        previous_ask = _step_sim(engine, spot, ts, previous_ask)
    last = now
    while not stop.is_set():
        ts = int(time.time())
        if ts != last:
            for missed in range(last + 1, ts + 1):
                previous_ask = _step_sim(engine, spot, missed, previous_ask)
            last = ts
        stop.wait(0.05)
    engine.save()


def advance_to_now(spot: SpotCache) -> None:
    """One catch-up to the current second, for a single printed frame."""
    config = load_config()
    engine = Engine(config=config, clips=ClipSchedule(config.strategy, seed=30))
    now = int(time.time())
    window = now - (now % config.strategy.window_seconds)
    _prime(spot, window)
    begin = window - config.strategy.window_seconds - config.strategy.split_lead_seconds - 2
    previous_ask: Decimal | None = None
    for ts in range(begin, now + 1):
        previous_ask = _step_sim(engine, spot, ts, previous_ask)
    engine.save()


def _prime(spot: SpotCache, window: int) -> None:
    from spork30.spot import fetch_price_to_beat

    spot.follow(window)
    spot.remember(window, fetch_price_to_beat(window))
    spot.remember(window - 300, fetch_price_to_beat(window - 300))
    deadline = time.time() + 2.0
    while spot.snapshot()[0] is None and time.time() < deadline:
        time.sleep(0.05)


def _step_sim(engine: Engine, spot: SpotCache, now: int, previous_ask: Decimal | None) -> Decimal | None:
    window = engine.config.strategy.window_seconds
    current = now - (now % window)
    spot.follow(current)
    btc, beat, beat_window, _error = spot.snapshot()
    if beat_window != current:
        beat = spot.price_for(current)
    ask = _sim_up(btc, beat, previous_ask)
    # One-tick book on the venue's 0.01 grid, as observed live.
    book = BookTop(bid=ask - Decimal("0.01"), ask=ask, observed_ts=now, tick=Decimal("0.01"))
    resolved, winner = _previous_resolution(spot, current, now, btc)
    # The fair value (U6) gets the same Chainlink TWAP the simulated book is
    # built from, against the window's Polymarket beat: one series, no basis.
    engine.step(
        now=now,
        book=book,
        last_up_trade=None,
        resolved=resolved,
        winner=winner,
        emergency_stop=False,
        spot=btc,
        beat=beat,
    )
    _demo_fills(engine, f"{engine.config.strategy.slug_prefix}-{current}", now, current, window, btc, beat)
    return ask


def _sim_up(btc: Decimal | None, beat: Decimal | None, previous: Decimal | None) -> Decimal:
    if btc is None or beat is None or beat <= 0:
        target = Decimal("0.500")
    else:
        shift = (btc - beat) / beat * Decimal("40")
        target = Decimal("0.50") + shift
        if target < Decimal("0.05"):
            target = Decimal("0.05")
        elif target > Decimal("0.95"):
            target = Decimal("0.95")
    target = target.quantize(Decimal("0.01"))
    if previous is None:
        return target
    delta = target - previous
    if delta > Decimal("0.05"):
        delta = Decimal("0.05")
    elif delta < Decimal("-0.05"):
        delta = Decimal("-0.05")
    return (previous + delta).quantize(Decimal("0.01"))


def _previous_resolution(
    spot: SpotCache, current: int, now: int, btc: Decimal | None
) -> tuple[bool, str | None]:
    previous = current - 300
    # The account's redeem-only print is 65 seconds after the window ends.
    if now < previous + 365:
        return False, None
    beat = spot.price_for(previous)
    end_price = spot.price_for(current)
    if end_price is None:
        end_price = btc
    if beat is None or end_price is None:
        return False, None
    return True, "Up" if end_price >= beat else "Down"


def _demo_fills(
    engine: Engine,
    slug: str,
    now: int,
    start: int,
    window: int,
    btc: Decimal | None,
    beat: Decimal | None,
) -> None:
    """Lift 5 shares of the side the spot move favors, every 20 seconds.

    Local demonstration flow so the tape shows sells. Not the target account.
    """
    if not (start <= now < start + window) or (now - start) % 20 != 0 or now == start:
        return
    market = engine.markets.get(slug)
    if market is None:
        return
    favor = "Up" if btc is not None and beat is not None and btc >= beat else "Down"
    for order in list(market.orders.values()):
        if order.live and order.outcome == favor and order.remaining >= 5:
            engine.apply_fill(order.order_id, Decimal("5"), order.price, ts=now)
            return


class _Screen:
    """Redraw in place once a second. A full clear on every quote is the flicker."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._on = False

    def start(self) -> None:
        _enable_vt()
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")
        sys.stdout.flush()
        self._on = True

    def paint(self, layout: object) -> None:
        with self.console.capture() as capture:
            self.console.print(layout)
        sys.stdout.write("\x1b[H")
        sys.stdout.write(capture.get())
        sys.stdout.write("\x1b[J")
        sys.stdout.flush()

    def stop(self) -> None:
        if not self._on:
            return
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        self._on = False


def _enable_vt() -> None:
    if os.name != "nt":
        return
    import ctypes

    handle = ctypes.windll.kernel32.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def _consume_key() -> str | None:
    if os.name == "nt":
        import msvcrt

        if not msvcrt.kbhit():
            return None
        char = msvcrt.getwch()
        return char.lower() if isinstance(char, str) else None
    return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Watch spork30 simulation or live activity")
    parser.add_argument("--follow", action="store_true", help="tail the decision log and do not start a simulation")
    parser.add_argument("--once", action="store_true", help="print one frame and exit, for a non-interactive terminal")
    parser.add_argument("--pace", type=float, default=0.012, help="seconds to sleep per simulated second")
    parser.add_argument("--init", action="store_true", help="clear this mode's decision log and state, then open the screen")
    args = parser.parse_args(argv)
    config = load_config()
    if args.init:
        reset_runtime(config)
    log_path = Path(config.decision_log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    live = config.trading_mode is TradingMode.LIVE
    follow = args.follow or live
    # One interactive live screen starts the loop. --follow and --once only watch.
    sends = live and not args.follow and not args.once and sys.stdout.isatty()
    if live:
        status = live_status()
    elif follow:
        status = "following the decision log"
    else:
        status = "live 5-minute clock, sim sells are local"
        log_path.write_text("", encoding="utf-8")

    stop = threading.Event()
    loop_error: list[str] = []
    limits = {"max_daily_loss": config.risk.max_daily_loss, "max_total_exposure": config.risk.max_total_exposure,
              "maker_rebate_bps": config.strategy.maker_rebate_bps}
    book = Book(require_ack=live)
    tail = LogTail(log_path)
    spot = SpotCache()
    try:
        if args.once or not sys.stdout.isatty():
            if not follow:
                advance_to_now(spot)
            for record in tail.read_new():
                book.apply(record)
            wall = int(time.time())
            btc, beat = _spot_for(book, spot, wall)
            console = Console()
            console.print(
                build(
                    book,
                    mode=config.trading_mode.value,
                    status=status,
                    log_path=str(log_path),
                    emergency=Path(config.emergency_stop_path).exists(),
                    btc=btc,
                    beat=beat,
                    clock=wall,
                    notice=_notice(book, str(log_path), wall, follow, loop_error, sends),
                    sends=sends,
                    limits=limits,
                )
            )
            return

        worker: threading.Thread | None = None
        if sends:
            worker = threading.Thread(target=_embedded_loop, args=(stop, loop_error), name="live-loop", daemon=True)
            worker.start()
        elif not follow:
            worker = threading.Thread(target=run_wall_clock, args=(stop, spot), daemon=True)
            worker.start()

        console = Console()
        screen = _Screen(console)
        screen.start()
        try:
            painted_at: object = -1
            while not stop.is_set():
                for record in tail.read_new():
                    book.apply(record)
                wall = int(time.time())
                btc, beat = _spot_for(book, spot, wall)
                stamp = (wall, str(btc), str(beat))
                if stamp != painted_at:
                    emergency = Path(config.emergency_stop_path).exists()
                    screen.paint(
                        build(
                            book,
                            mode=config.trading_mode.value,
                            status=status,
                            log_path=str(log_path),
                            emergency=emergency,
                            btc=btc,
                            beat=beat,
                            clock=wall,
                            notice=_notice(book, str(log_path), wall, follow, loop_error, sends),
                            sends=sends,
                            limits=limits,
                        )
                    )
                    painted_at = stamp
                key_press = _consume_key()
                if key_press == "q":
                    stop.set()
                    break
                time.sleep(0.05)
        except KeyboardInterrupt:
            stop.set()
        finally:
            screen.stop()
        stop.set()
        if worker is not None:
            worker.join(timeout=2)
    finally:
        spot.close()


def _embedded_loop(stop: threading.Event, loop_error: list[str]) -> None:
    try:
        from spork30.loop import run_live

        run_live(stop=stop, quiet=True)
    except Exception as exc:
        from spork30.live import _redact

        loop_error.append(_redact(str(exc)))


def _notice(book: Book, log_path: str, now: int, follow: bool, loop_error: list[str], sends: bool) -> str:
    if loop_error:
        return loop_error[0]
    if not follow:
        return ""
    notice = trading_notice(book, log_path, now)
    if sends and notice.startswith("order loop is not running"):
        return "order loop is starting"
    return notice


def _spot_for(book: Book, spot: SpotCache, wall: int) -> tuple[Decimal | None, Decimal | None]:
    window = wall - (wall % 300)
    market = book.active(wall)
    if market is not None and market.start is not None and market.start <= wall < market.start + 300:
        window = market.start
    spot.follow(window)
    btc, beat, beat_window, _error = spot.snapshot()
    if beat_window != window:
        beat = spot.price_for(window)
    return btc, beat


if __name__ == "__main__":
    main()
