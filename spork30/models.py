"""Explicit market and order state."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Phase(str, Enum):
    DISCOVERED = "DISCOVERED"
    SPLIT_PENDING = "SPLIT_PENDING"
    QUOTING = "QUOTING"
    FLATTEN_PENDING = "FLATTEN_PENDING"
    REDEEM_PENDING = "REDEEM_PENDING"
    CLOSED = "CLOSED"
    HALTED = "HALTED"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderKind(str, Enum):
    LIMIT_MAKER = "LIMIT_MAKER"


@dataclass
class BookTop:
    """Best prices the execution policy is allowed to see.

    bid/ask are for one outcome token. None means the side was not observed.
    """

    bid: Decimal | None
    ask: Decimal | None
    observed_ts: int
    # Venue tick for this token right now (0.01 mid-range, 0.001 near 0/1).
    # None means unknown; the strategy falls back to its configured tick.
    tick: Decimal | None = None


@dataclass
class DesiredOrder:
    outcome: str
    side: Side
    price: Decimal
    size: Decimal
    kind: OrderKind
    rule: str
    reference_price: Decimal
    position: Decimal
    risk_checks: list[str]
    # Best bid on this outcome's own book at pricing time, if known. A sell
    # at or below it is marketable and must not be sent post-only.
    touch_bid: Decimal | None = None


@dataclass
class WorkingOrder:
    order_id: str
    outcome: str
    side: Side
    price: Decimal
    size: Decimal
    filled: Decimal = Decimal("0")
    live: bool = True
    exchange_id: str = ""
    cancel_sent: bool = False
    # True once the exchange has confirmed a terminal state (matched or
    # cancelled) and any late fill has been applied. Dead-but-unreconciled
    # orders keep being polled so a fill that landed between our last poll
    # and our cancel is not lost.
    reconciled: bool = False
    sync_attempts: int = 0
    # Engine clock when the order was placed; 0 for orders from older state files.
    placed_ts: int = 0

    @property
    def remaining(self) -> Decimal:
        return self.size - self.filled


@dataclass
class MarketPosition:
    slug: str
    start: int
    phase: Phase = Phase.DISCOVERED
    split_size: Decimal = Decimal("0")
    up: Decimal = Decimal("0")
    down: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    split_submitted: bool = False
    flatten_done: bool = False
    winner: str | None = None
    resolved: bool = False
    orders: dict[str, WorkingOrder] = field(default_factory=dict)
    last_reference: Decimal | None = None
    # Spot-implied P(Up) used for the last quote refresh (None when U6 is off).
    last_fair: Decimal | None = None
    last_book_ts: int | None = None
    # First tick at which the spot reference went missing while quoting; the
    # resting asks are held for `spot_stale_grace_seconds` from here.
    spot_lost_since: int | None = None
    halt_reason: str | None = None
    pnl_booked: bool = False
    # Cash legs, so a closed cycle can be checked against
    # proceeds + merged + redeemed - split_size == cash.
    proceeds: Decimal = Decimal("0")
    sold_up: Decimal = Decimal("0")
    sold_down: Decimal = Decimal("0")
    merged: Decimal = Decimal("0")
    redeemed: Decimal = Decimal("0")
    # Last fill time per outcome (H4 cooldown). Not persisted: a restart
    # simply starts without a cooldown.
    last_fill_ts: dict[str, int] = field(default_factory=dict)
    # Price-jump pause: quoting resumes at this engine second, and the number
    # of jumps seen this window. Not persisted either.
    jump_paused_until: int | None = None
    jumps: int = 0
    transitions: list[str] = field(default_factory=list)

    def inventory(self, outcome: str) -> Decimal:
        if outcome == "Up":
            return self.up
        if outcome == "Down":
            return self.down
        raise KeyError(outcome)

    def add(self, outcome: str, delta: Decimal) -> None:
        if outcome == "Up":
            self.up += delta
        elif outcome == "Down":
            self.down += delta
        else:
            raise KeyError(outcome)

    def overlap(self) -> Decimal:
        return min(self.up, self.down)

    def resting(self, outcome: str, *, exclude: str | None = None) -> Decimal:
        """Shares of `outcome` already committed to sells the exchange may still fill.

        Counts live orders and dead orders the exchange has not yet confirmed
        as terminal (a replace sends the new ask before the old cancel is
        acknowledged; both can fill). `exclude` skips one order id.
        """
        total = Decimal("0")
        for order in self.orders.values():
            if order.outcome != outcome or order.side is not Side.SELL or order.order_id == exclude:
                continue
            if order.live or (order.exchange_id and not order.reconciled):
                total += max(order.remaining, Decimal("0"))
        return total

    def available(self, outcome: str, *, exclude: str | None = None) -> Decimal:
        return self.inventory(outcome) - self.resting(outcome, exclude=exclude)


def window_start_for(timestamp: int, window: int = 300) -> int:
    return timestamp - (timestamp % window)


def slug_for(start: int, prefix: str = "btc-updown-5m") -> str:
    if start % 300 != 0:
        raise ValueError(f"window start {start} is not 300-aligned")
    return f"{prefix}-{start}"
