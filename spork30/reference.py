"""The reference account (spork30, 0x1dd2...51c2): public activity, per-window
profile, and alignment of its real fills with the replay's modelled ones.

    python -m spork30.reference fetch --days 5
    python -m spork30.reference profile
    python -m spork30.reference align --queue 250 --lag 2 --json data/reference/align.json

Everything here is read-only against public endpoints (data-api.polymarket.com)
and the local print cache. Nothing signs or sends.

What the account's tape shows directly: splits, every fill (price, size,
time, outcome), merges, redeems, daily MAKER_REBATE payments. What is
inferred: the touch at the time of a fill (from the public prints before it),
the spot-implied fair value (from Binance 1-s closes), and the winner when no
redeem paid (the side sold more held the unpaired loser).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from spork30.backtest import WindowData, replay_config, replay_window
from spork30.config import StrategyConfig
from spork30.diagnostics import HORIZONS, WINDOW_SECONDS, _fair_outcome, bucket_summary, cached_windows, fill_rows
from spork30.fillmodel import TICK, ReconstructedBook
from spork30.strategy import sigma_per_second

ADDRESS = "0x1dd2a69e73ba444ecd5d87f0073d51a670ad51c2"
DATA_API = "https://data-api.polymarket.com/activity"
SLUG_PREFIX = "btc-updown-5m-"
DEFAULT_CACHE = Path("data/reference/activity.json")
PAGE = 500
# The API refuses offsets past about 5000; the time window is moved instead.
MAX_OFFSET = 4500


# --- activity -------------------------------------------------------------------------


def _get(url: str, tries: int = 4) -> list:
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def _key(row: dict) -> tuple:
    return (row.get("transactionHash"), row.get("asset"), row.get("type"), str(row.get("size")),
            str(row.get("price")), int(row.get("timestamp") or 0), row.get("outcome"))


def load_activity(cache: Path = DEFAULT_CACHE) -> list[dict]:
    if not cache.exists():
        return []
    return json.loads(cache.read_text(encoding="utf-8"))


def fetch_activity(
    *, since: int, until: int | None = None, cache: Path = DEFAULT_CACHE, user: str = ADDRESS,
    quiet: bool = False, sleep: float = 0.3,
) -> list[dict]:
    """Pull every activity row for `user` with timestamp >= `since` (and <= `until`),
    merge it into the cache and return the full cached list, oldest first."""
    rows: dict[tuple, dict] = {_key(r): r for r in load_activity(cache)}
    end = until if until is not None else int(time.time())
    offset = 0
    while True:
        url = f"{DATA_API}?user={user}&limit={PAGE}&offset={offset}&end={end}"
        try:
            page = _get(url)
        except Exception as exc:
            if not quiet:
                print(f"page failed: {exc}", file=sys.stderr)
            break
        if not page:
            break
        for r in page:
            rows[_key(r)] = r
        oldest = min(int(r["timestamp"]) for r in page)
        if not quiet:
            print(f"end {end} offset {offset} got {len(page)} oldest "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(oldest))}", flush=True)
        if oldest < since or len(page) < PAGE:
            break
        offset += PAGE
        if offset >= MAX_OFFSET:
            end = oldest
            offset = 0
        time.sleep(sleep)
    merged = sorted(rows.values(), key=lambda r: int(r["timestamp"]))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(merged), encoding="utf-8")
    return merged


# --- per-window reconstruction ----------------------------------------------------------


@dataclass(frozen=True)
class RefTrade:
    ts: int
    outcome: str
    price: Decimal
    size: Decimal
    usdc: Decimal


@dataclass
class RefWindow:
    slug: str
    start: int
    split_ts: int | None = None
    split_size: Decimal = Decimal("0")
    trades: list[RefTrade] = field(default_factory=list)
    merge_ts: int | None = None
    merged: Decimal = Decimal("0")
    redeem_ts: int | None = None
    redeemed: Decimal = Decimal("0")
    winner: str | None = None
    winner_source: str = ""  # "redeem" | "redeem-loser" | "inferred" | "cache" | ""

    @property
    def fills(self) -> int:
        return len(self.trades)

    @property
    def sold_up(self) -> Decimal:
        return sum((t.size for t in self.trades if t.outcome == "Up"), Decimal("0"))

    @property
    def sold_down(self) -> Decimal:
        return sum((t.size for t in self.trades if t.outcome == "Down"), Decimal("0"))

    @property
    def proceeds(self) -> Decimal:
        return sum((t.usdc for t in self.trades), Decimal("0"))

    @property
    def cash(self) -> Decimal:
        return self.proceeds + self.merged + self.redeemed - self.split_size

    @property
    def lean(self) -> Decimal:
        return abs(self.sold_up - self.sold_down)

    @property
    def sold_winner(self) -> Decimal:
        if self.winner == "Up":
            return self.sold_up
        if self.winner == "Down":
            return self.sold_down
        return Decimal("0")

    @property
    def lead(self) -> int | None:
        return self.start - self.split_ts if self.split_ts is not None else None

    def to_json(self) -> dict:
        return {
            "slug": self.slug, "start": self.start, "split_ts": self.split_ts, "split_size": str(self.split_size),
            "merge_ts": self.merge_ts, "merged": str(self.merged), "redeem_ts": self.redeem_ts,
            "redeemed": str(self.redeemed), "winner": self.winner, "winner_source": self.winner_source,
            "sold_up": str(self.sold_up), "sold_down": str(self.sold_down), "proceeds": str(self.proceeds),
            "cash": str(self.cash), "fills": self.fills,
            "trades": [[t.ts - self.start, t.outcome, str(t.price), str(t.size)] for t in self.trades],
        }


def _dec(value: object) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def windows_from_activity(rows: list[dict], *, settled_before: int | None = None) -> list[RefWindow]:
    """One `RefWindow` per btc-updown-5m slug that was minted and then merged or
    redeemed, with the window over by `settled_before` (default: 15 min before
    the newest row). Winner: the outcome whose redeem paid (a loser redeem
    pays 0 and names the other side); else
    the side sold more (its unpaired residual was the loser and was abandoned)."""
    if not rows:
        return []
    newest = max(int(r["timestamp"]) for r in rows)
    cutoff = settled_before if settled_before is not None else newest - 900
    by_slug: dict[str, RefWindow] = {}
    for r in rows:
        slug = str(r.get("slug") or "")
        if not slug.startswith(SLUG_PREFIX):
            continue
        tail = slug.rsplit("-", 1)[-1]
        if not tail.isdigit():
            continue
        w = by_slug.get(slug)
        if w is None:
            w = by_slug[slug] = RefWindow(slug=slug, start=int(tail))
        ts = int(r["timestamp"])
        kind = str(r.get("type") or "")
        if kind == "SPLIT":
            w.split_ts = ts if w.split_ts is None else min(w.split_ts, ts)
            w.split_size += _dec(r.get("usdcSize"))
        elif kind == "TRADE":
            w.trades.append(RefTrade(ts, str(r.get("outcome") or ""), _dec(r.get("price")), _dec(r.get("size")),
                                     _dec(r.get("usdcSize"))))
        elif kind == "MERGE":
            w.merge_ts = ts if w.merge_ts is None else max(w.merge_ts, ts)
            w.merged += _dec(r.get("usdcSize"))
        elif kind == "REDEEM":
            w.redeem_ts = ts if w.redeem_ts is None else max(w.redeem_ts, ts)
            paid = _dec(r.get("usdcSize"))
            w.redeemed += paid
            # The account redeems both residuals; only the winner's pays
            # (usdcSize = size). A loser redeem of any size says the other
            # side won.
            if r.get("outcome") in {"Up", "Down"}:
                outcome = str(r["outcome"])
                if paid > 0:
                    w.winner, w.winner_source = outcome, "redeem"
                elif w.winner_source != "redeem" and _dec(r.get("size")) > 0:
                    w.winner = "Down" if outcome == "Up" else "Up"
                    w.winner_source = "redeem-loser"
    out: list[RefWindow] = []
    for w in by_slug.values():
        if w.split_ts is None or w.start + WINDOW_SECONDS > cutoff or (w.merge_ts is None and w.redeem_ts is None):
            continue
        w.trades.sort(key=lambda t: t.ts)
        if w.winner is None and w.sold_up != w.sold_down:
            w.winner = "Up" if w.sold_up > w.sold_down else "Down"
            w.winner_source = "inferred"
        out.append(w)
    out.sort(key=lambda w: w.start)
    return out


def rebate_rows(rows: list[dict]) -> list[tuple[int, Decimal]]:
    return sorted((int(r["timestamp"]), _dec(r.get("usdcSize"))) for r in rows if r.get("type") == "MAKER_REBATE")


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def pair_sums(w: RefWindow, within: int = 2) -> list[float]:
    """Up + Down fill prices for fills on the two outcomes within `within` s of each other."""
    ups = [t for t in w.trades if t.outcome == "Up"]
    downs = [t for t in w.trades if t.outcome == "Down"]
    sums: list[float] = []
    for u in ups:
        for d in downs:
            if abs(u.ts - d.ts) <= within:
                sums.append(float(u.price + d.price))
                break
    return sums


def profile(windows: list[RefWindow], rebates: list[tuple[int, Decimal]] | None = None) -> dict:
    """Aggregate profile of the account's traded windows."""
    rebates = rebates or []
    traded = [w for w in windows if w.trades]
    n = len(windows)
    if not n:
        return {"windows": 0}
    cash = [float(w.cash) for w in windows]
    sold = sum((w.sold_up + w.sold_down for w in windows), Decimal("0"))
    won = sum((w.sold_winner for w in windows if w.winner), Decimal("0"))
    sold_known = sum((w.sold_up + w.sold_down for w in windows if w.winner), Decimal("0"))
    proceeds = sum((w.proceeds for w in windows), Decimal("0"))
    fills = [w.fills for w in traded]
    offsets = [t.ts - w.start for w in traded for t in w.trades if 0 <= t.ts - w.start < WINDOW_SECONDS]
    sizes = Counter(int(t.size) if t.size == int(t.size) else "partial" for w in traded for t in w.trades)
    sums = [s for w in traded for s in pair_sums(w)]
    distinct_prices = [len({t.price for t in w.trades if t.outcome == o}) for w in traded for o in ("Up", "Down")
                       if any(t.outcome == o for t in w.trades)]
    # Daily rebate against that day's proceeds.
    day_proceeds: dict[str, Decimal] = defaultdict(Decimal)
    day_windows: dict[str, int] = Counter()
    day_cash: dict[str, Decimal] = defaultdict(Decimal)
    for w in windows:
        day = _day(w.start)
        day_proceeds[day] += w.proceeds
        day_windows[day] += 1
        day_cash[day] += w.cash
    by_day: dict[str, dict] = {}
    for ts, usdc in rebates:
        # A rebate posted on day D pays for day D-1's trading.
        paid_for = _day(ts - 86400)
        if paid_for in day_proceeds and day_proceeds[paid_for] > 0:
            by_day[paid_for] = {
                "rebate": float(usdc), "proceeds": float(day_proceeds[paid_for]), "windows": day_windows[paid_for],
                "bps": round(float(usdc / day_proceeds[paid_for]) * 10_000, 1),
                "rebate_per_window": round(float(usdc) / day_windows[paid_for], 4),
                "cash": round(float(day_cash[paid_for]), 2),
            }
    bps = [d["bps"] for d in by_day.values()]
    rebate_bps = statistics.median(bps) if bps else None
    leads = [w.lead for w in windows if w.lead is not None]
    merge_off = [w.merge_ts - w.start for w in windows if w.merge_ts is not None]
    redeem_off = [w.redeem_ts - w.start for w in windows if w.redeem_ts is not None and w.redeemed >= 1]
    lean = [float(w.lean) for w in windows]
    return {
        "windows": n,
        "traded_windows": len(traded),
        "first": _day(windows[0].start),
        "last": _day(windows[-1].start),
        "fills_per_window": round(sum(fills) / n, 2),
        "fills_per_traded_window_median": statistics.median(fills) if fills else 0,
        "fills_p90": _pct([float(f) for f in fills], 0.9),
        "shares_per_window": round(float(sold) / n, 1),
        "shares_p10": _pct([float(w.sold_up + w.sold_down) for w in windows], 0.1),
        "shares_p90": _pct([float(w.sold_up + w.sold_down) for w in windows], 0.9),
        "sold_winner_pct": round(float(won / sold_known) * 100, 1) if sold_known else None,
        "lean_median": statistics.median(lean),
        "lean_p90": _pct(lean, 0.9),
        "cash_per_window": round(sum(cash) / n, 4),
        "cash_total": round(sum(cash), 2),
        "cash_median": round(statistics.median(cash), 4),
        "cash_worst": round(min(cash), 2),
        "cash_best": round(max(cash), 2),
        "cash_p10": round(_pct(cash, 0.1), 2),
        "cash_p90": round(_pct(cash, 0.9), 2),
        "proceeds_per_window": round(float(proceeds) / n, 2),
        "rebate_bps_median": rebate_bps,
        "rebate_per_window": round(float(proceeds) / n * (rebate_bps or 0) / 10_000, 4),
        "rebate_by_day": by_day,
        "fill_offset_p10_p50_p90": [_pct([float(o) for o in offsets], q) for q in (0.1, 0.5, 0.9)],
        "fills_by_minute": {str(m): sum(1 for o in offsets if o // 60 == m) for m in range(5)},
        "clip_sizes": dict(sizes.most_common(8)),
        "pair_sum_median": round(statistics.median(sums), 3) if sums else None,
        "pair_sum_share_at_or_above_1": round(sum(1 for s in sums if s >= 0.995) / len(sums), 3) if sums else None,
        "distinct_prices_per_side_median": statistics.median(distinct_prices) if distinct_prices else 0,
        "split_lead_median": statistics.median(leads) if leads else None,
        "merge_offset_median": statistics.median(merge_off) if merge_off else None,
        "redeem_offset_median": statistics.median(redeem_off) if redeem_off else None,
        "winner_source": dict(Counter(w.winner_source or "unknown" for w in windows)),
    }


# --- alignment with the replay --------------------------------------------------------------


@dataclass
class RefFillRow:
    """One real fill of the reference account with the state inferred around it."""

    start: int
    winner: str
    ts: int
    outcome: str
    price: float
    size: float
    touch: float | None  # print-implied best ask on this outcome just before the fill
    ticks_vs_touch: float | None  # (price - touch) / tick; <= 0 means at or inside the last touch
    fair_outcome: float | None  # spot-implied P(outcome) at the fill (spot `lag` s old)
    fair_at_place: float | None  # unknown for the account; same as fair_outcome so bucket_summary works
    edge_at_place: float | None
    edge_at_fill: float | None  # price - fair_outcome
    gap_ticks: float | None  # (fair_outcome - touch) / tick, as in diagnostics.FillRow
    seconds_into_window: int
    sigma: float
    markout_10: float | None
    markout_30: float | None
    markout_60: float | None
    settlement: float
    won: bool
    visible_as_print: bool  # a public taker BUY print at this second and price exists


def _touch_for(book: ReconstructedBook, outcome: str) -> Decimal | None:
    if outcome == "Up":
        return book.up_ask
    return (Decimal("1") - book.up_bid) if book.up_bid is not None else None


def align_window(w: RefWindow, data: WindowData, *, lag: int = 1) -> list[RefFillRow]:
    """The account's fills in `w` against the public prints and spot in `data`."""
    winner = data.winner or w.winner or ""
    end = w.start + WINDOW_SECONDS
    spot_at = dict(data.spot)
    beat = data.spot_beat()
    sigma = data.realised_sigma() or sigma_per_second(StrategyConfig().fair_vol_annual)
    prints = sorted(data.prints, key=lambda p: p.ts)
    book = ReconstructedBook()
    rows: list[RefFillRow] = []
    i = 0
    for t in w.trades:
        if not 0 <= t.ts - w.start < WINDOW_SECONDS:
            continue
        # Touch from prints strictly before the fill: the fill is itself a print.
        while i < len(prints) and prints[i].ts < t.ts:
            book.update(prints[i])
            i += 1
        touch = _touch_for(book, t.outcome)
        visible = any(p.ts == t.ts and p.outcome == t.outcome and p.side == "BUY" and abs(p.price - t.price) < Decimal("0.006")
                      for p in prints[i:i + 200])
        fair_o = None
        marks: dict[int, float | None] = {h: None for h in HORIZONS}
        if beat is not None:
            s0 = spot_at.get(t.ts - lag)
            if s0 is not None:
                fair_o = _fair_outcome(t.outcome, s0, beat, end - t.ts, sigma)
                for h in HORIZONS:
                    later = min(t.ts + h, end - 1)
                    s1 = spot_at.get(later)
                    if s1 is not None:
                        marks[h] = _fair_outcome(t.outcome, s1, beat, end - later, sigma) - fair_o
        price = float(t.price)
        won = winner == t.outcome
        rows.append(RefFillRow(
            start=w.start, winner=winner, ts=t.ts, outcome=t.outcome, price=price, size=float(t.size),
            touch=float(touch) if touch is not None else None,
            ticks_vs_touch=round((price - float(touch)) / float(TICK), 2) if touch is not None else None,
            fair_outcome=fair_o, fair_at_place=fair_o,
            edge_at_place=(price - fair_o) if fair_o is not None else None,
            edge_at_fill=(price - fair_o) if fair_o is not None else None,
            gap_ticks=round((fair_o - float(touch)) / float(TICK), 6) if (fair_o is not None and touch is not None) else None,
            seconds_into_window=t.ts - w.start, sigma=sigma,
            markout_10=marks[10], markout_30=marks[30], markout_60=marks[60],
            settlement=price - (1.0 if won else 0.0), won=won, visible_as_print=visible,
        ))
    return rows


def fill_breakdowns(rows: list) -> dict[str, dict[str, dict]]:
    """Buckets shared by reference and replay rows (both expose the FillRow fields)."""
    out: dict[str, dict[str, dict]] = {}
    out["side"] = {o: bucket_summary([r for r in rows if r.outcome == o]) for o in ("Up", "Down")}
    thirds = {"0-100s": (0, 100), "100-200s": (100, 200), "200-300s": (200, 300)}
    out["window_third"] = {k: bucket_summary([r for r in rows if lo <= r.seconds_into_window < hi])
                           for k, (lo, hi) in thirds.items()}
    gaps = [r for r in rows if r.gap_ticks is not None]
    if gaps:
        out["fair_minus_touch"] = {
            "below_touch (<= -1 tick)": bucket_summary([r for r in gaps if r.gap_ticks <= -1]),
            "at_touch (-1..1)": bucket_summary([r for r in gaps if -1 < r.gap_ticks < 1]),
            "1-3 ticks above": bucket_summary([r for r in gaps if 1 <= r.gap_ticks < 3]),
            "3+ ticks above": bucket_summary([r for r in gaps if r.gap_ticks >= 3]),
        }
    if rows and hasattr(rows[0], "ticks_vs_touch"):
        tv = [r for r in rows if r.ticks_vs_touch is not None]
        out["price_minus_touch"] = {
            "inside (<= -2 ticks)": bucket_summary([r for r in tv if r.ticks_vs_touch <= -2]),
            "one tick inside": bucket_summary([r for r in tv if -2 < r.ticks_vs_touch <= -0.5]),
            "at touch": bucket_summary([r for r in tv if -0.5 < r.ticks_vs_touch < 0.5]),
            "1 tick above": bucket_summary([r for r in tv if 0.5 <= r.ticks_vs_touch < 1.5]),
            "2+ above": bucket_summary([r for r in tv if r.ticks_vs_touch >= 1.5]),
        }
    return out


def align(
    windows: list[RefWindow], cache: list[WindowData], *, lag: int = 1, queue: Decimal = Decimal("250"),
    touch_only: bool = False, strategy: StrategyConfig | None = None, seed: int = 30, replay: bool = True,
) -> dict:
    """Reference fills and the fork's replay fills on the same windows, side by side."""
    strategy = strategy or StrategyConfig(spot_lag_seconds=lag)
    by_start = {d.start: d for d in cache}
    ref_rows: list[RefFillRow] = []
    rep_rows: list = []
    per_window: list[dict] = []
    config = replay_config(strategy)
    for w in windows:
        data = by_start.get(w.start)
        if data is None:
            continue
        if data.winner in {"Up", "Down"} and w.winner != data.winner:
            w.winner, w.winner_source = data.winner, "cache"
        rows = align_window(w, data, lag=lag)
        ref_rows.extend(rows)
        entry = {
            "start": w.start, "winner": w.winner, "ref_fills": w.fills, "ref_shares": float(w.sold_up + w.sold_down),
            "ref_lean": float(w.lean), "ref_cash": float(w.cash), "ref_proceeds": float(w.proceeds),
            "ref_sold_winner": float(w.sold_winner),
        }
        if replay:
            result = replay_window(data, queue=queue, seed=seed, config=config, queue_at_touch_only=touch_only)
            if result is not None:
                rep_rows.extend(fill_rows(result))
                entry.update({
                    "rep_fills": result.fills, "rep_shares": float(result.sold_up + result.sold_down),
                    "rep_lean": float(result.lean), "rep_cash": float(result.pnl), "rep_proceeds": float(result.proceeds),
                    "rep_sold_winner": float(result.sold_winner),
                })
        per_window.append(entry)
    n = len(per_window)
    ticks = [r.ticks_vs_touch for r in ref_rows if r.ticks_vs_touch is not None]
    hist = Counter(int(round(t)) for t in ticks)

    def side(prefix: str) -> dict:
        rows = [p for p in per_window if f"{prefix}_fills" in p]
        if not rows:
            return {}
        m = len(rows)
        shares = sum(p[f"{prefix}_shares"] for p in rows)
        won = sum(p[f"{prefix}_sold_winner"] for p in rows if p["winner"])
        cash = [p[f"{prefix}_cash"] for p in rows]
        proceeds = sum(p[f"{prefix}_proceeds"] for p in rows)
        rebate = proceeds * float(strategy.maker_rebate_bps) / 10_000
        return {
            "windows": m, "fills_per_window": round(sum(p[f"{prefix}_fills"] for p in rows) / m, 2),
            "shares_per_window": round(shares / m, 1), "lean_median": statistics.median(p[f"{prefix}_lean"] for p in rows),
            "sold_winner_pct": round(won / shares * 100, 1) if shares else None,
            "cash_per_window": round(sum(cash) / m, 4), "rebate_per_window": round(rebate / m, 4),
            "cash_plus_rebate_per_window": round((sum(cash) + rebate) / m, 4),
            "worst": round(min(cash), 2), "cash_total": round(sum(cash), 2),
        }

    return {
        "windows": n, "lag": lag, "queue": str(queue), "touch_only": touch_only,
        "fair_edge": str(strategy.fair_edge), "max_lean_shares": str(strategy.max_lean_shares),
        "reference": {**side("ref"), **bucket_summary(ref_rows),
                      "ticks_vs_touch_median": statistics.median(ticks) if ticks else None,
                      "share_at_or_inside_touch": round(sum(1 for t in ticks if t <= 0.5) / len(ticks), 3) if ticks else None,
                      "share_2plus_above_touch": round(sum(1 for t in ticks if t >= 1.5) / len(ticks), 3) if ticks else None,
                      "ticks_vs_touch_hist": {str(k): hist[k] for k in sorted(hist) if -6 <= k <= 8},
                      "visible_as_public_print": round(sum(1 for r in ref_rows if r.visible_as_print) / len(ref_rows), 3) if ref_rows else None,
                      "breakdowns": fill_breakdowns(ref_rows)},
        "replay": {**side("rep"), **bucket_summary(rep_rows), "breakdowns": fill_breakdowns(rep_rows)} if replay else {},
        "per_window": per_window,
    }


# --- CLI --------------------------------------------------------------------------------------


def _print_profile(p: dict) -> None:
    if not p.get("windows"):
        print("no settled windows")
        return
    print(f"reference {ADDRESS[:10]}..  {p['windows']} settled windows {p['first']} .. {p['last']}  "
          f"({p['traded_windows']} with fills)")
    print(f"  fills/w {p['fills_per_window']}  (traded median {p['fills_per_traded_window_median']}, p90 {p['fills_p90']})  "
          f"sh/w {p['shares_per_window']} (p10 {p['shares_p10']:.0f}, p90 {p['shares_p90']:.0f})  "
          f"lean median {p['lean_median']:.0f} p90 {p['lean_p90']:.0f}  sold winner {p['sold_winner_pct']}%")
    print(f"  cash/w {p['cash_per_window']:+.4f}  median {p['cash_median']:+.4f}  p10/p90 {p['cash_p10']:+.2f}/{p['cash_p90']:+.2f}  "
          f"worst {p['cash_worst']:+.2f}  best {p['cash_best']:+.2f}  total {p['cash_total']:+.2f}")
    print(f"  proceeds/w {p['proceeds_per_window']:.2f}  rebate {p['rebate_bps_median']} bps -> {p['rebate_per_window']:+.4f}/w  "
          f"cash+rebate/w {p['cash_per_window'] + p['rebate_per_window']:+.4f}")
    for day, d in sorted(p["rebate_by_day"].items()):
        print(f"    {day}: {d['windows']} windows  cash {d['cash']:+.2f}  rebate {d['rebate']:.2f} ({d['bps']} bps, "
              f"{d['rebate_per_window']:+.3f}/w)")
    print(f"  fill offset p10/p50/p90 {p['fill_offset_p10_p50_p90']}  by minute {p['fills_by_minute']}")
    print(f"  clip sizes {p['clip_sizes']}  pair sum median {p['pair_sum_median']}  share >= 1.00 "
          f"{p['pair_sum_share_at_or_above_1']}  distinct prices per side median {p['distinct_prices_per_side_median']}")
    print(f"  split lead {p['split_lead_median']}s  merge +{p['merge_offset_median']}s  redeem +{p['redeem_offset_median']}s  "
          f"winner source {p['winner_source']}")


def _fmt(v: object) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:+.4f}" if abs(v) < 10 else f"{v:+.2f}"
    return str(v)


def _print_align(rep: dict) -> None:
    ref, rp = rep["reference"], rep.get("replay") or {}
    print(f"{rep['windows']} overlap windows  lag {rep['lag']}s  replay queue {rep['queue']}"
          f"{' touch-only' if rep['touch_only'] else ''}  fair_edge {rep['fair_edge']}  lean cap {rep['max_lean_shares']}")
    keys = ["fills_per_window", "shares_per_window", "lean_median", "sold_winner_pct", "avg_price", "edge_at_fill",
            "markout_10", "markout_30", "markout_60", "settlement_per_share", "cash_per_window", "rebate_per_window",
            "cash_plus_rebate_per_window", "worst", "cash_total"]
    print(f"  {'':<28} {'reference':>12} {'replay':>12}")
    for k in keys:
        print(f"  {k:<28} {_fmt(ref.get(k)):>12} {_fmt(rp.get(k)):>12}")
    print(f"  reference price - touch: median {ref['ticks_vs_touch_median']} ticks, at/inside touch "
          f"{ref['share_at_or_inside_touch']}, 2+ above {ref['share_2plus_above_touch']}, visible as public print "
          f"{ref['visible_as_public_print']}")
    print(f"  hist {ref['ticks_vs_touch_hist']}")
    for name in ("price_minus_touch", "fair_minus_touch", "window_third", "side"):
        for label, side in (("reference", ref), ("replay", rp)):
            buckets = (side.get("breakdowns") or {}).get(name)
            if not buckets:
                continue
            print(f"  {label} by {name}:")
            for key, b in buckets.items():
                if not b["fills"]:
                    continue
                print(f"    {key:<26} fills {b['fills']:>5} sh {b['shares']:>8} win% {b['sold_winner_pct']!s:>5} "
                      f"edge {b['edge_at_fill']!s:>8} mo10 {b['markout_10']!s:>8} mo60 {b['markout_60']!s:>8} "
                      f"settle/sh {b['settlement_per_share']!s:>8} pnl {b['pnl']:>9.2f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Reference account profile and alignment")
    parser.add_argument("--activity", default=str(DEFAULT_CACHE), help="activity cache path")
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="pull public activity into the cache")
    f.add_argument("--days", type=float, default=5)
    f.add_argument("--since", type=int, default=None, help="unix ts; overrides --days")
    f.add_argument("--until", type=int, default=None)
    p = sub.add_parser("profile", help="per-window profile of the cached activity")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--json", default="")
    p.add_argument("--windows-json", default="", help="write one row per window here")
    a = sub.add_parser("align", help="reference fills vs the replay on cached windows")
    a.add_argument("--cache", default="data/backtest")
    a.add_argument("--since", type=int, default=0)
    a.add_argument("--lag", type=int, default=1)
    a.add_argument("--queue", default="250")
    a.add_argument("--touch-only", action="store_true")
    a.add_argument("--fair-edge", default=None)
    a.add_argument("--max-lean", default=None)
    a.add_argument("--no-replay", action="store_true")
    a.add_argument("--json", default="")
    args = parser.parse_args(argv)
    cache = Path(args.activity)

    if args.command == "fetch":
        since = args.since if args.since is not None else int(time.time() - args.days * 86400)
        rows = fetch_activity(since=since, until=args.until, cache=cache)
        print(f"{len(rows)} rows cached in {cache}")
        return

    rows = load_activity(cache)
    if not rows:
        print(f"no activity cached at {cache}; run `python -m spork30.reference fetch`", file=sys.stderr)
        sys.exit(1)
    windows = [w for w in windows_from_activity(rows) if w.start >= args.since]
    if args.command == "profile":
        prof = profile(windows, rebate_rows(rows))
        _print_profile(prof)
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps(prof, indent=1, default=str), encoding="utf-8")
        if args.windows_json:
            Path(args.windows_json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.windows_json).write_text(json.dumps([w.to_json() for w in windows]), encoding="utf-8")
        return

    defaults = StrategyConfig()
    strategy = StrategyConfig(
        spot_lag_seconds=args.lag,
        fair_edge=Decimal(args.fair_edge) if args.fair_edge is not None else defaults.fair_edge,
        max_lean_shares=Decimal(args.max_lean) if args.max_lean is not None else defaults.max_lean_shares,
    )
    data = cached_windows(Path(args.cache))
    rep = align(windows, data, lag=args.lag, queue=Decimal(args.queue), touch_only=args.touch_only,
                strategy=strategy, replay=not args.no_replay)
    _print_align(rep)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
