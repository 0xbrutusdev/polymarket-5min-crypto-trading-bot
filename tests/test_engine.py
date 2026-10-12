from decimal import Decimal

import pytest

import spork30.portfolio as portfolio
from spork30.models import MarketPosition, Phase, Side, WorkingOrder
from spork30.risk import RiskRefusal, RiskState, check_order
from spork30.strategy import market_slug

from conftest import BASE, WINDOW, book_at, events, make_engine


def _quoting_market(engine, start: int, up="750", down="750") -> MarketPosition:
    m = engine.ensure(start)
    m.phase = Phase.QUOTING
    m.split_submitted = True
    m.split_size = Decimal("750")
    m.up = Decimal(up)
    m.down = Decimal(down)
    m.cash = Decimal("-750")
    return m


# --- stranded REDEEM_PENDING -------------------------------------------------

def test_late_resolution_still_redeems_old_market(tmp_path):
    engine = make_engine(tmp_path)
    old = BASE - 5 * WINDOW  # well outside previous/current/upcoming
    m = engine.ensure(old)
    m.phase = Phase.REDEEM_PENDING
    m.split_submitted = True
    m.flatten_done = True
    m.split_size = Decimal("750")
    m.up = Decimal("30")
    m.down = Decimal("0")
    m.cash = Decimal("-30")
    assert engine.gross_exposure() == Decimal("750")

    # Before the upcoming window's mint (lead 293 s => BASE + 7), so exposure is only the old market.
    now = BASE + 2
    # Without a resolver nothing can be learned, but the market is still stepped and not lost.
    engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    assert m.phase is Phase.REDEEM_PENDING

    actions = engine.step(
        now=now + 1, book=book_at(now + 1), last_up_trade=None, resolved=False, winner=None,
        emergency_stop=False, resolve=lambda start: (True, "Up") if start == old else (False, None),
    )
    assert any(a.kind == "REDEEM" and a.slug == m.slug for a in actions)
    assert m.phase is Phase.CLOSED
    assert m.cash == Decimal("0")
    assert engine.gross_exposure() == Decimal("0")


def test_exposure_no_longer_accumulates_from_unresolved_windows(tmp_path):
    """Four stranded windows used to pin gross exposure at 3000 and halt every new mint."""
    engine = make_engine(tmp_path)
    for k in range(4, 8):
        m = engine.ensure(BASE - k * WINDOW)
        m.phase = Phase.REDEEM_PENDING
        m.split_submitted = True
        m.flatten_done = True
        m.split_size = Decimal("750")
        m.up = Decimal("5")
    assert engine.gross_exposure() == Decimal("3000")
    now = BASE + 2
    engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=False, winner=None,
                emergency_stop=False, resolve=lambda start: (True, "Up"))
    assert engine.gross_exposure() == Decimal("0")
    # The upcoming window can now mint (lead 293 s before BASE + WINDOW, i.e. BASE + 7).
    t = BASE + WINDOW - engine.config.strategy.split_lead_seconds
    assert t == BASE + 7
    actions = engine.step(now=t, book=book_at(t), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    assert [a.kind for a in actions if a.slug == market_slug(BASE + WINDOW, engine.config.strategy)] == ["SPLIT"]


def test_resolution_is_sticky(tmp_path):
    engine = make_engine(tmp_path)
    prev = BASE - WINDOW
    m = _quoting_market(engine, prev)
    m.phase = Phase.FLATTEN_PENDING
    now = BASE + 10
    engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=True, winner="Down", emergency_stop=False)
    assert m.resolved and m.winner == "Down"
    engine.step(now=now + 1, book=book_at(now + 1), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    assert m.resolved and m.winner == "Down"


# --- daily loss kill switch ----------------------------------------------------

def test_daily_loss_blocks_new_split_and_quotes(tmp_path):
    engine = make_engine(tmp_path, max_daily_loss=Decimal("10"))
    loser = engine.ensure(BASE - 3 * WINDOW)
    loser.phase = Phase.CLOSED
    loser.cash = Decimal("-20")
    engine.pnl_day = portfolio._utc_day(BASE)
    engine._book_cycle(loser)
    assert engine.risk_state.realized_pnl_today == Decimal("-20")
    assert engine.daily_loss_breached()

    t = BASE + WINDOW - engine.config.strategy.split_lead_seconds
    actions = engine.step(now=t, book=book_at(t), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    upcoming = market_slug(BASE + WINDOW, engine.config.strategy)
    assert [a.kind for a in actions if a.slug == upcoming] == ["HALT"]
    assert engine.markets[upcoming].halt_reason == "MAX_DAILY_LOSS"
    assert not events(engine, "split")

    with pytest.raises(RiskRefusal, match="MAX_DAILY_LOSS"):
        check_order(now=t, position=loser, outcome="Up", side=Side.SELL, price=Decimal("0.5"), size=Decimal("5"),
                    open_markets=1, gross_exposure=Decimal("0"), risk=engine.config.risk, state=engine.risk_state,
                    reference=Decimal("0.5"))


def test_cycle_is_booked_once_and_resets_on_utc_day_roll(tmp_path):
    engine = make_engine(tmp_path)
    prev = BASE - WINDOW
    m = _quoting_market(engine, prev, up="700", down="750")
    m.phase = Phase.FLATTEN_PENDING
    m.cash = Decimal("-750") + Decimal("50") * Decimal("0.52")
    # Past both clocks (+358 merge, +366 redeem) with the oracle in: merge then redeem on one tick.
    now = prev + 366
    actions = engine.step(now=now, book=None, last_up_trade=None, resolved=True, winner="Down", emergency_stop=False)
    assert [a.kind for a in actions if a.slug == m.slug] == ["MERGE", "REDEEM"]
    assert m.phase is Phase.CLOSED
    expected = m.cash
    assert engine.risk_state.realized_pnl_today == expected
    engine.step(now=now + 1, book=None, last_up_trade=None, resolved=True, winner="Down", emergency_stop=False)
    assert engine.risk_state.realized_pnl_today == expected  # not double counted
    assert len(events(engine, "cycle_closed")) == 1
    engine.step(now=now + 86_400, book=None, last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    assert engine.risk_state.realized_pnl_today == Decimal("0")
    assert events(engine, "day_roll")


def test_realized_pnl_survives_restart(tmp_path):
    engine = make_engine(tmp_path)
    engine.pnl_day = "2026-10-11"
    engine.risk_state.realized_pnl_today = Decimal("-123.45")
    closed = engine.ensure(BASE - 3 * WINDOW)
    closed.phase = Phase.CLOSED
    closed.pnl_booked = True
    engine.save()
    fresh = make_engine(tmp_path)
    fresh.load()
    assert fresh.pnl_day == "2026-10-11"
    assert fresh.risk_state.realized_pnl_today == Decimal("-123.45")
    assert fresh.markets[closed.slug].pnl_booked is True


def test_old_state_file_marks_closed_markets_as_booked(tmp_path):
    engine = make_engine(tmp_path)
    closed = engine.ensure(BASE - 3 * WINDOW)
    closed.phase = Phase.CLOSED
    closed.cash = Decimal("7")
    engine.save()
    import json
    from pathlib import Path
    path = Path(engine.config.state_path)
    payload = json.loads(path.read_text())
    payload.pop("pnl_day"); payload.pop("realized_pnl_today")
    for row in payload["markets"]:
        row.pop("pnl_booked")
    path.write_text(json.dumps(payload))
    fresh = make_engine(tmp_path)
    fresh.load()
    assert fresh.markets[closed.slug].pnl_booked is True
    fresh._book_cycle(fresh.markets[closed.slug])
    assert fresh.risk_state.realized_pnl_today == Decimal("0")


# --- price deviation check is no longer tautological ----------------------------

def test_quote_check_receives_side_specific_reference(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    m = _quoting_market(engine, BASE)
    seen: list[tuple[str, Decimal, Decimal]] = []
    real = portfolio.check_order

    def spy(**kwargs):
        seen.append((kwargs["outcome"], kwargs["price"], kwargs["reference"]))
        return real(**kwargs)

    monkeypatch.setattr(portfolio, "check_order", spy)
    now = BASE + 5
    engine.step(now=now, book=book_at(now, "0.52"), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    by_outcome = {o: (p, r) for o, p, r in seen}
    assert by_outcome["Up"] == (Decimal("0.52"), Decimal("0.52"))
    # Down rests at 1 - Up bid (0.49), one tick above the Down bid (0.48).
    assert by_outcome["Down"] == (Decimal("0.49"), Decimal("0.48"))
    # None of the references equal the price merely by construction when they differ:
    with pytest.raises(RiskRefusal, match="MAX_PRICE_DEVIATION"):
        check_order(now=now, position=m, outcome="Down", side=Side.SELL, price=Decimal("0.99"), size=Decimal("5"),
                    open_markets=1, gross_exposure=Decimal("750"), risk=engine.config.risk, state=RiskState(),
                    reference=Decimal("0.48"))


# --- fills ------------------------------------------------------------------

def test_full_fill_marks_order_as_nothing_to_cancel(tmp_path):
    engine = make_engine(tmp_path)
    _quoting_market(engine, BASE)
    now = BASE + 5
    actions = engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    placed = [a for a in actions if a.kind == "PLACE"]
    assert placed
    order_id = placed[0].detail["order_id"]
    order = engine.markets[placed[0].slug].orders[order_id]
    order.exchange_id = "ext-1"
    engine.apply_fill(order_id, order.size / 2, order.price, ts=now)
    assert order.live and not order.cancel_sent
    engine.apply_fill(order_id, order.remaining, order.price, ts=now)
    assert not order.live and order.cancel_sent


# --- R9: resting sells reserve inventory ----------------------------------------

def test_resting_unreconciled_sells_reserve_inventory(tmp_path):
    engine = make_engine(tmp_path)
    m = _quoting_market(engine, BASE, up="12", down="750")
    # A replaced Up ask the exchange has not confirmed cancelled yet: 10 shares still at risk.
    m.orders["old"] = WorkingOrder(order_id="old", outcome="Up", side=Side.SELL, price=Decimal("0.60"),
                                   size=Decimal("10"), live=False, exchange_id="ext-old", cancel_sent=True)
    assert m.resting("Up") == Decimal("10") and m.available("Up") == Decimal("2")
    now = BASE + 5
    actions = engine.step(now=now, book=book_at(now, "0.52"), last_up_trade=None, resolved=False, winner=None,
                          emergency_stop=False)
    placed = {a.detail["outcome"] for a in actions if a.kind == "PLACE"}
    assert placed == {"Down"}  # only 2 Up are free, below the venue minimum of 5
    # Once the exchange confirms the old order is gone, the Up inventory is quotable again.
    m.orders["old"].reconciled = True
    actions = engine.step(now=now + 1, book=book_at(now + 1, "0.53"), last_up_trade=None, resolved=False,
                          winner=None, emergency_stop=False)
    up = [a for a in actions if a.kind == "PLACE" and a.detail["outcome"] == "Up"]
    assert up and Decimal(up[0].detail["size"]) <= Decimal("12")


def test_check_order_refuses_sell_beyond_unreserved_inventory(tmp_path):
    engine = make_engine(tmp_path)
    m = _quoting_market(engine, BASE, up="20", down="20")
    kwargs = dict(now=BASE, position=m, outcome="Up", side=Side.SELL, price=Decimal("0.5"), size=Decimal("15"),
                  open_markets=1, gross_exposure=Decimal("750"), risk=engine.config.risk, state=RiskState(),
                  reference=Decimal("0.5"))
    assert "INVENTORY_OK" in check_order(**kwargs)
    with pytest.raises(RiskRefusal, match="SELL_EXCEEDS_INVENTORY"):
        check_order(**kwargs, reserved=Decimal("10"))


# --- R10: risk halts still unwind their tokens ----------------------------------

def test_halted_market_still_merges_and_redeems(tmp_path):
    engine = make_engine(tmp_path)
    prev = BASE - WINDOW
    m = _quoting_market(engine, prev, up="740", down="750")
    engine.halt(m, "MAX_PRICE_DEVIATION")
    assert m.phase is Phase.HALTED and engine.recoverable(m)
    # Not time yet: nothing is sent and the market stays halted.
    t = prev + 350
    actions = engine.step(now=t, book=book_at(t), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    assert not [a for a in actions if a.slug == m.slug]
    assert m.phase is Phase.HALTED
    # +358: merge the 740 pairs before the oracle; the 10 Down wait for +366 and resolution.
    t = prev + 358
    actions = engine.step(now=t, book=book_at(t), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    assert [a.kind for a in actions if a.slug == m.slug] == ["MERGE"]
    assert (m.up, m.down, m.cash) == (Decimal("0"), Decimal("10"), Decimal("-10"))
    assert events(engine, "unwind") and events(engine, "unwind")[0]["halt_reason"] == "MAX_PRICE_DEVIATION"
    # Resolved at +365 but the redeem clock is +366: nothing yet.
    actions = engine.step(now=prev + 365, book=book_at(prev + 365), last_up_trade=None, resolved=True, winner="Down",
                          emergency_stop=False)
    assert not [a for a in actions if a.slug == m.slug]
    actions = engine.step(now=prev + 366, book=book_at(prev + 366), last_up_trade=None, resolved=True, winner="Down",
                          emergency_stop=False)
    assert [a.kind for a in actions if a.slug == m.slug] == ["REDEEM"]
    assert m.phase is Phase.CLOSED and m.cash == Decimal("0") and m.halt_reason == "MAX_PRICE_DEVIATION"


def test_chain_failure_halts_are_not_retried(tmp_path):
    engine = make_engine(tmp_path)
    prev = BASE - WINDOW
    m = _quoting_market(engine, prev)
    engine.halt(m, "MERGE_FAILED")
    assert not engine.recoverable(m)
    t = prev + 366
    actions = engine.step(now=t, book=book_at(t), last_up_trade=None, resolved=True, winner="Up", emergency_stop=False)
    assert not [a for a in actions if a.slug == m.slug]
    assert m.phase is Phase.HALTED and m.up == Decimal("750")


def test_emergency_stop_file_halts_everything(tmp_path):
    engine = make_engine(tmp_path)
    _quoting_market(engine, BASE)
    from pathlib import Path
    Path(engine.config.emergency_stop_path).write_text("")
    now = BASE + 5
    actions = engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    assert all(a.kind == "HALT" for a in actions) and actions
    assert not any(o.live for _, o in engine.live_orders())
