"""Cash identity of a cycle, and the screen's independent replay of it."""

from decimal import Decimal

from spork30.activity import Book
from spork30.models import Phase
from spork30.portfolio import cash_identity_gap
from spork30.screen import risk_pane

from conftest import BASE, WINDOW, book_at, events, make_engine

SLUG = f"btc-updown-5m-{BASE}"


def _run_cycle(engine, fills, *, winner="Down", tamper=None):
    """Mint BASE, rest asks, apply `fills` ((outcome, size) at the resting price),
    merge at +358 and redeem at +366. Returns the market."""
    mint = BASE - engine.config.strategy.split_lead_seconds
    engine.step(now=mint, book=book_at(mint), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    market = engine.markets[SLUG]
    assert market.split_submitted and market.phase is Phase.QUOTING
    now = BASE + 5
    for outcome, size in fills:
        # Clips are 5-20 shares; take the wanted amount over as many resting asks as it needs.
        left = Decimal(size)
        while left > 0:
            engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
            order = next(o for o in market.orders.values() if o.live and o.outcome == outcome)
            part = min(left, order.remaining)
            engine.apply_fill(order.order_id, part, order.price, ts=now)
            left -= part
            now += 1
    if tamper is not None:
        tamper(market)
    for t in (BASE + 358, BASE + 366):
        engine.step(now=t, book=None, last_up_trade=None, resolved=True, winner=winner, emergency_stop=False)
    assert market.phase is Phase.CLOSED
    return market


def test_cycle_closed_carries_the_legs_and_they_sum_to_cash(tmp_path):
    engine = make_engine(tmp_path)
    market = _run_cycle(engine, [("Up", "10"), ("Down", "4")], winner="Down")
    assert cash_identity_gap(market) == 0
    (closed,) = events(engine, "cycle_closed")
    legs = {k: Decimal(closed[k]) for k in ("proceeds", "merged", "redeemed", "split_size", "cash")}
    assert legs["cash"] == legs["proceeds"] + legs["merged"] + legs["redeemed"] - legs["split_size"]
    # 10 Up and 4 Down sold: 740 pairs merge, the 6 unpaired Down win and redeem at 1.
    assert (legs["merged"], legs["redeemed"]) == (Decimal("740"), Decimal("6"))
    assert (closed["sold_up"], closed["sold_down"], closed["winner"]) == ("10", "4", "Down")
    assert not events(engine, "pnl_mismatch")


def test_winner_residual_under_dust_is_abandoned_and_still_balances(tmp_path):
    engine = make_engine(tmp_path)
    market = _run_cycle(engine, [("Up", "5"), ("Down", "4.5")], winner="Down")
    assert cash_identity_gap(market) == 0 and market.redeemed == 0
    (redeem,) = events(engine, "redeem")
    assert (redeem["size"], redeem["dust"]) == ("0", "0.5")
    assert not events(engine, "pnl_mismatch")


def test_a_cash_gap_is_logged_not_raised(tmp_path):
    engine = make_engine(tmp_path)

    def tamper(market):
        market.cash += Decimal("1")  # a leg went missing somewhere

    market = _run_cycle(engine, [("Up", "5")], winner="Up", tamper=tamper)
    assert market.phase is Phase.CLOSED
    (mismatch,) = events(engine, "pnl_mismatch")
    assert Decimal(mismatch["gap"]) == Decimal("1")


def test_screen_replay_of_the_log_matches_the_engines_booked_cash(tmp_path):
    engine = make_engine(tmp_path)
    market = _run_cycle(engine, [("Up", "10"), ("Down", "4")], winner="Down")
    book = Book()
    for record in engine.decisions:
        book.apply(record)
    view = book.markets[SLUG]
    assert view.phase == "CLOSED" and view.cash == market.cash == view.booked_cash
    assert book.mismatches() == []
    assert book.realized() == market.cash
    won, sold = book.sold_winner()
    assert (won, sold) == (Decimal("4"), Decimal("14"))  # the 4 Down sold went on to win
    text = risk_pane(book, BASE + 400, {}).renderable.renderables
    joined = " ".join(str(t) for t in text)
    assert "sold winner 29%" in joined and "pnl mismatch" not in joined


def test_screen_replay_flags_a_booked_cash_it_cannot_reproduce(tmp_path):
    engine = make_engine(tmp_path)
    _run_cycle(engine, [("Up", "10")], winner="Down")
    book = Book()
    for record in engine.decisions:
        if record.get("event") == "fill":
            continue  # a fill line lost on the way to the screen
        book.apply(record)
    assert [m.slug for m in book.mismatches()] == [SLUG]
    joined = " ".join(str(t) for t in risk_pane(book, BASE + 400, {}).renderable.renderables)
    assert "pnl mismatch 1" in joined
