"""PolymarketVenue against a fake client and a fake RPC. No network."""

import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import spork30.gateway as gateway
from spork30.gateway import PolymarketVenue
from spork30.models import Phase, Side, WorkingOrder

from conftest import BASE, make_engine


def _market(slug: str, cond: str = "0x" + "11" * 32, up_first: bool = True):
    up = SimpleNamespace(label="Up", token_id="tok-up", price=Decimal("0.5"))
    down = SimpleNamespace(label="Down", token_id="tok-down", price=Decimal("0.5"))
    yes, no = (up, down) if up_first else (down, up)
    return SimpleNamespace(slug=slug, condition_id=cond, outcomes=SimpleNamespace(yes=yes, no=no),
                           state=SimpleNamespace(closed=False), resolution=SimpleNamespace(uma_resolution_status=None))


class FakeClient:
    def __init__(self):
        self.markets: dict[str, object] = {}
        self.closed: dict[str, object] = {}
        self.orders: dict[str, SimpleNamespace] = {}
        self.get_market_calls = 0
        self.book = SimpleNamespace(
            asks=[SimpleNamespace(price="0.41", size="100"), SimpleNamespace(price="0.42", size="50")],
            bids=[SimpleNamespace(price="0.40", size="43"), SimpleNamespace(price="0.39", size="10")],
            last_trade_price=Decimal("0.41"), timestamp=None, tick_size=Decimal("0.01"),
        )

    def get_market(self, *, slug):
        self.get_market_calls += 1
        if slug in self.markets:
            return self.markets[slug]
        raise RuntimeError("404")

    def list_markets(self, *, slug, closed, page_size):
        return iter([self.closed[slug]] if slug in self.closed else [])

    def get_order_book(self, *, token_id):
        return self.book

    def get_order(self, *, order_id):
        if order_id not in self.orders:
            raise RuntimeError("404")
        return self.orders[order_id]


def _venue(tmp_path, client=None, rpc="http://rpc.test") -> tuple[PolymarketVenue, FakeClient]:
    client = client or FakeClient()
    venue = PolymarketVenue(client, rpc=rpc, cache_path=tmp_path / "markets.json")
    return venue, client


def _no_throttle(monkeypatch, venue):
    clock = {"t": 0.0}

    def tick():
        clock["t"] += gateway.RESOLUTION_POLL_SECONDS
        return clock["t"]
    monkeypatch.setattr(venue, "_monotonic", tick)


def _fake_rpc(monkeypatch, denominator: int, numerators: tuple[int, int]):
    def eth_call(rpc, data_hex):
        sel = data_hex[:8]
        if sel == gateway._SEL_PAYOUT_DENOMINATOR:
            return denominator
        index = int(data_hex[-64:], 16)
        return numerators[index]
    monkeypatch.setattr(gateway, "_eth_call_uint", eth_call)


def test_book_reports_tick_and_real_bid(tmp_path):
    venue, client = _venue(tmp_path)
    client.markets[f"btc-updown-5m-{BASE}"] = _market("x")
    top = venue.book(BASE + 10)
    assert (top.bid, top.ask, top.tick) == (Decimal("0.40"), Decimal("0.41"), Decimal("0.01"))
    client.book.bids = []
    top = venue.book(BASE + 11)
    assert top.bid is None and top.ask == Decimal("0.41")


def test_resolution_from_ctf_payout(tmp_path, monkeypatch):
    venue, client = _venue(tmp_path)
    _no_throttle(monkeypatch, venue)
    slug = f"btc-updown-5m-{BASE - 300}"
    client.markets[slug] = _market(slug)
    _fake_rpc(monkeypatch, 0, (0, 0))
    assert venue.resolution(BASE + 10) == (False, None)
    _fake_rpc(monkeypatch, 1, (0, 1))
    assert venue.resolution(BASE + 10) == (True, "Down")
    # Resolved answers are cached; a different window exercises the other shapes.
    client.markets[f"btc-updown-5m-{BASE - 600}"] = _market("y")
    _fake_rpc(monkeypatch, 1, (1, 0))
    assert venue.resolution_for(BASE - 600) == (True, "Up")
    client.markets[f"btc-updown-5m-{BASE - 900}"] = _market("z")
    _fake_rpc(monkeypatch, 2, (1, 1))
    assert venue.resolution_for(BASE - 900) == (False, None)


def test_resolution_is_cached_and_throttled(tmp_path, monkeypatch):
    venue, client = _venue(tmp_path)
    slug = f"btc-updown-5m-{BASE - 300}"
    client.markets[slug] = _market(slug)
    calls = {"n": 0}
    clock = {"t": 1000.0}
    monkeypatch.setattr(venue, "_monotonic", lambda: clock["t"])

    def eth_call(rpc, data_hex):
        calls["n"] += 1
        return 0
    monkeypatch.setattr(gateway, "_eth_call_uint", eth_call)
    assert venue.resolution_for(BASE - 300) == (False, None)
    assert venue.resolution_for(BASE - 300) == (False, None)
    assert calls["n"] == 1  # second read within the poll interval was skipped
    clock["t"] += gateway.RESOLUTION_POLL_SECONDS
    _fake_rpc(monkeypatch, 1, (1, 0))
    assert venue.resolution_for(BASE - 300) == (True, "Up")
    monkeypatch.setattr(gateway, "_eth_call_uint", eth_call)
    before = calls["n"]
    clock["t"] += 100
    assert venue.resolution_for(BASE - 300) == (True, "Up")
    assert calls["n"] == before  # resolved answers come from cache


def test_payout_index_follows_outcome_order(tmp_path, monkeypatch):
    venue, client = _venue(tmp_path)
    slug = f"btc-updown-5m-{BASE - 300}"
    client.markets[slug] = _market(slug, up_first=False)
    _fake_rpc(monkeypatch, 1, (0, 1))  # slot 1 pays; slot 1 is Up here
    assert venue.resolution_for(BASE - 300) == (True, "Up")


def test_legs_fall_back_to_closed_lookup_and_persist(tmp_path, monkeypatch):
    venue, client = _venue(tmp_path)
    slug = f"btc-updown-5m-{BASE - 3000}"
    client.closed[slug] = _market(slug, cond="0x" + "22" * 32)
    legs = venue._legs(slug)
    assert legs.condition == "0x" + "22" * 32 and legs.up == "tok-up"
    assert (tmp_path / "markets.json").exists()
    # A fresh venue (restart) resolves from the cache without any market lookup.
    fresh, client2 = _venue(tmp_path, client=FakeClient())
    _fake_rpc(monkeypatch, 1, (1, 0))
    assert fresh.resolution_for(BASE - 3000) == (True, "Up")
    assert client2.get_market_calls == 0


def test_gamma_fallback_when_no_rpc(tmp_path, monkeypatch):
    venue, client = _venue(tmp_path, rpc="")
    _no_throttle(monkeypatch, venue)
    slug = f"btc-updown-5m-{BASE - 300}"
    m = _market(slug)
    client.markets[slug] = m
    assert venue.resolution_for(BASE - 300) == (False, None)
    m.state.closed = True
    m.outcomes.no.price = Decimal("1")
    assert venue.resolution_for(BASE - 300) == (True, "Down")


def _engine_with_order(tmp_path, *, live: bool, cancel_sent: bool = False):
    engine = make_engine(tmp_path)
    m = engine.ensure(BASE)
    m.phase = Phase.QUOTING; m.split_submitted = True; m.split_size = Decimal("750")
    m.up = m.down = Decimal("750"); m.cash = Decimal("-750")
    order = WorkingOrder(order_id="o1", outcome="Up", side=Side.SELL, price=Decimal("0.41"), size=Decimal("10"),
                         exchange_id="ext-1", live=live, cancel_sent=cancel_sent)
    m.orders["o1"] = order
    return engine, m, order


def test_sync_fills_books_late_fill_on_cancelled_order(tmp_path):
    venue, client = _venue(tmp_path)
    engine, m, order = _engine_with_order(tmp_path, live=False, cancel_sent=True)
    client.orders["ext-1"] = SimpleNamespace(size_matched="10", status="matched")
    venue.sync_fills(engine, BASE + 20)
    assert order.filled == Decimal("10") and order.reconciled and not order.live
    assert m.up == Decimal("740") and m.cash == Decimal("-750") + Decimal("4.1")
    fills = [r for r in engine.decisions if r["event"] == "fill"]
    assert fills and fills[0]["late"] is True
    # Reconciled orders are not polled again.
    before = len(client.orders)
    client.orders["ext-1"] = SimpleNamespace(size_matched="10", status="matched")
    venue.sync_fills(engine, BASE + 21)
    assert order.filled == Decimal("10") and len(client.orders) == before


def test_sync_fills_gives_up_on_unknown_dead_order(tmp_path):
    venue, client = _venue(tmp_path)
    engine, m, order = _engine_with_order(tmp_path, live=False)
    for _ in range(gateway.MAX_SYNC_ATTEMPTS):
        venue.sync_fills(engine, BASE + 20)
    assert order.reconciled and order.cancel_sent


# --- R7: chain transactions do not block the loop ----------------------------------

class SlowHandle:
    def __init__(self, gate: threading.Event, fail: bool = False):
        self.gate = gate
        self.fail = fail

    def wait(self):
        self.gate.wait(timeout=5)
        if self.fail:
            raise RuntimeError("relayer: execution reverted")
        return SimpleNamespace(transaction_hash="0xdeadbeef")


def test_split_returns_ticket_before_the_transaction_lands(tmp_path):
    venue, client = _venue(tmp_path)
    slug = f"btc-updown-5m-{BASE}"
    client.markets[slug] = _market(slug)
    gate = threading.Event()
    client.split_position = lambda *, condition_id, amount: SlowHandle(gate)
    t0 = time.monotonic()
    ticket = venue({"kind": "SPLIT", "slug": slug, "size": "750"})
    assert ticket.startswith("tx-") and time.monotonic() - t0 < 1.0
    assert venue.finished_transactions() == [] and venue.chain.pending() == 1
    gate.set()
    assert venue.wait_for_chain(timeout=5)
    done = venue.finished_transactions()
    assert len(done) == 1 and done[0]["ok"] and done[0]["hash"] == "0xdeadbeef"
    assert done[0]["payload"]["kind"] == "SPLIT" and done[0]["payload"]["condition"] == "0x" + "11" * 32


def test_chain_jobs_run_in_order_and_skip_after_a_failure(tmp_path):
    venue, client = _venue(tmp_path)
    slug = f"btc-updown-5m-{BASE}"
    other = f"btc-updown-5m-{BASE + 300}"
    client.markets[slug] = _market(slug)
    client.markets[other] = _market(other, cond="0x" + "33" * 32)
    gate = threading.Event()
    calls: list[str] = []

    def merge(*, condition_id, amount):
        calls.append("MERGE")
        return SlowHandle(gate, fail=True)

    def redeem(*, condition_id):
        calls.append(f"REDEEM:{condition_id[-2:]}")
        return SlowHandle(gate)

    client.merge_positions = merge
    client.redeem_positions = redeem
    venue({"kind": "MERGE", "slug": slug, "size": "700"})
    venue({"kind": "REDEEM", "slug": slug, "size": "50", "winner": "Up"})
    venue({"kind": "REDEEM", "slug": other, "size": "5", "winner": "Up"})
    gate.set()
    assert venue.wait_for_chain(timeout=5)
    done = venue.finished_transactions()
    assert [d["ok"] for d in done] == [False, False, True]
    assert "reverted" in done[0]["error"] and "skipped" in done[1]["error"]
    # The REDEEM on the failed market was never sent; the other market's was.
    assert calls == ["MERGE", "REDEEM:33"]


def test_unknown_market_fails_synchronously(tmp_path):
    venue, client = _venue(tmp_path)
    with pytest.raises(RuntimeError):
        venue({"kind": "SPLIT", "slug": "btc-updown-5m-1", "size": "750"})
    assert venue.chain.pending() == 0


# --- R8: user feed replaces per-tick polling ----------------------------------------

class FakeFeed:
    def __init__(self, healthy: bool = True):
        self._healthy = healthy
        self.updates: dict[str, gateway.OrderUpdate] = {}

    def healthy(self) -> bool:
        return self._healthy

    def drain(self):
        out, self.updates = self.updates, {}
        return out


class CountingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.polls = 0

    def get_order(self, *, order_id):
        self.polls += 1
        return super().get_order(order_id=order_id)


def test_feed_update_books_fill_without_a_rest_call(tmp_path):
    client = CountingClient()
    feed = FakeFeed()
    venue = PolymarketVenue(client, rpc="", cache_path=tmp_path / "m.json", feed=feed)
    engine, m, order = _engine_with_order(tmp_path, live=True)
    feed.updates["ext-1"] = gateway.OrderUpdate("ext-1", Decimal("4"), "live", 0.0)
    venue.sync_fills(engine, BASE + 20)
    assert order.filled == Decimal("4") and order.live and client.polls == 0
    feed.updates["ext-1"] = gateway.OrderUpdate("ext-1", Decimal("10"), "matched", 0.0)
    venue.sync_fills(engine, BASE + 21)
    assert order.filled == Decimal("10") and order.reconciled and not order.live and client.polls == 0


def test_healthy_feed_slows_rest_polling_and_trade_hint_forces_it(tmp_path):
    client = CountingClient()
    client.orders["ext-1"] = SimpleNamespace(size_matched="0", status="live")
    feed = FakeFeed()
    venue = PolymarketVenue(client, rpc="", cache_path=tmp_path / "m.json", feed=feed)
    engine, m, order = _engine_with_order(tmp_path, live=True)
    for tick in range(gateway.SLOW_POLL_SECONDS * 2):
        venue.sync_fills(engine, BASE + tick)
    assert client.polls == 2  # once per SLOW_POLL_SECONDS instead of every tick
    feed.updates["ext-1"] = gateway.OrderUpdate("ext-1", None, "", 0.0)  # trade event named this order
    client.orders["ext-1"] = SimpleNamespace(size_matched="7", status="live")
    venue.sync_fills(engine, BASE + 100)
    assert client.polls == 3 and order.filled == Decimal("7")


def test_unhealthy_feed_polls_every_tick(tmp_path):
    client = CountingClient()
    client.orders["ext-1"] = SimpleNamespace(size_matched="0", status="live")
    venue = PolymarketVenue(client, rpc="", cache_path=tmp_path / "m.json", feed=FakeFeed(healthy=False))
    engine, m, order = _engine_with_order(tmp_path, live=True)
    for tick in range(5):
        venue.sync_fills(engine, BASE + tick)
    assert client.polls == 5


def test_user_feed_translates_sdk_events():
    feed = gateway.UserFeed(connect=None)  # never started
    order = SimpleNamespace(type="order", payload=SimpleNamespace(id="o-1", size_matched="3.5", status="LIVE",
                                                                  order_event_type="UPDATE"))
    cancel = SimpleNamespace(type="order", payload=SimpleNamespace(id="o-2", size_matched="0", status=None,
                                                                   order_event_type="CANCELLATION"))
    trade = SimpleNamespace(type="trade", payload=SimpleNamespace(
        maker_orders=(SimpleNamespace(order_id="o-1"), SimpleNamespace(order_id="o-3"))))
    for event in (order, cancel, trade):
        feed.push_event(event)
    latest = feed.drain()
    assert latest["o-1"].size_matched == Decimal("3.5") and latest["o-1"].status == "live"  # hint did not clobber
    assert latest["o-2"].status == "canceled"
    assert latest["o-3"].size_matched is None
    assert feed.events_seen == 3 and not feed.healthy()


def test_sync_fills_live_partial_then_cancelled(tmp_path):
    venue, client = _venue(tmp_path)
    engine, m, order = _engine_with_order(tmp_path, live=True)
    client.orders["ext-1"] = SimpleNamespace(size_matched="3", status="live")
    venue.sync_fills(engine, BASE + 20)
    assert order.filled == Decimal("3") and order.live and not order.reconciled
    client.orders["ext-1"] = SimpleNamespace(size_matched="3", status="canceled")
    venue.sync_fills(engine, BASE + 21)
    assert not order.live and order.reconciled and order.cancel_sent
