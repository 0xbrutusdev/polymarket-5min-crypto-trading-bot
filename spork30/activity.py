"""Reduce the decision log into the snapshot the terminal UI draws.

The engine appends one JSON object per line. Simulation and live use that
same file, so the screen does not care which process produced the event.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


def short_slug(slug: str) -> str:
    tail = slug.rsplit("-", 1)[-1]
    return tail if tail.isdigit() else slug[-18:]


@dataclass
class OrderView:
    order_id: str
    outcome: str
    side: str
    price: str
    size: str
    remaining: str
    placed_ts: int = 0


@dataclass
class MarketView:
    slug: str
    phase: str = "DISCOVERED"
    up: Decimal = Decimal("0")
    down: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    sold_up: Decimal = Decimal("0")
    sold_down: Decimal = Decimal("0")
    sold_up_usdc: Decimal = Decimal("0")
    sold_down_usdc: Decimal = Decimal("0")
    winner: str = ""
    halt_reason: str = ""
    split_size: Decimal = Decimal("0")
    reference: Decimal | None = None
    replaces: int = 0
    orders: dict[str, OrderView] = field(default_factory=dict)
    last_quote: dict[str, Decimal] = field(default_factory=dict)
    last: str = ""

    def inventory(self, outcome: str, value: Decimal) -> None:
        if outcome == "Up":
            self.up = value
        elif outcome == "Down":
            self.down = value

    @property
    def start(self) -> int | None:
        tail = self.slug.rsplit("-", 1)[-1]
        return int(tail) if tail.isdigit() else None

    def vwap(self, outcome: str) -> Decimal | None:
        size, usdc = (self.sold_up, self.sold_up_usdc) if outcome == "Up" else (self.sold_down, self.sold_down_usdc)
        if size <= 0:
            return None
        return usdc / size


@dataclass
class TapeLine:
    event: str
    slug: str
    detail: str
    ts: int = 0


@dataclass
class Book:
    """`require_ack` waits for an exchange `send` before a mint, order, or fill counts.

    The decision log also contains simulation lines and intents the chain
    rejected. Live mode sets this so those lines cannot become inventory,
    open orders, or PnL.
    """

    markets: dict[str, MarketView] = field(default_factory=dict)
    tape: deque[TapeLine] = field(default_factory=lambda: deque(maxlen=80))
    counts: dict[str, int] = field(default_factory=dict)
    last_ts: int = 0
    require_ack: bool = False
    # Last `health` record from the loop, and when the log last moved.
    health: dict = field(default_factory=dict)
    realized_today: Decimal | None = None
    _pending: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    _acked_orders: set[str] = field(default_factory=set)

    def _blotter(self, slug: str, event: str, detail: str, ts: int = 0) -> None:
        if slug:
            self.tape.append(TapeLine(event=event, slug=slug, detail=detail, ts=ts))

    def apply(self, record: dict) -> None:
        if self.require_ack:
            routed = self._route_ack(record)
            if routed is None:
                return
            record = routed
        event = str(record.get("event") or "event")
        self.counts[event] = self.counts.get(event, 0) + 1
        slug = str(record.get("slug") or "")
        market = self.markets.get(slug)
        if slug and market is None:
            market = MarketView(slug=slug)
            self.markets[slug] = market
        detail = _detail(event, record)
        if record.get("ts") is not None:
            self.last_ts = int(record["ts"])
        if event in {"loop_error", "loop_abort"}:
            self.tape.append(TapeLine(event="error", slug=slug or "loop", detail=str(record.get("error") or record.get("reason") or event), ts=_ts(record)))
            return
        if event == "health":
            self.health = dict(record)
            return
        if event == "cycle_closed":
            self.realized_today = _dec(record.get("realized_today"))
            return
        if event == "day_roll":
            self.realized_today = Decimal("0")
            self.tape.append(TapeLine(event="day", slug="", detail=f"UTC day {record.get('to')}: realized reset (was {record.get('realized')})", ts=_ts(record)))
            return
        if market is None:
            return
        market.last = detail
        if event == "split":
            market.up = _dec(record.get("up"))
            market.down = _dec(record.get("down"))
            market.cash -= _dec(record.get("size"))
            market.split_size = _dec(record.get("size"))
            market.phase = "QUOTING"
            self._blotter(slug, "split", f"mint {record.get('size')} Up and {record.get('size')} Down", _ts(record))
        elif event == "place":
            order_id = str(record.get("order_id") or "")
            outcome = str(record.get("outcome") or "")
            price = _dec(record.get("price"))
            size = str(record.get("size") or "")
            previous = market.last_quote.get(outcome)
            market.orders[order_id] = OrderView(
                order_id=order_id,
                outcome=outcome,
                side=str(record.get("side") or "SELL"),
                price=str(record.get("price") or ""),
                size=size,
                remaining=size,
                placed_ts=_ts(record),
            )
            market.last_quote[outcome] = price
            if record.get("reference_price") is not None:
                market.reference = _dec(record.get("reference_price"))
            if previous is None:
                self._blotter(slug, "open", f"resting sell {size} {outcome} @ {record.get('price')}", _ts(record))
            else:
                market.replaces += 1
                if abs(price - previous) >= Decimal("0.01"):
                    self._blotter(slug, "reprice", f"{outcome} ask {previous} -> {record.get('price')}", _ts(record))
        elif event == "cancel":
            market.orders.pop(str(record.get("order_id") or ""), None)
        elif event == "risk_refuse":
            self._blotter(slug, "refuse", f"{record.get('outcome')} {record.get('reason')} @ {record.get('price')} x {record.get('size')}", _ts(record))
        elif event == "tx_confirmed":
            tx = str(record.get("hash") or "")
            shown = f"{tx[:10]}..{tx[-4:]}" if len(tx) > 14 else tx
            self._blotter(slug, "confirm", f"{str(record.get('kind') or '').lower()} landed {shown}", _ts(record))
        elif event == "unwind":
            self._blotter(slug, "unwind", f"halted ({record.get('halt_reason')}) but holding {record.get('up')} Up / {record.get('down')} Down: unwinding", _ts(record))
        elif event == "fill":
            outcome = str(record.get("outcome") or "")
            size = _dec(record.get("size"))
            price = _dec(record.get("price"))
            if "inventory" in record:
                market.inventory(outcome, _dec(record.get("inventory")))
            market.cash += size * price
            if outcome == "Up":
                market.sold_up += size
                market.sold_up_usdc += size * price
            elif outcome == "Down":
                market.sold_down += size
                market.sold_down_usdc += size * price
            left = record.get("inventory")
            left_note = f", {left} {outcome} left" if left is not None else ""
            self._blotter(slug, "fill", f"sold {record.get('size')} {outcome} @ {record.get('price')}{left_note}", _ts(record))
            order = market.orders.get(str(record.get("order_id") or ""))
            if order is not None:
                order.remaining = str(record.get("remaining_order") or "0")
                if _dec(order.remaining) <= 0:
                    market.orders.pop(order.order_id, None)
        elif event == "merge":
            market.up = _dec(record.get("up"))
            market.down = _dec(record.get("down"))
            market.cash += _dec(record.get("size"))
            market.orders.clear()
            self._blotter(slug, "merge", f"merged {record.get('size')} pairs back to USDC", _ts(record))
        elif event == "redeem":
            winner = str(record.get("winner") or "")
            amount = _dec(record.get("size"))
            if winner == "Up":
                market.up -= amount
            elif winner == "Down":
                market.down -= amount
            if "cash" in record:
                market.cash = _dec(record.get("cash"))
            market.phase = "CLOSED"
            market.winner = winner
            market.orders.clear()
            self._blotter(slug, "redeem", f"{winner} won, redeemed {record.get('size')}, cycle pnl {record.get('cash')}", _ts(record))
        elif event == "halt":
            market.phase = "HALTED"
            market.halt_reason = str(record.get("reason") or "halt")
            market.orders.clear()
            self._blotter(slug, "halt", market.halt_reason, _ts(record))
        elif event == "transition":
            phase = _phase(str(record.get("note") or ""))
            if phase:
                market.phase = phase
            if phase in {"FLATTEN_PENDING", "CLOSED", "HALTED"}:
                market.orders.clear()

    def _route_ack(self, record: dict) -> dict | None:
        event = str(record.get("event") or "")
        slug = str(record.get("slug") or "")
        if event in {"split", "place", "merge", "redeem"}:
            kind = event.upper()
            order_id = str(record.get("order_id") or "") if event == "place" else ""
            self._pending[(slug, kind, order_id)] = record
            return None
        if event == "fill":
            if str(record.get("order_id") or "") not in self._acked_orders:
                return None
            return record
        if event == "cancel":
            order_id = str(record.get("order_id") or "")
            if order_id and order_id not in self._acked_orders:
                self._pending.pop((slug, "PLACE", order_id), None)
                return None
            return record
        if event == "send":
            kind = str(record.get("kind") or "")
            order_id = str(record.get("order_id") or "") if kind == "PLACE" else ""
            if kind == "PLACE" and order_id:
                self._acked_orders.add(order_id)
            return self._pending.pop((slug, kind, order_id), None)
        if event == "send_fail":
            self._drop_failed(record)
            return None
        return record

    def _drop_failed(self, record: dict) -> None:
        slug = str(record.get("slug") or "")
        kind = str(record.get("kind") or "")
        order_id = str(record.get("order_id") or "")
        if kind == "PLACE" and order_id:
            self._pending.pop((slug, "PLACE", order_id), None)
        elif kind == "PLACE":
            for key in [key for key in self._pending if key[0] == slug and key[1] == "PLACE"]:
                self._pending.pop(key, None)
        else:
            self._pending.pop((slug, kind, ""), None)
        if slug and slug not in self.markets:
            self.markets[slug] = MarketView(slug=slug)
        error = str(record.get("error") or "rejected")
        self._blotter(slug, "reject", f"{kind.lower()} rejected: {error}", _ts(record))
        market = self.markets.get(slug)
        # A failed mint leaves no tokens whether the failure was reported at
        # send time or, with the chain worker, a few ticks after the ack.
        if market is not None and kind == "SPLIT":
            market.up = Decimal("0")
            market.down = Decimal("0")
            market.phase = "HALTED"
            market.orders.clear()

    def traded(self) -> list[MarketView]:
        rows = [market for market in self.markets.values() if market.phase != "DISCOVERED" or market.up or market.down]
        return sorted(rows, key=lambda market: market.start or 0)

    def active(self, clock: int) -> MarketView | None:
        live = [market for market in self.traded() if market.start is not None and market.start <= clock < market.start + 300]
        if live:
            return live[-1]
        rows = self.traded()
        return rows[-1] if rows else None

    def total_pnl(self) -> Decimal:
        return sum((market.cash for market in self.markets.values()), Decimal("0"))

    def realized(self) -> Decimal:
        return sum((market.cash for market in self.markets.values() if market.phase == "CLOSED"), Decimal("0"))

    def open_cash(self) -> Decimal:
        return sum((market.cash for market in self.markets.values() if market.phase != "CLOSED"), Decimal("0"))

    def closed_count(self) -> int:
        return sum(1 for market in self.markets.values() if market.phase == "CLOSED")

    def sold_notional(self) -> Decimal:
        """USDC received for every sell fill, across all windows."""
        return sum((m.sold_up_usdc + m.sold_down_usdc for m in self.markets.values()), Decimal("0"))

    def open_markets(self) -> list[MarketView]:
        """Minted windows still holding tokens or an obligation on-chain."""
        return [m for m in self.traded() if m.phase not in {"CLOSED", "HALTED", "DISCOVERED"} and m.split_size > 0]

    def exposure(self) -> Decimal:
        return sum((m.split_size for m in self.open_markets()), Decimal("0"))

    def resting_orders(self) -> list[tuple[MarketView, OrderView]]:
        return [(m, o) for m in self.traded() for o in m.orders.values()]

    def daily_pnl(self, now: int) -> tuple[str, Decimal]:
        """Cash from every window that opened on this UTC date, settled or still open."""
        from datetime import datetime, timezone

        if not now:
            return "-", Decimal("0")
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        total = Decimal("0")
        for market in self.markets.values():
            if market.start is None:
                continue
            stamp = datetime.fromtimestamp(market.start, tz=timezone.utc).strftime("%Y-%m-%d")
            if stamp == day:
                total += market.cash
        return day, total

    def day_pnl(self) -> tuple[str, Decimal]:
        from datetime import datetime, timezone

        buckets: dict[str, Decimal] = {}
        for market in self.markets.values():
            if market.phase != "CLOSED" or market.start is None:
                continue
            day = datetime.fromtimestamp(market.start, tz=timezone.utc).strftime("%Y-%m-%d")
            buckets[day] = buckets.get(day, Decimal("0")) + market.cash
        if not buckets:
            return "-", Decimal("0")
        day = max(buckets)
        return day, buckets[day]

    def signature(self, btc: object, beat: object, emergency: bool, status: str, clock: int) -> tuple:
        orders = tuple(
            sorted(
                (market.slug, order.outcome, order.price, order.remaining)
                for market in self.markets.values()
                for order in market.orders.values()
            )
        )
        cycles = tuple(
            (market.slug, market.phase, str(market.cash), str(market.sold_up), str(market.sold_down), market.winner)
            for market in self.traded()
        )
        blotter = tuple((line.event, line.slug, line.detail) for line in list(self.tape)[-8:])
        return (orders, cycles, blotter, str(btc), str(beat), emergency, status)


class LogTail:
    """Read newly appended JSON lines. Resets if the file is replaced or truncated.

    Only the bytes past `offset` are read. The previous version read the whole
    file on every call, which is what made the screen crawl once the decision
    log grew (the sim log on this machine is 100 MB and was being re-read
    twenty times a second).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def read_new(self) -> list[dict]:
        if not self.path.exists():
            self.offset = 0
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read(size - self.offset)
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        complete = chunk[: end + 1]
        self.offset += len(complete)
        records: list[dict] = []
        for line in complete.decode("utf-8", "replace").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records


def _ts(record: dict) -> int:
    try:
        return int(record.get("ts") or 0)
    except (TypeError, ValueError):
        return 0


def _dec(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _phase(note: str) -> str:
    if "->" not in note:
        return note.split(":", 1)[0] if note else ""
    return note.split("->", 1)[1].split(":", 1)[0]


def _detail(event: str, record: dict) -> str:
    if event == "split":
        return f"mint {record.get('size')}  lead {record.get('lead_seconds')}s"
    if event == "place":
        return f"{record.get('outcome')} {record.get('side')} {record.get('price')} x {record.get('size')}"
    if event == "fill":
        return f"{record.get('outcome')} {record.get('price')} x {record.get('size')}"
    if event == "cancel":
        return str(record.get("reason") or "cancel")
    if event == "merge":
        return f"overlap {record.get('size')}"
    if event == "redeem":
        return f"{record.get('winner')} {record.get('size')}"
    if event == "halt":
        return str(record.get("reason") or "halt")
    if event == "risk_refuse":
        return f"{record.get('outcome')} {record.get('reason')}"
    if event == "transition":
        return str(record.get("note") or "")
    return event
