"""The pane screen renders from the decision log alone. No engine, no venue."""

from decimal import Decimal

from spork30.activity import Book
from spork30.screen import build, snapshot_text

from conftest import BASE


def _records(slug: str, start: int) -> list[dict]:
    return [
        {"event": "split", "slug": slug, "ts": start - 212, "size": "750", "up": "750", "down": "750", "lead_seconds": 212},
        {"event": "place", "slug": slug, "ts": start + 1, "order_id": "o-up", "outcome": "Up", "side": "SELL",
         "price": "0.52", "size": "10", "reference_price": "0.52"},
        {"event": "place", "slug": slug, "ts": start + 1, "order_id": "o-dn", "outcome": "Down", "side": "SELL",
         "price": "0.49", "size": "15", "reference_price": "0.52"},
        {"event": "fill", "slug": slug, "ts": start + 30, "order_id": "o-up", "outcome": "Up", "price": "0.52",
         "size": "10", "remaining_order": "0", "inventory": "740"},
        {"event": "risk_refuse", "slug": slug, "ts": start + 31, "outcome": "Down", "reason": "WOULD_CROSS",
         "price": "0.48", "size": "5"},
        {"event": "health", "slug": "", "ts": start + 35, "tick_ms": 41, "live_orders": 1, "feed": True,
         "feed_events": 7, "chain_pending": 1, "rpc": True},
        {"event": "tx_confirmed", "slug": slug, "ts": start + 36, "kind": "SPLIT", "ticket": "tx-1",
         "hash": "0x" + "ab" * 32},
    ]


def test_live_screen_shows_quotes_orders_risk_and_health():
    slug = f"btc-updown-5m-{BASE}"
    book = Book(require_ack=False)
    for record in _records(slug, BASE):
        book.apply(record)
    layout = build(
        book, mode="LIVE", status="confirm yes  key set", log_path="x/decisions.jsonl", emergency=False,
        btc=Decimal("68000.5"), beat=Decimal("67990"), clock=BASE + 40, sends=True,
        limits={"max_daily_loss": Decimal("1000"), "max_total_exposure": Decimal("3000"),
                "maker_rebate_bps": Decimal("42")},
    )
    text = snapshot_text(layout)
    assert "LIVE" in text and "runs the order loop" in text
    # 10 sold at 0.52 = 5.20 notional; 42 bps of that is 0.02, shown as an estimate, never booked.
    assert "rebate est" in text and "42 bps of 5.20 sold, not booked" in text
    assert book.sold_notional() == Decimal("5.20")
    assert "ask Up 0.52" in text and "Down 0.49" in text and "pair 1.01" in text
    assert "o-dn" not in text and "Down" in text and "39s" in text  # resting Down ask aged 39 s
    assert "sold" in text and "10 @ 0.520" in text
    assert "websocket connected, 7 events" in text and "1 pending" in text and "41 ms" in text
    assert "exposure" in text and "750.00 of 3000.00" in text
    assert "refusals" in text and "WOULD_CROSS" in text
    assert "split landed 0xabababab" in text


def test_simulation_screen_without_events_is_calm():
    book = Book()
    text = snapshot_text(build(book, mode="SIMULATION", status="sim", log_path="p", emergency=False, clock=BASE + 5))
    assert "SIMULATION" in text and "no minted window" in text and "none resting" in text
    assert "no events yet" in text and "real taker prints" in text and "modelled queue" in text


def test_emergency_and_halt_are_loud():
    slug = f"btc-updown-5m-{BASE}"
    book = Book()
    book.apply({"event": "split", "slug": slug, "ts": BASE - 212, "size": "750", "up": "750", "down": "750"})
    book.apply({"event": "halt", "slug": slug, "ts": BASE + 9, "reason": "MAX_PRICE_DEVIATION"})
    book.apply({"event": "unwind", "slug": slug, "ts": BASE + 372, "halt_reason": "MAX_PRICE_DEVIATION", "up": "750", "down": "750"})
    text = snapshot_text(build(book, mode="LIVE", status="s", log_path="p", emergency=True, clock=BASE + 373))
    assert "EMERGENCY STOP" in text and "HALTED" in text and "MAX_PRICE_DEVIATION" in text
    assert "unwinding" in text
