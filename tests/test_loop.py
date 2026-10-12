from decimal import Decimal

from spork30.config import TradingMode
from spork30.loop import _dispatch_resting_cancels, cancel_all_live, run_loop
from spork30.models import Phase

from conftest import BASE, WINDOW, FakeVenue, events, make_engine


def _clock(start: int):
    t = {"now": start - 1}

    def now() -> int:
        t["now"] += 1
        return t["now"]

    return now


def _armed(tmp_path, **risk):
    engine = make_engine(tmp_path, TradingMode.LIVE, **risk)
    venue = FakeVenue()
    return engine, venue


def test_tick_exception_is_logged_and_loop_continues(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    venue.sync_raises = 1
    run_loop(engine.config, engine, venue, now_fn=_clock(BASE + 5), sleep=lambda s: None, cycles=3, quiet=True, cancel_on_exit=False)
    errs = events(engine, "loop_error")
    assert len(errs) == 1 and "502" in errs[0]["error"]
    # Later ticks still ran: the trio was discovered and stepped.
    assert len(engine.markets) >= 3


def test_loop_aborts_after_consecutive_errors(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    venue.sync_raises = 10_000
    run_loop(engine.config, engine, venue, now_fn=_clock(BASE + 5), sleep=lambda s: None, cycles=10_000, quiet=True, cancel_on_exit=False)
    assert events(engine, "loop_abort")
    assert len(events(engine, "loop_error")) == 30


def test_exit_cancels_resting_orders(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    m = engine.ensure(BASE)
    m.phase = Phase.QUOTING; m.split_submitted = True; m.split_size = Decimal("750")
    m.up = m.down = Decimal("750"); m.cash = Decimal("-750")
    run_loop(engine.config, engine, venue, now_fn=_clock(BASE + 5), sleep=lambda s: None, cycles=1, quiet=True)
    placed = [p for p in venue.sent if p["kind"] == "PLACE"]
    cancels = [p for p in venue.sent if p["kind"] == "CANCEL"]
    assert len(placed) == 2
    assert {c["order_id"] for c in cancels} == {p["order_id"] for p in placed}
    assert not engine.live_orders()
    assert [r for r in events(engine, "cancel") if r["reason"] == "shutdown"]


def test_filled_orders_are_not_cancelled_forever(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    m = engine.ensure(BASE)
    m.phase = Phase.QUOTING; m.split_submitted = True; m.split_size = Decimal("750")
    m.up = m.down = Decimal("750"); m.cash = Decimal("-750")
    run_loop(engine.config, engine, venue, now_fn=_clock(BASE + 5), sleep=lambda s: None, cycles=1, quiet=True, cancel_on_exit=False)
    (_, order) = engine.live_orders()[0]
    engine.apply_fill(order.order_id, order.size, order.price, ts=BASE + 6)
    before = len(venue.sent)
    for _ in range(5):
        _dispatch_resting_cancels(engine.config, engine, venue, quiet=True)
    assert len(venue.sent) == before


def test_partially_filled_dead_order_still_gets_one_cancel(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    m = engine.ensure(BASE)
    m.phase = Phase.QUOTING; m.split_submitted = True; m.split_size = Decimal("750")
    m.up = m.down = Decimal("750"); m.cash = Decimal("-750")
    run_loop(engine.config, engine, venue, now_fn=_clock(BASE + 5), sleep=lambda s: None, cycles=1, quiet=True, cancel_on_exit=False)
    (_, order) = engine.live_orders()[0]
    engine.apply_fill(order.order_id, Decimal("1"), order.price, ts=BASE + 6)
    engine._cancel_all(m, "stale_book")
    before = len(venue.sent)
    _dispatch_resting_cancels(engine.config, engine, venue, quiet=True)
    _dispatch_resting_cancels(engine.config, engine, venue, quiet=True)
    sent = [p for p in venue.sent[before:] if p["kind"] == "CANCEL"]
    assert len(sent) == 2 and {p["order_id"] for p in sent} == {o.order_id for o in m.orders.values()}


def test_failed_merge_is_rolled_back_and_halts(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    venue.fail.add("MERGE")
    prev = BASE - WINDOW
    m = engine.ensure(prev)
    m.phase = Phase.FLATTEN_PENDING; m.split_submitted = True; m.split_size = Decimal("750")
    m.up = Decimal("700"); m.down = Decimal("720"); m.cash = Decimal("-750") + Decimal("80") * Decimal("0.5")
    cash_before = m.cash
    run_loop(engine.config, engine, venue, now_fn=_clock(prev + 358), sleep=lambda s: None, cycles=1, quiet=True, cancel_on_exit=False)
    assert "MERGE" in venue.kinds()
    assert m.phase is Phase.HALTED and m.halt_reason == "MERGE_FAILED"
    assert (m.up, m.down, m.cash) == (Decimal("700"), Decimal("720"), cash_before)
    assert events(engine, "send_fail")


def test_failed_redeem_is_rolled_back_including_booked_pnl(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    venue.fail.add("REDEEM")
    prev = BASE - WINDOW
    m = engine.ensure(prev)
    # Already merged at +358; 30 unpaired Down (the winner) wait for the +366 redeem.
    m.phase = Phase.REDEEM_PENDING; m.split_submitted = True; m.flatten_done = True; m.split_size = Decimal("750")
    m.up = Decimal("0"); m.down = Decimal("30"); m.cash = Decimal("-750") + Decimal("70") * Decimal("0.5") + Decimal("700")
    venue.resolutions[prev] = (True, "Down")
    cash_before = m.cash
    run_loop(engine.config, engine, venue, now_fn=_clock(prev + 366), sleep=lambda s: None, cycles=1, quiet=True, cancel_on_exit=False)
    assert [p["kind"] for p in venue.sent if p["slug"] == m.slug] == ["REDEEM"]
    assert m.phase is Phase.HALTED and m.halt_reason == "REDEEM_FAILED"
    assert m.down == Decimal("30") and m.cash == cash_before
    assert engine.risk_state.realized_pnl_today == Decimal("0")
    assert m.pnl_booked is False


def test_loop_uses_venue_resolution_for_late_markets(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    old = BASE - 6 * WINDOW
    m = engine.ensure(old)
    m.phase = Phase.REDEEM_PENDING; m.split_submitted = True; m.flatten_done = True; m.split_size = Decimal("750")
    m.up = Decimal("40"); m.cash = Decimal("-40")
    venue.resolutions[old] = (True, "Up")
    run_loop(engine.config, engine, venue, now_fn=_clock(BASE + 5), sleep=lambda s: None, cycles=1, quiet=True, cancel_on_exit=False)
    assert m.phase is Phase.CLOSED
    assert [p for p in venue.sent if p["kind"] == "REDEEM" and p["slug"] == m.slug]


def test_full_lifecycle_merges_first_then_redeems_the_residual(tmp_path, live_env):
    """Split at -293 s, quote through the window, oracle reports at +65 s (as the
    CTF does live), MERGE fires at +358 s before the oracle, REDEEM of the
    unpaired winner at +366 s, nothing is stranded."""
    engine, venue = _armed(tmp_path)
    start = BASE + WINDOW
    first = start - 293
    last = start + 380
    ticks = last - first + 1

    class OracleVenue(FakeVenue):
        def resolution_for(self, s):
            if s == start and self._now >= start + WINDOW + 65:
                return True, "Up"
            return False, None

        def resolution(self, now):
            self._now = now
            return self.resolution_for(now - now % WINDOW - WINDOW)

        def sync_fills(self, engine, now):
            # One Down clip sells at +30 s so the merge leaves an unpaired Up residual.
            if now == start + 30:
                for market, order in engine.live_orders():
                    if market.slug == f"btc-updown-5m-{start}" and order.outcome == "Down":
                        engine.apply_fill(order.order_id, order.size, order.price, ts=now)
                        break

    venue = OracleVenue()
    venue._now = first
    run_loop(engine.config, engine, venue, now_fn=_clock(first), sleep=lambda s: None, cycles=ticks, quiet=True, cancel_on_exit=False)
    m = engine.markets[f"btc-updown-5m-{start}"]
    kinds = [p["kind"] for p in venue.sent if p["slug"] == m.slug]
    assert kinds.count("SPLIT") == 1
    assert kinds.count("MERGE") == 1 and kinds.count("REDEEM") == 1
    assert kinds.index("MERGE") < kinds.index("REDEEM")
    assert m.phase is Phase.CLOSED and m.resolved and m.winner == "Up"
    merge = [r for r in events(engine, "merge") if r["slug"] == m.slug][0]
    redeem = [r for r in events(engine, "redeem") if r["slug"] == m.slug][0]
    assert merge["offset"] == 358 and redeem["offset"] == 366
    sold_down = sum(Decimal(f["size"]) for f in events(engine, "fill") if f["slug"] == m.slug)
    assert sold_down > 0 and Decimal(merge["size"]) == Decimal("750") - sold_down
    assert Decimal(redeem["size"]) == sold_down  # the unpaired Up residual
    assert m.up == 0 and m.down == 0
    assert not engine.stale_open_markets(last)
    placed = [p for p in venue.sent if p["kind"] == "PLACE" and p["slug"] == m.slug]
    assert placed and all(Decimal(p["price"]) % Decimal("0.01") == 0 for p in placed)
    downs = {Decimal(p["price"]) for p in placed if p["outcome"] == "Down"}
    ups = {Decimal(p["price"]) for p in placed if p["outcome"] == "Up"}
    # Fake book is 0.51/0.52: Up joins 0.52, Down joins 1 - 0.51 = 0.49.
    assert ups == {Decimal("0.52")} and downs == {Decimal("0.49")}
    assert not events(engine, "risk_refuse")


class DeferredVenue(FakeVenue):
    """Chain transactions are acked at once; their outcome arrives on a later tick."""

    def __init__(self) -> None:
        super().__init__()
        self.outcomes: list[dict] = []
        self.drained = 0

    def __call__(self, payload: dict) -> str:
        self.sent.append(dict(payload))
        if payload["kind"] in {"SPLIT", "MERGE", "REDEEM"}:
            ok = payload["kind"] not in self.fail
            self.outcomes.append({"ticket": f"tx-{len(self.sent)}", "payload": dict(payload), "ok": ok,
                                  "hash": "0xabc" if ok else "", "error": "" if ok else "relayer: reverted"})
            return f"tx-{len(self.sent)}"
        return f"ext-{len(self.sent)}"

    def finished_transactions(self) -> list[dict]:
        out, self.outcomes = self.outcomes, []
        return out

    def wait_for_chain(self, timeout: float = 1.0) -> bool:
        self.drained += 1
        return True

    def health(self) -> dict:
        return {"feed": True, "feed_events": 3, "chain_pending": len(self.outcomes), "rpc": True}


def test_deferred_split_failure_rolls_back_on_a_later_tick(tmp_path, live_env):
    engine = make_engine(tmp_path, TradingMode.LIVE)
    venue = DeferredVenue()
    venue.fail.add("SPLIT")
    t = BASE + WINDOW - 212
    run_loop(engine.config, engine, venue, now_fn=_clock(t), sleep=lambda s: None, cycles=1, quiet=True, cancel_on_exit=False)
    m = engine.markets[f"btc-updown-5m-{BASE + WINDOW}"]
    # Tick 1 acked and booked the mint (split -> send) before any failure was known...
    order = [r["event"] for r in engine.decisions if r["event"] in {"split", "send", "send_fail", "halt"}]
    assert order == ["split", "send", "send_fail", "halt"]
    # ...and exit drained the worker and applied the failure before saving.
    assert venue.drained == 1
    assert m.phase is Phase.HALTED and m.halt_reason == "SPLIT_FAILED"
    assert (m.up, m.down, m.cash) == (Decimal("0"), Decimal("0"), Decimal("0"))
    fail = [r for r in events(engine, "send_fail") if r["kind"] == "SPLIT"]
    assert fail and "reverted" in fail[0]["error"]


def test_deferred_success_is_logged_and_changes_nothing(tmp_path, live_env):
    engine = make_engine(tmp_path, TradingMode.LIVE)
    venue = DeferredVenue()
    prev = BASE - WINDOW
    venue.resolutions[prev] = (True, "Up")
    m = engine.ensure(prev)
    m.phase = Phase.FLATTEN_PENDING; m.split_submitted = True; m.split_size = Decimal("750")
    m.up = Decimal("700"); m.down = Decimal("730"); m.cash = Decimal("-750") + Decimal("70") * Decimal("0.5")
    # +365: merge the 700 pairs (the oracle is in, but the merge clock is +358 and comes first).
    # +366: the Up residual is 0, so the cycle closes flat without a redeem.
    run_loop(engine.config, engine, venue, now_fn=_clock(prev + 365), sleep=lambda s: None, cycles=2, quiet=True, cancel_on_exit=False)
    assert m.phase is Phase.CLOSED and m.cash == Decimal("-750") + Decimal("35") + Decimal("700")
    assert [p["kind"] for p in venue.sent if p["slug"] == m.slug] == ["MERGE"]
    confirmed = [r for r in events(engine, "tx_confirmed") if r["slug"] == m.slug]
    assert confirmed and confirmed[0]["kind"] == "MERGE" and confirmed[0]["hash"] == "0xabc"
    health = events(engine, "health")
    assert health and health[0]["feed"] is True and "tick_ms" in health[0] and health[0]["live_orders"] == 0


def test_cancel_all_live_skips_unacked_orders(tmp_path, live_env):
    engine, venue = _armed(tmp_path)
    m = engine.ensure(BASE)
    from spork30.models import Side, WorkingOrder
    m.orders["a"] = WorkingOrder(order_id="a", outcome="Up", side=Side.SELL, price=Decimal("0.5"), size=Decimal("5"), exchange_id="")
    m.orders["b"] = WorkingOrder(order_id="b", outcome="Down", side=Side.SELL, price=Decimal("0.5"), size=Decimal("5"), exchange_id="x")
    assert cancel_all_live(engine.config, engine, venue, quiet=True) == 1
    assert venue.kinds() == ["CANCEL"] and not m.orders["a"].live and not m.orders["b"].live
