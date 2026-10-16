"""Print-driven fills, shared by the replay and the simulation screen.

A resting ask fills only when a taker BUY on that outcome prints at or above
its price, after `queue` shares ahead at the same level have traded. The top
of book is rebuilt from prints (a taker BUY prints at the ask of its token, a
taker SELL at the bid; the Down book mirrors the Up book).

What is measured: prints, sizes, prices, timing. What is modelled, and must
be stated wherever the PnL is shown: queue position, the print-implied touch,
and the assumption that our own asks change nobody else's behaviour.

Nothing here signs, sends, or fetches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from spork30.models import BookTop
from spork30.portfolio import Engine
from spork30.strategy import quantize_price

TICK = Decimal("0.01")


@dataclass(frozen=True)
class Print:
    ts: int
    outcome: str  # "Up" | "Down"
    side: str  # taker side, "BUY" | "SELL"
    price: Decimal
    size: Decimal


def print_from_trade(trade: object, labels: dict[str, str]) -> Print | None:
    """A public `Trade` (polymarket-client) as a Print, or None for another token."""
    outcome = labels.get(str(getattr(trade, "asset_id", "")))
    if outcome not in {"Up", "Down"}:
        return None
    side = trade.side.value if hasattr(trade.side, "value") else str(trade.side)
    return Print(
        int(trade.timestamp.timestamp()),
        outcome,
        side.upper(),
        Decimal(str(trade.price)),
        Decimal(str(trade.size)),
    )


class ReconstructedBook:
    """Up-token top of book implied by the most recent prints on either token."""

    def __init__(self) -> None:
        self.up_ask: Decimal | None = None
        self.up_bid: Decimal | None = None

    def update(self, p: Print) -> None:
        price = quantize_price(p.price, TICK)
        sets_ask = (p.outcome == "Up") == (p.side == "BUY")
        level = price if p.outcome == "Up" else quantize_price(Decimal("1") - price, TICK)
        if sets_ask:
            self.up_ask = level
            # The print is the freshest fact: a bid at or above the new ask is stale.
            if self.up_bid is not None and self.up_bid >= level:
                self.up_bid = level - TICK
        else:
            self.up_bid = level
            if self.up_ask is not None and self.up_ask <= level:
                self.up_ask = level + TICK

    def top(self, now: int) -> BookTop | None:
        if self.up_ask is None:
            return None
        ask = min(Decimal("0.99"), max(TICK, self.up_ask))
        bid = self.up_bid
        if bid is not None and bid >= ask:
            bid = ask - TICK
        return BookTop(bid=bid, ask=ask, observed_ts=now, tick=TICK)


@dataclass
class Resting:
    order_id: str
    outcome: str
    price: Decimal
    queue_ahead: Decimal
    placed_at: int
    # Set when the engine cancelled the order; a print stamped inside
    # [placed_at, retired_at) still met it on the exchange.
    retired_at: int | None = None


# How long a cancelled ask stays matchable against late-indexed prints. The
# public tape ran 15 s to over 2 min behind the exchange on Oct 15 2026.
LATE_PRINT_HORIZON_SECONDS = 600


@dataclass(frozen=True)
class FillRecord:
    """One modelled fill with the state the quote was priced from, for markouts."""

    ts: int
    order_id: str
    outcome: str
    price: Decimal
    size: Decimal
    # Spot-implied P(Up) the engine held when the print hit us (None when quoting the touch).
    fair: Decimal | None
    # Print-implied best ask on this outcome when the order was placed.
    touch: Decimal | None
    seconds_into_window: int
    queue_ahead_at_place: Decimal
    # Spot-implied P(Up) the ask was priced from when it was placed.
    fair_at_place: Decimal | None = None


@dataclass
class FillModel:
    """Match prints against the engine's resting asks for one market."""

    engine: Engine
    slug: str
    queue: Decimal
    book: ReconstructedBook = field(default_factory=ReconstructedBook)
    queue_at_touch_only: bool = False
    window_start: int = 0
    resting: dict[str, Resting] = field(default_factory=dict)
    # Cancelled asks, kept while a late print could still be stamped inside
    # their resting interval. Live stepping sees prints seconds to minutes
    # after they happened, and the engine replaces a touch quote every second
    # or two in a moving market; without this the simulation cancels almost
    # every ask before the print that filled it arrives, and fills nothing.
    # Replay delivers prints in time order, so this path never runs there.
    retired: dict[str, Resting] = field(default_factory=dict)
    fills: int = 0
    late_fills: int = 0
    sold: dict[str, Decimal] = field(default_factory=lambda: {"Up": Decimal("0"), "Down": Decimal("0")})
    proceeds: Decimal = Decimal("0")
    places: int = 0
    rest_seconds: int = 0
    records: list[FillRecord] = field(default_factory=list)
    _touch_at_place: dict[str, Decimal | None] = field(default_factory=dict)
    _queue_at_place: dict[str, Decimal] = field(default_factory=dict)
    _fair_at_place: dict[str, Decimal | None] = field(default_factory=dict)

    def retire(self, order_id: str, now: int) -> None:
        gone = self.resting.pop(order_id, None)
        if gone is not None:
            self.rest_seconds += now - gone.placed_at
            gone.retired_at = now
            self.retired[order_id] = gone
        for old_id in [k for k, r in self.retired.items() if r.retired_at is not None
                       and r.retired_at < now - LATE_PRINT_HORIZON_SECONDS]:
            del self.retired[old_id]

    def on_actions(self, actions: list, now: int, top: BookTop | None = None) -> None:
        """Register the engine's PLACE and CANCEL actions for this market.

        `top` is the book the engine quoted from when it is not the
        print-implied one (the simulation polls the real CLOB); it sets the
        touch recorded against each order.
        """
        for action in actions:
            if action.slug != self.slug:
                continue
            if action.kind == "PLACE":
                self.places += 1
                detail = action.detail
                price = Decimal(detail["price"])
                top = top if top is not None else self.book.top(now)
                touch = None
                if top is not None:
                    if detail["outcome"] == "Up":
                        touch = top.ask
                    elif top.bid is not None:
                        touch = Decimal("1") - top.bid
                # `queue` shares are ahead of us when we join the touch. With
                # queue_at_touch_only an ask resting above the touch starts a
                # fresh level and has none ahead; the uniform model charges the
                # queue at every level.
                ahead = Decimal("0") if (self.queue_at_touch_only and touch is not None and price > touch) else self.queue
                order_id = detail["order_id"]
                self.resting[order_id] = Resting(order_id, detail["outcome"], price, ahead, now)
                self._touch_at_place[order_id] = touch
                self._queue_at_place[order_id] = ahead
                market = self.engine.markets.get(self.slug)
                self._fair_at_place[order_id] = market.last_fair if market is not None else None
            elif action.kind == "CANCEL":
                self.retire(action.detail.get("order_id", ""), now)

    def sweep(self, now: int) -> None:
        """Drop resting entries whose engine order is no longer live."""
        market = self.engine.markets.get(self.slug)
        if market is None:
            return
        for order_id in list(self.resting):
            order = market.orders.get(order_id)
            if order is None or not order.live:
                self.retire(order_id, now)

    def on_print(self, p: Print, *, quoting: bool) -> int:
        """Update the book with one print and fill against it. Returns fills made."""
        self.book.update(p)
        market = self.engine.markets.get(self.slug)
        if not quoting or p.side != "BUY" or market is None:
            return 0
        made = 0
        size_left = p.size
        # Price-time priority: the cheapest of our asks at or under the print
        # price fills first. An ask placed after the print was already on the
        # tape (prints reach the simulation a poll late) cannot have met it,
        # nor can one the engine had already cancelled when the print hit.
        eligible = sorted(
            (
                r
                for r in (*self.resting.values(), *self.retired.values())
                if r.outcome == p.outcome and r.price <= p.price and r.placed_at <= p.ts
                and (r.retired_at is None or p.ts < r.retired_at)
            ),
            key=lambda r: r.price,
        )
        for r in eligible:
            if size_left <= 0:
                break
            ahead = min(size_left, r.queue_ahead)
            r.queue_ahead -= ahead
            size_left -= ahead
            if size_left <= 0:
                break
            order = market.orders.get(r.order_id)
            if order is None:
                continue
            fill = min(size_left, order.remaining)
            if r.retired_at is not None:
                # The exchange matched it before our cancel landed. Inventory
                # already promised to the ask that replaced it stays with that
                # ask, as the venue's balance check would have it.
                held = market.up if r.outcome == "Up" else market.down
                fill = min(fill, held - market.resting(r.outcome))
            if fill <= 0:
                continue
            self.engine.apply_fill(r.order_id, fill, r.price, ts=p.ts, late=r.retired_at is not None)
            self.fills += 1
            if r.retired_at is not None:
                self.late_fills += 1
            made += 1
            self.sold[r.outcome] += fill
            self.proceeds += fill * r.price
            size_left -= fill
            self.records.append(
                FillRecord(
                    ts=p.ts,
                    order_id=r.order_id,
                    outcome=r.outcome,
                    price=r.price,
                    size=fill,
                    fair=market.last_fair,
                    touch=self._touch_at_place.get(r.order_id),
                    seconds_into_window=p.ts - self.window_start,
                    queue_ahead_at_place=self._queue_at_place.get(r.order_id, self.queue),
                    fair_at_place=self._fair_at_place.get(r.order_id),
                )
            )
            if not order.live:
                self.retire(r.order_id, p.ts)
        return made

    def close(self, now: int) -> None:
        for order_id in list(self.resting):
            self.retire(order_id, now)
