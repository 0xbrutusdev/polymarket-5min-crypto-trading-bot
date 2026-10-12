"""Regime B decisions. Pure functions. No I/O.

Strategy (what): which market, when to mint, when to flatten.
Execution (how) lives in execution.py and only prices the asks this module requests.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from spork30.config import StrategyConfig
from spork30.models import Phase, slug_for, window_start_for

# Polymarket settles btc-updown-5m on the 60-second Chainlink TWAP at the
# close against the TWAP open ("price to beat"). The TWAP averages the last
# 60 s, so a spot move with less than that left only counts in part.
TWAP_SECONDS = 60
SECONDS_PER_YEAR = 365 * 86_400


def quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    units = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * tick


def ceil_price(price: Decimal, tick: Decimal) -> Decimal:
    units = (price / tick).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return units * tick


def sigma_per_second(annual_vol: Decimal) -> float:
    return float(annual_vol) / math.sqrt(SECONDS_PER_YEAR)


def fair_up(spot: Decimal, beat: Decimal, seconds_left: int, sigma: float) -> Decimal:
    """P(Up) implied by spot versus the price to beat (U6).

    Log-distance of spot from the beat, scaled by the volatility of the
    settlement TWAP over the time left. With `seconds_left` to the close the
    TWAP still has to average the final 60 s, so its remaining variance is
    that of a random walk over roughly (seconds_left - 30) seconds, floored
    at a few seconds so the last ticks do not divide by zero. A driftless
    normal is assumed: there is no directional view in this number, only the
    distance already travelled.
    """
    if spot <= 0 or beat <= 0:
        return Decimal("0.5")
    horizon = max(3.0, float(seconds_left) - TWAP_SECONDS / 2)
    scale = max(sigma, 1e-9) * math.sqrt(horizon)
    z = math.log(float(spot) / float(beat)) / scale
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return Decimal(str(round(min(0.999, max(0.001, p)), 6)))


def fair_asks(
    fair: Decimal,
    strategy: StrategyConfig,
    *,
    up_ask: Decimal,
    up_bid: Decimal | None,
    tick: Decimal,
) -> tuple[Decimal, Decimal]:
    """Both asks at fair + edge, each on the maker side of its own book.

    Up ask = ceil(fair + edge); Down ask = ceil((1 - fair) + edge). Neither
    goes below one tick above its own bid (the Down bid is 1 - Up ask).
    Without `fair_undercut` neither goes below the current best ask either:
    the quote joins the touch when fair is below it and rests behind the
    touch when fair is above it, which is where the touch is about to go.
    """
    edge = strategy.fair_edge
    up = ceil_price(fair + edge, tick)
    down = ceil_price((Decimal("1") - fair) + edge, tick)
    up_floor = (up_bid + tick) if up_bid is not None else up_ask
    down_bid = Decimal("1") - up_ask
    down_floor = down_bid + tick
    if not strategy.fair_undercut:
        up_floor = max(up_floor, up_ask)
        down_touch = (Decimal("1") - up_bid) if up_bid is not None else down_bid + tick
        down_floor = max(down_floor, down_touch)
    up = max(up, up_floor)
    down = max(down, down_floor)
    top = Decimal("1") - tick
    return min(top, max(tick, up)), min(top, max(tick, down))


def active_window(now: int, strategy: StrategyConfig) -> int:
    return window_start_for(now, strategy.window_seconds)


def upcoming_window(now: int, strategy: StrategyConfig) -> int:
    """The window that should be minted while the current one is quoting.

    Observed lead on traded windows is 293 seconds (Oct 5-12), so the next
    window is the mint target a few seconds after the current one opens.
    """
    current = active_window(now, strategy)
    return current + strategy.window_seconds


def should_split(now: int, start: int, strategy: StrategyConfig, already_split: bool) -> bool:
    """Mint once inside the observed lead band.

    Polled every second, the first true instant is exactly `split_lead_seconds`
    before the window (median 293 s on 2,012 traded windows, Oct 5-12; it
    was 212 s in September). If the process wakes late, it still mints while
    the lead is at least `min_split_lead_seconds`. Later than that is left
    unminted.
    """
    if already_split:
        return False
    lead = start - now
    return strategy.min_split_lead_seconds <= lead <= strategy.split_lead_seconds


def split_amount(strategy: StrategyConfig) -> Decimal:
    return strategy.split_size


def quoting_open(now: int, start: int, strategy: StrategyConfig) -> bool:
    return start <= now < start + strategy.window_seconds


def flatten_method(now: int, start: int, strategy: StrategyConfig, resolved: bool) -> str | None:
    """Which flatten to send now, or None if it is not time yet.

    Merge first, oracle or not. On Oct 5-12 the account merged every
    remaining pair at +358 s in 1,953 of 2,012 windows (before the CTF
    payout is set at ~+365 s) and redeemed the unpaired winner at +366 s.
    The old two-clock rule (redeem at +365 if resolved, else merge at +372)
    left the merge waiting on the oracle; this one does not. The residual
    redeem is gated by `redeem_due` and is the engine's job.

    `resolved` is accepted for callers written against the old signature.
    """
    del resolved
    if now >= start + strategy.flatten_delay_seconds:
        return "MERGE"
    return None


def redeem_due(now: int, start: int, strategy: StrategyConfig) -> bool:
    """The unpaired winner is redeemed at +366 s (median, n=1821), after the merge."""
    return now >= start + strategy.redeem_delay_seconds


def should_flatten(now: int, start: int, strategy: StrategyConfig, already: bool, resolved: bool) -> bool:
    if already:
        return False
    return flatten_method(now, start, strategy, resolved) is not None


def should_requote(
    current_price: Decimal,
    new_price: Decimal,
    age_seconds: int,
    tick: Decimal,
    strategy: StrategyConfig,
) -> bool:
    """Replace a resting ask only when the touch has really left it (U1).

    With both knobs at 0 this is the old rule: replace on any price change.
    Otherwise the order stays while it is younger than `requote_min_seconds`
    or while the new price is within `requote_threshold_ticks` of it. An ask
    the market moved away from keeps resting and fills on the bounce; one the
    market moved through fills where it was and is not immediately re-posted
    a tick higher. That re-posting is what sold the bot the winner at the
    leading edge of every move.
    """
    if current_price == new_price:
        return False
    if age_seconds < strategy.requote_min_seconds:
        return False
    moved = abs(new_price - current_price) / tick
    return moved >= max(1, strategy.requote_threshold_ticks)


def lean_blocked(outcome: str, up: Decimal, down: Decimal, strategy: StrategyConfig) -> bool:
    """True while `outcome` has been sold more than `max_lean_shares` ahead of
    the other side (U1b). Both legs were minted in equal size, so the lean is
    just the inventory difference. The ask on the leading side is withdrawn
    until the other side catches up; the pair stays balanced and the worst
    case per window is one cap of the winner sold, not a trend's worth.
    """
    cap = strategy.max_lean_shares
    if cap <= 0:
        return False
    this, other = (up, down) if outcome == "Up" else (down, up)
    return other - this > cap


def complement_asks(
    up_reference: Decimal,
    strategy: StrategyConfig,
    *,
    up_bid: Decimal | None = None,
    tick: Decimal | None = None,
) -> tuple[Decimal, Decimal]:
    """Resting ask on each outcome, both on the maker side of their own book.

    On a binary market the Down book is the mirror of the Up book:
    Down bid = 1 - Up ask and Down ask = 1 - Up bid. The Up ask joins the Up
    ask. The Down ask therefore has to be priced off the Up *bid*; pricing it
    at 1 - Up ask (the previous rule) put it exactly on the Down bid, where a
    post-only sell is rejected and a plain sell crosses as a taker.

    When the bid is unknown, rest one tick above the Down bid. The pair sums to
    1 + spread, which is the only spread a two-sided maker can earn here.
    """
    tick = tick or strategy.price_tick
    up = quantize_price(up_reference, tick)
    down_bid = quantize_price(Decimal("1") - up, tick)
    if up_bid is not None and up_bid < up:
        down = quantize_price(Decimal("1") - up_bid, tick)
        # Inside-the-spread pricing (U2): when the book is wide, step both
        # asks one tick inside the touch. Each still rests at least one tick
        # above its own bid. The pair becomes 1 + spread - 2 ticks.
        improve = strategy.improve_spread_ticks
        if improve > 0 and (up - up_bid) / tick >= max(2, improve):
            up -= tick
            down -= tick
    else:
        down = down_bid + tick
    # Optional distance behind the touch; 0 by default (join the ask).
    behind = tick * max(0, strategy.ask_offset_ticks)
    up += behind
    down += behind
    # Keep both inside the tradable band. The account does print at 0.01 and 0.99.
    top = Decimal("1") - tick
    up = min(top, max(tick, up))
    down = min(top, max(tick, down))
    return up, down


def market_slug(start: int, strategy: StrategyConfig) -> str:
    return slug_for(start, strategy.slug_prefix)


def phase_after_split() -> Phase:
    return Phase.QUOTING
