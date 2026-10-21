"""Maker-ask execution policy for regime B.

The strategy asks for two complementary sells. This module turns that into
resting post-only orders and replaces them when the reference price moves.
Fill timing is exogenous. This does not invent a directional signal.
"""

from __future__ import annotations

import random
from decimal import Decimal

from spork30.config import StrategyConfig
from spork30.models import BookTop, DesiredOrder, OrderKind, Side
from spork30.strategy import complement_asks, fair_asks, fair_guard_blocks, quantize_price


class ClipSchedule:
    """Seeded sampler over the observed 5/10/15/20 cluster weights.

    The activity feed does not identify which of those sizes the account
    chooses on the next order. Sampling the observed weights is the
    best-supported reconstruction. The seed makes a replay repeatable.
    """

    def __init__(self, strategy: StrategyConfig, seed: int = 30) -> None:
        self._sizes = strategy.clip_sizes
        self._weights = strategy.clip_weights
        if len(self._sizes) != len(self._weights):
            raise ValueError("clip sizes and weights differ in length")
        self._rng = random.Random(seed)

    def next_size(self) -> Decimal:
        choice = self._rng.choices(self._sizes, weights=self._weights, k=1)[0]
        return Decimal(choice)


def reference_up(book: BookTop | None, last_trade: Decimal | None) -> Decimal | None:
    """Prefer the observed Up ask. Fall back to the last Up trade print.

    Joining the ask is the best-supported reading of maker sells. The feed
    does not show unfilled quotes, so an inside-spread or behind-touch
    policy cannot be distinguished when no book is present.
    """
    if book is not None and book.ask is not None:
        return book.ask
    return last_trade


def desired_asks(
    *,
    up_reference: Decimal,
    inventory_up: Decimal,
    inventory_down: Decimal,
    strategy: StrategyConfig,
    clips: ClipSchedule,
    up_bid: Decimal | None = None,
    tick: Decimal | None = None,
    rule: str = "regime_B_complementary_maker_ask",
    skip: frozenset[str] = frozenset(),
    fair: Decimal | None = None,
    edge: Decimal | None = None,
    at_touch: frozenset[str] = frozenset(),
    size_caps: dict[str, Decimal] | None = None,
    sizes: dict[str, Decimal] | None = None,
) -> list[DesiredOrder]:
    """Both asks, except outcomes in `skip` (the pair-balance cap withdraws one).

    With `fair` (spot-implied P(Up)) and `fair_edge` > 0 in fair mode the asks
    are priced at fair + edge (`edge` overrides the configured one); otherwise
    off the Polymarket touch. Outcomes in `at_touch` (the lagging side while
    rebalancing) join the touch in either mode.

    `size_caps` bounds each outcome's clip (the lean allowance); a clip that
    no longer reaches `min_order_size` is skipped. `sizes` keeps a replaced
    ask's clip instead of drawing a new one (`sticky_clip`).
    """
    grid = tick or strategy.price_tick
    touch_up, touch_down = complement_asks(up_reference, strategy, up_bid=up_bid, tick=tick)
    if fair is not None and strategy.fair_edge > 0 and strategy.quote_mode == "fair":
        fair_up_price, fair_down_price = fair_asks(fair, strategy, up_ask=quantize_price(up_reference, grid),
                                                   up_bid=up_bid, tick=grid, edge=edge)
        prices = {"Up": fair_up_price, "Down": fair_down_price}
        rules = {"Up": "spot_fair_plus_edge_maker_ask", "Down": "spot_fair_plus_edge_maker_ask"}
    else:
        prices = {"Up": touch_up, "Down": touch_down}
        touch_rule = "touch_join_maker_ask" if strategy.quote_mode == "touch" else rule
        rules = {"Up": touch_rule, "Down": touch_rule}
    for outcome in at_touch:
        prices[outcome] = touch_up if outcome == "Up" else touch_down
        rules[outcome] = "rebalance_lagging_side_at_touch"
    # Best bid each ask must stay above. Down bid is the mirror of the Up ask
    # (the observed touch, not our own price, which may rest behind it).
    bids = {"Up": up_bid, "Down": Decimal("1") - quantize_price(up_reference, tick or strategy.price_tick)}
    orders: list[DesiredOrder] = []
    for outcome, inventory in (("Up", inventory_up), ("Down", inventory_down)):
        price = prices[outcome]
        if outcome in skip or inventory < strategy.min_order_size:
            continue
        if strategy.underdog_min_price > 0 and price < strategy.underdog_min_price:
            continue
        if fair_guard_blocks(outcome, price, fair, grid, strategy):
            continue
        cap = size_caps.get(outcome) if size_caps else None
        if cap is not None and cap < strategy.min_order_size:
            continue
        kept = sizes.get(outcome) if sizes else None
        size = min(kept if kept is not None else clips.next_size(), inventory)
        if cap is not None:
            size = min(size, cap)
        # Keep the submitted size on the observed grid when inventory allows.
        if size < strategy.min_order_size:
            continue
        orders.append(
            DesiredOrder(
                outcome=outcome,
                side=Side.SELL,
                price=price,
                size=size,
                kind=OrderKind.LIMIT_MAKER,
                rule=rules[outcome],
                reference_price=up_reference,
                position=inventory,
                risk_checks=[],
                touch_bid=bids[outcome],
            )
        )
    return orders
