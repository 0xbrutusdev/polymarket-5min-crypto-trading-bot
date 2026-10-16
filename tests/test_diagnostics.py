"""Markout, decomposition and bootstrap arithmetic on a tiny fixture."""

from decimal import Decimal

from spork30.backtest import WindowResult
from spork30.diagnostics import (
    block_bootstrap,
    bucket_summary,
    decompose,
    drawdown,
    fill_rows,
    percentile,
    regime_expectancy,
)
from spork30.fillmodel import FillRecord
from spork30.strategy import fair_up

from conftest import BASE

BEAT = Decimal("100000")
SIGMA = 1e-4


def _result(records, *, winner="Up", sold_up="0", sold_down="0", proceeds="0", pnl="0", spot=None):
    return WindowResult(
        start=BASE, winner=winner, pnl=Decimal(pnl), fills=len(records), sold_up=Decimal(sold_up),
        sold_down=Decimal(sold_down), proceeds=Decimal(proceeds), places=len(records), phase="CLOSED",
        sigma=SIGMA, records=records, spot=spot or [], beat=BEAT,
    )


def _spot_path(jump_at: int, after: Decimal):
    """Flat at the beat until `jump_at`, then `after`, for the whole window and its lookback."""
    return [(ts, BEAT if ts < jump_at else after) for ts in range(BASE - 300, BASE + 300)]


def _record(ts, outcome, price, *, fair, fair_at_place=None, touch=None):
    return FillRecord(ts=ts, order_id="o", outcome=outcome, price=Decimal(price), size=Decimal("5"),
                      fair=Decimal(fair), touch=None if touch is None else Decimal(touch),
                      seconds_into_window=ts - BASE, queue_ahead_at_place=Decimal("250"),
                      fair_at_place=None if fair_at_place is None else Decimal(fair_at_place))


def test_markouts_follow_the_spot_implied_fair_of_the_sold_outcome():
    ts = BASE + 100
    spot = _spot_path(ts + 5, BEAT + 20)  # spot moves up 20 shortly after the fill
    up = _record(ts, "Up", "0.55", fair="0.52", fair_at_place="0.50", touch="0.49")
    down = _record(ts, "Down", "0.51", fair="0.52", fair_at_place="0.50", touch="0.52")
    rows = fill_rows(_result([up, down], winner="Up", spot=spot))
    r_up, r_down = rows
    f0 = float(fair_up(BEAT, BEAT, 300 - 100, SIGMA))
    f10 = float(fair_up(BEAT + 20, BEAT, 300 - 110, SIGMA))
    assert r_up.markout_10 == f10 - f0 > 0  # market moved against the Up sale
    assert r_down.markout_10 == (1 - f10) - (1 - f0) < 0  # and in favour of the Down sale
    assert r_up.settlement == 0.55 - 1.0 and r_up.won
    assert r_down.settlement == 0.51 and not r_down.won
    assert (r_up.edge_at_place, r_up.edge_at_fill) == (0.55 - 0.50, 0.55 - 0.52)
    assert r_up.gap_ticks == 1.0 and r_down.gap_ticks == -2.0  # fair_at_place - touch, in ticks
    assert (r_up.seconds_into_window, r_up.seconds_left) == (100, 200)
    # Horizons past the end of the window are read at the last second.
    late = _record(BASE + 290, "Up", "0.60", fair="0.6")
    (row,) = fill_rows(_result([late], spot=spot))
    assert row.markout_60 is not None


def test_markouts_are_none_without_spot_or_fair():
    row_no_spot, = fill_rows(_result([_record(BASE + 10, "Up", "0.5", fair="0.5")]))
    assert (row_no_spot.markout_10, row_no_spot.markout_60) == (None, None)
    rec = FillRecord(ts=BASE + 10, order_id="o", outcome="Up", price=Decimal("0.5"), size=Decimal("5"), fair=None,
                     touch=None, seconds_into_window=10, queue_ahead_at_place=Decimal("0"))
    row_no_fair, = fill_rows(_result([rec], spot=_spot_path(BASE, BEAT)))
    assert row_no_fair.markout_10 is None and row_no_fair.edge_at_fill is None and row_no_fair.gap_ticks is None


def test_bucket_summary_is_size_weighted():
    big = FillRecord(ts=BASE + 1, order_id="a", outcome="Up", price=Decimal("0.60"), size=Decimal("15"), fair=Decimal("0.5"),
                     touch=None, seconds_into_window=1, queue_ahead_at_place=Decimal("0"))
    small = FillRecord(ts=BASE + 2, order_id="b", outcome="Down", price=Decimal("0.40"), size=Decimal("5"), fair=Decimal("0.5"),
                       touch=None, seconds_into_window=2, queue_ahead_at_place=Decimal("0"))
    rows = fill_rows(_result([big, small], winner="Up"))
    s = bucket_summary(rows)
    assert (s["fills"], s["shares"], s["sold_winner_pct"]) == (2, 20.0, 75.0)
    assert s["avg_price"] == round((0.60 * 15 + 0.40 * 5) / 20, 4)
    assert s["pnl"] == round(15 * (0.60 - 1) + 5 * 0.40, 2)


def test_decomposition_matches_the_engines_cash_rules():
    # 10 Up and 4 Down sold at 0.5; Down wins: 740 merge, 6 Down redeem.
    pnl = Decimal("7") + Decimal("740") + Decimal("6") - Decimal("750")
    d = decompose(_result([], winner="Down", sold_up="10", sold_down="4", proceeds="7", pnl=str(pnl)), Decimal("750"))
    assert (d["merged"], d["redeemed"], d["residual_loser_abandoned"], d["identity_gap"]) == (740.0, 6.0, 0.0, 0.0)
    # Up wins instead: the 6 unpaired Down are worthless.
    pnl = Decimal("7") + Decimal("740") - Decimal("750")
    d = decompose(_result([], winner="Up", sold_up="10", sold_down="4", proceeds="7", pnl=str(pnl)), Decimal("750"))
    assert (d["redeemed"], d["residual_loser_abandoned"], d["identity_gap"]) == (0.0, 6.0, 0.0)
    # A winning residual under one share is dust the engine does not redeem.
    pnl = Decimal("4.75") + Decimal("745") - Decimal("750")
    d = decompose(_result([], winner="Down", sold_up="5", sold_down="4.5", proceeds="4.75", pnl=str(pnl)), Decimal("750"))
    assert (d["redeemed"], d["winner_dust_abandoned"], d["identity_gap"]) == (0.0, 0.5, 0.0)


def test_drawdown_percentile_and_bootstrap():
    assert drawdown([1, -3, 2, -4, 5]) == -5.0
    assert drawdown([]) == 0.0 and drawdown([1, 2, 3]) == 0.0
    assert percentile([5, 1, 3, 2, 4], 0.5) == 3 and percentile([5, 1, 3, 2, 4], 0.9) == 5 and percentile([], 0.5) == 0.0
    constant = block_bootstrap([2.0] * 30, block=12)
    assert (constant["mean"], constant["ci_low"], constant["ci_high"], constant["blocks"]) == (2.0, 2.0, 2.0, 3)
    mixed = block_bootstrap([1.0, -1.0] * 24, block=12, seed=1)
    assert mixed["ci_low"] <= mixed["mean"] <= mixed["ci_high"]
    assert block_bootstrap(mixed_pnls := [1.0, -1.0] * 24, seed=3) == block_bootstrap(mixed_pnls, seed=3)  # repeatable
    assert block_bootstrap([])["blocks"] == 0


def test_regime_expectancy_buckets_by_vol_and_winner():
    results = []
    for k in range(9):
        r = _result([], winner="Up" if k % 2 else "Down", pnl=str(k - 4), spot=_spot_path(BASE, BEAT + k))
        r.sigma = SIGMA * (1 + k)
        r.start = BASE + 300 * k
        results.append(r)
    reg = regime_expectancy(results)
    assert [b["windows"] for b in reg["vol_tercile"].values()] == [3, 3, 3]
    assert reg["vol_tercile"]["low"]["mean"] == (-4 - 3 - 2) / 3
    assert reg["winner"]["Up"]["windows"] == 4 and reg["winner"]["Down"]["windows"] == 5
