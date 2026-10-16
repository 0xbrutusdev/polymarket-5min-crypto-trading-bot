"""Print-driven matching (spork30.fillmodel) and the simulation built on it. No network."""

from decimal import Decimal

from spork30.fillmodel import FillModel, Print
from spork30.models import Phase, Side, WorkingOrder
from spork30.portfolio import Action
from spork30.spotfeed import SpotFeed
from spork30.strategy import fair_up

from conftest import BASE, WINDOW, make_engine

SLUG = f"btc-updown-5m-{BASE}"


def _engine_with_asks(tmp_path, asks, *, queue="0", touch_only=False, placed_at=BASE):
    """A quoting market holding 750/750 with the given (outcome, price, size) asks resting."""
    engine = make_engine(tmp_path)
    market = engine.ensure(BASE)
    market.phase = Phase.QUOTING
    market.split_submitted = True
    market.split_size = market.up = market.down = Decimal("750")
    market.cash = Decimal("-750")
    model = FillModel(engine, SLUG, queue=Decimal(queue), queue_at_touch_only=touch_only, window_start=BASE)
    # Pre-window prints pin the Up book at 0.49 / 0.50.
    model.book.update(Print(BASE - 10, "Up", "BUY", Decimal("0.50"), Decimal("1")))
    model.book.update(Print(BASE - 9, "Up", "SELL", Decimal("0.49"), Decimal("1")))
    actions = []
    for i, (outcome, price, size) in enumerate(asks):
        oid = f"{SLUG}:{outcome}:{price}:{placed_at + i}"
        market.orders[oid] = WorkingOrder(order_id=oid, outcome=outcome, side=Side.SELL, price=Decimal(price),
                                          size=Decimal(size), placed_ts=placed_at)
        actions.append(Action("PLACE", SLUG, {"order_id": oid, "outcome": outcome, "price": price, "size": size}))
    model.on_actions(actions, placed_at)
    return engine, market, model


def _buy(ts, price, size, outcome="Up"):
    return Print(ts, outcome, "BUY", Decimal(price), Decimal(size))


def test_price_time_priority_fills_the_cheapest_ask_first(tmp_path):
    _, _, model = _engine_with_asks(tmp_path, [("Up", "0.52", "5"), ("Up", "0.50", "5")])
    made = model.on_print(_buy(BASE + 1, "0.52", "7"), quoting=True)
    assert made == 2
    assert [(r.price, r.size) for r in model.records] == [(Decimal("0.50"), Decimal("5")), (Decimal("0.52"), Decimal("2"))]
    assert model.proceeds == Decimal("0.50") * 5 + Decimal("0.52") * 2


def test_partial_fill_keeps_the_order_and_its_place(tmp_path):
    _, market, model = _engine_with_asks(tmp_path, [("Up", "0.50", "10")])
    (oid,) = market.orders
    model.on_print(_buy(BASE + 1, "0.50", "4"), quoting=True)
    assert market.orders[oid].remaining == Decimal("6") and market.orders[oid].live
    assert oid in model.resting
    model.on_print(_buy(BASE + 2, "0.50", "6"), quoting=True)
    assert market.orders[oid].remaining == 0 and not market.orders[oid].live
    assert oid not in model.resting and model.fills == 2 and model.sold["Up"] == Decimal("10")


def test_queue_ahead_is_consumed_before_we_fill(tmp_path):
    _, _, model = _engine_with_asks(tmp_path, [("Up", "0.50", "10")], queue="10")
    (resting,) = model.resting.values()
    assert model.on_print(_buy(BASE + 1, "0.50", "6"), quoting=True) == 0
    assert resting.queue_ahead == Decimal("4")
    assert model.on_print(_buy(BASE + 2, "0.50", "6"), quoting=True) == 1
    assert model.records[-1].size == Decimal("2") and model.records[-1].queue_ahead_at_place == Decimal("10")


def test_cancel_and_replace_loses_the_queue_position(tmp_path):
    _, market, model = _engine_with_asks(tmp_path, [("Up", "0.50", "10")], queue="10")
    (old_id,) = market.orders
    model.on_print(_buy(BASE + 1, "0.50", "8"), quoting=True)
    assert model.resting[old_id].queue_ahead == Decimal("2")
    market.orders[old_id].live = False
    model.on_actions([Action("CANCEL", SLUG, {"order_id": old_id, "reason": "replace"})], BASE + 2)
    new_id = f"{SLUG}:Up:0.50:{BASE + 2}"
    market.orders[new_id] = WorkingOrder(order_id=new_id, outcome="Up", side=Side.SELL, price=Decimal("0.50"),
                                         size=Decimal("10"), placed_ts=BASE + 2)
    model.on_actions([Action("PLACE", SLUG, {"order_id": new_id, "outcome": "Up", "price": "0.50", "size": "10"})], BASE + 2)
    assert model.resting[new_id].queue_ahead == Decimal("10")
    assert model.on_print(_buy(BASE + 3, "0.50", "8"), quoting=True) == 0
    assert model.rest_seconds == 2


def test_a_late_print_stamped_while_a_replaced_ask_rested_still_fills_it(tmp_path):
    """Live stepping sees prints seconds to minutes after they happened; an
    ask the engine replaced meanwhile was on the exchange when the print hit."""
    _, market, model = _engine_with_asks(tmp_path, [("Up", "0.50", "10")])
    (old_id,) = market.orders
    market.orders[old_id].live = False
    model.on_actions([Action("CANCEL", SLUG, {"order_id": old_id, "reason": "replace"})], BASE + 20)
    new_id = f"{SLUG}:Up:0.52:{BASE + 20}"
    market.orders[new_id] = WorkingOrder(order_id=new_id, outcome="Up", side=Side.SELL, price=Decimal("0.52"),
                                         size=Decimal("10"), placed_ts=BASE + 20)
    model.on_actions([Action("PLACE", SLUG, {"order_id": new_id, "outcome": "Up", "price": "0.52", "size": "10"})], BASE + 20)
    # A print from BASE+5 arrives now: the old ask was resting then, the new one was not.
    assert model.on_print(_buy(BASE + 5, "0.51", "4"), quoting=True) == 1
    assert model.records[-1].order_id == old_id and model.records[-1].price == Decimal("0.50")
    assert market.up == Decimal("746") and model.late_fills == 1
    assert market.orders[old_id].filled == Decimal("4") and not market.orders[old_id].live
    # A print stamped after the cancel cannot meet the old ask; it meets the new one.
    assert model.on_print(_buy(BASE + 21, "0.52", "3"), quoting=True) == 1
    assert model.records[-1].order_id == new_id
    # Replay order (prints never arrive behind a cancel) is unchanged: the
    # old ask takes nothing stamped at or after its retirement.
    assert model.on_print(_buy(BASE + 20, "0.50", "3"), quoting=True) == 0


def test_a_late_fill_cannot_sell_inventory_promised_to_the_replacement(tmp_path):
    _, market, model = _engine_with_asks(tmp_path, [("Up", "0.50", "10")])
    market.up = Decimal("12")  # nearly sold out: 10 promised to the live replacement leaves 2
    (old_id,) = market.orders
    market.orders[old_id].live = False
    model.on_actions([Action("CANCEL", SLUG, {"order_id": old_id, "reason": "replace"})], BASE + 20)
    new_id = f"{SLUG}:Up:0.52:{BASE + 20}"
    market.orders[new_id] = WorkingOrder(order_id=new_id, outcome="Up", side=Side.SELL, price=Decimal("0.52"),
                                         size=Decimal("10"), placed_ts=BASE + 20)
    model.on_actions([Action("PLACE", SLUG, {"order_id": new_id, "outcome": "Up", "price": "0.52", "size": "10"})], BASE + 20)
    assert model.on_print(_buy(BASE + 5, "0.50", "10"), quoting=True) == 1
    assert model.records[-1].size == Decimal("2") and market.up == Decimal("10")


def test_an_ask_placed_after_the_print_cannot_have_met_it(tmp_path):
    _, _, model = _engine_with_asks(tmp_path, [("Up", "0.50", "5")], placed_at=BASE + 5)
    assert model.on_print(_buy(BASE + 3, "0.50", "50"), quoting=True) == 0
    assert model.on_print(_buy(BASE + 5, "0.50", "50"), quoting=True) == 1


def test_never_fills_above_the_print_price_or_without_a_buy_print(tmp_path):
    _, _, model = _engine_with_asks(tmp_path, [("Up", "0.55", "5"), ("Down", "0.50", "5")])
    assert model.on_print(_buy(BASE + 1, "0.54", "500"), quoting=True) == 0
    assert model.on_print(Print(BASE + 2, "Up", "SELL", Decimal("0.99"), Decimal("500")), quoting=True) == 0
    assert model.on_print(_buy(BASE + 3, "0.49", "500", outcome="Down"), quoting=True) == 0
    # Outside the quoting window nothing fills either, whatever prints.
    assert model.on_print(_buy(BASE + WINDOW + 1, "0.99", "500"), quoting=False) == 0
    assert model.fills == 0 and model.proceeds == 0
    for p in model.records:
        raise AssertionError(p)


def test_touch_only_queue_charges_nothing_to_an_ask_above_the_touch(tmp_path):
    _, _, model = _engine_with_asks(tmp_path, [("Up", "0.50", "5"), ("Up", "0.53", "5")], queue="100", touch_only=True)
    ahead = {r.price: r.queue_ahead for r in model.resting.values()}
    assert ahead == {Decimal("0.50"): Decimal("100"), Decimal("0.53"): Decimal("0")}


def test_fill_record_keeps_fair_at_place_and_at_fill(tmp_path):
    _, market, model = _engine_with_asks(tmp_path, [])
    market.last_fair = Decimal("0.47")
    oid = f"{SLUG}:Up:0.50:{BASE}"
    market.orders[oid] = WorkingOrder(order_id=oid, outcome="Up", side=Side.SELL, price=Decimal("0.50"),
                                      size=Decimal("5"), placed_ts=BASE)
    model.on_actions([Action("PLACE", SLUG, {"order_id": oid, "outcome": "Up", "price": "0.50", "size": "5"})], BASE)
    market.last_fair = Decimal("0.52")  # spot moved toward Up before the taker arrived
    model.on_print(_buy(BASE + 1, "0.50", "5"), quoting=True)
    (rec,) = model.records
    assert (rec.fair_at_place, rec.fair, rec.touch) == (Decimal("0.47"), Decimal("0.52"), Decimal("0.50"))
    assert rec.seconds_into_window == 1


# --- simulation realism -------------------------------------------------------


def _sim(tmp_path, feed):
    from spork30.config import StrategyConfig
    from spork30.simulate import BookPoller, PrintTape, Simulation

    from conftest import make_config

    config = make_config(tmp_path, strategy=StrategyConfig())
    sim = Simulation(config, chainlink=None, spot_feed=feed, tape=PrintTape(start=False), books=BookPoller(start=False),
                     queue_ahead=Decimal("0"), seed_spot=False)
    # Pre-window prints give the window a book (Up 0.49 / 0.50); the sim
    # cannot quote a market whose touch it has never seen.
    book = sim.model_for(SLUG, BASE).book
    book.update(Print(BASE - 10, "Up", "BUY", Decimal("0.50"), Decimal("1")))
    book.update(Print(BASE - 9, "Up", "SELL", Decimal("0.49"), Decimal("1")))
    return sim


def _seeded_feed(start, end, base=Decimal("100000")):
    feed = SpotFeed(start=False)
    for ts in range(start, end):
        feed.record(base + Decimal(ts % 7) - Decimal(ts % 3), ts + 0.999)
    return feed


def test_simulation_prices_from_the_binance_feed(tmp_path):
    now = BASE + 10
    feed = _seeded_feed(BASE - 400, now)
    sim = _sim(tmp_path, feed)
    for ts in range(BASE - WINDOW, now + 1):
        sim.step(ts, [], replay=True)
    market = sim.engine.markets[SLUG]
    assert market.phase is Phase.QUOTING
    expected = fair_up(feed.price_before(now), feed.beat_for(BASE), BASE + WINDOW - now, feed.sigma())
    assert market.last_fair == expected
    live = [o for o in market.orders.values() if o.live]
    assert live, "with spot, beat and sigma present the engine rests asks"
    # The resting asks are at fair + edge, never below the print-implied touch.
    for o in live:
        fair_o = expected if o.outcome == "Up" else Decimal("1") - expected
        assert o.price >= fair_o + sim.config.strategy.fair_edge - Decimal("0.01")
    assert sim.models[SLUG].records == []  # no prints, no fills


def test_simulation_without_spot_rests_nothing(tmp_path):
    now = BASE + 10
    sim = _sim(tmp_path, SpotFeed(start=False))
    # A book exists from prints, but the fair value has no spot to come from.
    for ts in range(BASE - WINDOW, now + 1):
        sim.step(ts, [], replay=True)
    market = sim.engine.markets[SLUG]
    assert market.last_fair is None
    assert not [o for o in market.orders.values() if o.live]
    assert any(r.get("reason") == "no_spot_reference" for r in sim.engine.decisions if r.get("event") == "cancel") or \
        not any(r.get("event") == "place" for r in sim.engine.decisions)


def test_simulation_fill_only_through_a_real_print(tmp_path):
    now = BASE + 10
    feed = _seeded_feed(BASE - 400, now + 5)
    sim = _sim(tmp_path, feed)
    for ts in range(BASE - WINDOW, now + 1):
        sim.step(ts, [], replay=True)
    market = sim.engine.markets[SLUG]

    def up_ask():
        return next(o for o in market.orders.values() if o.live and o.outcome == "Up")

    # Prints reach the sim a poll late, so a print stamped `now` is applied at
    # the next step against the asks that were resting at `now`.
    below = up_ask().price - Decimal("0.01")
    sim.step(now + 1, [_buy(now, str(below), "500")], replay=True)
    assert market.sold_up == 0, "a print one tick below our ask is not a fill"
    at = up_ask().price
    sim.step(now + 2, [_buy(now + 1, str(at), "3")], replay=True)
    assert market.sold_up == Decimal("3"), "a print at our ask fills at most the print size"
    rec = sim.models[SLUG].records[-1]
    assert rec.price == at and rec.fair_at_place is not None and rec.fair is not None
    health = [r for r in sim.engine.decisions if r.get("event") == "health"]
    assert health and health[-1]["fill_model"] == "prints" and health[-1]["queue_ahead"] == "0"
