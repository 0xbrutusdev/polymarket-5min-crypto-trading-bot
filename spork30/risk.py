"""Safety checks. Independent of the strategy rules.

Fail closed: a failed check returns a reason and the caller must not send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from spork30.config import RiskConfig
from spork30.models import MarketPosition, Side


@dataclass
class RiskState:
    realized_pnl_today: Decimal = Decimal("0")
    orders_in_window: list[int] = field(default_factory=list)
    emergency_stop: bool = False
    data_stale: bool = False

    def prune_orders(self, now: int) -> None:
        self.orders_in_window = [ts for ts in self.orders_in_window if now - ts < 10]


class RiskRefusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_order(
    *,
    now: int,
    position: MarketPosition,
    outcome: str,
    side: Side,
    price: Decimal,
    size: Decimal,
    open_markets: int,
    gross_exposure: Decimal,
    risk: RiskConfig,
    state: RiskState,
    reference: Decimal | None,
    bid: Decimal | None = None,
    reserved: Decimal = Decimal("0"),
) -> list[str]:
    """Return the checks that passed. Raise RiskRefusal on the first failure.

    `bid` is the best bid on the order's own outcome. A SELL at or below it
    would cross; post-only rejects it and a plain order pays the taker fee.
    `reserved` is inventory already committed to other sells the exchange may
    still fill; a new sell must fit in what is left.
    """
    passed: list[str] = []
    if state.emergency_stop:
        raise RiskRefusal("EMERGENCY_STOP")
    if state.data_stale:
        raise RiskRefusal("STALE_DATA")
    if state.realized_pnl_today <= -risk.max_daily_loss:
        raise RiskRefusal("MAX_DAILY_LOSS")
    if open_markets > risk.max_open_markets:
        raise RiskRefusal("MAX_OPEN_MARKETS")
    if gross_exposure > risk.max_total_exposure:
        raise RiskRefusal("MAX_TOTAL_EXPOSURE")
    if size <= 0:
        raise RiskRefusal("NON_POSITIVE_SIZE")
    if size > risk.max_order_size:
        raise RiskRefusal("MAX_ORDER_SIZE")
    if price < risk.min_price or price > risk.max_price:
        raise RiskRefusal("PRICE_BAND")
    if reference is not None and abs(price - reference) > risk.max_price_jump:
        raise RiskRefusal("MAX_PRICE_DEVIATION")
    if side is Side.SELL and bid is not None and price <= bid:
        raise RiskRefusal("WOULD_CROSS")
    inventory = position.inventory(outcome)
    if side is Side.SELL and size > inventory - reserved:
        raise RiskRefusal("SELL_EXCEEDS_INVENTORY")
    projected = inventory if side is Side.SELL else inventory + size
    if projected > risk.max_position_per_outcome:
        raise RiskRefusal("MAX_POSITION")
    state.prune_orders(now)
    if len(state.orders_in_window) >= risk.max_orders_per_10s:
        raise RiskRefusal("MAX_ORDER_RATE")
    passed.extend(
        [
            "EMERGENCY_STOP_CLEAR",
            "DATA_FRESH",
            "DAILY_LOSS_OK",
            "OPEN_MARKETS_OK",
            "EXPOSURE_OK",
            "SIZE_OK",
            "PRICE_OK",
            "MAKER_OK",
            "INVENTORY_OK",
            "RATE_OK",
        ]
    )
    return passed


def note_order(state: RiskState, now: int) -> None:
    state.orders_in_window.append(now)
