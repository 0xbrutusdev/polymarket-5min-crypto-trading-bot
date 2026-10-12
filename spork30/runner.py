"""One-window simulation entrypoint. Never sends a live order."""

from __future__ import annotations

from decimal import Decimal

from spork30.config import load_config
from spork30.execution import ClipSchedule
from spork30.models import BookTop
from spork30.portfolio import Engine


def simulate_window(start: int = 1_790_144_100) -> list[str]:
    config = load_config()
    if config.live_enabled:
        raise SystemExit("runner refuses to start when TRADING_MODE=LIVE; use the live module explicitly")
    engine = Engine(config=config, clips=ClipSchedule(config.strategy, seed=30))
    events: list[str] = []
    # Walk from just before the split through the flatten second.
    begin = start - config.strategy.split_lead_seconds - 2
    end = start + max(config.strategy.redeem_delay_seconds, config.strategy.flatten_delay_seconds) + 1
    # Synthetic spot for the fair value: drifts up 0.5 USD/s from a 100,000 beat.
    beat = Decimal("100000")
    for now in range(begin, end):
        book = None
        resolved = False
        winner = None
        spot = None
        if start <= now < start + config.strategy.window_seconds:
            # A drifting Up ask. Down is priced as the complement by the policy.
            seconds = now - start
            ask = Decimal("0.50") + Decimal(seconds) / Decimal("1000")
            book = BookTop(bid=ask - Decimal("0.01"), ask=ask, observed_ts=now)
            spot = beat + Decimal(seconds) / Decimal("2")
        if now >= start + config.strategy.window_seconds + 60:
            resolved = True
            winner = "Up"
        actions = engine.step(
            now=now,
            book=book,
            last_up_trade=None,
            resolved=resolved,
            winner=winner,
            emergency_stop=False,
            spot=spot,
            beat=beat,
        )
        for action in actions:
            if action.kind in {"PLACE", "QUOTES_PULLED", "RISK_REFUSE"}:
                continue
            events.append(f"{now - start:5d} {action.kind} {action.detail.get('rule') or action.detail.get('method') or action.detail.get('reason', '')}")
    engine.save()
    return events


def main() -> None:
    for line in simulate_window():
        print(line)


if __name__ == "__main__":
    main()
