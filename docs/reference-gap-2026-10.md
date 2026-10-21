# Reference account gap audit, October 2026

Scope: the bot is a fork of the Polymarket maker account
`0x1dd2a69e73ba444ecd5d87f0073d51a670ad51c2`, which shows +$2.25K for October
(trading +$658, maker rebate +$1.60K, 93% winning days) while the fork's
simulation and replay are flat to negative. This audit measures the account's
own profile from its public activity, aligns its fills with the fork's inputs
on the same windows, finds what the fork's simulation and feed got wrong, and
states what ships and what does not.

Tools: `spork30/reference.py` (fetch, profile, align), `spork30/audit.py`
(`--rebate-bps`, reference rows, `calibrate`). Data: 77,595 activity rows
Oct 5-16 under `data/reference/`; the frozen replay dataset of 384 windows
Oct 11-13 (`data/backtest/audit-dataset.json`). Nothing here sends orders;
the only live checks were read-only.

## 1. The reference account

3,108 settled windows, Oct 5-16 (`python -m spork30.reference profile`):

| | reference | fork replay (q250, lag 1, edge 0.03, lean 3) |
|---|---|---|
| fills per window | 21.3 (median 19, p90 40) | 4.2 |
| shares sold per window | 154 (p10 46, p90 282) | 31 |
| lean (unpaired sold) | median 6, p90 15 | cap 3 |
| sold-winner share | 48.6% | 61.0% |
| fill price minus spot-fair at fill | −0.0004 | +0.0365 |
| fair markout 10 / 30 / 60 s | +0.0001 / 0.0000 / +0.0022 | +0.0135 / +0.0099 / +0.0135 |
| settlement per sold share | +0.0021 | −0.0054 |
| cash per window | +0.29 (median +0.45, worst −40.3) | −0.17 |
| rebate per window (43 bps of proceeds) | +0.32 | +0.08 |
| cash + rebate per window | +0.61 | −0.09 |

(The account redeems both residuals; only the winner's redeem pays. The
first cut of `reference.py` took the larger redeem as the winner and was
wrong on 63 windows where it redeemed a loser residual above one share;
fixed, sold-winner moved from 47.7% to 48.6%. The alignment tables below
take the winner from the cached replay windows and were not affected.)

Mechanics match the fork: mint 750/750 at lead 293 s, clips 5/10/15/20,
merge at +358 s, redeem the unpaired winner at +366 s, pair sum median 1.00.
Its daily rebate is 42-43 bps of proceeds (64.8 bps on Oct 5), posted the day
after the trading it pays for. Rebates fell on Oct 13-14 (61 and 55 USDC
against 87-130 before) and Oct 13 was a cash-negative day (−33), the first
since Oct 10.

Where its fills sit (384 overlap windows, touch rebuilt from prints strictly
before the fill, `python -m spork30.reference align --queue 250 --lag 1`):

| fill price vs print touch | fills | shares | sold-winner | settle/sh | pnl |
|---|---|---|---|---|---|
| 2+ ticks inside | 3,078 | 21,507 | 43.1% | +0.0004 | +8 |
| 1 tick inside | 2,236 | 14,380 | 46.1% | +0.0095 | +137 |
| at the touch | 1,929 | 11,647 | 49.7% | +0.0083 | +97 |
| 1 tick above | 522 | 3,579 | 59.8% | −0.0069 | −25 |
| 2+ ticks above | 1,270 | 7,940 | 60.4% | −0.0117 | −93 |

80% of its fills are at or inside the print-implied touch; 87% are visible
as public prints. Its fills carry no information against the spot-fair (edge
−0.0004, markouts at zero): it is a volume maker whose income is the rebate
plus a small settlement edge from selling the touch. Its only losing bucket
is the 14% of fills that rest 2+ ticks above the touch and fill at 60%
sold-winner; that bucket is where the fork lives.

The same table by spot-fair minus touch answers the plan's key question
(does the Binance fair know more than the market?):

| fair − touch | reference: sold-winner, settle/sh, pnl | fork replay: sold-winner, settle/sh, pnl |
|---|---|---|
| fair below touch | 40.1%, −0.0035, −106 | 42.6%, +0.0089, +53 |
| at the touch (±1) | 47.1%, +0.0106, +52 | 77.0%, −0.0014, −2 |
| fair 1-3 ticks above | 62.9%, −0.0254, −121 | 73.9%, −0.0063, −6 |
| fair 3+ ticks above | 58.2%, +0.0154, +298 | 82.1%, −0.0327, −110 |

When the Binance fair sits well above the market, the reference sells the
touch (12 cents under fair) and makes +298; the fork rests at fair + 0.03
behind the market and sells the winner 82% of the time for −110. The market
is right and the fair is noise there: the market tracks Chainlink, which
sits ~110 USD (12 bps) above Binance with ±20 USD of noise, and with little
margin left near the window end that noise is the whole fair value.

## 2. Gap decomposition

Per window, reference +0.61 vs fork replay −0.09, both before fees:

1. Volume: 154 vs 31 shares; rebate +0.32 vs +0.08 (−0.24).
2. Price level: the fork sells at fair + 0.03 above the market (edge +0.037
   per share vs −0.0004); it does not sell more for it, it sells less, and
   what it sells is the leading edge of moves.
3. Adverse selection: sold-winner 61% vs 48%; settlement −0.0054/sh vs
   +0.0021 (−0.17 per window at the fork's own volume, −0.9 at the
   reference's).
4. Downside: reference worst window −40 on 3,080 (−18 on the 384 overlap),
   fork −13 on 384. Both run the same lean cap mechanics; the reference's cap
   is about twice as loose (median lean 6, p90 15 vs cap 3).

## 3. Simulation and execution defects found

### 3.1 Spot staleness pulled the quotes 42 times per window

The simulation showed 2,390 `no_spot_reference` pulls and 1,395
`MAX_PRICE_DEVIATION` refusals over 57 cycles, with a live order on only 82%
of ticks. Measured on this machine (Oct 15): clock skew vs Binance 4 ms,
websocket latency 0.14 s; the staleness was not the network. BTCUSDT goes 2-3
s without a trade in quiet seconds (p99 inter-trade gap 3.1 s), so a trade
stream alone reports stale spot, the engine cancelled both asks, and re-posted
them at the back of the queue when the next trade arrived.

Fixed: `SpotFeed` subscribes to the combined `trade` + `bookTicker` streams
(the mid of the book ticker is the current price in tradeless seconds); spot
older than 1.5 s went from 10.7% of ticks to 0%. `SPOT_STALE_GRACE_SECONDS`
(default 2) keeps resting asks through a shorter outage instead of pulling on
the first stale tick (`QUOTES_HELD`, counted in health). A 106-second live
check after the fix: quoted 105 s, 0 refusals, 1 pull.

### 3.2 The simulation's touch was minutes old

The simulation rebuilt its top of book from the public print tape. On Oct 15
the data-api indexed prints 15 s to over 2 min late: in one window no new
print arrived for 105 s while the real Up ask went 0.37 to 0.84. A quote
priced off that book is priced off a market that no longer exists; this is
the source of the "9 ticks above the touch" median and most of the
`MAX_PRICE_DEVIATION` refusals. Checks that ruled out the other explanations:
Binance beat minus Polymarket's price-to-beat is 110.6 USD, equal to the
spot basis (110.9), so the beat is not biased; Binance fair vs the real ask
is ±5.5 ticks at p10/p90, which is the Chainlink noise described above, not a
feed defect.

Fixed: `simulate.BookPoller` reads the live window's real CLOB book once a
second (the same `get_order_book` call the live gateway uses) and the engine
quotes from it when the snapshot is ≤ 3 s old; prints are kept for fills and
stand in for the touch only when the poll is down. Health reports
`book_source` and `book_age_s`; the screen shows them. The replay is not
affected (it has no indexer lag) and its 48-window fingerprint is unchanged
(`20833825200925a2`).

### 3.3 Replaced asks never filled in the simulation

With a live touch the engine replaces a touch quote every 1-3 s in a moving
market (24 distinct prices per side in one window; the reference's median is
8). The fill model dropped a cancelled ask at once, so a print stamped while
it rested but indexed after the replace could not fill it; the simulation
cancelled almost every ask before the print that filled it arrived and filled
nothing (56 places, 0 fills in 160 s on Oct 15).

Fixed: `FillModel` keeps retired asks matchable for prints stamped inside
their resting interval (`late_fills`, booked through `apply_fill(late=True)`
the way a live fill behind a cancel is, capped at inventory not promised to
the replacement). Replay delivers prints in time order so this path never
runs there; fingerprint unchanged.

### 3.4 Queue calibration

`python -m spork30.audit calibrate` replays a reference-style quoter (touch,
lean cap 6) on the 384 overlap windows and compares its volume with the
account's actual tape on the same windows:

| fill model | fills/w | sh/w | cash/w | sold-winner |
|---|---|---|---|---|
| uniform q0 | 122.0 | 741 | −15.9 | 50.2% |
| uniform q100 | 39.9 | 282 | −10.6 | 50.6% |
| uniform q150 | 28.0 | 201 | −8.6 | 50.9% |
| **uniform q200** | **20.7** | **150** | −7.0 | 51.6% |
| uniform q250 | 15.7 | 116 | −5.5 | 52.0% |
| reference (actual tape) | 23.5 | 154 | +0.32 | 47.7% |

Touch-only queueing is identical to uniform for a touch quoter (it never
rests above the touch). `SIM_QUEUE_AHEAD` now defaults to 200, the first
measured anchor the fill model has had; the replay tables keep 250 and 100.

The cash column is the finding the plan asked to settle: the replay cannot
price touch quoting. Its touch is the last taker-buy price, so a replayed
touch quoter rests at a stale level and the fill rule gives it away to the
next print at a higher price (mean sale 0.47 for shares that win 52%). The
account quotes the real book, sells at fair, and earns the rebate on the same
volume. The replay is still valid for what it was built for (asks priced off
spot, where the touch is only a floor) and it stays the gate for those knobs.

## 4. Pricing grid and requote check

`python -m spork30.audit experiments --hypothesis pricing` (objective cash +
42 bps rebate, train 230 / test 154, both fill models): no candidate passes.
"edge 0.03, gap<1, lean 6" is chosen on train (−0.30 vs baseline −0.22) and on
test improves +0.44/w at q250 (CI [−0.18, +1.01], includes zero) and +0.58/w
at touch-only q100 (CI [+0.06, +1.10]) with a worse worst window in both;
rejected by the rule. Touch quoting (edge 0) replays at −5 to −12.5/w for the
reason in §3.4 and is not judged by this table. Undercutting (`FAIR_UNDERCUT`)
loses in every row.

Requote path (R3): `should_requote` with both knobs at 0 replaces on every
price change, and each replace restarts the queue. Against the real book that
is 40-80 replaces per side per window in a moving market against the
reference's ~8 distinct prices. `REQUOTE_MIN_SECONDS` / `REQUOTE_THRESHOLD_TICKS`
exist for this; they cannot be judged in the replay (§3.4) and are left at 0.
A second resting level (`LADDER_LEVELS`) was not added: 11% of the reference's
same-second fills are at two prices, which is consistent with a ladder, but
nothing here can measure one.

## 5. What ships

- `SpotFeed` on `trade` + `bookTicker`; `SPOT_STALE_GRACE_SECONDS=2` (live
  and simulation behaviour; measured, §3.1).
- Simulation on the real CLOB book, late fills on replaced asks,
  `SIM_QUEUE_AHEAD=200`, quoted-seconds / refusals / pulls / holds / places /
  fills on the health pane (simulation only, §3.2-3.4).
- `spork30/reference.py`; `audit.py` scores cash + rebate, prints the
  reference row, `calibrate`; tests for all of it (`tests/test_reference.py`).
- Pricing defaults unchanged: `FAIR_EDGE=0.03`, `MAX_LEAN_SHARES=3`, requote
  knobs 0. Replay fingerprint with knobs at 0: `20833825200925a2`.

## 6. What does not ship, and how to decide it

The evidence says the account's income is touch-quoting volume plus the
rebate, and that the fork's fair + 0.03 pricing is the gap. It is not shipped
as a default because the only out-of-sample gate in this repo (the replay)
cannot evaluate a touch quoter, and the live preflight still refuses
`FAIR_EDGE=0` without `ALLOW_TOUCH_QUOTING=YES`.

The gate for it is now the simulation itself, which quotes the real book:

```bash
FAIR_EDGE=0 MAX_LEAN_SHARES=6 BOT_DATA_DIR=data/backtest/refsim python -m spork30.dashboard
```

run for a day, then `python -m spork30.reference fetch --days 2` and compare
per window with the account on the same windows (fills/w, sh/w, sold-winner,
cash, rebate). Acceptance: sold-winner within 2 points of the account's, cash
per window not below zero on the paired difference, and volume within 30% of
it; then `REQUOTE_MIN_SECONDS` / `REQUOTE_THRESHOLD_TICKS` by the same
comparison.

First data point, Oct 15 21:50-22:40 UTC, ten windows
(`data/backtest/refsim*/`, gitignored), the account on the same windows
fetched afterwards:

| | reference (same 10 windows) | sim: edge 0, lean 6, requote 0/0 | sim: edge 0, lean 6, requote 15 s / 2 ticks |
|---|---|---|---|
| places per window | ~8 distinct prices per side | 193 | 22 |
| fills per window | 14 | 3.8 | 6.2 |
| shares per window | 112 | 22 | 32 |
| cash per window | +2.91 | −0.68 | −1.99 |

Ten windows decide nothing (the account's own per-window p10/p90 is −5/+5),
but two things in the run are structural and show only against a real book:

- With requote knobs at 0 the engine replaced each ask every 1-2 s (up to
  380 places in a window) and the queue never matured; the reference
  reprices about 8 times per side. `REQUOTE_MIN_SECONDS` /
  `REQUOTE_THRESHOLD_TICKS` are the knobs; 15 s / 2 ticks tripled the
  volume in this sample. Their value has to come from this comparison, run
  longer.
- Two risk guards fire routinely on a real book and did not on the stale
  one: `WOULD_CROSS` on the Down ask once the Up ask is 0.01 (the window is
  decided; ~30 refusals per window, harmless but they inflate the refusals
  counter), and `MAX_PRICE_DEVIATION` (`max_price_jump` 0.50) when the Up
  ask moves more than 0.50 in one second as BTC crosses the beat near the
  end, which halts the market for the rest of the window (one of ten). Both
  are risk settings outside the measured policy and are left as they are;
  the second is worth a look before a touch quoter goes live.

## 7. Oct 16: where the simulation's loss sits, and the knobs added for it

`python -m spork30.reference simcompare` on the default simulation
(`data/runtime/sim`, 203 cycles over 17 h, fair edge 0.03, lean cap 3,
requote 0/0), against the account's own tape on 2,811 windows:

| | simulation | reference |
|---|---|---|
| fills / shares per window | 1.2 / 9.3 | 21 / 154 |
| one-sided windows (of traded) | 104 of 131 (79%) | 24 of 2,811 |
| matched-pair PnL | +1.32 | −2,453 |
| leftover (unmatched lean) PnL | −31.80 | +3,487 |
| sold winner | 44% | 48.6% |
| late fills (cancelled ask hit by an earlier print) | 181 of 238, −36.78 | – |
| places per window (median) | 95 | ~8 distinct prices per side |
| market halted on a price jump | 26 of 203 windows | – |

The whole loss is the unmatched lean. Two causes are mechanical:

- The lean cap of 3 cannot hold with 5-20 share clips: the first fill takes
  the lean to 10, which blocks that side, while the other side rests at
  fair + 0.03 above its touch and rarely fills. The window settles as a
  directional bet.
- The engine replaces each ask every second or two; three quarters of its
  fills are prints that met an ask it was already replacing, mostly cheap
  asks after a move (asks under 0.20 sold the winner 35% of the time, the
  reference's 10%).

The account's sells by price: under 0.20 −0.005/share, 0.20-0.40 −0.017,
0.40-0.60 +0.008, 0.60-0.80 +0.020, 0.80+ +0.009. By minute it is flat
except minute 3 (−0.002). The loss per window, −0.15, is inside the
account's own p10/p90 of −5/+5; the structural gap (volume, so rebate, and
one-sided exposure) is what matters.

Knobs added, all off by default (replay fingerprint at defaults unchanged):
`QUOTE_MODE=touch`, `FAIR_GUARD_TICKS`, `LEAN_SIZE_CLAMP`,
`REBALANCE_AT_TOUCH`, `STICKY_CLIP`, `UNDERDOG_MIN_PRICE`,
`JUMP_PAUSE_SECONDS` / `MAX_JUMPS_PER_WINDOW`. Two changes apply at
defaults: `should_requote` replaces an ask at once when the new price is
above it (the ask is under the market; with the requote knobs at 0 it
already did), and an ask that would cross is skipped without a
`risk_refuse` record. The live preflight treats `QUOTE_MODE=touch` like
`FAIR_EDGE=0` and still needs `ALLOW_TOUCH_QUOTING=YES`.

Gate: `scripts/ab-sim.ps1` runs five headless simulations side by side
(`python -m spork30.simulate --hours 24`), each in its own data dir:

| variant | knobs |
|---|---|
| ab-baseline | defaults |
| ab-touch | touch, rebalance, lean cap 6 with clamp, jump pause 5 s |
| ab-touch-rq3 | ab-touch + requote 3 s / 1 tick + sticky clip |
| ab-touch-rq10 | ab-touch + requote 10 s / 2 ticks + sticky clip |
| ab-touch-rq3-dog | ab-touch-rq3 + underdog floor 0.35 |

A default changes only if a variant passes, against the account on the same
windows (`simcompare --baseline` prints the checks): one-sided windows under
10%, sold-winner within 2 points, volume within 30%, cash + rebate per
window above the baseline's with a paired block-bootstrap 95% interval
excluding zero, and no window worse than −40.

First reading after 6 windows per variant (noise, recorded for the shape
only): touch mode sold 44 sh/w against the baseline's 19 and was one-sided
in 40% of traded windows against 75%; the 10 s / 2 tick hold halved the
replaces (375 vs 805), the 3 s / 1 tick hold barely moved them (681),
because most remaining replaces are moves up, which are replaced at once by
design. No variant is near the reference's 154 sh/w yet. Defaults stay as
they are until the 24 h comparison is in.

Assumption throughout: your account earns the maker rebate at the same
42-43 bps. If it does not, the target is the cash leg alone (+0.29/w, worst
window −40), and the volume argument weakens to the settlement edge.
