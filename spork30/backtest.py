"""Replay the regime-B loop against public taker prints.

    python -m spork30.backtest --windows 48 --queue 0,250,500,1000

The simulation dashboard fills only the side spot is moving toward, which
makes its PnL a property of that fill model rather than a measurement. This
module drives the real `Engine` with a book reconstructed from the venue's
own trade tape and fills resting asks only when a taker print on that
outcome trades through them, after `queue` shares ahead at the same level.

What is measured: prints, sizes, prices, timing and the on-chain winner are
real. What is modelled, and stated as such in the output:

* The top of book is reconstructed from prints (a taker BUY on Up prints at
  the Up ask, a taker SELL at the Up bid, and the Down book mirrors it).
  Unfilled resting liquidity is not in the public tape, so the touch can
  lag between prints.
* Queue position is a parameter. `queue` shares at our price trade before
  our order does; the order loses its place whenever the engine replaces it.
* Our own asks do not change anyone else's behaviour.

Nothing here signs or sends. Data is public and cached under data/backtest.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from spork30.config import AppConfig, RiskConfig, StrategyConfig, TradingMode
from spork30.execution import ClipSchedule
from spork30.models import BookTop, Phase
from spork30.portfolio import Engine
from spork30.spot import fetch_open
from spork30.strategy import quantize_price, sigma_per_second

TICK = Decimal("0.01")
# Oracle lag measured on the CTF contract for these markets (pass 2).
ORACLE_LAG_SECONDS = 65


@dataclass(frozen=True)
class Print:
    ts: int
    outcome: str  # "Up" | "Down"
    side: str  # taker side, "BUY" | "SELL"
    price: Decimal
    size: Decimal


@dataclass
class WindowData:
    start: int
    slug: str
    condition: str
    winner: str | None
    prints: list[Print]
    # BTC spot, one close per second (Binance BTCUSDT 1s klines), from
    # SPOT_HISTORY_SECONDS before the window to its end. Used by the
    # spot-referenced fair value (U6); empty when not fetched.
    spot: list[tuple[int, Decimal]] = field(default_factory=list)
    # Polymarket's price to beat (TWAP-60 open) for the window.
    beat: Decimal | None = None

    def to_json(self) -> dict:
        return {
            "start": self.start,
            "slug": self.slug,
            "condition": self.condition,
            "winner": self.winner,
            "prints": [[p.ts, p.outcome, p.side, str(p.price), str(p.size)] for p in self.prints],
            "spot": [[ts, str(price)] for ts, price in self.spot],
            "beat": str(self.beat) if self.beat is not None else None,
        }

    @classmethod
    def from_json(cls, row: dict) -> "WindowData":
        return cls(
            start=int(row["start"]),
            slug=str(row["slug"]),
            condition=str(row["condition"]),
            winner=row.get("winner"),
            prints=[Print(int(p[0]), str(p[1]), str(p[2]), Decimal(p[3]), Decimal(p[4])) for p in row["prints"]],
            spot=[(int(s[0]), Decimal(str(s[1]))) for s in row.get("spot") or []],
            beat=Decimal(str(row["beat"])) if row.get("beat") is not None else None,
        )

    def spot_beat(self, seconds: int = 60) -> Decimal | None:
        """Price to beat measured on the spot series itself: its TWAP over the
        `seconds` before the window.

        Polymarket's beat is a Chainlink BTC/USD TWAP; Binance BTCUSDT trades
        at a steady basis to it (+11 bps, stdev 0.6 bps, on the Oct 11-12
        windows). That basis is 3-5x the volatility scale of the fair value,
        so spot must be compared with a beat from the same series.
        """
        closes = [price for ts, price in self.spot if self.start - seconds <= ts < self.start]
        if len(closes) < seconds // 2:
            return None
        return sum(closes, Decimal("0")) / Decimal(len(closes))

    def realised_sigma(self, seconds: int = 300) -> float | None:
        """Per-second volatility of 1-s log returns over the `seconds` before the window."""
        closes = [float(price) for ts, price in self.spot if self.start - seconds <= ts < self.start]
        if len(closes) < 30:
            return None
        returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(returns) < 2:
            return None
        return statistics.pstdev(returns)


# Seconds of spot kept before the window: the realised-vol lookback.
SPOT_HISTORY_SECONDS = 300
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


class ReconstructedBook:
    """Up-token top of book implied by the most recent prints on either token."""

    def __init__(self) -> None:
        self.up_ask: Decimal | None = None
        self.up_bid: Decimal | None = None

    def update(self, p: Print) -> None:
        price = quantize_price(p.price, TICK)
        # A taker BUY prints at the ask of its token, a taker SELL at the bid.
        # The Down book mirrors the Up book, so a Down print pins the other
        # side of the Up book.
        sets_ask = (p.outcome == "Up") == (p.side == "BUY")
        level = price if p.outcome == "Up" else quantize_price(Decimal("1") - price, TICK)
        if sets_ask:
            self.up_ask = level
            # The print is the freshest fact: a bid at or above the new ask is stale.
            if self.up_bid is not None and self.up_bid >= level:
                self.up_bid = level - TICK
        else:
            self.up_bid = level
            if self.up_ask is not None and self.up_ask <= level:
                self.up_ask = level + TICK

    def top(self, now: int) -> BookTop | None:
        if self.up_ask is None:
            return None
        ask = min(Decimal("0.99"), max(TICK, self.up_ask))
        bid = self.up_bid
        if bid is not None and bid >= ask:
            bid = ask - TICK
        return BookTop(bid=bid, ask=ask, observed_ts=now, tick=TICK)


@dataclass
class Resting:
    order_id: str
    outcome: str
    price: Decimal
    queue_ahead: Decimal
    placed_at: int


@dataclass
class WindowResult:
    start: int
    winner: str
    pnl: Decimal
    fills: int
    sold_up: Decimal
    sold_down: Decimal
    proceeds: Decimal
    places: int
    phase: str
    rest_seconds: int = 0  # summed lifetime of every resting order
    used_fair: bool = False
    sigma: float = 0.0

    @property
    def sold_winner(self) -> Decimal:
        return self.sold_up if self.winner == "Up" else self.sold_down

    @property
    def sold_loser(self) -> Decimal:
        return self.sold_down if self.winner == "Up" else self.sold_up

    @property
    def lean(self) -> Decimal:
        return abs(self.sold_up - self.sold_down)


class ReplayEngine(Engine):
    """Engine that keeps its decision log in memory."""

    def _log(self, record: dict) -> None:
        self.decisions.append(record)


def replay_config(strategy: StrategyConfig | None = None) -> AppConfig:
    return AppConfig(
        trading_mode=TradingMode.SIMULATION,
        strategy=strategy or StrategyConfig(),
        risk=RiskConfig(),
        state_path="data/backtest/unused-state.json",
        decision_log_path="data/backtest/unused-decisions.jsonl",
        emergency_stop_path="data/backtest/no-such-file",
    )


def replay_window(
    data: WindowData,
    *,
    queue: Decimal,
    maker_fee_bps: Decimal = Decimal("0"),
    seed: int = 30,
    config: AppConfig | None = None,
    realised_vol: bool = True,
    queue_at_touch_only: bool = False,
) -> WindowResult | None:
    """Run one window through the real engine with print-driven fills."""
    if data.winner not in {"Up", "Down"}:
        return None
    config = config or replay_config()
    strategy = config.strategy
    start = data.start
    window = strategy.window_seconds
    engine = ReplayEngine(config=config, clips=ClipSchedule(strategy, seed=seed))
    book = ReconstructedBook()
    by_ts: dict[int, list[Print]] = {}
    first_tick = start - strategy.split_lead_seconds
    for p in data.prints:
        if p.ts < first_tick:
            book.update(p)
        else:
            by_ts.setdefault(p.ts, []).append(p)
    resting: dict[str, Resting] = {}
    fills = 0
    sold = {"Up": Decimal("0"), "Down": Decimal("0")}
    proceeds = Decimal("0")
    places = 0
    rest_seconds = 0
    slug = data.slug

    def retire(order_id: str, now: int) -> None:
        nonlocal rest_seconds
        gone = resting.pop(order_id, None)
        if gone is not None:
            rest_seconds += now - gone.placed_at

    # Spot-referenced fair value (U6): the engine sees the spot close from
    # `spot_lag_seconds` ago (the kline for second t closes at t+1, so lag 1
    # is the freshest print that was knowable at t). Realised 1-s vol over
    # the five minutes before the window, else the configured fallback.
    spot_at: dict[int, Decimal] = dict(data.spot)
    beat = data.spot_beat()
    use_fair = strategy.fair_edge > 0 and beat is not None
    sigma = sigma_per_second(strategy.fair_vol_annual)
    if use_fair and realised_vol:
        sigma = data.realised_sigma() or sigma
    lag = max(1, strategy.spot_lag_seconds)
    last_spot: Decimal | None = None

    last_tick = start + max(strategy.redeem_delay_seconds, strategy.flatten_delay_seconds) + 1
    for now in range(first_tick, last_tick + 1):
        quoting = start <= now < start + window
        resolved = now >= start + window + ORACLE_LAG_SECONDS
        if use_fair:
            last_spot = spot_at.get(now - lag, last_spot)
        actions = engine.step(
            now=now,
            book=book.top(now) if quoting else None,
            last_up_trade=None,
            resolved=resolved,
            winner=data.winner if resolved else None,
            emergency_stop=False,
            spot=last_spot if use_fair else None,
            beat=beat if use_fair else None,
            sigma=sigma,
        )
        market = engine.markets.get(slug)
        for action in actions:
            if action.slug != slug:
                continue
            if action.kind == "PLACE":
                places += 1
                detail = action.detail
                price = Decimal(detail["price"])
                # `queue` shares are ahead of us when we join the touch. With
                # queue_at_touch_only an ask resting above the touch (where
                # fair + edge put it) starts a fresh level and has none ahead;
                # the uniform model charges the queue at every level.
                top = book.top(now)
                touch = None
                if top is not None:
                    touch = top.ask if detail["outcome"] == "Up" else (Decimal("1") - top.bid if top.bid is not None else None)
                ahead = Decimal("0") if (queue_at_touch_only and touch is not None and price > touch) else queue
                resting[detail["order_id"]] = Resting(detail["order_id"], detail["outcome"], price, ahead, now)
            elif action.kind == "CANCEL":
                retire(action.detail.get("order_id", ""), now)
        if market is not None:
            for order_id in list(resting):
                order = market.orders.get(order_id)
                if order is None or not order.live:
                    retire(order_id, now)
        for p in by_ts.get(now, []):
            book.update(p)
            if not quoting or p.side != "BUY" or market is None:
                continue
            size_left = p.size
            # Price-time priority: the cheapest of our asks at or under the
            # print price fills first.
            eligible = sorted(
                (r for r in resting.values() if r.outcome == p.outcome and r.price <= p.price),
                key=lambda r: r.price,
            )
            for r in eligible:
                if size_left <= 0:
                    break
                ahead = min(size_left, r.queue_ahead)
                r.queue_ahead -= ahead
                size_left -= ahead
                if size_left <= 0:
                    break
                order = market.orders[r.order_id]
                fill = min(size_left, order.remaining)
                if fill <= 0:
                    continue
                engine.apply_fill(r.order_id, fill, r.price, ts=p.ts)
                fills += 1
                sold[r.outcome] += fill
                proceeds += fill * r.price
                size_left -= fill
                if not order.live:
                    retire(r.order_id, p.ts)
    for order_id in list(resting):
        retire(order_id, start + window)
    market = engine.markets.get(slug)
    if market is None or not market.split_submitted:
        return None
    pnl = market.cash - proceeds * maker_fee_bps / Decimal("10000")
    return WindowResult(
        start=start,
        winner=data.winner,
        pnl=pnl,
        fills=fills,
        sold_up=sold["Up"],
        sold_down=sold["Down"],
        proceeds=proceeds,
        places=places,
        phase=market.phase.value,
        rest_seconds=rest_seconds,
        used_fair=use_fair,
        sigma=sigma,
    )


@dataclass
class Summary:
    queue: Decimal
    offset: int = 0
    label: str = ""
    rebate_bps: Decimal = Decimal("0")
    results: list[WindowResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        pnls = [float(r.pnl) for r in self.results]
        n = len(pnls)
        mean = statistics.fmean(pnls) if n else 0.0
        stdev = statistics.stdev(pnls) if n > 1 else 0.0
        sold_w = sum((r.sold_winner for r in self.results), Decimal("0"))
        sold_l = sum((r.sold_loser for r in self.results), Decimal("0"))
        sold = sold_w + sold_l
        places = sum(r.places for r in self.results)
        proceeds = sum((r.proceeds for r in self.results), Decimal("0"))
        # Maker rebate estimate: measured at 42 bps of sold notional on the
        # reference account's daily payments. Not paid through the CLOB, so
        # the replay cannot observe it; it is reported separately.
        rebate = float(proceeds * self.rebate_bps / Decimal("10000")) / n if n else 0.0
        return {
            "queue": str(self.queue),
            "offset": self.offset,
            "label": self.label,
            "windows": n,
            "windows_with_fills": sum(1 for r in self.results if r.fills),
            "fills": sum(r.fills for r in self.results),
            "shares_sold": float(round(sold, 1)),
            "shares_per_window": float(sold) / n if n else 0.0,
            "sold_winner_pct": float(sold_w / sold * 100) if sold else 0.0,
            "lean_median": float(statistics.median(float(r.lean) for r in self.results)) if n else 0.0,
            "mean_rest_seconds": (sum(r.rest_seconds for r in self.results) / places) if places else 0.0,
            "mean_pnl": mean,
            "median_pnl": statistics.median(pnls) if n else 0.0,
            "stdev_pnl": stdev,
            "total_pnl": sum(pnls),
            "worst": min(pnls) if n else 0.0,
            "best": max(pnls) if n else 0.0,
            "t_stat": (mean / (stdev / n**0.5)) if n > 1 and stdev > 0 else 0.0,
            "places_per_window": (sum(r.places for r in self.results) / n) if n else 0.0,
            "rebate_per_window": rebate,
            "mean_pnl_with_rebate": mean + rebate,
            "unsettled": sum(1 for r in self.results if r.phase != Phase.CLOSED.value),
            "fair_windows": sum(1 for r in self.results if r.used_fair),
        }


def variant_label(strategy: StrategyConfig) -> str:
    if strategy.fair_edge > 0:
        return (f"fair edge={strategy.fair_edge} cut={int(strategy.fair_undercut)} lag={strategy.spot_lag_seconds} "
                f"lean={strategy.max_lean_shares} min={strategy.requote_min_seconds}s")
    return (f"off={strategy.ask_offset_ticks} thr={strategy.requote_threshold_ticks} "
            f"min={strategy.requote_min_seconds}s lean={strategy.max_lean_shares} imp={strategy.improve_spread_ticks}")


def summarize(
    windows: list[WindowData],
    queues: list[Decimal],
    *,
    maker_fee_bps: Decimal,
    seed: int,
    offsets: list[int] | None = None,
    variants: list[StrategyConfig] | None = None,
    rebate_bps: Decimal | None = None,
    realised_vol: bool = True,
    queue_at_touch_only: bool = False,
) -> list[Summary]:
    """One Summary per (strategy variant, queue).

    `variants` are full StrategyConfigs (the U1/U2/U6 knobs plus the offset);
    `offsets` is the older shorthand for offset-only sweeps.
    """
    if variants is None:
        variants = [StrategyConfig(ask_offset_ticks=offset) for offset in (offsets or [0])]
    out: list[Summary] = []
    for strategy in variants:
        config = replay_config(strategy)
        bps = strategy.maker_rebate_bps if rebate_bps is None else rebate_bps
        for q in queues:
            summary = Summary(queue=q, offset=strategy.ask_offset_ticks, label=variant_label(strategy), rebate_bps=bps)
            for data in windows:
                result = replay_window(data, queue=q, maker_fee_bps=maker_fee_bps, seed=seed, config=config,
                                       realised_vol=realised_vol, queue_at_touch_only=queue_at_touch_only)
                if result is not None:
                    summary.results.append(result)
            out.append(summary)
    return out


# --- data -------------------------------------------------------------------------


def fetch_spot(start: int, window_seconds: int = 300, history: int = SPOT_HISTORY_SECONDS) -> list[tuple[int, Decimal]]:
    """Binance BTCUSDT 1-second closes from `history` s before the window to its end.

    Public, unauthenticated. One request covers the ~600 s needed (limit 1000).
    """
    first = start - history
    last = start + window_seconds
    query = urllib.parse.urlencode({
        "symbol": "BTCUSDT", "interval": "1s", "startTime": first * 1000, "endTime": last * 1000 - 1, "limit": 1000,
    })
    request = urllib.request.Request(f"{BINANCE_KLINES}?{query}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
        rows = json.loads(response.read().decode("utf-8"))
    return [(int(row[0]) // 1000, Decimal(str(row[4]))) for row in rows]


def ensure_spot(data: WindowData, cache_dir: Path) -> WindowData:
    """Add the spot series and price to beat to a cached window that lacks them."""
    changed = False
    if not data.spot:
        data.spot = fetch_spot(data.start)
        changed = bool(data.spot)
    if data.beat is None:
        beat, final = fetch_open(data.start)
        if beat is not None and final:
            data.beat = beat
            changed = True
    if changed:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{data.slug}.json").write_text(json.dumps(data.to_json()), encoding="utf-8")
    return data


def fetch_window(client, start: int, cache_dir: Path, *, spot: bool = False) -> WindowData | None:
    """Public prints and the settled winner for one window, cached on disk.

    With `spot`, the Binance 1-s series and the price to beat are fetched too.
    """
    slug = f"btc-updown-5m-{start}"
    cached = cache_dir / f"{slug}.json"
    if cached.exists():
        try:
            data = WindowData.from_json(json.loads(cached.read_text(encoding="utf-8")))
            if data.winner is not None:
                return ensure_spot(data, cache_dir) if spot else data
        except (ValueError, KeyError):
            pass
    market = None
    try:
        market = client.get_market(slug=slug)
    except Exception:
        for page in client.list_markets(slug=slug, closed=True, page_size=1):
            items = getattr(page, "items", None)
            market = items[0] if items else None
            break
    if market is None:
        return None
    labels = {str(market.outcomes.yes.token_id): str(market.outcomes.yes.label),
              str(market.outcomes.no.token_id): str(market.outcomes.no.label)}
    winner = None
    for leg in (market.outcomes.yes, market.outcomes.no):
        if Decimal(str(leg.price)) >= Decimal("0.99"):
            winner = str(leg.label)
    prints: list[Print] = []
    for page in client.list_trades(condition_id=market.condition_id, taker_only=True, page_size=500):
        for t in page.items:
            outcome = labels.get(str(t.asset_id))
            if outcome not in {"Up", "Down"}:
                continue
            side = t.side.value if hasattr(t.side, "value") else str(t.side)
            prints.append(Print(int(t.timestamp.timestamp()), outcome, side.upper(), Decimal(str(t.price)), Decimal(str(t.size))))
        if len(prints) > 50_000:
            break
    prints.sort(key=lambda p: p.ts)
    data = WindowData(start=start, slug=slug, condition=str(market.condition_id), winner=winner, prints=prints)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data.to_json()), encoding="utf-8")
    return ensure_spot(data, cache_dir) if spot else data


def load_windows(
    count: int, *, end: int | None, cache_dir: Path, quiet: bool = False, spot: bool = False
) -> list[WindowData]:
    from polymarket import PublicClient

    client = PublicClient()
    now = int(time.time())
    # Latest window that is finished and (at ~+600 s) marked closed on gamma.
    last = end if end is not None else now - (now % 300) - 900
    windows: list[WindowData] = []
    start = last
    tried = 0
    while len(windows) < count and tried < count * 3:
        tried += 1
        try:
            data = fetch_window(client, start, cache_dir, spot=spot)
        except Exception as exc:
            if not quiet:
                print(f"{start}: {type(exc).__name__}: {exc}", file=sys.stderr)
            data = None
        if data is not None and data.winner is not None and data.prints:
            windows.append(data)
        elif not quiet:
            print(f"{start}: skipped (no winner or no prints)", file=sys.stderr)
        start -= 300
    windows.sort(key=lambda w: w.start)
    return windows


def _print_report(summaries: list[Summary], windows: list[WindowData]) -> None:
    if windows:
        first = time.strftime("%Y-%m-%d %H:%M", time.gmtime(windows[0].start))
        last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(windows[-1].start))
        prints = sum(len(w.prints) for w in windows)
        print(f"{len(windows)} windows {first} .. {last} UTC, {prints} taker prints")
    print(f"{'variant':<40} {'queue':>5} {'win':>4} {'fills':>6} {'sh/w':>6} {'win%':>5} {'lean':>5} "
          f"{'mean':>7} {'median':>7} {'stdev':>6} {'worst':>7} {'t':>5} {'pl/w':>5} {'rest':>5} {'reb/w':>5} {'m+reb':>6}")
    for s in summaries:
        d = s.as_dict()
        print(f"{d['label']:<40} {d['queue']:>5} {d['windows']:>4} {d['fills']:>6} {d['shares_per_window']:>6.0f} "
              f"{d['sold_winner_pct']:>5.1f} {d['lean_median']:>5.0f} {d['mean_pnl']:>7.2f} {d['median_pnl']:>7.2f} "
              f"{d['stdev_pnl']:>6.2f} {d['worst']:>7.2f} {d['t_stat']:>5.2f} {d['places_per_window']:>5.1f} "
              f"{d['mean_rest_seconds']:>5.1f} {d['rebate_per_window']:>5.2f} {d['mean_pnl_with_rebate']:>6.2f}")
    print("off = ticks behind the touch; thr/min = requote threshold ticks / minimum order age; lean = pair-balance cap "
          "(median |sold Up - sold Down| shown); imp = inside-the-spread from this many ticks of spread.")
    print("fair = asks at spot-implied P(Up) + edge (Binance 1-s spot vs price to beat, realised vol); cut = may undercut "
          "the best ask; lag = seconds of spot latency assumed.")
    print("win% = share of sold shares that were the eventual winner (50 is neutral); sh/w = shares sold per window; "
          "pl/w = orders placed per window; rest = mean seconds an order rested; reb/w = rebate estimate per window.")
    print("Reference account, Oct 5-12 (2,012 windows): 180 sh/w, 48.9% win, lean 6, mean +0.29 cash, +0.38 rebate.")
    print("Modelled: queue position, print-implied top of book, no market impact. Measured: prints, winner, timing.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Replay the maker loop against public taker prints")
    parser.add_argument("--windows", type=int, default=24, help="how many finished windows to replay")
    parser.add_argument("--end", type=int, default=None, help="window start (unix, 300-aligned) to count back from")
    parser.add_argument("--queue", default="0,250,500,1000", help="shares ahead of us at our price, comma list")
    parser.add_argument("--offset", default="0", help="ticks behind the touch to rest, comma list (0 = join the ask)")
    defaults = StrategyConfig()
    parser.add_argument("--threshold", default=str(defaults.requote_threshold_ticks),
                        help="requote only when the touch moved this many ticks, comma list (0 = every change)")
    parser.add_argument("--min-seconds", default=str(defaults.requote_min_seconds),
                        help="never replace an order younger than this, comma list")
    parser.add_argument("--max-lean", default=str(defaults.max_lean_shares),
                        help="pair-balance cap in shares, comma list (0 = off)")
    parser.add_argument("--improve", default=str(defaults.improve_spread_ticks),
                        help="step inside the touch when the spread is at least this many ticks, comma list (0 = off)")
    parser.add_argument("--rebate-bps", default=str(defaults.maker_rebate_bps), help="maker rebate estimate on sold notional")
    parser.add_argument("--fair-edge", default=str(defaults.fair_edge),
                        help="U6: price asks at spot-implied fair + this edge, comma list (0 = off, quote off the touch)")
    parser.add_argument("--undercut", default="1" if defaults.fair_undercut else "0",
                        help="U6: let the fair ask undercut the best ask (0/1, comma list)")
    parser.add_argument("--lag", default=str(defaults.spot_lag_seconds), help="U6: seconds the spot print lags, comma list")
    parser.add_argument("--vol", default="rv",
                        help="U6: 'rv' = realised 1-s vol over the prior 300 s, or a fixed annualised vol (e.g. 0.45)")
    parser.add_argument("--queue-touch-only", action="store_true",
                        help="charge the queue only when joining the touch; an ask above it has nobody ahead")
    parser.add_argument("--maker-fee-bps", default="0", help="fee on proceeds; these markets charge takers only")
    parser.add_argument("--seed", type=int, default=30)
    parser.add_argument("--cache", default="data/backtest", help="where prints are cached")
    parser.add_argument("--json", default="", help="also write the summary table to this path")
    args = parser.parse_args(argv)
    cache_dir = Path(args.cache)

    def ints(text: str) -> list[int]:
        return [int(x.strip()) for x in text.split(",") if x.strip()]

    def decimals(text: str) -> list[Decimal]:
        return [Decimal(x.strip()) for x in text.split(",") if x.strip()]

    edges = decimals(args.fair_edge)
    need_spot = any(edge > 0 for edge in edges)
    windows = load_windows(args.windows, end=args.end, cache_dir=cache_dir, spot=need_spot)
    if not windows:
        print("no windows with prints and a settled winner were found", file=sys.stderr)
        sys.exit(1)

    realised = args.vol.strip().lower() == "rv"
    vol = defaults.fair_vol_annual if realised else Decimal(args.vol)
    queues = decimals(args.queue)
    variants = [
        StrategyConfig(ask_offset_ticks=off, requote_threshold_ticks=thr, requote_min_seconds=mins,
                       max_lean_shares=lean, improve_spread_ticks=imp, maker_rebate_bps=Decimal(args.rebate_bps),
                       fair_edge=edge, fair_undercut=bool(cut), spot_lag_seconds=lag, fair_vol_annual=vol)
        for off in ints(args.offset)
        for thr in ints(args.threshold)
        for mins in ints(args.min_seconds)
        for lean in decimals(args.max_lean)
        for imp in ints(args.improve)
        for edge in edges
        for cut in (ints(args.undercut) if edge > 0 else [0])
        for lag in (ints(args.lag) if edge > 0 else [defaults.spot_lag_seconds])
    ]
    summaries = summarize(windows, queues, maker_fee_bps=Decimal(args.maker_fee_bps), seed=args.seed, variants=variants,
                          realised_vol=realised, queue_at_touch_only=args.queue_touch_only)
    if args.queue_touch_only:
        print("queue model: at the touch only (asks resting above it start with nobody ahead)")
    _print_report(summaries, windows)
    if args.json:
        payload = {
            "windows": [w.start for w in windows],
            "summaries": [s.as_dict() for s in summaries],
            "per_window": {
                f"{s.label},queue={s.queue}": [
                    {"start": r.start, "winner": r.winner, "pnl": float(r.pnl), "fills": r.fills,
                     "sold_up": str(r.sold_up), "sold_down": str(r.sold_down), "places": r.places}
                    for r in s.results
                ]
                for s in summaries
            },
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
