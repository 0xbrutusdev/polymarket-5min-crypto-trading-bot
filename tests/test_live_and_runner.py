from decimal import Decimal

import pytest

from spork30.config import StrategyConfig
from spork30.live import LiveTradingDisabled, _redact, assert_live_allowed, submit
from spork30.models import MarketPosition, Side
from spork30.risk import RiskRefusal, RiskState, check_order
from spork30.strategy import complement_asks

from conftest import make_config


def test_sell_at_or_below_bid_is_refused(tmp_path):
    config = make_config(tmp_path)
    position = MarketPosition(slug="x", start=300, up=Decimal("750"), down=Decimal("750"))
    common = dict(now=1, position=position, outcome="Down", side=Side.SELL, size=Decimal("5"), open_markets=1,
                  gross_exposure=Decimal("750"), risk=config.risk, state=RiskState(), reference=Decimal("0.59"))
    with pytest.raises(RiskRefusal, match="WOULD_CROSS"):
        check_order(price=Decimal("0.59"), bid=Decimal("0.59"), **common)
    with pytest.raises(RiskRefusal, match="WOULD_CROSS"):
        check_order(price=Decimal("0.58"), bid=Decimal("0.59"), **common)
    assert "MAKER_OK" in check_order(price=Decimal("0.60"), bid=Decimal("0.59"), **common)
    assert "MAKER_OK" in check_order(price=Decimal("0.59"), bid=None, **common)


def test_redact_covers_every_secret(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("POLYMARKET_BUILDER_SECRET", "s3cr3t-value-xyz")
    monkeypatch.setenv("POLYMARKET_BUILDER_PASSPHRASE", "passphrase-value-123")
    msg = f"auth failed key=0x{'ab' * 32} secret=s3cr3t-value-xyz pass=passphrase-value-123"
    out = _redact(msg)
    assert "ab" * 32 not in out and "s3cr3t" not in out and "passphrase-value" not in out
    assert "redacted" in out


def test_redact_ignores_short_or_unset_values(monkeypatch):
    monkeypatch.setenv("POLYMARKET_BUILDER_SECRET", "ab")
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    assert _redact("abc abc") == "abc abc"


def test_live_gates(tmp_path, monkeypatch):
    from spork30.config import TradingMode
    config = make_config(tmp_path, TradingMode.LIVE)
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    with pytest.raises(LiveTradingDisabled):
        assert_live_allowed(config)
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "YES")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    assert_live_allowed(config)
    with pytest.raises(LiveTradingDisabled, match="no signer"):
        submit(config, {"kind": "PLACE", "slug": "x"}, None)
    sim = make_config(tmp_path)
    with pytest.raises(LiveTradingDisabled):
        submit(sim, {"kind": "PLACE", "slug": "x"}, lambda p: "id")


def test_simulation_window_runs_end_to_end(sim_env):
    from spork30.runner import simulate_window
    lines = simulate_window()
    kinds = [line.split()[1] for line in lines]
    # Nothing fills in the runner, so every pair merges at +358 and there is no residual to redeem.
    assert "SPLIT" in kinds and "MERGE" in kinds and "REDEEM" not in kinds
    assert "HALT" not in kinds


def test_runner_refuses_live(sim_env, monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    from spork30.runner import simulate_window
    with pytest.raises(SystemExit):
        simulate_window()


def test_down_ask_rests_above_the_down_bid_on_live_book():
    """Live book 2026-10-11: Up 0.40/0.41, Down 0.59/0.60. The Down ask must
    join the Down ask (0.60), not sit on the Down bid (0.59)."""
    strategy = StrategyConfig()
    up, down = complement_asks(Decimal("0.41"), strategy, up_bid=Decimal("0.40"), tick=Decimal("0.01"))
    assert (up, down) == (Decimal("0.41"), Decimal("0.60"))


def test_down_ask_without_bid_rests_one_tick_above_down_bid():
    strategy = StrategyConfig()
    up, down = complement_asks(Decimal("0.41"), strategy, tick=Decimal("0.01"))
    assert (up, down) == (Decimal("0.41"), Decimal("0.60"))


def test_wide_spread_joins_far_side():
    strategy = StrategyConfig()
    up, down = complement_asks(Decimal("0.45"), strategy, up_bid=Decimal("0.40"), tick=Decimal("0.01"))
    assert (up, down) == (Decimal("0.45"), Decimal("0.60"))
    assert down > Decimal("1") - up


def test_book_tick_overrides_config_tick():
    strategy = StrategyConfig()
    up, down = complement_asks(Decimal("0.0245"), strategy, up_bid=Decimal("0.023"), tick=Decimal("0.001"))
    assert (up, down) == (Decimal("0.025"), Decimal("0.977"))
    up2, _ = complement_asks(Decimal("0.4249"), strategy)  # config default 0.01
    assert up2 == Decimal("0.42")


def test_pair_sum_is_one_plus_spread():
    strategy = StrategyConfig()
    up, down = complement_asks(Decimal("0.52"), strategy, up_bid=Decimal("0.51"), tick=Decimal("0.01"))
    assert up + down == Decimal("1.01")


def test_ask_offset_rests_behind_the_touch_and_defaults_to_zero():
    assert StrategyConfig().ask_offset_ticks == 0
    behind = StrategyConfig(ask_offset_ticks=2)
    up, down = complement_asks(Decimal("0.52"), behind, up_bid=Decimal("0.51"), tick=Decimal("0.01"))
    assert (up, down) == (Decimal("0.54"), Decimal("0.51"))
    assert up + down == Decimal("1.05")  # 1 + spread + 2 ticks each side
    # Clamped at the top of the band.
    up, down = complement_asks(Decimal("0.99"), behind, up_bid=Decimal("0.98"), tick=Decimal("0.01"))
    assert up == Decimal("0.99") and down == Decimal("0.04")
