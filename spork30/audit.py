"""Baseline-versus-candidate evaluation on a frozen window set.

    python -m spork30.audit baseline
    python -m spork30.audit experiments [--hypothesis gap|pause|voledge|lean|pricing|all]
    python -m spork30.audit compare --set fair_gap_withhold_ticks=1
    python -m spork30.audit calibrate

The window list is frozen the first time this runs (data/backtest/
audit-dataset.json) so every later table is on identical windows. The first
60% of windows by time are the training set a parameter may be chosen on;
the last 40% are only ever read for the final test number.

The objective is cash plus the maker rebate (`--rebate-bps`, default the
configured 42 bps of sold notional; 0 scores cash alone). The reference
account earns more from the rebate than from its fills, so a rule scored
on cash alone cannot see most of its income. Cash and rebate are also
reported separately.

A candidate is adopted only when, on the test set, under both conservative
fill models (uniform queue 250 and touch-only queue 100, spot lag 2 s):
the mean PnL per window beats the baseline, the worst window is not worse,
the block-bootstrap 95% interval of the paired per-window improvement
(candidate minus baseline on identical windows) excludes zero, and the
improvement is non-negative in every volatility tercile. That is the rule
written before the numbers were seen; it is not tuned to them.

`calibrate` replays a reference-style quoter (asks at the touch, lean cap 6)
across queue settings and compares fills and shares per window with the
reference account's actual tape on the same windows (`spork30.reference`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from spork30.backtest import WindowData, WindowResult, replay_config, replay_window
from spork30.config import StrategyConfig
from spork30.diagnostics import block_bootstrap, drawdown

TRAIN_FRACTION = 0.6
DATASET_FILE = "audit-dataset.json"

# The fill models every candidate must beat the baseline under. Both are
# conservative: the uniform model charges 250 shares of queue at every
# level; the touch-only model charges 100 at the touch and none above it.
CONSERVATIVE_FILLS: tuple[tuple[str, Decimal, bool], ...] = (
    ("uniform q250", Decimal("250"), False),
    ("touch-only q100", Decimal("100"), True),
)
EVAL_LAG = 2


@dataclass(frozen=True)
class Fill:
    label: str
    queue: Decimal
    touch_only: bool


@dataclass
class Stats:
    """Per-window statistics. `total`, `mean`, the interval, `worst`, the
    drawdown and the tercile means are on the objective (cash + rebate at
    `rebate_bps`); `cash_mean` and `rebate_mean` split it."""

    label: str
    windows: int
    fills: int
    shares: float
    total: float
    mean: float
    ci_low: float
    ci_high: float
    worst: float
    max_drawdown: float
    sold_winner_pct: float | None
    vol_tercile_mean: tuple[float, float, float]
    vol_half_mean: tuple[float, float]
    cash_mean: float = 0.0
    rebate_mean: float = 0.0
    rebate_bps: float = 0.0

    def row(self) -> str:
        lo, mid, hi = self.vol_tercile_mean
        return (f"{self.label:<34} n {self.windows:>3} fills {self.fills:>5} sh/w {self.shares / max(1, self.windows):>6.1f} "
                f"total {self.total:>+9.2f} mean {self.mean:>+7.3f} [{self.ci_low:>+6.3f},{self.ci_high:>+6.3f}] "
                f"(cash {self.cash_mean:>+6.3f} reb {self.rebate_mean:>+5.3f}) "
                f"worst {self.worst:>+7.2f} dd {self.max_drawdown:>+8.2f} win-sold {self.sold_winner_pct!s:>5}% "
                f"vol lo/mid/hi {lo:>+6.2f}/{mid:>+6.2f}/{hi:>+6.2f}")


def objective(result: WindowResult, rebate_bps: Decimal) -> float:
    """Cash plus the maker rebate on the window's sold notional."""
    return float(result.pnl + result.proceeds * rebate_bps / Decimal("10000"))


def stats(
    label: str, results: list[WindowResult], sigma_cuts: tuple[float, float], sigma_median: float,
    rebate_bps: Decimal = Decimal("0"),
) -> Stats:
    results = sorted(results, key=lambda r: r.start)
    pnls = [objective(r, rebate_bps) for r in results]
    cash = [float(r.pnl) for r in results]
    boot = block_bootstrap(pnls)
    sold = sum(float(r.sold_up + r.sold_down) for r in results)
    won = sum(float(r.sold_winner) for r in results)
    n = len(results)

    def mean_where(pred) -> float:
        xs = [objective(r, rebate_bps) for r in results if pred(r.sigma)]
        return sum(xs) / len(xs) if xs else 0.0

    lo_cut, hi_cut = sigma_cuts
    return Stats(
        label=label,
        windows=n,
        fills=sum(r.fills for r in results),
        shares=sold,
        total=sum(pnls),
        mean=boot["mean"],
        ci_low=boot["ci_low"],
        ci_high=boot["ci_high"],
        worst=min(pnls) if pnls else 0.0,
        max_drawdown=drawdown(pnls),
        sold_winner_pct=round(won / sold * 100, 1) if sold else None,
        vol_tercile_mean=(
            mean_where(lambda s: s < lo_cut),
            mean_where(lambda s: lo_cut <= s < hi_cut),
            mean_where(lambda s: s >= hi_cut),
        ),
        vol_half_mean=(mean_where(lambda s: s < sigma_median), mean_where(lambda s: s >= sigma_median)),
        cash_mean=sum(cash) / n if n else 0.0,
        rebate_mean=(sum(pnls) - sum(cash)) / n if n else 0.0,
        rebate_bps=float(rebate_bps),
    )


def reference_stats(
    label: str, starts: Iterable[int], sigma_by_start: dict[int, float], sigma_cuts: tuple[float, float],
    sigma_median: float, rebate_bps: Decimal, path: Path = Path("data/reference/windows.json"),
) -> Stats | None:
    """The reference account's actual windows, as a `Stats` row on the same
    starts (None when its tape is not cached or does not overlap)."""
    if not path.exists():
        return None
    wanted = set(starts)
    rows = [w for w in json.loads(path.read_text(encoding="utf-8")) if int(w["start"]) in wanted]
    if not rows:
        return None
    results = [
        WindowResult(
            start=int(w["start"]), winner=str(w.get("winner") or ""), pnl=Decimal(str(w["cash"])), fills=int(w["fills"]),
            sold_up=Decimal(str(w["sold_up"])), sold_down=Decimal(str(w["sold_down"])),
            proceeds=Decimal(str(w["proceeds"])), places=0, phase="CLOSED",
            sigma=sigma_by_start.get(int(w["start"]), 0.0),
        )
        for w in rows
    ]
    return stats(label, results, sigma_cuts, sigma_median, rebate_bps)


# --- dataset ---------------------------------------------------------------------


def _light_windows(cache_dir: Path):
    """Usable cached windows (winner, prints, spot with a beat), one at a time."""
    for file in sorted(cache_dir.glob("btc-updown-5m-*.json")):
        try:
            data = WindowData.from_json(json.loads(file.read_text(encoding="utf-8")))
        except (ValueError, KeyError):
            continue
        if data.winner in {"Up", "Down"} and data.prints and data.spot and data.spot_beat() is not None:
            yield data


def frozen_dataset(cache_dir: Path) -> tuple[list[WindowData], list[WindowData]]:
    """(train, test) on the frozen window list, freezing it on first use."""
    path = cache_dir / DATASET_FILE
    # Prints dropped as each file is read; only starts and spot are kept here.
    windows = [dataclasses.replace(w, prints=[]) for w in _light_windows(cache_dir)]
    if path.exists():
        frozen = json.loads(path.read_text(encoding="utf-8"))
        wanted = set(frozen["starts"])
        windows = [w for w in windows if w.start in wanted]
        missing = wanted - {w.start for w in windows}
        if missing:
            print(f"warning: {len(missing)} frozen windows are missing from the cache", file=sys.stderr)
        cut = int(frozen["train_until"])
    else:
        if not windows:
            raise SystemExit("no cached windows with spot; run spork30.backtest with --fair-edge > 0 first")
        n_train = int(len(windows) * TRAIN_FRACTION)
        cut = windows[n_train].start if n_train < len(windows) else windows[-1].start + 1
        path.write_text(json.dumps({
            "frozen_at": int(time.time()),
            "starts": [w.start for w in windows],
            "train_until": cut,
            "train_fraction": TRAIN_FRACTION,
        }, indent=1), encoding="utf-8")
        print(f"froze {len(windows)} windows to {path}; train < {cut}")
    train = [w for w in windows if w.start < cut]
    test = [w for w in windows if w.start >= cut]
    return train, test


def sigma_cuts(windows: Iterable[WindowData]) -> tuple[tuple[float, float], float]:
    sig = sorted(w.realised_sigma() or 0.0 for w in windows)
    n = len(sig)
    if n < 3:
        return (float("inf"), float("inf")), float("inf")
    return (sig[n // 3], sig[(2 * n) // 3]), sig[n // 2]


def sigma_by_start(windows: Iterable[WindowData]) -> dict[int, float]:
    return {w.start: (w.realised_sigma() or 0.0) for w in windows}


# --- evaluation ------------------------------------------------------------------


def _windows_for(cache_dir: str, starts: list[int]):
    """Yield the frozen windows one at a time from the cache.

    Parsed on demand and dropped after the replay: the whole cache as
    Decimal objects is several hundred MB, which on a small machine swaps
    and turns a 5-minute run into hours.
    """
    for start in starts:
        path = Path(cache_dir) / f"btc-updown-5m-{start}.json"
        if not path.exists():
            continue
        try:
            yield WindowData.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError):
            continue


def _run(args: tuple) -> tuple[str, list[WindowResult]]:
    key, cache_dir, starts, overrides, queue, touch_only, seed = args
    strategy = StrategyConfig(**overrides)
    config = replay_config(strategy)
    out = []
    for w in _windows_for(cache_dir, starts):
        r = replay_window(w, queue=queue, seed=seed, config=config, queue_at_touch_only=touch_only)
        if r is not None:
            # Drop the heavy payloads; the tables only need the numbers.
            r = dataclasses.replace(r, records=[], spot=[])
            out.append(r)
    return key, out


def evaluate(
    windows: list[WindowData],
    variants: dict[str, dict],
    fills: Iterable[Fill],
    *,
    seed: int = 30,
    workers: int = 2,
    cache_dir: str = "data/backtest",
) -> dict[tuple[str, str], list[WindowResult]]:
    """Replay every (variant, fill model) pair; keyed by (variant, fill label).

    Workers receive window starts, not windows: the prints are too large to
    pickle into every job.
    """
    starts = [w.start for w in windows]
    jobs = [
        (f"{name}\u0000{fill.label}", cache_dir, starts, overrides, fill.queue, fill.touch_only, seed)
        for name, overrides in variants.items()
        for fill in fills
    ]
    results: dict[tuple[str, str], list[WindowResult]] = {}
    if workers <= 1 or len(jobs) == 1:
        done = map(_run, jobs)
    else:
        pool = ProcessPoolExecutor(max_workers=workers)
        done = pool.map(_run, jobs)
    for key, out in done:
        name, label = key.split("\u0000")
        results[(name, label)] = out
    return results


def overrides(**kwargs) -> dict:
    """StrategyConfig field overrides, Decimal-typed where the default is a Decimal."""
    defaults = StrategyConfig()
    out = {}
    for k, v in kwargs.items():
        base = getattr(defaults, k)
        if isinstance(base, Decimal):
            out[k] = Decimal(str(v))
        elif isinstance(base, bool):
            out[k] = v if isinstance(v, bool) else str(v).strip().lower() in {"1", "true", "yes", "on"}
        else:
            out[k] = type(base)(v)
    return out


def parse_set(text: str) -> dict:
    out = {}
    for part in text.split(","):
        if not part.strip():
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return overrides(**out)


# --- acceptance ------------------------------------------------------------------


def paired_improvement(
    base: list[WindowResult], cand: list[WindowResult], rebate_bps: Decimal = Decimal("0")
) -> dict:
    """Candidate minus baseline objective on identical windows, block-bootstrapped.

    The two runs see the same prints and spot, so the per-window difference
    removes the common path and the interval is for the improvement itself,
    not for the candidate's own mean.
    """
    by_start = {r.start: objective(r, rebate_bps) for r in base}
    diffs = [objective(r, rebate_bps) - by_start[r.start] for r in sorted(cand, key=lambda r: r.start) if r.start in by_start]
    return block_bootstrap(diffs)


def accept(base: Stats, cand: Stats, improvement: dict) -> tuple[bool, list[str]]:
    """The pre-registered rule, applied to one fill model's test-set numbers.

    `improvement` is `paired_improvement(...)` for the same windows.
    """
    reasons = []
    if not cand.mean > base.mean:
        reasons.append(f"mean {cand.mean:+.3f} not above baseline {base.mean:+.3f}")
    if cand.worst < base.worst - 1e-9:
        reasons.append(f"worst window {cand.worst:+.2f} worse than baseline {base.worst:+.2f}")
    if not improvement["ci_low"] > 0:
        reasons.append(f"improvement {improvement['mean']:+.3f}/window, 95% CI "
                       f"[{improvement['ci_low']:+.3f},{improvement['ci_high']:+.3f}] includes zero")
    deltas = [c - b for c, b in zip(cand.vol_tercile_mean, base.vol_tercile_mean)]
    if not all(d >= 0 for d in deltas):
        reasons.append("improvement does not hold in every vol tercile "
                       f"(lo/mid/hi {deltas[0]:+.2f}/{deltas[1]:+.2f}/{deltas[2]:+.2f})")
    return (not reasons), reasons


# --- commands --------------------------------------------------------------------


def _table(title: str, rows: list[Stats | None]) -> None:
    print(title)
    for s in rows:
        if s is not None:
            print("  " + s.row())


def _reference_row(windows: list[WindowData], cuts, median, rebate_bps: Decimal, label: str = "reference (actual tape)") -> Stats | None:
    return reference_stats(label, [w.start for w in windows], sigma_by_start(windows), cuts, median, rebate_bps)


def cmd_baseline(args) -> None:
    cache = Path(args.cache)
    rebate = Decimal(args.rebate_bps)
    train, test = frozen_dataset(cache)
    both = train + test
    cuts, median = sigma_cuts(both)
    print(f"dataset: {len(both)} windows, train {len(train)} (< {test[0].start if test else '-'}), test {len(test)}; "
          f"objective cash + {rebate} bps rebate")
    variants = {f"lag {lag}": overrides(spot_lag_seconds=lag) for lag in (1, 2, 3)}
    fills = [Fill(f"uniform q{q}", Decimal(q), False) for q in ("0", "100", "250")] + [Fill("touch-only q100", Decimal("100"), True)]
    results = evaluate(both, variants, fills, seed=args.seed, workers=args.workers, cache_dir=args.cache)
    rows_all, rows_train, rows_test = [], [], []
    for (name, label), res in results.items():
        cut = test[0].start if test else None
        rows_all.append(stats(f"{name} {label}", res, cuts, median, rebate))
        rows_train.append(stats(f"{name} {label}", [r for r in res if cut is None or r.start < cut], cuts, median, rebate))
        rows_test.append(stats(f"{name} {label}", [r for r in res if cut is not None and r.start >= cut], cuts, median, rebate))
    rows_all.append(_reference_row(both, cuts, median, rebate))
    rows_train.append(_reference_row(train, cuts, median, rebate))
    rows_test.append(_reference_row(test, cuts, median, rebate))
    rows_all, rows_train, rows_test = ([s for s in rows if s is not None] for rows in (rows_all, rows_train, rows_test))
    _table("baseline, all windows", rows_all)
    _table("baseline, train", rows_train)
    _table("baseline, test", rows_test)
    if args.json:
        payload = {"dataset": {"train": len(train), "test": len(test)},
                   "all": [dataclasses.asdict(s) for s in rows_all],
                   "train": [dataclasses.asdict(s) for s in rows_train],
                   "test": [dataclasses.asdict(s) for s in rows_test]}
        Path(args.json).write_text(json.dumps(payload, indent=1), encoding="utf-8")


def hypotheses() -> dict[str, dict[str, dict]]:
    """Candidate settings per hypothesis. Each is a small grid chosen on train."""
    return {
        # H1: do not rest an ask above the touch when the spot-fair sits at or
        # above the touch (the book disagrees with the spot model; the only
        # takers who reach such an ask are sweeping through the touch).
        "gap": {f"gap<{g}": overrides(fair_gap_withhold_ticks=g) for g in (1, 2, 3, 5)},
        # H2: pull quotes for the last N seconds of the window.
        "pause": {f"pause {n}s": overrides(quote_pause_final_seconds=n) for n in (30, 60, 100)},
        # H3: edge scales with the volatility of the time left. Realised
        # sigma is ~1e-4 per second, so sigma*sqrt(300) is ~0.0015: multipliers
        # below ~20 never clear the 0.03 floor (the first grid, 0.5/1/2,
        # reproduced the baseline exactly). These make the edge 3-7 cents at
        # the open, falling to the floor as the window runs out.
        "voledge": {f"voledge x{c}": overrides(fair_edge_vol_mult=c) for c in ("20", "35", "50")},
        # H4: inventory discipline, tighter lean cap or a per-side fill cooldown.
        "lean": {
            **{f"lean cap {c}": overrides(max_lean_shares=c) for c in ("1", "2")},
            **{f"fill cooldown {s}s": overrides(fill_cooldown_seconds=s) for s in (10, 30)},
        },
        # R1 (reference gap): quote at or inside the touch, with the fair
        # value as a guard rather than a width. The reference account sells
        # 80% of its shares at or inside the print-implied touch with no
        # edge against spot-fair and earns on volume and rebate; the fork's
        # fair + 0.03 rests behind the market and sells 5x less.
        "pricing": {
            # Touch-joining, no spot model (the account's observed behaviour).
            "touch lean 6": overrides(fair_edge="0", max_lean_shares="6"),
            "touch lean 14": overrides(fair_edge="0", max_lean_shares="14"),
            # Touch-joining with the spot guard: join the touch but withhold
            # the ask when fair + 0.01 is still above it (gap guard).
            "edge 0.01 gap<1 lean 6": overrides(fair_edge="0.01", fair_gap_withhold_ticks=1, max_lean_shares="6"),
            "edge 0.02 gap<1 lean 6": overrides(fair_edge="0.02", fair_gap_withhold_ticks=1, max_lean_shares="6"),
            "edge 0.03 gap<1 lean 6": overrides(fair_gap_withhold_ticks=1, max_lean_shares="6"),
            # Allowed to undercut the touch when fair + edge is below it.
            "edge 0.01 cut gap<1 lean 6": overrides(fair_edge="0.01", fair_undercut=True, fair_gap_withhold_ticks=1,
                                                   max_lean_shares="6"),
            "edge 0.02 cut gap<1 lean 6": overrides(fair_edge="0.02", fair_undercut=True, fair_gap_withhold_ticks=1,
                                                   max_lean_shares="6"),
            "edge 0.01 cut gap<1 lean 14": overrides(fair_edge="0.01", fair_undercut=True, fair_gap_withhold_ticks=1,
                                                    max_lean_shares="14"),
        },
    }


def cmd_experiments(args) -> None:
    cache = Path(args.cache)
    rebate = Decimal(args.rebate_bps)
    train, test = frozen_dataset(cache)
    cuts, median = sigma_cuts(train + test)
    fills = [Fill(*f) for f in CONSERVATIVE_FILLS]
    picks = hypotheses()
    if args.hypothesis != "all":
        picks = {args.hypothesis: picks[args.hypothesis]}
    variants = {"baseline": overrides(spot_lag_seconds=EVAL_LAG)}
    for group in picks.values():
        for name, ov in group.items():
            variants[name] = {**ov, "spot_lag_seconds": EVAL_LAG}
    print(f"train {len(train)} windows, test {len(test)}; fills: {[f.label for f in fills]}; lag {EVAL_LAG}; "
          f"objective cash + {rebate} bps rebate")
    t0 = time.time()
    train_res = evaluate(train, variants, fills, seed=args.seed, workers=args.workers, cache_dir=args.cache)
    print(f"train replays done in {time.time() - t0:.0f}s")
    report: dict = {"train": {}, "test": {}, "chosen": {}, "accepted": {}, "rebate_bps": str(rebate)}
    ref_train = _reference_row(train, cuts, median, rebate)
    ref_test = _reference_row(test, cuts, median, rebate)
    for fill in fills:
        rows = [stats(name, train_res[(name, fill.label)], cuts, median, rebate) for name in variants]
        _table(f"train, {fill.label}", [*rows, ref_train])
        report["train"][fill.label] = [dataclasses.asdict(s) for s in rows]
    if ref_train is not None:
        report["reference"] = {"train": dataclasses.asdict(ref_train),
                               "test": dataclasses.asdict(ref_test) if ref_test else None}
    # Choose per hypothesis on train: the candidate with the best mean of the
    # two conservative fill models' means. Ties and no-improvement both leave
    # the hypothesis without a candidate.
    base_train = {f.label: stats("baseline", train_res[("baseline", f.label)], cuts, median, rebate) for f in fills}
    chosen: dict[str, str] = {}
    for hyp, group in picks.items():
        best, best_score = None, sum(s.mean for s in base_train.values()) / len(fills)
        for name in group:
            score = sum(stats(name, train_res[(name, f.label)], cuts, median, rebate).mean for f in fills) / len(fills)
            if score > best_score:
                best, best_score = name, score
        chosen[hyp] = best or "-"
        print(f"chosen on train for {hyp}: {best or 'none improves the baseline'}")
    report["chosen"] = chosen
    test_variants = {"baseline": variants["baseline"]}
    for name in chosen.values():
        if name != "-":
            test_variants[name] = variants[name]
    if len(test_variants) > 1:
        t0 = time.time()
        test_res = evaluate(test, test_variants, fills, seed=args.seed, workers=args.workers, cache_dir=args.cache)
        print(f"test replays done in {time.time() - t0:.0f}s")
        for fill in fills:
            rows = [stats(name, test_res[(name, fill.label)], cuts, median, rebate) for name in test_variants]
            _table(f"TEST, {fill.label}", [*rows, ref_test])
            report["test"][fill.label] = [dataclasses.asdict(s) for s in rows]
        for name in test_variants:
            if name == "baseline":
                continue
            verdicts = []
            for fill in fills:
                base = stats("baseline", test_res[("baseline", fill.label)], cuts, median, rebate)
                cand = stats(name, test_res[(name, fill.label)], cuts, median, rebate)
                gain = paired_improvement(test_res[("baseline", fill.label)], test_res[(name, fill.label)], rebate)
                ok, why = accept(base, cand, gain)
                summary = f"improvement {gain['mean']:+.3f}/window, 95% CI [{gain['ci_low']:+.3f},{gain['ci_high']:+.3f}]"
                why = [summary] if ok else [*why, summary]
                verdicts.append((fill.label, ok, why))
                report.setdefault("improvement", {}).setdefault(name, {})[fill.label] = gain
            accepted = all(ok for _, ok, _ in verdicts)
            report["accepted"][name] = {"accepted": accepted, "verdicts": verdicts}
            print(f"{'ADOPT' if accepted else 'reject'} {name}")
            for label, ok, why in verdicts:
                print(f"    {label}: {'passes, ' if ok else ''}{'; '.join(why)}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")


def cmd_compare(args) -> None:
    cache = Path(args.cache)
    rebate = Decimal(args.rebate_bps)
    train, test = frozen_dataset(cache)
    cuts, median = sigma_cuts(train + test)
    fills = [Fill(*f) for f in CONSERVATIVE_FILLS]
    cand = parse_set(args.set)
    variants = {"baseline": overrides(spot_lag_seconds=args.lag), "candidate": {**cand, "spot_lag_seconds": args.lag}}
    report: dict = {"set": args.set, "rebate_bps": str(rebate), "lag": args.lag}
    for split_name, windows in (("train", train), ("test", test)):
        res = evaluate(windows, variants, fills, seed=args.seed, workers=args.workers, cache_dir=args.cache)
        ref = _reference_row(windows, cuts, median, rebate)
        for fill in fills:
            rows = [stats(name, res[(name, fill.label)], cuts, median, rebate) for name in variants]
            _table(f"{split_name}, {fill.label}", [*rows, ref])
            gain = paired_improvement(res[("baseline", fill.label)], res[("candidate", fill.label)], rebate)
            print(f"    improvement {gain['mean']:+.3f}/window, 95% CI [{gain['ci_low']:+.3f},{gain['ci_high']:+.3f}]")
            entry = {"rows": [dataclasses.asdict(s) for s in rows], "improvement": gain,
                     "reference": dataclasses.asdict(ref) if ref else None}
            if split_name == "test":
                ok, why = accept(rows[0], rows[1], gain)
                print(f"    {'passes' if ok else '; '.join(why)}")
                entry["accepted"], entry["reasons"] = ok, why
            report.setdefault(split_name, {})[fill.label] = entry
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")


# Queue settings the calibration sweeps: shares ahead of a new ask at its level.
CALIBRATION_QUEUES = ("0", "25", "50", "100", "250")


def cmd_calibrate(args) -> None:
    """Which fill model reproduces the reference account's volume?

    A reference-style quoter (asks at the touch, lean cap 6, no spot model) is
    replayed under each queue setting on the overlap windows. The queue whose
    shares per window come closest to the account's own tape is the fill
    model's measured anchor; the account's fills are what actually traded
    against asks like these.
    """
    cache = Path(args.cache)
    rebate = Decimal(args.rebate_bps)
    train, test = frozen_dataset(cache)
    both = train + test
    cuts, median = sigma_cuts(both)
    ref_path = Path(args.reference)
    ref = reference_stats("reference (actual tape)", [w.start for w in both], sigma_by_start(both), cuts, median, rebate,
                          path=ref_path)
    if ref is None:
        raise SystemExit(f"no reference windows at {ref_path}; run `python -m spork30.reference profile --windows-json {ref_path}`")
    overlap_starts = {int(w["start"]) for w in json.loads(ref_path.read_text(encoding="utf-8"))}
    windows = [w for w in both if w.start in overlap_starts]
    style = overrides(fair_edge="0", max_lean_shares=args.lean, spot_lag_seconds=EVAL_LAG)
    queues = [q.strip() for q in args.queues.split(",") if q.strip()]
    fills = [Fill(f"uniform q{q}", Decimal(q), False) for q in queues]
    # A touch quoter never rests above the touch, so touch-only queueing is
    # the uniform model for it (Oct 15 2026: identical rows at every queue);
    # it is still replayed so the table says so.
    fills += [Fill(f"touch-only q{q}", Decimal(q), True) for q in queues if q != "0" and not args.uniform_only]
    print(f"{len(windows)} overlap windows; reference-style quoter: touch, lean cap {args.lean}; "
          f"objective cash + {rebate} bps")
    res = evaluate(windows, {"touch": style}, fills, seed=args.seed, workers=args.workers, cache_dir=args.cache)
    rows = [stats(f.label, res[("touch", f.label)], cuts, median, rebate) for f in fills]
    target = ref.shares / max(1, ref.windows)
    best = min(rows, key=lambda s: abs((s.shares / max(1, s.windows)) - target))
    _table("reference-style quoter by fill model", [*rows, ref])
    print(f"closest to the reference's {target:.1f} sh/w: {best.label} "
          f"({best.shares / max(1, best.windows):.1f} sh/w, {best.fills / max(1, best.windows):.1f} fills/w vs "
          f"{ref.fills / max(1, ref.windows):.1f})")
    print("read: the replay's fills under the matched queue are the same volume the account really got; the gap in "
          "mean between that row and the reference row is what the replay's touch and adverse-selection model add.")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "overlap_windows": len(windows), "lean": args.lean, "rebate_bps": str(rebate),
            "rows": [dataclasses.asdict(s) for s in rows], "reference": dataclasses.asdict(ref),
            "closest": best.label,
        }, indent=1, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Baseline and candidate evaluation on frozen windows")
    parser.add_argument("--cache", default="data/backtest")
    parser.add_argument("--seed", type=int, default=30)
    parser.add_argument("--workers", type=int, default=1,
                        help="replay processes; each holds the whole print cache, so 1 on small machines")
    parser.add_argument("--rebate-bps", default=str(StrategyConfig().maker_rebate_bps),
                        help="maker rebate added to cash in the objective (0 scores cash alone)")
    parser.add_argument("--reference", default="data/reference/windows.json",
                        help="reference account windows from `spork30.reference profile --windows-json`")
    parser.add_argument("--json", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("baseline")
    exp = sub.add_parser("experiments")
    exp.add_argument("--hypothesis", default="all", choices=["all", "gap", "pause", "voledge", "lean", "pricing"])
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--set", required=True, help="comma list of StrategyConfig overrides, e.g. fair_gap_withhold_ticks=1")
    cmp_.add_argument("--lag", type=int, default=EVAL_LAG)
    cal = sub.add_parser("calibrate")
    cal.add_argument("--lean", default="6", help="lean cap of the reference-style quoter")
    cal.add_argument("--queues", default=",".join(CALIBRATION_QUEUES),
                     help="comma list of SIM_QUEUE_AHEAD values to replay")
    cal.add_argument("--uniform-only", action="store_true", help="skip the touch-only rows")
    args = parser.parse_args(argv)
    {"baseline": cmd_baseline, "experiments": cmd_experiments, "compare": cmd_compare,
     "calibrate": cmd_calibrate}[args.command](args)


if __name__ == "__main__":
    main()
