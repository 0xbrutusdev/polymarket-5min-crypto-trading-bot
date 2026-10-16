"""Fill-level diagnostics for the replay: markouts, regimes, expectancy.

    python -m spork30.diagnostics --queue 250 --lag 2
    python -m spork30.diagnostics --queue 100 --touch-only --json out.json

Every number here is computed from `WindowResult.records` (one row per
modelled fill, with the fair value and touch the ask was priced from) and the
window's spot path. Markouts are moves in the spot-implied fair value of the
sold outcome after the fill: positive means the market moved against the
sale, which is what adverse selection looks like. Settlement markout is the
per-share PnL of the sale, 1[outcome won] subtracted from the price.

Observations within an hour are correlated (same BTC path, same book), so
the expectancy interval is a block bootstrap over contiguous hours, not a
per-window standard error.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from spork30.backtest import WindowData, WindowResult, replay_config, replay_window
from spork30.config import StrategyConfig
from spork30.strategy import fair_up

HORIZONS = (10, 30, 60)
WINDOW_SECONDS = 300
BLOCK_WINDOWS = 12  # one hour of 5-minute windows


@dataclass
class FillRow:
    start: int
    winner: str
    ts: int
    outcome: str
    price: float
    size: float
    fair_outcome: float | None  # P(outcome) the engine held when the print hit
    fair_at_place: float | None  # P(outcome) the ask was priced from
    touch: float | None
    edge_at_place: float | None  # price - fair_at_place: the edge we asked for
    edge_at_fill: float | None  # price - fair_outcome: the edge left when hit
    gap_ticks: float | None  # (fair_at_place - touch) / 0.01: how far spot had left the touch
    seconds_into_window: int
    seconds_left: int
    queue_ahead_at_place: float
    sigma: float
    markout_10: float | None
    markout_30: float | None
    markout_60: float | None
    settlement: float  # price - 1[outcome won], per share
    won: bool


def _side(outcome: str, fair_up_value: Decimal | None) -> float | None:
    if fair_up_value is None:
        return None
    return float(fair_up_value) if outcome == "Up" else 1.0 - float(fair_up_value)


def _fair_outcome(outcome: str, spot: Decimal, beat: Decimal, seconds_left: int, sigma: float) -> float:
    up = float(fair_up(spot, beat, seconds_left, sigma))
    return up if outcome == "Up" else 1.0 - up


def fill_rows(result: WindowResult) -> list[FillRow]:
    """Flatten one window's fills, with markouts off the window's spot path."""
    spot_at = dict(result.spot)
    rows: list[FillRow] = []
    end = result.start + WINDOW_SECONDS
    for r in result.records:
        fair_o = _side(r.outcome, r.fair)
        fair_p = _side(r.outcome, r.fair_at_place)
        touch = float(r.touch) if r.touch is not None else None
        edge = (float(r.price) - fair_o) if fair_o is not None else None
        edge_p = (float(r.price) - fair_p) if fair_p is not None else None
        # Rounded so a gap of exactly one tick lands in the right bucket.
        gap = round((fair_p - touch) / 0.01, 6) if (fair_p is not None and touch is not None) else None
        marks: dict[int, float | None] = {}
        for h in HORIZONS:
            marks[h] = None
            if result.beat is None or fair_o is None:
                continue
            later = min(r.ts + h, end - 1)
            s0 = spot_at.get(r.ts - 1)
            s1 = spot_at.get(later)
            if s0 is None or s1 is None:
                continue
            f0 = _fair_outcome(r.outcome, s0, result.beat, end - r.ts, result.sigma)
            f1 = _fair_outcome(r.outcome, s1, result.beat, end - later, result.sigma)
            marks[h] = f1 - f0
        won = result.winner == r.outcome
        rows.append(
            FillRow(
                start=result.start,
                winner=result.winner,
                ts=r.ts,
                outcome=r.outcome,
                price=float(r.price),
                size=float(r.size),
                fair_outcome=fair_o,
                fair_at_place=fair_p,
                touch=touch,
                edge_at_place=edge_p,
                edge_at_fill=edge,
                gap_ticks=gap,
                seconds_into_window=r.seconds_into_window,
                seconds_left=WINDOW_SECONDS - r.seconds_into_window,
                queue_ahead_at_place=float(r.queue_ahead_at_place),
                sigma=result.sigma,
                markout_10=marks[10],
                markout_30=marks[30],
                markout_60=marks[60],
                settlement=float(r.price) - (1.0 if won else 0.0),
                won=won,
            )
        )
    return rows


# --- aggregation ---------------------------------------------------------------


def _wmean(rows: list[FillRow], key: str) -> float | None:
    pairs = [(getattr(r, key), r.size) for r in rows if getattr(r, key) is not None]
    weight = sum(w for _, w in pairs)
    return (sum(v * w for v, w in pairs) / weight) if weight else None


def bucket_summary(rows: list[FillRow]) -> dict:
    shares = sum(r.size for r in rows)
    won = sum(r.size for r in rows if r.won)
    return {
        "fills": len(rows),
        "shares": round(shares, 1),
        "sold_winner_pct": round(won / shares * 100, 1) if shares else None,
        "avg_price": round(_wmean(rows, "price") or 0.0, 4) if rows else None,
        "edge_at_place": _round(_wmean(rows, "edge_at_place")),
        "edge_at_fill": _round(_wmean(rows, "edge_at_fill")),
        "markout_10": _round(_wmean(rows, "markout_10")),
        "markout_30": _round(_wmean(rows, "markout_30")),
        "markout_60": _round(_wmean(rows, "markout_60")),
        "settlement_per_share": _round(_wmean(rows, "settlement")),
        "pnl": round(sum(r.settlement * r.size for r in rows), 2),
    }


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def _terciles(values: list[float]) -> tuple[float, float]:
    ordered = sorted(values)
    n = len(ordered)
    if n < 3:
        return (math.inf, math.inf)
    return ordered[n // 3], ordered[(2 * n) // 3]


def breakdowns(rows: list[FillRow], results: list[WindowResult]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    out["side"] = {o: bucket_summary([r for r in rows if r.outcome == o]) for o in ("Up", "Down")}
    thirds = {"0-100s": (0, 100), "100-200s": (100, 200), "200-300s": (200, 300)}
    out["window_third"] = {
        name: bucket_summary([r for r in rows if lo <= r.seconds_into_window < hi]) for name, (lo, hi) in thirds.items()
    }
    lo_s, hi_s = _terciles([res.sigma for res in results])
    out["vol_tercile"] = {
        "low": bucket_summary([r for r in rows if r.sigma < lo_s]),
        "mid": bucket_summary([r for r in rows if lo_s <= r.sigma < hi_s]),
        "high": bucket_summary([r for r in rows if r.sigma >= hi_s]),
    }
    gaps = [r.gap_ticks for r in rows if r.gap_ticks is not None]
    if gaps:
        out["fair_minus_touch"] = {
            "below_touch (<= -1 tick)": bucket_summary([r for r in rows if r.gap_ticks is not None and r.gap_ticks <= -1]),
            "at_touch (-1..1)": bucket_summary([r for r in rows if r.gap_ticks is not None and -1 < r.gap_ticks < 1]),
            "1-3 ticks above": bucket_summary([r for r in rows if r.gap_ticks is not None and 1 <= r.gap_ticks < 3]),
            "3+ ticks above": bucket_summary([r for r in rows if r.gap_ticks is not None and r.gap_ticks >= 3]),
        }
    moves = {res.start: _window_move(res) for res in results}
    mv = [m for m in moves.values() if m is not None]
    if mv:
        lo_m, hi_m = _terciles(mv)
        out["move_tercile"] = {
            "small": bucket_summary([r for r in rows if moves.get(r.start) is not None and moves[r.start] < lo_m]),
            "mid": bucket_summary([r for r in rows if moves.get(r.start) is not None and lo_m <= moves[r.start] < hi_m]),
            "large": bucket_summary([r for r in rows if moves.get(r.start) is not None and moves[r.start] >= hi_m]),
        }
    return out


def regime_expectancy(results: list[WindowResult]) -> dict[str, dict[str, dict]]:
    """Window-level mean PnL with a block-bootstrap 95% interval per regime bucket.

    Windows in one bucket are not contiguous in time, so the blocks are over
    the bucket's windows in time order: a coarser correction than the
    overall interval's, stated as such.
    """
    results = sorted(results, key=lambda r: r.start)
    out: dict[str, dict[str, dict]] = {}

    def bucket(rows: list[WindowResult]) -> dict:
        boot = block_bootstrap([float(r.pnl) for r in rows])
        return {"windows": len(rows), **{k: round(v, 4) if isinstance(v, float) else v for k, v in boot.items()}}

    lo_s, hi_s = _terciles([r.sigma for r in results])
    out["vol_tercile"] = {
        "low": bucket([r for r in results if r.sigma < lo_s]),
        "mid": bucket([r for r in results if lo_s <= r.sigma < hi_s]),
        "high": bucket([r for r in results if r.sigma >= hi_s]),
    }
    moves = {r.start: _window_move(r) for r in results}
    mv = [m for m in moves.values() if m is not None]
    if mv:
        lo_m, hi_m = _terciles(mv)
        out["move_tercile"] = {
            "small": bucket([r for r in results if moves[r.start] is not None and moves[r.start] < lo_m]),
            "mid": bucket([r for r in results if moves[r.start] is not None and lo_m <= moves[r.start] < hi_m]),
            "large": bucket([r for r in results if moves[r.start] is not None and moves[r.start] >= hi_m]),
        }
    out["winner"] = {w: bucket([r for r in results if r.winner == w]) for w in ("Up", "Down")}
    return out


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile, q in [0, 1]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[k]


def _window_move(result: WindowResult) -> float | None:
    """Absolute log move of spot over the window in units of sigma*sqrt(300)."""
    if result.beat is None or not result.spot or result.sigma <= 0:
        return None
    end = result.start + WINDOW_SECONDS
    closes = [p for ts, p in result.spot if end - 5 <= ts < end]
    if not closes:
        return None
    return abs(math.log(float(closes[-1]) / float(result.beat))) / (result.sigma * math.sqrt(WINDOW_SECONDS))


# --- window-level -------------------------------------------------------------------


def decompose(result: WindowResult, split_size: Decimal, dust_shares: Decimal = Decimal("1")) -> dict:
    """proceeds + merged + redeemed - split == cash, as the engine books it.

    The residual (the side sold less) is redeemed at 1.00 when it won and is
    at least `dust_shares`; a winning residual below dust and any losing
    residual are abandoned.
    """
    more, less = max(result.sold_up, result.sold_down), min(result.sold_up, result.sold_down)
    merged = split_size - more
    residual = more - less
    residual_side = "Down" if result.sold_up > result.sold_down else ("Up" if result.sold_down > result.sold_up else None)
    redeemed = residual if (residual_side == result.winner and residual >= dust_shares) else Decimal("0")
    return {
        "start": result.start,
        "winner": result.winner,
        "proceeds": float(result.proceeds),
        "merged": float(merged),
        "redeemed": float(redeemed),
        "residual_loser_abandoned": float(residual) if residual_side and residual_side != result.winner else 0.0,
        "winner_dust_abandoned": float(residual) if (residual_side == result.winner and redeemed == 0) else 0.0,
        "pnl": float(result.pnl),
        "identity_gap": float(result.proceeds + merged + redeemed - split_size - result.pnl),
        "lean": float(result.lean),
        "fills": result.fills,
    }


def drawdown(pnls: list[float]) -> float:
    peak = cum = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def block_bootstrap(pnls: list[float], *, block: int = BLOCK_WINDOWS, draws: int = 2000, seed: int = 7) -> dict:
    """Mean PnL per window with a 95% interval from resampling contiguous blocks."""
    n = len(pnls)
    if n == 0:
        return {"mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "blocks": 0}
    blocks = [pnls[i : i + block] for i in range(0, n, block)]
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(draws):
        sample: list[float] = []
        while len(sample) < n:
            sample.extend(rng.choice(blocks))
        means.append(statistics.fmean(sample[:n]))
    means.sort()
    return {
        "mean": statistics.fmean(pnls),
        "ci_low": means[int(0.025 * draws)],
        "ci_high": means[int(0.975 * draws) - 1],
        "blocks": len(blocks),
    }


def report(
    results: list[WindowResult], *, split_size: Decimal, rebate_bps: Decimal, dust_shares: Decimal = Decimal("1")
) -> dict:
    results = sorted(results, key=lambda r: r.start)
    rows = [row for r in results for row in fill_rows(r)]
    pnls = [float(r.pnl) for r in results]
    proceeds = sum(float(r.proceeds) for r in results)
    n = len(results)
    rebate = proceeds * float(rebate_bps) / 10_000
    per_window = [decompose(r, split_size, dust_shares) for r in results]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "windows": n,
        "fills": len(rows),
        "shares_sold": round(sum(r.size for r in rows), 1),
        "gross_pnl": round(sum(pnls), 2),
        "rebate_estimate": round(rebate, 2),
        "net_pnl_with_rebate": round(sum(pnls) + rebate, 2),
        "pnl_per_window": round(sum(pnls) / n, 4) if n else 0.0,
        "pnl_per_fill": round(sum(pnls) / len(rows), 4) if rows else 0.0,
        "pnl_per_share": round(sum(pnls) / sum(r.size for r in rows), 4) if rows else 0.0,
        "fill_rate_per_window": round(len(rows) / n, 2) if n else 0.0,
        "shares_per_window": round(sum(r.size for r in rows) / n, 1) if n else 0.0,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_win": round(statistics.fmean(wins), 3) if wins else 0.0,
        "avg_loss": round(statistics.fmean(losses), 3) if losses else 0.0,
        "worst_window": round(min(pnls), 2) if pnls else 0.0,
        "max_drawdown": round(drawdown(pnls), 2),
        "lean_median": statistics.median(float(r.lean) for r in results) if results else 0.0,
        "lean_p90": percentile([float(r.lean) for r in results], 0.9),
        "lean_max": max((float(r.lean) for r in results), default=0.0),
        "pnl_p10": percentile(pnls, 0.1),
        "pnl_p90": percentile(pnls, 0.9),
        "expectancy": block_bootstrap(pnls),
        "regime_expectancy": regime_expectancy(results),
        "identity_gap_max": max((abs(p["identity_gap"]) for p in per_window), default=0.0),
        "decomposition": {
            "proceeds": round(proceeds, 2),
            "merged": round(sum(p["merged"] for p in per_window), 2),
            "redeemed": round(sum(p["redeemed"] for p in per_window), 2),
            "residual_loser_abandoned": round(sum(p["residual_loser_abandoned"] for p in per_window), 2),
            "winner_dust_abandoned": round(sum(p["winner_dust_abandoned"] for p in per_window), 2),
            "split": round(float(split_size) * n, 2),
        },
        "overall": bucket_summary(rows),
        "breakdowns": breakdowns(rows, results),
        "per_window": per_window,
    }


# --- data -----------------------------------------------------------------------------


def cached_windows(cache_dir: Path, *, require_spot: bool = True) -> list[WindowData]:
    """Every cached window with a winner and prints (and spot, when required), oldest first."""
    windows: list[WindowData] = []
    for path in sorted(cache_dir.glob("btc-updown-5m-*.json")):
        try:
            data = WindowData.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError):
            continue
        if data.winner not in {"Up", "Down"} or not data.prints:
            continue
        if require_spot and (not data.spot or data.spot_beat() is None):
            continue
        windows.append(data)
    windows.sort(key=lambda w: w.start)
    return windows


def _print_report(rep: dict, label: str) -> None:
    e = rep["expectancy"]
    print(f"{label}: {rep['windows']} windows, {rep['fills']} fills, {rep['shares_sold']} shares")
    print(f"  gross {rep['gross_pnl']:+.2f}  rebate est {rep['rebate_estimate']:+.2f}  net {rep['net_pnl_with_rebate']:+.2f}")
    print(f"  per window {rep['pnl_per_window']:+.3f}  95% block-bootstrap [{e['ci_low']:+.3f}, {e['ci_high']:+.3f}] "
          f"({e['blocks']} hourly blocks)")
    print(f"  per fill {rep['pnl_per_fill']:+.4f}  per share {rep['pnl_per_share']:+.4f}  "
          f"fills/w {rep['fill_rate_per_window']}  sh/w {rep['shares_per_window']}")
    print(f"  win rate {rep['win_rate_pct']}%  avg win {rep['avg_win']:+.2f}  avg loss {rep['avg_loss']:+.2f}  "
          f"p10/p90 {rep['pnl_p10']:+.2f}/{rep['pnl_p90']:+.2f}  worst {rep['worst_window']:+.2f}  "
          f"max drawdown {rep['max_drawdown']:+.2f}")
    print(f"  lean |sold Up - sold Down| median {rep['lean_median']:.0f}  p90 {rep['lean_p90']:.0f}  max {rep['lean_max']:.0f}")
    for name, buckets in rep["regime_expectancy"].items():
        parts = [f"{key} n{b['windows']} {b['mean']:+.3f} [{b['ci_low']:+.3f},{b['ci_high']:+.3f}]"
                 for key, b in buckets.items() if b["windows"]]
        print(f"  per-window mean by {name}: " + "  ".join(parts))
    o = rep["overall"]
    print(f"  sold winner {o['sold_winner_pct']}%  edge asked {o['edge_at_place']}  edge at fill {o['edge_at_fill']}  "
          f"markout 10/30/60 s {o['markout_10']} / {o['markout_30']} / {o['markout_60']}  settlement/sh {o['settlement_per_share']}")
    d = rep["decomposition"]
    print(f"  decomposition: proceeds {d['proceeds']:+.2f}  merged {d['merged']:+.2f}  redeemed {d['redeemed']:+.2f}  "
          f"split {-d['split']:+.2f}  | loser residual abandoned {d['residual_loser_abandoned']:.1f} sh, "
          f"winner dust {d['winner_dust_abandoned']:.2f} sh")
    if rep["identity_gap_max"] > 1e-6:
        print(f"  ACCOUNTING IDENTITY GAP up to {rep['identity_gap_max']}")
    for name, buckets in rep["breakdowns"].items():
        print(f"  by {name}:")
        for key, b in buckets.items():
            if not b["fills"]:
                continue
            print(f"    {key:<26} fills {b['fills']:>5} sh {b['shares']:>7} win% {b['sold_winner_pct']!s:>5} "
                  f"asked {b['edge_at_place']!s:>7} at fill {b['edge_at_fill']!s:>8} mo10 {b['markout_10']!s:>7} "
                  f"mo60 {b['markout_60']!s:>7} "
                  f"settle/sh {b['settlement_per_share']!s:>8} pnl {b['pnl']:>8.2f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fill-level diagnostics on the replay")
    defaults = StrategyConfig()
    parser.add_argument("--cache", default="data/backtest")
    parser.add_argument("--windows", type=int, default=0, help="use only the newest N cached windows (0 = all)")
    parser.add_argument("--queue", default="250")
    parser.add_argument("--touch-only", action="store_true")
    parser.add_argument("--lag", type=int, default=defaults.spot_lag_seconds)
    parser.add_argument("--fair-edge", default=str(defaults.fair_edge))
    parser.add_argument("--max-lean", default=str(defaults.max_lean_shares))
    parser.add_argument("--rebate-bps", default=str(defaults.maker_rebate_bps))
    parser.add_argument("--seed", type=int, default=30)
    parser.add_argument("--json", default="", help="write the full report here")
    parser.add_argument("--fills", default="", help="write one JSON row per fill here")
    args = parser.parse_args(argv)
    windows = cached_windows(Path(args.cache))
    if args.windows:
        windows = windows[-args.windows :]
    if not windows:
        print("no cached windows with spot; run spork30.backtest first", file=sys.stderr)
        sys.exit(1)
    strategy = StrategyConfig(fair_edge=Decimal(args.fair_edge), spot_lag_seconds=args.lag,
                              max_lean_shares=Decimal(args.max_lean), maker_rebate_bps=Decimal(args.rebate_bps))
    config = replay_config(strategy)
    results = []
    for w in windows:
        r = replay_window(w, queue=Decimal(args.queue), seed=args.seed, config=config, queue_at_touch_only=args.touch_only)
        if r is not None:
            results.append(r)
    rep = report(results, split_size=strategy.split_size, rebate_bps=strategy.maker_rebate_bps,
                 dust_shares=strategy.dust_shares)
    label = f"edge {strategy.fair_edge} lag {args.lag} queue {args.queue}{' touch-only' if args.touch_only else ''}"
    _print_report(rep, label)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    if args.fills:
        rows = [asdict(row) for r in results for row in fill_rows(r)]
        Path(args.fills).parent.mkdir(parents=True, exist_ok=True)
        Path(args.fills).write_text(json.dumps(rows), encoding="utf-8")


if __name__ == "__main__":
    main()
