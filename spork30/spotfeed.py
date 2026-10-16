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

# Trades carry the exchange print time; the book ticker (best bid/ask) updates
# many times a second even when nobody trades. In quiet seconds BTCUSDT goes
# 2-3 s without a trade (measured Oct 15 2026: p99 inter-trade gap 3.1 s),
# which the trade stream alone reported as stale spot, pulled the quotes and
# re-posted them at the back of the queue. The mid of the book ticker is the
# current price in those seconds.
_WS = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@bookTicker"
_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
HISTORY_SECONDS = 420
SIGMA_SECONDS = 300
# A quote priced off spot older than this is a quote priced off nothing.
# Replay on 288 windows Oct 12-13 2026 (edge 0.03, queue 100): spot 1 s old
# +0.42 per window, 3 s old -1.07 (t -4.3), 5 s old -3.14.
MAX_SPOT_AGE_SECONDS = 1.5


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
        self.trades: int = 0
        self.ticks: int = 0
        self._thread: threading.Thread | None = None
        if start:
            self.start()

    def start(self) -> None:
        """Open the websocket. Separate from __init__ so history can be seeded first."""
        if self._thread is not None:
            return
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

    def price_before(self, ts: int, max_age: float = MAX_SPOT_AGE_SECONDS) -> Decimal | None:
        """Close of the latest second strictly before `ts`, as a replay at `ts`
        could have known it; None when that second is more than `max_age` old."""
        with self._lock:
            for second, price in reversed(self._seconds):
                if second < ts:
                    return price if ts - second <= max_age else None
        return None

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
                "spot_source": self.source, "spot_error": self.error,
                "spot_trades": self.trades, "spot_ticks": self.ticks}

    # --- writers -----------------------------------------------------------

    def record(self, price: Decimal, ts: float | None = None) -> None:
        """Add one print. Public so tests and replays can drive the feed."""
        stamp = ts if ts is not None else time.time()
        second = int(stamp)
        with self._lock:
            if self._last is None or stamp >= self._last[0]:
                self._last = (stamp, price)
            if self._seconds and self._seconds[-1][0] == second:
                self._seconds[-1] = (second, price)
            elif not self._seconds or second > self._seconds[-1][0]:
                self._seconds.append((second, price))
            else:
                # Seeded history arriving behind live prints: keep the series
                # ordered, and do not overwrite a second the feed already saw.
                if all(existing != second for existing, _ in self._seconds):
                    rows = list(self._seconds)
                    index = next(i for i, (existing, _) in enumerate(rows) if existing > second)
                    rows.insert(index, (second, price))
                    self._seconds = deque(rows)
            newest = self._seconds[-1][0]
            while self._seconds and self._seconds[0][0] < newest - HISTORY_SECONDS:
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
        if not isinstance(payload, dict):
            return
        # Combined streams wrap the event: {"stream": "...", "data": {...}}.
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        try:
            if "p" in data:
                # Trade. Exchange timestamps are the print time; the age the
                # loop sees is the local clock minus that, so clock skew shows
                # up as age, not as a silently fresh quote.
                price = Decimal(str(data["p"]))
                stamp = int(data.get("T") or data.get("E") or 0) / 1000 or time.time()
                self.trades += 1
            elif "b" in data and "a" in data:
                # Book ticker: mid of the best bid and ask. The raw stream has
                # no event time; receipt time is used (0.14 s behind the
                # exchange on this machine, measured Oct 15 2026).
                price = (Decimal(str(data["b"])) + Decimal(str(data["a"]))) / 2
                stamp = int(data.get("E") or 0) / 1000 or time.time()
                self.ticks += 1
            else:
                return
        except (TypeError, ValueError, ArithmeticError):
            return
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
