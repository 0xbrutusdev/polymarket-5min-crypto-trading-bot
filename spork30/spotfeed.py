"""Fast BTC spot for the spot-referenced fair value (U6).

Binance BTCUSDT trade stream over websocket, with a REST poll fallback. The
feed keeps a rolling history so the engine's inputs come from one series:

* `price()`         latest spot and its age;
* `beat_for(start)` the price to beat measured on this series, the TWAP over
                    the 60 s before the window (Polymarket's Chainlink beat
                    sits a steady ~11 bps away from Binance; comparing spot
                    with a beat from another series puts that basis straight
                    into the fair value, see backtest.WindowData.spot_beat);
* `sigma()`         realised per-second volatility over the trailing 300 s.

Nothing here trades. The loop reads snapshots and never blocks on it.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import threading
import time
import urllib.request
from collections import deque
from decimal import Decimal

from spork30.strategy import TWAP_SECONDS

_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
HISTORY_SECONDS = 420
SIGMA_SECONDS = 300
# A quote priced off spot older than this is a quote priced off nothing.
MAX_SPOT_AGE_SECONDS = 3.0


class SpotFeed:
    def __init__(self, *, start: bool = True) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # (unix seconds, last price seen in that second)
        self._seconds: deque[tuple[int, Decimal]] = deque()
        self._last: tuple[float, Decimal] | None = None
        self._beats: dict[int, Decimal] = {}
        self.error: str | None = None
        self.source: str = "none"
        self._thread: threading.Thread | None = None
        if start:
            self._thread = threading.Thread(target=self._thread_main, name="binance-spot", daemon=True)
            self._thread.start()

    # --- readers -----------------------------------------------------------

    def price(self, now: float | None = None) -> tuple[Decimal | None, float]:
        """(latest spot, age in seconds). (None, inf) before the first print."""
        with self._lock:
            last = self._last
        if last is None:
            return None, math.inf
        return last[1], max(0.0, (now if now is not None else time.time()) - last[0])

    def fresh_price(self, now: float | None = None, max_age: float = MAX_SPOT_AGE_SECONDS) -> Decimal | None:
        price, age = self.price(now)
        return price if price is not None and age <= max_age else None

    def beat_for(self, window_start: int) -> Decimal | None:
        """TWAP over the `TWAP_SECONDS` before the window, frozen once computed.

        None until the window has started and at least half of those seconds
        have a print (a feed that connected mid-way has no beat for the window
        in progress and must wait for the next one).
        """
        with self._lock:
            cached = self._beats.get(window_start)
            if cached is not None:
                return cached
            closes = [p for ts, p in self._seconds if window_start - TWAP_SECONDS <= ts < window_start]
            if time.time() < window_start or len(closes) < TWAP_SECONDS // 2:
                return None
            beat = sum(closes, Decimal("0")) / Decimal(len(closes))
            self._beats[window_start] = beat
            for old in [w for w in self._beats if w < window_start - 3 * 300]:
                del self._beats[old]
            return beat

    def sigma(self, seconds: int = SIGMA_SECONDS) -> float | None:
        """Realised per-second volatility of 1-s log returns; None when too thin."""
        with self._lock:
            if not self._seconds:
                return None
            latest = self._seconds[-1][0]
            closes = [float(p) for ts, p in self._seconds if ts > latest - seconds]
        if len(closes) < 30:
            return None
        returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        return statistics.pstdev(returns) if len(returns) >= 2 else None

    def health(self) -> dict:
        price, age = self.price()
        return {"spot": str(price) if price is not None else None,
                "spot_age_s": None if math.isinf(age) else round(age, 1),
                "spot_source": self.source, "spot_error": self.error}

    # --- writers -----------------------------------------------------------

    def record(self, price: Decimal, ts: float | None = None) -> None:
        """Add one print. Public so tests and replays can drive the feed."""
        stamp = ts if ts is not None else time.time()
        second = int(stamp)
        with self._lock:
            self._last = (stamp, price)
            if self._seconds and self._seconds[-1][0] == second:
                self._seconds[-1] = (second, price)
            elif not self._seconds or second > self._seconds[-1][0]:
                self._seconds.append((second, price))
            while self._seconds and self._seconds[0][0] < second - HISTORY_SECONDS:
                self._seconds.popleft()

    def close(self) -> None:
        self._stop.set()

    # --- background --------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # pragma: no cover - thread boundary
            self.error = f"{type(exc).__name__}"

    async def _run(self) -> None:
        try:
            import websockets
        except ImportError:
            websockets = None
        while not self._stop.is_set():
            if websockets is not None:
                try:
                    await self._session(websockets)
                except Exception as exc:
                    self.error = f"ws: {type(exc).__name__}"
            # Fallback: one REST print per second until the socket is back.
            deadline = time.monotonic() + 10
            while not self._stop.is_set() and time.monotonic() < deadline:
                self._poll_once()
                await asyncio.sleep(1)

    async def _session(self, websockets: object) -> None:
        connect = websockets.connect  # type: ignore[attr-defined]
        async with connect(_WS, open_timeout=8, ping_interval=20) as ws:
            self.source = "binance-ws"
            self.error = None
            while not self._stop.is_set():
                message = await asyncio.wait_for(ws.recv(), timeout=5)
                self._on_message(message)

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or "p" not in payload:
            return
        try:
            price = Decimal(str(payload["p"]))
            stamp = int(payload.get("T") or payload.get("E") or 0) / 1000 or time.time()
        except (TypeError, ValueError, ArithmeticError):
            return
        # Exchange timestamps are the print time; the age the loop sees is the
        # local clock minus that, so clock skew shows up as age, not as a
        # silently fresh quote.
        self.record(price, stamp)

    def _poll_once(self) -> None:
        try:
            request = urllib.request.Request(_REST, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.record(Decimal(str(payload["price"])))
            self.source = "binance-rest"
        except Exception as exc:
            self.error = f"rest: {type(exc).__name__}"
