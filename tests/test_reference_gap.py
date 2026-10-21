"""Reference-gap knobs: lean clamp, rebalance, touch mode, guard, underdog floor,
sticky clip, jump pause, quiet cross skip, and the simulation comparison."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from spork30.config import StrategyConfig, TradingMode, load_config
from spork30.live import live_preflight
from spork30.models import Phase
from spork30.reference import RefTrade, RefWindow, decompose_window, sim_windows, simcompare
from spork30.strategy import fair_guard_blocks, lagging_outcome, lean_allowance

from conftest import BASE, book_at, events, make_config, make_engine

SIGMA = 1e-4
TICK = Decimal("0.01")
SLUG = f"btc-updown-5m-{BASE}"


def _minted(engine, start: int = BASE):
    m = engine.ensure(start)
    m.phase = Phase.QUOTING
    m.split_submitted = True
    m.split_size = Decimal("750")
    m.up = m.down = Decimal("750")
    m.cash = Decimal("-750")
    return m


def _step(engine, now, ask="0.52", **kw):
    return engine.step(now=now, book=book_at(now, ask), last_up_trade=None, resolved=False, winner=None,
                       emergency_stop=False, **kw)


def _places(actions):
    return {a.detail["outcome"]: a.detail for a in actions if a.kind == "PLACE"}


# --- strategy helpers -------------------------------------------------------------


def test_lean_allowance_counts_resting_and_is_off_by_default():
    on = StrategyConfig(lean_size_clamp=True, max_lean_shares=Decimal("6"))
    up, down = Decimal("745"), Decimal("750")  # Up sold 5 ahead
    assert lean_allowance("Up", up, down, Decimal("0"), on) == Decimal("1")
    assert lean_allowance("Down", up, down, Decimal("0"), on) == Decimal("11")
    assert lean_allowance("Down", up, down, Decimal("10"), on) == Decimal("1")
    assert lean_allowance("Up", up, down, Decimal("0"), StrategyConfig()) is None


def test_lagging_outcome_is_the_side_sold_less():
    on = StrategyConfig(rebalance_at_touch=True)
    assert lagging_outcome(Decimal("745"), Decimal("750"), on) == "Down"
    assert lagging_outcome(Decimal("750"), Decimal("740"), on) == "Up"
    assert lagging_outcome(Decimal("750"), Decimal("750"), on) is None
    assert lagging_outcome(Decimal("745"), Decimal("750"), StrategyConfig()) is None


def test_fair_guard_only_in_touch_mode():
    guard = StrategyConfig(quote_mode="touch", fair_guard_ticks=5)
    assert fair_guard_blocks("Up", Decimal("0.40"), Decimal("0.46"), TICK, guard)
    assert not fair_guard_blocks("Up", Decimal("0.42"), Decimal("0.46"), TICK, guard)
    assert fair_guard_blocks("Down", Decimal("0.40"), Decimal("0.50"), TICK, guard)  # Down fair 0.50
    assert not fair_guard_blocks("Up", Decimal("0.40"), None, TICK, guard)
    assert not fair_guard_blocks("Up", Decimal("0.40"), Decimal("0.46"), TICK, replace(guard, quote_mode="fair"))


# --- engine -----------------------------------------------------------------------


def test_lean_clamp_keeps_every_clip_inside_the_cap(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0"), max_lean_shares=Decimal("6"), lean_size_clamp=True)
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    placed = _places(_step(engine, BASE + 2))
    assert all(Decimal(p["size"]) <= 6 for p in placed.values())
    engine.apply_fill(placed["Up"]["order_id"], Decimal("5"), Decimal("0.52"), ts=BASE + 3)
    placed = _places(_step(engine, BASE + 4, ask="0.53"))
    # Up is 5 ahead with a cap of 6: one share of room, below the 5-share minimum.
    assert "Up" not in placed
    market = engine.markets[SLUG]
    assert not any(o.live and o.outcome == "Up" for o in market.orders.values())


def test_rebalance_prices_the_lagging_side_at_the_touch(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0.03"), max_lean_shares=Decimal("6"), rebalance_at_touch=True)
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    spot = {"spot": Decimal("100000"), "beat": Decimal("100000"), "sigma": SIGMA}
    placed = _places(_step(engine, BASE + 2, **spot))
    assert Decimal(placed["Down"]["price"]) == Decimal("0.53")  # fair 0.5 + 0.03
    engine.apply_fill(placed["Up"]["order_id"], Decimal("5"), Decimal("0.53"), ts=BASE + 3)
    placed = _places(_step(engine, BASE + 4, **spot))
    # Down lags: it joins its touch (1 - Up bid 0.51 = 0.49) instead of fair + edge.
    assert placed["Down"]["rule"] == "rebalance_lagging_side_at_touch"
    assert Decimal(placed["Down"]["price"]) == Decimal("0.49")


def test_touch_mode_quotes_the_touch_without_spot(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0.03"), quote_mode="touch")
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    actions = _step(engine, BASE + 2)
    placed = _places(actions)
    assert set(placed) == {"Up", "Down"}
    assert {p["rule"] for p in placed.values()} == {"touch_join_maker_ask"}
    assert Decimal(placed["Up"]["price"]) == Decimal("0.52") and Decimal(placed["Down"]["price"]) == Decimal("0.49")
    assert not any(a.detail.get("reason") == "no_spot_reference" for a in actions)


def test_touch_mode_guard_withholds_an_ask_far_under_fair(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0"), quote_mode="touch", fair_guard_ticks=5)
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    # Spot 0.3% over the beat with 290 s left: fair Up is far above the 0.52 touch.
    placed = _places(_step(engine, BASE + 10, spot=Decimal("100300"), beat=Decimal("100000"), sigma=SIGMA))
    assert "Up" not in placed and "Down" in placed


def test_underdog_floor_skips_cheap_asks(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0"), max_lean_shares=Decimal("0"), underdog_min_price=Decimal("0.35"))
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    placed = _places(_step(engine, BASE + 2, ask="0.80"))
    assert "Down" not in placed and "Up" in placed  # Down ask 0.21 is under the floor


def test_sticky_clip_keeps_the_size_on_replace(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0"), max_lean_shares=Decimal("0"), sticky_clip=True)
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    first = _places(_step(engine, BASE + 2))
    for i, ask in enumerate(("0.53", "0.54", "0.55", "0.56")):
        again = _places(_step(engine, BASE + 3 + i, ask=ask))
        assert again["Up"]["size"] == first["Up"]["size"]


def test_jump_pauses_then_resumes_and_halts_on_the_nth(tmp_path):
    engine = make_engine(tmp_path, jump_pause_seconds=5, max_jumps_per_window=3)
    _minted(engine)
    _step(engine, BASE + 2, ask="0.80")
    actions = _step(engine, BASE + 3, ask="0.20")
    market = engine.markets[SLUG]
    assert market.phase is Phase.QUOTING and events(engine, "jump_pause")
    assert any(a.detail.get("reason") == "price_jump" for a in actions)
    assert not any(o.live for o in market.orders.values())
    assert not _places(_step(engine, BASE + 5, ask="0.21"))  # still paused
    assert _places(_step(engine, BASE + 8, ask="0.21"))  # resumed
    _step(engine, BASE + 9, ask="0.80")  # second jump: paused again
    assert market.phase is Phase.QUOTING
    _step(engine, BASE + 15, ask="0.80")
    _step(engine, BASE + 16, ask="0.10")  # third jump: halt
    assert market.phase is Phase.HALTED and market.halt_reason == "MAX_PRICE_DEVIATION"


def test_jump_halts_at_once_by_default(tmp_path):
    engine = make_engine(tmp_path)
    _minted(engine)
    _step(engine, BASE + 2, ask="0.80")
    _step(engine, BASE + 3, ask="0.20")
    assert engine.markets[SLUG].phase is Phase.HALTED


def test_an_ask_that_would_cross_is_skipped_without_a_refusal(tmp_path):
    engine = make_engine(tmp_path)
    _minted(engine)
    placed = _places(_step(engine, BASE + 2, ask="0.01"))
    assert "Down" not in placed
    assert not [r for r in events(engine, "risk_refuse") if r["reason"] == "WOULD_CROSS"]


# --- config and preflight -----------------------------------------------------------


def test_config_reads_the_new_knobs(monkeypatch, sim_env):
    monkeypatch.setenv("QUOTE_MODE", "touch")
    monkeypatch.setenv("LEAN_SIZE_CLAMP", "1")
    monkeypatch.setenv("MAX_LEAN_SHARES", "6")
    monkeypatch.setenv("REBALANCE_AT_TOUCH", "yes")
    monkeypatch.setenv("JUMP_PAUSE_SECONDS", "5")
    monkeypatch.setenv("UNDERDOG_MIN_PRICE", "0.35")
    config = load_config()
    s = config.strategy
    assert s.quote_mode == "touch" and s.lean_size_clamp and s.rebalance_at_touch
    assert s.max_lean_shares == Decimal("6") and s.underdog_min_price == Decimal("0.35")
    assert config.risk.jump_pause_seconds == 5 and config.risk.max_jumps_per_window == 3


def test_config_refuses_a_clamp_with_a_cap_under_the_minimum_order(monkeypatch, sim_env):
    monkeypatch.setenv("LEAN_SIZE_CLAMP", "1")
    monkeypatch.setenv("MAX_LEAN_SHARES", "3")
    with pytest.raises(SystemExit, match="LEAN_SIZE_CLAMP"):
        load_config()
    monkeypatch.setenv("QUOTE_MODE", "sideways")
    monkeypatch.setenv("LEAN_SIZE_CLAMP", "0")
    with pytest.raises(SystemExit, match="QUOTE_MODE"):
        load_config()


def test_preflight_needs_an_opt_in_for_touch_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_TOUCH_QUOTING", raising=False)
    config = make_config(tmp_path, TradingMode.LIVE, StrategyConfig(quote_mode="touch"))
    assert any("touch quoting" in p for p in live_preflight(config, collateral=None, open_markets=0))
    monkeypatch.setenv("ALLOW_TOUCH_QUOTING", "YES")
    assert live_preflight(config, collateral=None, open_markets=0) == []


# --- simulation comparison ----------------------------------------------------------


def _window(start, trades, winner, split=Decimal("750")):
    w = RefWindow(slug=f"btc-updown-5m-{start}", start=start, split_ts=start - 293, split_size=split, winner=winner)
    for ts, outcome, price, size in trades:
        p, s = Decimal(price), Decimal(size)
        w.trades.append(RefTrade(start + ts, outcome, p, s, p * s))
    sold_up, sold_down = w.sold_up, w.sold_down
    w.merged = split - max(sold_up, sold_down)
    w.redeemed = (split - (sold_up if winner == "Up" else sold_down)) - w.merged
    return w


def test_decompose_sums_to_cash():
    w = _window(BASE, [(10, "Up", "0.60", "10"), (20, "Down", "0.45", "5")], "Up")
    pair, leftover = decompose_window(w)
    assert pair == pytest.approx(5 * (0.60 + 0.45 - 1))
    assert leftover == pytest.approx(5 * (0.60 - 1))
    assert pair + leftover == pytest.approx(float(w.cash))


def test_simcompare_reads_a_log_and_scores_against_the_reference(tmp_path):
    log = tmp_path / "decisions.jsonl"
    rows = []
    for k, (winner, trades) in enumerate([("Up", [("Up", "0.40", "10", False)]),
                                          ("Down", [("Up", "0.55", "10", True), ("Down", "0.47", "10", False)])]):
        start = BASE + 300 * k
        slug = f"btc-updown-5m-{start}"
        for i, (outcome, price, size, late) in enumerate(trades):
            rows.append({"event": "place", "slug": slug, "order_id": f"{slug}:{i}"})
            fill = {"event": "fill", "slug": slug, "outcome": outcome, "price": price, "size": size, "ts": start + 30 + i}
            if late:
                fill["late"] = True
            rows.append(fill)
        up = sum(Decimal(s) for o, _, s, _ in trades if o == "Up")
        down = sum(Decimal(s) for o, _, s, _ in trades if o == "Down")
        proceeds = sum(Decimal(p) * Decimal(s) for _, p, s, _ in trades)
        merged = Decimal("750") - max(up, down)
        redeemed = Decimal("750") - (up if winner == "Up" else down) - merged
        rows.append({"event": "cycle_closed", "slug": slug, "split_size": "750", "merged": str(merged),
                     "redeemed": str(redeemed), "proceeds": str(proceeds), "winner": winner,
                     "cash": str(proceeds + merged + redeemed - 750)})
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sim = sim_windows(log)
    assert [s.window.fills for s in sim] == [1, 2]
    assert float(sim[0].window.cash) == pytest.approx(4.0 - 10.0)
    ref = [_window(BASE, [(10, "Up", "0.40", "10"), (20, "Down", "0.55", "10")], "Up")]
    rep = simcompare(sim, ref, rebate_bps=Decimal("42"), baseline=sim)
    assert rep["overlap_windows"] == 1
    assert rep["sim_all"]["one_sided_pct"] == 50.0
    assert rep["sim_all"]["late_fill_share"] == pytest.approx(1 / 3, abs=1e-3)
    assert rep["reference_overlap"]["one_sided_pct"] == 0.0
    assert rep["paired_improvement"]["mean"] == 0.0
    assert rep["acceptance"]["passed"] is False
