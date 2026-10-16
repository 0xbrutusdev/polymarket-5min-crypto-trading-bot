"""Regressions for the Oct 13 2026 loss audit."""

from decimal import Decimal

from spork30.activity import Book
from spork30.config import StrategyConfig
from spork30.live import live_preflight
from spork30.models import Phase
from spork30.strategy import market_slug

from conftest import BASE, WINDOW, book_at, make_config, make_engine

SLUG = f"btc-updown-5m-{BASE}"


def _mint_time(engine) -> int:
    return BASE + WINDOW - engine.config.strategy.split_lead_seconds


def test_unminted_windows_from_restarts_do_not_block_the_mint(tmp_path):
    engine = make_engine(tmp_path, max_open_markets=8)
    for k in range(3, 23):
        engine.ensure(BASE - k * WINDOW)
    engine.save()
    fresh = make_engine(tmp_path, max_open_markets=8)
    fresh.load()
    assert fresh.open_count() == 0
    t = _mint_time(fresh)
    actions = fresh.step(now=t, book=book_at(t), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    upcoming = market_slug(BASE + WINDOW, fresh.config.strategy)
    assert [a.kind for a in actions if a.slug == upcoming] == ["SPLIT"]


def test_open_market_limit_counts_the_mint_about_to_open(tmp_path):
    engine = make_engine(tmp_path, max_open_markets=1)
    held = engine.ensure(BASE - 4 * WINDOW)
    held.phase = Phase.REDEEM_PENDING
    held.split_submitted = True
    held.split_size = Decimal("750")
    held.up = Decimal("5")
    assert engine.open_count() == 1
    t = _mint_time(engine)
    engine.step(now=t, book=book_at(t), last_up_trade=None, resolved=False, winner=None, emergency_stop=False)
    upcoming = engine.markets[market_slug(BASE + WINDOW, engine.config.strategy)]
    assert upcoming.phase is Phase.HALTED and upcoming.halt_reason == "MAX_OPEN_MARKETS"


def test_screen_returns_split_debit_when_acked_split_reverts():
    book = Book(require_ack=True)
    book.apply({"event": "split", "slug": SLUG, "ts": 1, "size": "750", "up": "750", "down": "750"})
    book.apply({"event": "send", "slug": SLUG, "kind": "SPLIT", "external_id": "tx-1", "order_id": ""})
    assert book.markets[SLUG].cash == Decimal("-750")
    book.apply({"event": "send_fail", "slug": SLUG, "kind": "SPLIT", "order_id": "", "error": "execution reverted"})
    market = book.markets[SLUG]
    assert (market.cash, market.up, market.down, market.phase) == (Decimal("0"), Decimal("0"), Decimal("0"), "HALTED")
    assert book.open_cash() == 0 and book.daily_pnl(BASE)[1] == 0


def test_screen_unacked_split_failure_changes_no_cash():
    book = Book(require_ack=True)
    book.apply({"event": "split", "slug": SLUG, "ts": 1, "size": "750", "up": "750", "down": "750"})
    book.apply({"event": "send_fail", "slug": SLUG, "kind": "SPLIT", "order_id": "", "error": "refused"})
    assert book.markets[SLUG].cash == 0


def test_open_window_is_marked_at_merge_value():
    book = Book()
    book.apply({"event": "split", "slug": SLUG, "ts": BASE - 200, "size": "750", "up": "750", "down": "750"})
    book.apply({"event": "fill", "slug": SLUG, "order_id": "a", "outcome": "Up", "price": "0.60", "size": "10",
                "inventory": "740"})
    market = book.markets[SLUG]
    assert market.cash == Decimal("-744.00")
    # 740 pairs merge for 740; the 10 unpaired Down are not counted until redeemed.
    assert market.marked() == Decimal("-4.00")
    assert book.open_marked() == Decimal("-4.00")
    assert book.daily_pnl(BASE)[1] == Decimal("-4.00")


def test_spot_two_seconds_old_is_not_quoted_on():
    from spork30.spotfeed import SpotFeed

    feed = SpotFeed(start=False)
    feed.record(Decimal("100000"), BASE + 0.5)
    assert feed.fresh_price(now=BASE + 1.5) == Decimal("100000")
    assert feed.fresh_price(now=BASE + 2.5) is None


def test_preflight_refuses_touch_quoting_and_an_empty_wallet(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_TOUCH_QUOTING", raising=False)
    touch = make_config(tmp_path, strategy=StrategyConfig(fair_edge=Decimal("0")))
    assert any("FAIR_EDGE" in p for p in live_preflight(touch, collateral=Decimal("5000"), open_markets=0))
    monkeypatch.setenv("ALLOW_TOUCH_QUOTING", "YES")
    assert live_preflight(touch, collateral=Decimal("5000"), open_markets=0) == []

    fair = make_config(tmp_path, strategy=StrategyConfig())
    assert any("collateral" in p for p in live_preflight(fair, collateral=Decimal("0"), open_markets=0))
    # Positions left to unwind: start anyway so the merge and redeem still run.
    assert live_preflight(fair, collateral=Decimal("0"), open_markets=2) == []
    # Balance unknown (read failed): do not block on it.
    assert live_preflight(fair, collateral=None, open_markets=0) == []
    assert live_preflight(fair, collateral=Decimal("750"), open_markets=0) == []
