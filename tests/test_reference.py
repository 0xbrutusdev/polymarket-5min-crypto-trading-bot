"""Reference-account gap audit (Oct 2026): reference reconstruction, the spot
feed's book-ticker prints, the stale-spot grace hold and the sim's CLOB book."""

from decimal import Decimal

from spork30.config import StrategyConfig
from spork30.models import BookTop, Phase
from spork30.reference import profile, rebate_rows, windows_from_activity
from spork30.simulate import BookPoller
from spork30.spotfeed import SpotFeed

from conftest import BASE, WINDOW, book_at, events, make_engine

SLUG = f"btc-updown-5m-{BASE}"


def _row(kind: str, ts: int, *, outcome: str = "", price: str = "0", size: str = "0", usdc: str = "0",
         slug: str = SLUG) -> dict:
    return {"type": kind, "timestamp": ts, "slug": slug, "outcome": outcome, "price": price, "size": size,
            "usdcSize": usdc, "transactionHash": f"0x{kind}{ts}{outcome}{price}", "asset": outcome or "usdc"}


def _activity() -> list[dict]:
    rows = [
        _row("SPLIT", BASE - 293, usdc="750"),
        _row("TRADE", BASE + 40, outcome="Up", price="0.51", size="10", usdc="5.10"),
        _row("TRADE", BASE + 41, outcome="Down", price="0.49", size="10", usdc="4.90"),
        _row("TRADE", BASE + 120, outcome="Up", price="0.55", size="5", usdc="2.75"),
        _row("MERGE", BASE + 358, usdc="735"),
        _row("REDEEM", BASE + 366, outcome="Down", size="5", usdc="5"),
        # The window after is still open at the newest row and must be skipped.
        _row("SPLIT", BASE + WINDOW - 293, usdc="750", slug=f"btc-updown-5m-{BASE + WINDOW}"),
        _row("MAKER_REBATE", BASE + 86400 + 3600, usdc="1.2"),
    ]
    return rows


def test_reference_window_is_rebuilt_from_activity_rows():
    windows = windows_from_activity(_activity(), settled_before=BASE + 2 * WINDOW)
    assert [w.slug for w in windows] == [SLUG]
    w = windows[0]
    assert (w.lead, w.fills, w.sold_up, w.sold_down) == (293, 3, Decimal("15"), Decimal("10"))
    # 750 minted, 12.75 proceeds, 735 merged, 5 redeemed: +2.75 on the window.
    assert w.cash == Decimal("2.75")
    assert (w.winner, w.winner_source, w.sold_winner) == ("Down", "redeem", Decimal("10"))
    assert w.lean == Decimal("5")


def test_reference_profile_reports_volume_cash_and_rebate_per_window():
    rows = _activity()
    windows = windows_from_activity(rows, settled_before=BASE + 2 * WINDOW)
    report = profile(windows, rebate_rows(rows))
    assert report["windows"] == 1
    assert report["fills_per_window"] == 3
    assert report["shares_per_window"] == 25
    assert report["cash_per_window"] == 2.75
    assert report["split_lead_median"] == 293 and report["merge_offset_median"] == 358
    assert report["pair_sum_median"] == 1.0
    # 1.2 USDC rebate posted the next day pays for 12.75 of proceeds: 941 bps.
    day = next(iter(report["rebate_by_day"].values()))
    assert (day["rebate"], day["proceeds"], day["windows"]) == (1.2, 12.75, 1)
    assert report["rebate_bps_median"] == day["bps"]


def test_winner_is_inferred_from_the_side_sold_more_without_a_redeem():
    rows = [r for r in _activity() if r["type"] != "REDEEM"]
    w = windows_from_activity(rows, settled_before=BASE + 2 * WINDOW)[0]
    assert (w.winner, w.winner_source) == ("Up", "inferred")


def test_spot_feed_reads_trades_and_book_ticker_from_the_combined_stream():
    feed = SpotFeed(start=False)
    feed._on_message('{"stream":"btcusdt@trade","data":{"e":"trade","E":1000500,"T":1000400,"p":"100000.5","q":"1"}}')
    price, _ = feed.price(now=1000.9)
    assert price == Decimal("100000.5") and feed.trades == 1
    # Book ticker: mid of best bid and ask, stamped at receipt when the raw
    # event has no time.
    feed._on_message('{"stream":"btcusdt@bookTicker","data":{"u":1,"s":"BTCUSDT","b":"100001","B":"1","a":"100003","A":"1"}}')
    price, age = feed.price()
    assert price == Decimal("100002") and feed.ticks == 1 and age < 1
    # Anything else on the stream is ignored.
    feed._on_message('{"result":null,"id":1}')
    feed._on_message("not json")
    assert feed.health()["spot_trades"] == 1


def test_quotes_are_held_through_a_short_spot_outage_and_pulled_after_grace(tmp_path):
    strategy = StrategyConfig(fair_edge=Decimal("0.03"), max_lean_shares=Decimal("6"), spot_stale_grace_seconds=2)
    engine = make_engine(tmp_path, strategy=strategy)
    market = engine.ensure(BASE)
    market.phase = Phase.QUOTING
    market.split_submitted = True
    market.split_size = Decimal("750")
    market.up = market.down = Decimal("750")
    market.cash = Decimal("-750")
    t = BASE + 10

    def step(now: int, spot):
        return engine.step(now=now, book=book_at(now), last_up_trade=None, resolved=False, winner=None,
                           emergency_stop=False, spot=spot, beat=Decimal("100000"), sigma=0.0003)

    step(t, Decimal("100010"))
    assert any(o.live for o in market.orders.values())
    # Spot drops out: the asks stay for the grace period, the risk state says stale.
    held = step(t + 1, None)
    assert [a.kind for a in held if a.slug == SLUG] == ["QUOTES_HELD"]
    assert engine.risk_state.data_stale and any(o.live for o in market.orders.values())
    step(t + 2, None)
    pulled = step(t + 3, None)
    assert [a.kind for a in pulled if a.slug == SLUG] == ["QUOTES_PULLED"]
    assert not any(o.live for o in market.orders.values())
    # Spot back: the clock resets and the quotes return.
    step(t + 4, Decimal("100010"))
    assert market.spot_lost_since is None and any(o.live for o in market.orders.values())
    assert events(engine, "cancel") and all(e["reason"] == "no_spot_reference" for e in events(engine, "cancel"))


def test_sim_book_poller_serves_a_fresh_clob_top_and_nothing_stale():
    books = BookPoller(start=False)
    assert books.top(BASE) is None and books.health()["book_source"] == "prints"
    books.follow(SLUG)
    books.push(BookTop(bid=Decimal("0.36"), ask=Decimal("0.37"), observed_ts=BASE), asof=1000.0)
    top = books.top(BASE + 1, wall=1001.0)
    assert (top.bid, top.ask, top.observed_ts) == (Decimal("0.36"), Decimal("0.37"), BASE + 1)
    assert books.top(BASE + 5, wall=1004.5) is None
    # A new window drops the old snapshot.
    books.follow(f"btc-updown-5m-{BASE + WINDOW}")
    assert books.top(BASE + 1, wall=1001.0) is None


def test_sim_book_poller_reads_the_up_token_book_like_the_gateway():
    class Level:
        def __init__(self, price: str) -> None:
            self.price = price

    class Book:
        tick_size = "0.01"
        asks = [Level("0.40"), Level("0.37"), Level("0.38")]
        bids = [Level("0.30"), Level("0.36")]

    class Outcome:
        token_id = "123"

    class Market:
        class outcomes:
            yes = Outcome()

    class Client:
        calls: list = []

        def get_market(self, *, slug):
            self.calls.append(("market", slug))
            return Market()

        def get_order_book(self, *, token_id):
            self.calls.append(("book", token_id))
            return Book()

    books = BookPoller(client_factory=Client, start=False)
    books.poll_once()  # nothing followed yet: no call
    books.follow(SLUG)
    books.poll_once()
    books.poll_once()
    # The token is resolved once per window, the book read on every poll.
    assert Client.calls == [("market", SLUG), ("book", "123"), ("book", "123")]
    top = books.top(BASE)
    assert (top.bid, top.ask, top.tick) == (Decimal("0.36"), Decimal("0.37"), Decimal("0.01"))
    assert books.health()["book_source"] == "clob" and books.polls == 2
