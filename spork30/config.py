"""Runtime configuration.

Trading mode defaults to simulation. Live sending is a separate, explicit gate
in spork30.live and is never implied by these defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path


class TradingMode(str, Enum):
    SIMULATION = "SIMULATION"
    LIVE = "LIVE"


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(os.environ.get(name, default))


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else int(raw)


@dataclass(frozen=True)
class StrategyConfig:
    """Active policy: regime B, retimed from the reference account's Oct 5-12 2026 tape.

    Constants are the traded-window measurements on 2,012 settled windows:
    750 shares, 293 s lead, merge every remaining pair at +358 s, redeem the
    unpaired winner at +366 s.
    """

    window_seconds: int = 300
    slug_prefix: str = "btc-updown-5m"
    split_size: Decimal = Decimal("750")
    # Median split lead Oct 5-12 (p10 291, p90 296, n=2012): the next window is
    # minted about 7 s after the current one opens. Was 212 s in September.
    split_lead_seconds: int = 293
    # Late-start floor. Below this the account almost never mints.
    min_split_lead_seconds: int = 90
    # The unpaired winner is redeemed at +366 s (median, n=1821), after the merge.
    redeem_delay_seconds: int = 366
    # Every remaining pair is merged at +358 s (median, 1,953 of 2,012 windows),
    # before the oracle is in. Was "merge at +372 only if unresolved".
    flatten_delay_seconds: int = 358
    complement_sum: Decimal = Decimal("1")
    # Fallback tick when the book does not report one. The venue uses 0.01
    # across the middle of the range and 0.001 only below 0.04 / above 0.96;
    # the live book's tick_size overrides this per quote.
    price_tick: Decimal = Decimal("0.01")
    # Venue minimum order size for these markets (orderMinSize = 5).
    min_order_size: Decimal = Decimal("5")
    # Ticks behind the touch each ask rests. 0 joins the ask (the observed
    # regime-B behaviour). Exists so the replay can price the alternative;
    # the default is unchanged.
    ask_offset_ticks: int = 0
    # Resting quotes (U1). A live ask is replaced only when the touch has
    # moved at least `requote_threshold_ticks` from it and the order is at
    # least `requote_min_seconds` old. 0 / 0 replaces on every price change.
    # The 96-window replay rejected both knobs: holding a stale ask for 10 s
    # took the mean from -5.9 to -15 per window (win% 66), a 2-tick
    # threshold was neutral. Kept at 0; see data/backtest/upgrade-fair.json.
    requote_threshold_ticks: int = 0
    requote_min_seconds: int = 0
    # Pair balance cap (U1b). The ask on an outcome is withdrawn while that
    # outcome has been sold more than `max_lean_shares` ahead of the other
    # one; it comes back once the other side catches up. The account's
    # per-window |sold Up - sold Down| has median 6 and p90 14 on 2,012
    # windows; a cap of 3 with 5-share clips reproduces that median (6-7 in
    # the replay) and cuts the worst window from -105 to -8. 0 disables.
    max_lean_shares: Decimal = Decimal("3")
    # Inside-the-spread pricing (U2). When the Up spread is at least this
    # many ticks, both asks step one tick inside the touch (pair = 1 +
    # spread - 2 ticks). The account's Up+Down prints sum to 1.000 at the
    # median, which a maker can only do inside a spread of 2+ ticks. 0 = off.
    improve_spread_ticks: int = 0
    # Spot-referenced fair value (U6). When `fair_edge` > 0 and a BTC spot
    # price and the window's price-to-beat are available, each ask is priced
    # at fair + edge (fair = P(Up) from the spot distance to the beat, the
    # time left and realised volatility) and never below the touch's maker
    # side. The Polymarket touch lags spot by seconds; a taker who lifts an
    # ask at spot-fair is not acting on information the quote lacked. 0 = off
    # (price off the Polymarket touch only, as before).
    #
    # Replay, 96 windows Oct 11-12, lean cap 3, mean PnL per window before
    # rebate: touch-only quoting -21.9 (t -5.0); fair edge 0.03 with 1 s of
    # spot latency +0.71 (t +2.0, worst -8.4, 101 sh/w, win% 53, lean 6),
    # with 2 s of latency -0.31. The sign depends on quoting within about a
    # second of spot; the rebate estimate (+0.2/window) does not change it.
    # With the fair value on and no fresh spot the engine rests nothing.
    fair_edge: Decimal = Decimal("0.03")
    # Allow the fair ask to undercut the current best ask (down to one tick
    # above the bid) when fair + edge is below it. Off: join the touch.
    # Undercutting was 0.3-3 per window worse in the replay.
    fair_undercut: bool = False
    # Seconds of spot the quote is assumed to lag (the replay uses the spot
    # print this many seconds old; live, the feed's own latency applies).
    spot_lag_seconds: int = 1
    # Fallback annualised volatility when realised vol cannot be measured.
    fair_vol_annual: Decimal = Decimal("0.45")
    # With the fair value on and spot momentarily missing (no print inside
    # MAX_SPOT_AGE_SECONDS), resting asks are held this many seconds before
    # they are pulled; no new ask is placed meanwhile. Cancelling on the first
    # stale tick re-posted at the back of the queue 42 times per window in the
    # Oct 15 simulation (2,390 `no_spot_reference` pulls over 57 windows). 0 =
    # pull at once, the old behaviour.
    spot_stale_grace_seconds: int = 2
    # --- Audit hypotheses (Oct 2026), each off at 0. See docs/audit-2026-10.md
    # for the train/test evidence behind the defaults chosen here.
    #
    # H1 Withhold an ask that would rest this many ticks or more above its
    # outcome's touch. With the fair value above the touch the engine rests
    # at fair + edge, behind the market; the only takers who reach it are
    # sweeping through the touch, and those fills sold the winner 75-84% of
    # the time in the replay. 1 = only ever join the touch. 0 = off.
    fair_gap_withhold_ticks: int = 0
    # H2 Pull both asks for the final N seconds of the window. 0 = off.
    quote_pause_final_seconds: int = 0
    # H3 Edge scales with the volatility of the time left:
    # edge = max(fair_edge, mult * sigma * sqrt(seconds_left)). 0 = off.
    fair_edge_vol_mult: Decimal = Decimal("0")
    # H4 After a fill on one outcome, withhold that outcome's ask for N
    # seconds so one sweep cannot take several clips in a row. 0 = off.
    fill_cooldown_seconds: int = 0
    # --- Reference-gap knobs (Oct 16 2026), each off by default. On 203
    # simulated cycles 104 of 131 traded windows sold one side only; matched
    # pairs made +1.32 and the unmatched lean -31.80. The reference account
    # had 24 one-sided windows in 2,811. See docs/reference-gap-2026-10.md.
    #
    # Clamp each clip so a fill can never take the lean past
    # `max_lean_shares`. With clips of 5-20 and a cap of 3 the first fill
    # already overshot the cap. Needs max_lean_shares >= min_order_size.
    lean_size_clamp: bool = False
    # While one outcome has been sold ahead, the lagging outcome joins its
    # touch instead of resting at fair + edge, so the pair completes.
    rebalance_at_touch: bool = False
    # "fair" prices both asks at fair + edge (U6). "touch" joins the
    # Polymarket touch on both sides, the way the reference account sells;
    # the fair value is still computed when spot is available, for the guard.
    quote_mode: str = "fair"
    # Touch mode only: withhold an outcome's ask while the spot fair for that
    # outcome is this many ticks or more above the ask (selling it far under
    # fair). The reference made money selling 3+ ticks under fair, so 0 = off.
    fair_guard_ticks: int = 0
    # Keep a replaced ask's clip size instead of drawing a new one each time
    # the price moves.
    sticky_clip: bool = False
    # Do not sell an outcome below this price. On the reference tape asks
    # under 0.40 settled at -0.017/share and above 0.60 at +0.009 to +0.020.
    # 0 = off.
    underdog_min_price: Decimal = Decimal("0")
    # Maker rebate as measured on the account's seven daily MAKER_REBATE
    # payments: 41-43 bps of sold notional (64 on one day). Used for the
    # estimate on the screen and in the replay; the bot never sees the
    # actual payment.
    maker_rebate_bps: Decimal = Decimal("42")
    # On-grid clustered fill sizes, with Oct 5-12 fill counts
    # (10 42%, 5 33%, 15 23%, 20 2%). Fill sizes understate order sizes
    # because of partials, so this is a prior.
    clip_sizes: tuple[int, ...] = (5, 10, 15, 20)
    clip_weights: tuple[int, ...] = (8892, 11348, 6151, 581)
    dust_shares: Decimal = Decimal("1")


@dataclass(frozen=True)
class RiskConfig:
    """Safety bounds around the reconstructed policy.

    These are not inferred strategy rules. They exist so a bug cannot scale
    the policy without a human change. Limits sit outside observed behavior:
    one 750 split, clips of at most 20, about two to three markets open.
    """

    max_position_per_outcome: Decimal = Decimal("800")
    max_order_size: Decimal = Decimal("20")
    max_daily_loss: Decimal = Decimal("1000")
    max_total_exposure: Decimal = Decimal("3000")
    max_open_markets: int = 8
    max_orders_per_10s: int = 40
    max_retry_count: int = 3
    stale_data_timeout_seconds: int = 5
    # Halt if a reference price jumps by more than this in one update.
    max_price_jump: Decimal = Decimal("0.50")
    # On a jump larger than `max_price_jump`, pull the quotes for this many
    # seconds and then resume, instead of halting the market for the rest of
    # the window (26 of 203 simulated windows). 0 = halt, the old behaviour.
    jump_pause_seconds: int = 0
    # With the pause on, the market still halts on this many jumps in one window.
    max_jumps_per_window: int = 3
    min_price: Decimal = Decimal("0.001")
    max_price: Decimal = Decimal("0.999")


@dataclass(frozen=True)
class AppConfig:
    trading_mode: TradingMode
    strategy: StrategyConfig
    risk: RiskConfig
    state_path: str
    decision_log_path: str
    emergency_stop_path: str
    funder: str = ""
    signature_type: int = 3
    polygon_rpc: str = ""

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode is TradingMode.LIVE


def load_env_file(path: Path | None = None) -> None:
    """Load spork30/.env without overriding variables already set in the process."""
    env_path = path or Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> AppConfig:
    load_env_file()
    raw_mode = os.environ.get("TRADING_MODE", "SIMULATION").strip().upper()
    try:
        mode = TradingMode(raw_mode)
    except ValueError as exc:
        raise SystemExit(f"TRADING_MODE must be SIMULATION or LIVE, got {raw_mode!r}") from exc
    base = os.environ.get("BOT_DATA_DIR", "data/runtime")
    # Simulation writes local mints and demo fills. Live keeps a separate log
    # so those lines are not shown as exchange trades.
    root = os.path.join(base, "live" if mode is TradingMode.LIVE else "sim")
    defaults = StrategyConfig()
    quote_mode = os.environ.get("QUOTE_MODE", defaults.quote_mode).strip().lower()
    if quote_mode not in {"fair", "touch"}:
        raise SystemExit(f"QUOTE_MODE must be fair or touch, got {quote_mode!r}")
    lean_clamp = _flag("LEAN_SIZE_CLAMP", defaults.lean_size_clamp)
    lean_cap = _decimal("MAX_LEAN_SHARES", str(defaults.max_lean_shares))
    if lean_clamp and lean_cap < defaults.min_order_size:
        raise SystemExit(f"LEAN_SIZE_CLAMP needs MAX_LEAN_SHARES >= {defaults.min_order_size}, got {lean_cap}; "
                         "a smaller cap leaves no clip that fits")
    return AppConfig(
        trading_mode=mode,
        strategy=StrategyConfig(
            split_size=_decimal("SPLIT_SIZE", "750"),
            split_lead_seconds=_int("SPLIT_LEAD_SECONDS", defaults.split_lead_seconds),
            min_split_lead_seconds=_int("MIN_SPLIT_LEAD_SECONDS", defaults.min_split_lead_seconds),
            redeem_delay_seconds=_int("REDEEM_DELAY_SECONDS", defaults.redeem_delay_seconds),
            flatten_delay_seconds=_int("FLATTEN_DELAY_SECONDS", defaults.flatten_delay_seconds),
            ask_offset_ticks=_int("ASK_OFFSET_TICKS", defaults.ask_offset_ticks),
            requote_threshold_ticks=_int("REQUOTE_THRESHOLD_TICKS", defaults.requote_threshold_ticks),
            requote_min_seconds=_int("REQUOTE_MIN_SECONDS", defaults.requote_min_seconds),
            max_lean_shares=lean_cap,
            improve_spread_ticks=_int("IMPROVE_SPREAD_TICKS", defaults.improve_spread_ticks),
            fair_edge=_decimal("FAIR_EDGE", str(defaults.fair_edge)),
            fair_undercut=os.environ.get("FAIR_UNDERCUT", "1" if defaults.fair_undercut else "0").strip() in {"1", "true", "yes", "on"},
            spot_lag_seconds=_int("SPOT_LAG_SECONDS", defaults.spot_lag_seconds),
            fair_vol_annual=_decimal("FAIR_VOL_ANNUAL", str(defaults.fair_vol_annual)),
            spot_stale_grace_seconds=_int("SPOT_STALE_GRACE_SECONDS", defaults.spot_stale_grace_seconds),
            maker_rebate_bps=_decimal("MAKER_REBATE_BPS", str(defaults.maker_rebate_bps)),
            fair_gap_withhold_ticks=_int("FAIR_GAP_WITHHOLD_TICKS", defaults.fair_gap_withhold_ticks),
            quote_pause_final_seconds=_int("QUOTE_PAUSE_FINAL_SECONDS", defaults.quote_pause_final_seconds),
            fair_edge_vol_mult=_decimal("FAIR_EDGE_VOL_MULT", str(defaults.fair_edge_vol_mult)),
            fill_cooldown_seconds=_int("FILL_COOLDOWN_SECONDS", defaults.fill_cooldown_seconds),
            lean_size_clamp=lean_clamp,
            rebalance_at_touch=_flag("REBALANCE_AT_TOUCH", defaults.rebalance_at_touch),
            quote_mode=quote_mode,
            fair_guard_ticks=_int("FAIR_GUARD_TICKS", defaults.fair_guard_ticks),
            sticky_clip=_flag("STICKY_CLIP", defaults.sticky_clip),
            underdog_min_price=_decimal("UNDERDOG_MIN_PRICE", str(defaults.underdog_min_price)),
        ),
        risk=RiskConfig(
            max_position_per_outcome=_decimal("MAX_POSITION", "800"),
            max_order_size=_decimal("MAX_ORDER_SIZE", "20"),
            max_daily_loss=_decimal("MAX_DAILY_LOSS", "1000"),
            max_total_exposure=_decimal("MAX_TOTAL_EXPOSURE", "3000"),
            max_open_markets=_int("MAX_OPEN_MARKETS", 8),
            max_orders_per_10s=_int("MAX_ORDER_RATE", 40),
            max_retry_count=_int("MAX_RETRY_COUNT", 3),
            stale_data_timeout_seconds=_int("STALE_DATA_TIMEOUT", 5),
            jump_pause_seconds=_int("JUMP_PAUSE_SECONDS", RiskConfig.jump_pause_seconds),
            max_jumps_per_window=_int("MAX_JUMPS_PER_WINDOW", RiskConfig.max_jumps_per_window),
        ),
        state_path=os.path.join(root, "state.json"),
        decision_log_path=os.path.join(root, "decisions.jsonl"),
        emergency_stop_path=os.environ.get("EMERGENCY_STOP_FILE", os.path.join(base, "EMERGENCY_STOP")),
        funder=os.environ.get("POLYMARKET_FUNDER", "").strip(),
        signature_type=_int("POLYMARKET_SIGNATURE_TYPE", 3),
        polygon_rpc=os.environ.get("POLYGON_RPC", "").strip(),
    )


def reset_runtime(config: AppConfig) -> None:
    """Delete this mode's decision log and engine state. The emergency stop file stays."""
    for path in (config.decision_log_path, config.state_path):
        file = Path(path)
        if file.is_file():
            file.unlink()
