"""Spot-referenced fair value (U6), the pair-balance cap (U1b) and the quote
holding rules (U1). Pure functions first, then the engine, then the feed."""

from decimal import Decimal

from spork30.config import StrategyConfig
from spork30.models import Phase
from spork30.spotfeed import SpotFeed
from spork30.strategy import (
    complement_asks,
    fair_asks,
    fair_up,
    lean_blocked,
    should_requote,
    sigma_per_second,
)

from conftest import BASE, book_at, events, make_engine

TICK = Decimal("0.01")
SIGMA = sigma_per_second(Decimal("0.45"))


# --- fair_up ----------------------------------------------------------------


def test_fair_is_half_on_the_beat_and_moves_with_spot():
    beat = Decimal("100000")
    assert fair_up(beat, beat, 150, SIGMA) == Decimal("0.5")
    up = fair_up(Decimal("100050"), beat, 150, SIGMA)
    down = fair_up(Decimal("99950"), beat, 150, SIGMA)
    assert up > Decimal("0.5") > down
    assert abs((up + down) - Decimal("1")) < Decimal("0.0002")  # symmetric up to log curvature


def test_fair_sharpens_as_the_close_approaches_and_saturates():
    beat, spot = Decimal("100000"), Decimal("100030")
    early = fair_up(spot, beat, 280, SIGMA)
    late = fair_up(spot, beat, 20, SIGMA)
    assert Decimal("0.5") < early < late <= Decimal("0.999")
    assert fair_up(Decimal("110000"), beat, 100, SIGMA) == Decimal("0.999")
    assert fair_up(Decimal("90000"), beat, 100, SIGMA) == Decimal("0.001")
    # Degenerate inputs never raise and carry no view.
    assert fair_up(Decimal("0"), beat, 100, SIGMA) == Decimal("0.5")
    assert fair_up(spot, beat, 0, 0.0) in {Decimal("0.999"), Decimal("0.001"), Decimal("0.5")}


# --- fair_asks --------------------------------------------------------------


def test_fair_asks_rest_at_fair_plus_edge_above_the_touch():
    strategy = StrategyConfig(fair_edge=Decimal("0.03"))
    up, down = fair_asks(Decimal("0.5"), strategy, up_ask=Decimal("0.50"), up_bid=Decimal("0.49"), tick=TICK)
    assert (up, down) == (Decimal("0.53"), Decimal("0.53"))
    # A fair of 0.734 rounds up to the grid: 0.77 Up, (0.266 + 0.03) -> 0.30 Down, but the Down
    # touch (1 - 0.49 = 0.51) is the floor without undercutting.
    up, down = fair_asks(Decimal("0.734"), strategy, up_ask=Decimal("0.50"), up_bid=Decimal("0.49"), tick=TICK)
    assert (up, down) == (Decimal("0.77"), Decimal("0.51"))


def test_fair_asks_undercut_only_when_allowed_and_never_cross_the_bid():
    joins = StrategyConfig(fair_edge=Decimal("0.03"), fair_undercut=False)
    cuts = StrategyConfig(fair_edge=Decimal("0.03"), fair_undercut=True)
    # Up touch 0.40/0.45 (wide), fair 0.38 -> Up 0.41 would undercut the 0.45 ask.
    up, down = fair_asks(Decimal("0.38"), joins, up_ask=Decimal("0.45"), up_bid=Decimal("0.40"), tick=TICK)
    assert up == Decimal("0.45")
    up, down = fair_asks(Decimal("0.38"), cuts, up_ask=Decimal("0.45"), up_bid=Decimal("0.40"), tick=TICK)
    assert up == Decimal("0.41") and up > Decimal("0.40")
    # Even undercutting stops one tick above the bid.
    up, _ = fair_asks(Decimal("0.30"), cuts, up_ask=Decimal("0.45"), up_bid=Decimal("0.40"), tick=TICK)
    assert up == Decimal("0.41")
    # Clamped to the grid's top; the Down ask is (1 - 0.999) + 0.03 rounded up to the grid.
    up, down = fair_asks(Decimal("0.999"), joins, up_ask=Decimal("0.98"), up_bid=Decimal("0.97"), tick=TICK)
    assert up == Decimal("0.99") and down == Decimal("0.04")


# --- U1 knobs ---------------------------------------------------------------


def test_should_requote_rules():
    off = StrategyConfig()
    assert not should_requote(Decimal("0.52"), Decimal("0.52"), 0, TICK, off)
    assert should_requote(Decimal("0.52"), Decimal("0.53"), 0, TICK, off)
    held = StrategyConfig(requote_threshold_ticks=2, requote_min_seconds=10)
    assert not should_requote(Decimal("0.52"), Decimal("0.51"), 30, TICK, held)  # one tick away: hold
    assert should_requote(Decimal("0.52"), Decimal("0.50"), 30, TICK, held)
    assert not should_requote(Decimal("0.52"), Decimal("0.44"), 5, TICK, held)  # too young
    # A move up leaves the ask under the market: replaced at once, young or not.
    assert should_requote(Decimal("0.52"), Decimal("0.53"), 0, TICK, held)


def test_lean_blocked_withdraws_the_side_that_ran_ahead():
    cap = StrategyConfig(max_lean_shares=Decimal("3"))
    up, down = Decimal("745"), Decimal("750")  # sold 5 Up, 0 Down -> Up is 5 ahead
    assert lean_blocked("Up", up, down, cap) and not lean_blocked("Down", up, down, cap)
    assert not lean_blocked("Up", Decimal("745"), Decimal("745"), cap)
    off = StrategyConfig(max_lean_shares=Decimal("0"))
    assert not lean_blocked("Up", up, down, off)


def test_inside_spread_pricing_steps_one_tick_in():
    inside = StrategyConfig(improve_spread_ticks=2)
    up, down = complement_asks(Decimal("0.45"), inside, up_bid=Decimal("0.40"), tick=TICK)
    assert (up, down) == (Decimal("0.44"), Decimal("0.59"))
    up, down = complement_asks(Decimal("0.41"), inside, up_bid=Decimal("0.40"), tick=TICK)  # 1-tick spread: no room
    assert (up, down) == (Decimal("0.41"), Decimal("0.60"))


# --- engine -----------------------------------------------------------------


def _quote_once(engine, now, **kw):
    return engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=False, winner=None,
                       emergency_stop=False, **kw)


def _minted(engine, start: int = BASE):
    """A market already split 750/750, as the engine leaves it 293 s before the window."""
    m = engine.ensure(start)
    m.phase = Phase.QUOTING
    m.split_submitted = True
    m.split_size = Decimal("750")
    m.up = m.down = Decimal("750")
    m.cash = Decimal("-750")
    return m


def test_engine_with_fair_on_rests_nothing_without_spot(tmp_path):
    engine = make_engine(tmp_path, strategy=StrategyConfig(fair_edge=Decimal("0.03")))
    _minted(engine)
    actions = _quote_once(engine, BASE + 2)
    kinds = [a.kind for a in actions]
    assert "PLACE" not in kinds and "QUOTES_PULLED" in kinds
    assert any(a.detail.get("reason") == "no_spot_reference" for a in actions)
    assert engine.risk_state.data_stale
    # Spot and beat arrive: both asks go out, priced off fair + edge, not the touch.
    actions = _quote_once(engine, BASE + 3, spot=Decimal("100000"), beat=Decimal("100000"), sigma=SIGMA)
    places = [a for a in actions if a.kind == "PLACE"]
    assert {p.detail["outcome"] for p in places} == {"Up", "Down"}
    assert all(p.detail["rule"] == "spot_fair_plus_edge_maker_ask" for p in places)
    assert {Decimal(p.detail["price"]) for p in places} == {Decimal("0.53")}
    assert not engine.risk_state.data_stale
    market = engine.markets[f"btc-updown-5m-{BASE}"]
    assert market.last_fair == Decimal("0.5")


def test_engine_lean_cap_withdraws_and_restores_the_leading_side(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0"), max_lean_shares=Decimal("3"))
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    actions = _quote_once(engine, BASE + 2)
    up_order = next(a.detail["order_id"] for a in actions if a.kind == "PLACE" and a.detail["outcome"] == "Up")
    engine.apply_fill(up_order, Decimal("5"), Decimal("0.52"), ts=BASE + 3)
    actions = _quote_once(engine, BASE + 4)
    placed = {a.detail["outcome"] for a in actions if a.kind == "PLACE"}
    assert "Up" not in placed  # Up is 5 ahead of Down: no Up ask while the cap holds
    market = engine.markets[f"btc-updown-5m-{BASE}"]
    assert not any(o.live and o.outcome == "Up" for o in market.orders.values())
    cancels = [r for r in events(engine, "cancel") if r.get("reason") == "lean_cap"]
    assert cancels == [] or all(r["reason"] == "lean_cap" for r in cancels)
    # Down catches up: the Up ask comes back.
    down_order = next(o.order_id for o in market.orders.values() if o.live and o.outcome == "Down")
    engine.apply_fill(down_order, Decimal("5"), Decimal("0.49"), ts=BASE + 5)
    actions = _quote_once(engine, BASE + 6)
    assert "Up" in {a.detail["outcome"] for a in actions if a.kind == "PLACE"}


def test_engine_lean_cap_cancels_a_live_ask_that_gets_ahead(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0"), max_lean_shares=Decimal("1"))
    engine = make_engine(tmp_path, strategy=strategy)
    _minted(engine)
    actions = _quote_once(engine, BASE + 2)
    up_order = next(a.detail["order_id"] for a in actions if a.kind == "PLACE" and a.detail["outcome"] == "Up")
    # A partial fill leaves the Up ask live with Up now ahead of Down.
    engine.apply_fill(up_order, Decimal("2"), Decimal("0.52"), ts=BASE + 3)
    market = engine.markets[f"btc-updown-5m-{BASE}"]
    assert market.orders[up_order].live
    actions = _quote_once(engine, BASE + 4)
    cancels = [a for a in actions if a.kind == "CANCEL" and a.detail.get("reason") == "lean_cap"]
    assert len(cancels) == 1 and cancels[0].detail["order_id"] == up_order
    assert not market.orders[up_order].live


def test_placed_ts_survives_save_and_load(tmp_path):
    engine = make_engine(tmp_path)
    _minted(engine)
    _quote_once(engine, BASE + 2)
    engine.save()
    again = make_engine(tmp_path)
    again.load()
    orders = [o for m in again.markets.values() for o in m.orders.values() if o.live]
    assert orders and all(o.placed_ts == BASE + 2 for o in orders)


def test_engine_with_fair_off_still_quotes_the_touch(tmp_path):
    engine = make_engine(tmp_path)  # conftest: fair_edge 0, no cap
    _minted(engine)
    actions = _quote_once(engine, BASE + 2, spot=Decimal("100000"), beat=Decimal("100000"))
    places = [a for a in actions if a.kind == "PLACE"]
    assert {Decimal(p.detail["price"]) for p in places} == {Decimal("0.52"), Decimal("0.49")}
    assert engine.markets[f"btc-updown-5m-{BASE}"].phase is Phase.QUOTING


# --- spot feed --------------------------------------------------------------


def test_spot_feed_beat_is_its_own_twap_and_freshness_is_enforced():
    feed = SpotFeed(start=False)
    assert feed.price() == (None, float("inf")) and feed.fresh_price() is None
    start = BASE
    for ts in range(start - 90, start):
        feed.record(Decimal("100000") + (Decimal("60") if ts >= start - 60 else Decimal("0")), ts)
    assert feed.beat_for(start) == Decimal("100060")  # only the last 60 s count
    assert feed.beat_for(start) == Decimal("100060")  # frozen
    assert feed.beat_for(start + 300) is None  # no history for the next window yet
    feed.record(Decimal("100100"), start + 1.0)
    assert feed.fresh_price(now=start + 2.5) == Decimal("100100")
    assert feed.fresh_price(now=start + 10) is None
    assert feed.sigma() is not None and feed.sigma() >= 0.0
    health = feed.health()
    assert health["spot"] == "100100" and health["spot_source"] == "none"


def test_spot_feed_parses_binance_trade_messages_and_ignores_noise():
    feed = SpotFeed(start=False)
    feed._on_message('{"e":"trade","T":1791771300500,"p":"83778.48","q":"0.001"}')
    price, _ = feed.price(now=1791771301.0)
    assert price == Decimal("83778.48")
    feed._on_message("not json")
    feed._on_message('{"result":null,"id":1}')
    assert feed.price(now=1791771301.0)[0] == Decimal("83778.48")


def test_loop_passes_spot_beat_and_sigma_to_the_engine(tmp_path, live_env):
    from spork30.config import TradingMode
    from spork30.loop import run_loop
    from conftest import FakeVenue

    strategy = StrategyConfig(fair_edge=Decimal("0.03"))
    engine = make_engine(tmp_path, TradingMode.LIVE, strategy=strategy)
    _minted(engine)
    feed = SpotFeed(start=False)
    for ts in range(BASE - 90, BASE + 3):
        feed.record(Decimal("100000"), ts)
    clock = iter(range(BASE + 2, BASE + 4))
    venue = FakeVenue()
    # A feed read at BASE+2/3 against prints stamped the same seconds is fresh.
    run_loop(engine.config, engine, venue, now_fn=lambda: next(clock), sleep=lambda s: None, cycles=2,
             quiet=True, cancel_on_exit=False, spot_feed=feed)
    places = [p for p in venue.sent if p["kind"] == "PLACE"]
    assert places and {Decimal(p["price"]) for p in places} == {Decimal("0.53")}
