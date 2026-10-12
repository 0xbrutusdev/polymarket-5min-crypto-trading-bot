"""Shared fixtures. Configs are built directly so no test reads spork30/.env."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from spork30.config import AppConfig, RiskConfig, StrategyConfig, TradingMode
from spork30.execution import ClipSchedule
from spork30.models import BookTop, window_start_for
from spork30.portfolio import Engine

WINDOW = 300
# A 300-aligned window start well away from zero.
BASE = 1_790_144_100


# The engine-mechanics tests drive the engine with a book only (no spot), the
# way the bot quoted before U6: off the touch, no pair cap. Tests of the fair
# value and the cap pass their own `strategy`.
TOUCH_QUOTING = StrategyConfig(fair_edge=Decimal("0"), max_lean_shares=Decimal("0"))


def make_config(
    tmp_path: Path, mode: TradingMode = TradingMode.SIMULATION, strategy: StrategyConfig | None = None, **risk
) -> AppConfig:
    root = tmp_path / ("live" if mode is TradingMode.LIVE else "sim")
    return AppConfig(
        trading_mode=mode,
        strategy=strategy or TOUCH_QUOTING,
        risk=RiskConfig(**risk),
        state_path=str(root / "state.json"),
        decision_log_path=str(root / "decisions.jsonl"),
        emergency_stop_path=str(tmp_path / "EMERGENCY_STOP"),
    )


def make_engine(
    tmp_path: Path, mode: TradingMode = TradingMode.SIMULATION, strategy: StrategyConfig | None = None, **risk
) -> Engine:
    config = make_config(tmp_path, mode, strategy, **risk)
    return Engine(config=config, clips=ClipSchedule(config.strategy, seed=1))


def book_at(now: int, ask: str = "0.52") -> BookTop:
    a = Decimal(ask)
    return BookTop(bid=a - Decimal("0.01"), ask=a, observed_ts=now)


def events(engine: Engine, kind: str) -> list[dict]:
    return [r for r in engine.decisions if r.get("event") == kind]


class FakeVenue:
    """Records every payload. Fails the kinds listed in `fail`."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail: set[str] = set()
        self.resolutions: dict[int, tuple[bool, str | None]] = {}
        self.ask = "0.52"
        self.sync_raises: int = 0

    def __call__(self, payload: dict) -> str:
        self.sent.append(dict(payload))
        if payload["kind"] in self.fail:
            raise RuntimeError(f"venue refused {payload['kind']}")
        return f"ext-{len(self.sent)}"

    def book(self, now: int) -> BookTop | None:
        return book_at(now, self.ask)

    def resolution(self, now: int) -> tuple[bool, str | None]:
        return self.resolution_for(window_start_for(now) - WINDOW)

    def resolution_for(self, start: int) -> tuple[bool, str | None]:
        return self.resolutions.get(start, (False, None))

    def sync_fills(self, engine: Engine, now: int) -> None:
        if self.sync_raises > 0:
            self.sync_raises -= 1
            raise RuntimeError("exchange 502")

    def kinds(self) -> list[str]:
        return [p["kind"] for p in self.sent]


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "YES")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0x" + "ab" * 32)
    yield


@pytest.fixture
def sim_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_MODE", "SIMULATION")
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    yield
