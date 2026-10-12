"""Chainlink BTC/USD TWAP-60, the price the 5-minute market resolves on.

Polymarket settles btc-updown-5m by the 60-second Chainlink TWAP versus the
window's open (price to beat). The live quote on this screen is that TWAP.
Price to beat is the same open the event page shows: the crypto-price open
requested with the 60-second TWAP enabled.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

_RTDS = "wss://ws-live-data.polymarket.com"
# type "*" is the live stream. type "update" returns one history dump and then goes quiet.
_SUBSCRIBE = json.dumps(
    {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "*",
                "filters": json.dumps({"symbol": "btc/usd"}, separators=(",", ":")),
            }
        ],
    }
)


def _get(url: str, timeout: float = 4.0) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://polymarket.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def price_to_beat_url(window_start: int) -> str:
    """The event page's target-price query: 60-second TWAP, not the spot open."""
    start = datetime.fromtimestamp(int(window_start), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.fromtimestamp(int(window_start) + 300, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "https://polymarket.com/api/crypto/crypto-price"
        f"?symbol=BTC&eventStartTime={start}&variant=fiveminute&endDate={end}"
        "&twapEnabled=true&twapLookbackSeconds=60"
    )


def open_from_payload(payload: object) -> tuple[Decimal | None, bool]:
    """Return (price to beat, final). A provisional open is shown, then replaced."""
    if not isinstance(payload, dict) or payload.get("openPrice") is None:
        return None, False
    try:
        price = Decimal(str(payload["openPrice"]))
    except (ArithmeticError, ValueError):
        return None, False
    final = payload.get("incomplete") is not True and payload.get("completed") is True
    return price, final


def fetch_open(window_start: int) -> tuple[Decimal | None, bool]:
    try:
        payload = _get(price_to_beat_url(window_start))
    except Exception:
        return None, False
    return open_from_payload(payload)


def fetch_price_to_beat(window_start: int) -> Decimal | None:
    """TWAP-60 open for this 5-minute window, the price Polymarket labels price to beat."""
    price, _final = fetch_open(window_start)
    return price


class SpotCache:
    """Background Chainlink quote. The UI reads a snapshot and never waits."""

    def __init__(self) -> None:
        self.btc: Decimal | None = None
        self.beat: Decimal | None = None
        self.beat_window: int | None = None
        self.asof: float = 0.0
        self.error: str | None = None
        self.extra: dict[int, Decimal] = {}
        self._final: dict[int, bool] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._window: int | None = None
        self._fetched_at: dict[int, float] = {}
        self._fetching = False
        self._thread = threading.Thread(target=self._thread_main, name="chainlink-btc", daemon=True)
        self._thread.start()

    def follow(self, window_start: int | None) -> None:
        start_fetch = False
        with self._lock:
            self._window = window_start
            if window_start is not None and window_start not in self.extra and not self._fetching:
                self._fetching = True
                start_fetch = True
        if start_fetch:
            threading.Thread(target=self._fetch_beat, name="price-to-beat", daemon=True).start()

    def _fetch_beat(self) -> None:
        try:
            self._refresh_open()
        finally:
            with self._lock:
                self._fetching = False

    def remember(self, window: int, price: Decimal | None, final: bool = False) -> None:
        if price is None:
            return
        with self._lock:
            self.extra[window] = price
            self._final[window] = self._final.get(window, False) or final
            self._fetched_at[window] = time.time()
            if self.beat_window is None or self.beat_window == window:
                self.beat = price
                self.beat_window = window

    def price_for(self, window: int) -> Decimal | None:
        with self._lock:
            if window == self.beat_window and self.beat is not None:
                return self.beat
            return self.extra.get(window)

    def snapshot(self) -> tuple[Decimal | None, Decimal | None, int | None, str | None]:
        with self._lock:
            return self.btc, self.beat, self.beat_window, self.error

    def close(self) -> None:
        self._stop.set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            with self._lock:
                if self.btc is None:
                    self.error = "twap unavailable"

    async def _run(self) -> None:
        import websockets

        while not self._stop.is_set():
            try:
                await self._session(websockets)
            except Exception:
                with self._lock:
                    if self.btc is None:
                        self.error = "twap unavailable"
                await asyncio.sleep(2)

    async def _session(self, websockets: object) -> None:
        connect = websockets.connect  # type: ignore[attr-defined]
        async with connect(
            _RTDS,
            additional_headers={"User-Agent": "Mozilla/5.0", "Origin": "https://polymarket.com"},
            open_timeout=8,
        ) as ws:
            await ws.send(_SUBSCRIBE)
            last_ping = time.monotonic()
            while not self._stop.is_set():
                self._refresh_open()
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=1)
                except TimeoutError:
                    message = ""
                if message:
                    self._on_message(message)
                if time.monotonic() - last_ping >= 5:
                    await ws.send("PING")
                    last_ping = time.monotonic()

    def _refresh_open(self) -> None:
        with self._lock:
            window = self._window
            final = self._final.get(window, False) if window is not None else False
            fetched = self._fetched_at.get(window) if window is not None else None
        if window is None or final:
            return
        now = time.time()
        if fetched is not None and now - fetched < 5:
            return
        with self._lock:
            self._fetched_at[window] = now
        price, is_final = fetch_open(window)
        if price is not None:
            self.remember(window, price, final=is_final)

    def _on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        if not message or message in {"PING", "PONG"}:
            return
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("topic") not in (None, "crypto_prices_twap_sixty"):
            return
        body = payload.get("payload")
        if isinstance(body, dict) and body.get("symbol") not in (None, "btc/usd"):
            return
        if isinstance(body, dict) and body.get("window_s") not in (None, 60):
            return
        points: list = []
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            points = body["data"]
        elif isinstance(body, dict) and "value" in body:
            points = [body]
        latest: tuple[int, Decimal] | None = None
        for point in points:
            if not isinstance(point, dict) or "value" not in point:
                continue
            try:
                stamp = int(point.get("timestamp") or 0)
                value = Decimal(str(point["value"]))
            except (TypeError, ValueError, ArithmeticError):
                continue
            if latest is None or stamp >= latest[0]:
                latest = (stamp, value)
        if latest is None:
            return
        with self._lock:
            self.btc = latest[1]
            self.asof = time.time()
            self.error = None
