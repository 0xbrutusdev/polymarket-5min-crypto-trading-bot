"""Replay fill model against synthetic prints. No network."""

from decimal import Decimal

from spork30.backtest import Print, ReconstructedBook, WindowData, replay_config, replay_window as _replay
from spork30.config import StrategyConfig

from conftest import BASE

# The fill-model tests quote off the touch with no pair cap, the pre-U6
# behaviour; the fair-value tests below build their own windows with spot.
TOUCH = replay_config(StrategyConfig(fair_edge=Decimal("0"), max_lean_shares=Decimal("0")))


def replay_window(data, **kw):
    kw.setdefault("config", TOUCH)
    return _replay(data, **kw)


def _window(prints: list[Print], winner: str) -> WindowData:
    return WindowData(start=BASE, slug=f"btc-updown-5m-{BASE}", condition="0x" + "11" * 32, winner=winner, prints=prints)


def _flat_book() -> list[Print]:
    # Pre-window prints pin the Up book at 0.49 / 0.50 (Down 0.50 / 0.51).
    return [
        Print(BASE - 1000, "Up", "BUY", Decimal("0.50"), Decimal("10")),
        Print(BASE - 999, "Up", "SELL", Decimal("0.49"), Decimal("10")),
    ]


def test_book_is_reconstructed_from_both_tokens():
    book = ReconstructedBook()
    assert book.top(0) is None
    book.update(Print(1, "Down", "BUY", Decimal("0.52"), Decimal("1")))  # Down ask 0.52 -> Up bid 0.48
    assert book.top(1) is None  # no ask yet
    book.update(Print(2, "Down", "SELL", Decimal("0.51"), Decimal("1")))  # Down bid 0.51 -> Up ask 0.49
    top = book.top(2)
    assert (top.bid, top.ask, top.tick) == (Decimal("0.48"), Decimal("0.49"), Decimal("0.01"))
    # A taker SELL printing at 0.60 means someone bid 0.60, so the 0.49 ask is stale: it moves up.
    book.update(Print(3, "Up", "SELL", Decimal("0.60"), Decimal("1")))
    top = book.top(3)
    assert top.bid == Decimal("0.60") and top.ask == Decimal("0.61")
    # And a taker BUY printing at 0.55 means the ask is 0.55, so the 0.60 bid is stale: it moves down.
    book.update(Print(4, "Up", "BUY", Decimal("0.55"), Decimal("1")))
    top = book.top(4)
    assert top.bid == Decimal("0.54") and top.ask == Decimal("0.55")


def test_sold_loser_earns_and_sold_winner_loses():
    prints = _flat_book() + [Print(BASE + 10, "Up", "BUY", Decimal("0.50"), Decimal("20"))]
    down_wins = replay_window(_window(prints, "Down"), queue=Decimal("0"), seed=1)
    up_wins = replay_window(_window(prints, "Up"), queue=Decimal("0"), seed=1)
    assert down_wins.fills == 1 and down_wins.sold_up > 0 and down_wins.sold_down == 0
    # Sold x Up at 0.50: merge 750-x pairs, redeem x Down -> +0.5x; or Up wins -> -0.5x.
    x = down_wins.sold_up
    assert down_wins.pnl == x * Decimal("0.5") and up_wins.pnl == -x * Decimal("0.5")
    assert down_wins.phase == "CLOSED" and up_wins.phase == "CLOSED"
    assert down_wins.sold_winner == 0 and up_wins.sold_winner == x


def test_queue_ahead_must_trade_through_before_we_fill():
    prints = _flat_book() + [
        Print(BASE + 10, "Up", "BUY", Decimal("0.50"), Decimal("30")),
        Print(BASE + 11, "Up", "BUY", Decimal("0.50"), Decimal("75")),
    ]
    none = replay_window(_window(prints, "Down"), queue=Decimal("200"), seed=1)
    some = replay_window(_window(prints, "Down"), queue=Decimal("100"), seed=1)
    assert none.fills == 0 and none.pnl == 0
    assert some.fills == 1 and some.sold_up == Decimal("5")  # 105 traded, 100 ahead, clip-limited


def test_prints_below_our_ask_or_on_the_bid_side_do_not_fill():
    prints = _flat_book() + [
        Print(BASE + 10, "Up", "BUY", Decimal("0.49"), Decimal("500")),  # below our 0.50 ask
        Print(BASE + 11, "Up", "SELL", Decimal("0.49"), Decimal("500")),  # taker sells hit bids, not asks
    ]
    result = replay_window(_window(prints, "Down"), queue=Decimal("0"), seed=1)
    assert result.fills == 0 and result.pnl == 0


def _spot(history: str, during: str | None = None) -> list[tuple[int, Decimal]]:
    # 300 s of flat history before the window (that is the beat), then the window at `during`.
    before, after = Decimal(history), Decimal(during or history)
    return [(ts, before if ts < BASE else after) for ts in range(BASE - 300, BASE + 300)]


def _fair_window(prints: list[Print], winner: str, spot: list[tuple[int, Decimal]]) -> WindowData:
    data = _window(prints, winner)
    data.spot = spot
    data.beat = Decimal("100011")  # Polymarket's beat, 11 bps off: must not be the one used
    return data


FAIR = replay_config(StrategyConfig(fair_edge=Decimal("0.03"), max_lean_shares=Decimal("0")))


def test_window_data_round_trips_spot_and_beat_and_measures_its_own_beat():
    data = _fair_window(_flat_book(), "Up", _spot("100000"))
    back = WindowData.from_json(data.to_json())
    assert back.spot == data.spot and back.beat == Decimal("100011")
    assert back.spot_beat() == Decimal("100000")  # TWAP of the series, not Polymarket's number
    assert back.realised_sigma() == 0.0  # flat series
    legacy = WindowData.from_json({"start": BASE, "slug": "s", "condition": "c", "winner": "Up", "prints": []})
    assert legacy.spot == [] and legacy.beat is None and legacy.spot_beat() is None


def test_fair_value_rests_above_the_touch_when_spot_sits_on_the_beat():
    # Spot == beat -> P(Up) = 0.5 -> both asks at 0.53, above the 0.50 / 0.51 touches.
    prints = _flat_book() + [
        Print(BASE + 10, "Up", "BUY", Decimal("0.50"), Decimal("50")),  # at the touch: below our ask
        Print(BASE + 20, "Up", "BUY", Decimal("0.53"), Decimal("50")),  # reaches us
    ]
    result = _replay(_fair_window(prints, "Down", _spot("100000")), queue=Decimal("0"), seed=1, config=FAIR)
    assert result.used_fair and result.fills == 1 and result.sold_up > 0
    assert result.proceeds == result.sold_up * Decimal("0.53")


def test_fair_value_sells_only_the_side_spot_has_made_cheap():
    # Spot 0.5% above the beat with 1-s vol ~0 -> P(Up) ~ 1: the Up ask goes to
    # the top of the grid and the Down ask drops to its own touch (0.51).
    prints = _flat_book() + [
        Print(BASE + 10, "Up", "BUY", Decimal("0.60"), Decimal("50")),
        Print(BASE + 11, "Down", "BUY", Decimal("0.51"), Decimal("50")),
    ]
    result = _replay(_fair_window(prints, "Up", _spot("100000", "100500")), queue=Decimal("0"), seed=1, config=FAIR)
    assert result.sold_up == 0 and result.sold_down > 0


def test_fair_value_on_without_spot_rests_nothing():
    prints = _flat_book() + [Print(BASE + 10, "Up", "BUY", Decimal("0.50"), Decimal("20"))]
    result = _replay(_window(prints, "Down"), queue=Decimal("0"), seed=1, config=FAIR)
    assert result is not None and result.places == 0 and result.fills == 0 and not result.used_fair
    assert result.pnl == 0 and result.phase == "CLOSED"


def test_touch_only_queue_spares_an_ask_resting_above_the_touch():
    prints = _flat_book() + [Print(BASE + 20, "Up", "BUY", Decimal("0.53"), Decimal("20"))]
    data = _fair_window(prints, "Down", _spot("100000"))
    uniform = _replay(data, queue=Decimal("100"), seed=1, config=FAIR)
    touch_only = _replay(data, queue=Decimal("100"), seed=1, config=FAIR, queue_at_touch_only=True)
    assert uniform.fills == 0 and touch_only.fills == 1


def test_unresolved_window_is_skipped_and_fee_is_applied():
    prints = _flat_book() + [Print(BASE + 10, "Down", "BUY", Decimal("0.51"), Decimal("20"))]
    assert replay_window(_window(prints, None), queue=Decimal("0")) is None
    gross = replay_window(_window(prints, "Up"), queue=Decimal("0"), seed=1)
    net = replay_window(_window(prints, "Up"), queue=Decimal("0"), seed=1, maker_fee_bps=Decimal("100"))
    assert gross.sold_down > 0 and net.pnl == gross.pnl - gross.proceeds * Decimal("0.01")
