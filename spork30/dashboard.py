"""Fullscreen terminal dashboard for simulation and live activity.

Simulation follows the current 5-minute window on the wall clock, driven by
spork30.simulate (real Binance spot, real Polymarket taker prints, modelled
queue). Live mode starts the order loop in this process and draws it.
`--follow` only tails a loop that is already running. Rendering lives in
spork30.screen; this module owns the process plumbing (log tail, sim thread,
keys, redraw).

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
from spork30.screen import build
from spork30.simulate import advance_to_now, run_wall_clock
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
    parser.add_argument("--pace", type=float, default=0.012,
                        help="seconds to sleep per replayed second while the simulation catches up to now (0 = as fast as possible)")
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
        status = "live 5-minute clock, fills modelled from public taker prints"
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
                advance_to_now(spot, config)
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
            worker = threading.Thread(target=run_wall_clock, args=(stop, spot, config),
                                      kwargs={"pace": args.pace}, daemon=True)
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
