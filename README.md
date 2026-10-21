# spork30

<div align="center">

### A maker bot for Polymarket Bitcoin 5-minute Up/Down markets

[![Python](https://img.shields.io/badge/python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Mode](https://img.shields.io/badge/default-simulation-16a34a.svg?style=flat-square)](#quick-start)
[![Orders](https://img.shields.io/badge/orders-post--only-6e40c9.svg?style=flat-square)](#how-the-asks-are-priced)
[![Venue](https://img.shields.io/badge/venue-Polymarket-1f6feb.svg?style=flat-square)](https://polymarket.com)

> **One window. Two post-only asks. Nothing is sent until live mode is armed.**
> Mint a complete set, rest a sell on Up and a sell on Down, merge what is left, redeem the unpaired winner.

<br/>

**[Quick start](#quick-start) · [The window](#the-window) · [Pricing](#how-the-asks-are-priced) · [Engine](#engine) · [Safety](#safety) · [Run](#run) · [Configuration](#configuration)**

Account [spork30](https://polymarket.com/profile/0x1dd2a69e73ba444ecd5d87f0073d51a670ad51c2) · `0x1dd2a69e73ba444ecd5d87f0073d51a670ad51c2`

</div>

---

## Quick start

Python 3.10 or newer. Simulation is the default. This tree does not send an order unless live mode is armed on purpose.

<table>
<tr>
<td width="50%" valign="top">

### Run it on this machine

Quotes, fills, merge, and redeem stay local. The screen follows the live 5-minute clock.

```bash
pip install -r requirements.txt
python -m spork30.dashboard
```

`q` or Ctrl+C quits. `--init` clears this mode's decision log and state first.

</td>
<td width="50%" valign="top">

### Check the account, send nothing

Authenticates and reads collateral. The order loop is a separate flag.

```bash
python -m spork30.live          # check only
python -m spork30.live --run    # signs, only with the three gates below
```

Live needs `TRADING_MODE=LIVE`, `LIVE_TRADING_CONFIRM=YES`, and `POLYMARKET_PRIVATE_KEY`.

</td>
</tr>
</table>

Optional overrides go in `spork30/.env`, which is gitignored. A variable already set in the process wins. The full list is in [Configuration](#configuration).

---

## By the numbers

Measured on the account's Oct 5–12 2026 tape (2,012 settled windows). These are the defaults in `StrategyConfig`.

<div align="center">

| Window | Mint | Quote | Flatten |
|:---:|:---:|:---:|:---:|
| **300 s** | **750 / 750** | **post-only asks** | **merge +358 s, redeem +366 s** |

</div>

| | |
|---|---|
| **Lead** | 293 s before the open (still mints down to 90 s; later than that, the window is skipped) |
| **Clips** | 5, 10, 15, 20 shares — about 33%, 42%, 23%, 2% of fills |
| **Lean** | pull a side once it is 3 shares ahead (the account's own median lean is about 6) |
| **Edge** | spot-implied fair + 0.03, never through the touch |
| **Rebate** | about 42 bps of sold notional, shown as an estimate; the bot never sees the payment |
| **Spot** | Binance BTCUSDT; a quote is refused when spot is older than 1.5 s |

Past windows, including this tape and any replay, are a measurement of that period. They are not a forecast.

---

## The window

Each `btc-updown-5m` market lasts 300 seconds. Settlement compares the 60-second Chainlink TWAP at the close with the window's price to beat (the TWAP open). Up pays $1 if the close is at or above that open. Down pays $1 otherwise. One Up token plus one Down token is always worth $1, so the bot can mint both by splitting USDC and later merge a pair back into USDC.

The bot only makes. Every quote is a post-only sell. A price that would cross the bid is not sent.

| # | Step | What happens | When |
|---|------|----------------|------|
| 1 | **Mint** | Split 750 USDC into 750 Up and 750 Down. A slug is split once. | 293 s before the open |
| 2 | **Quote** | Rest a maker ask on Up and a maker ask on Down. | open until close |
| 3 | **Stay paired** | Pull the ask on a side that has run more than 3 shares ahead, until the other side catches up. | while quoting |
| 4 | **Merge** | Merge every remaining pair back to USDC, before the oracle is in. 1,953 of 2,012 windows on that tape. | +358 s |
| 5 | **Redeem** | Redeem the unpaired winner. | +366 s |

A stale book, a missing spot price, or the emergency-stop file pulls quotes. The bot does not invent a price.

---

## How the asks are priced

Two pricers live in the strategy. The default is the spot one (`QUOTE_MODE=fair`).

| Mode | Edge in one line | Key spec |
|------|------------------|----------|
| **Spot fair** | Price the distance BTC has already travelled, then add an edge | fair + 0.03 · join the touch when fair is below it · post-only |
| **Touch** | Sell the book the way the reference account does | join both touches · off until `ALLOW_TOUCH_QUOTING=YES` |

<details>
<summary><b>How the two pricers actually work</b></summary>

<br/>

**Spot fair value.** P(Up) is the probability implied by BTC spot versus the window's price to beat, the time left, and realised volatility. The settlement TWAP still has to average the last 60 seconds, so a move with less than that left only partly counts. There is no directional view in the number: it is only the distance already travelled. Each ask is `fair + 0.03`, rounded up to the tick, and never through the touch. If fair is below the best ask, the quote joins the ask. If spot is older than 1.5 seconds, the bot rests nothing; the replay turns negative at 3 seconds of spot latency.

**Touch quoting.** With fair value off, or with `QUOTE_MODE=touch`, the Up ask joins the Up ask and the Down ask is the complement, one tick above the Down bid, so both orders stay post-only. The pair sums to `1 + spread`. The replay cannot price a touch quoter (it scored −12.4 USDC per window on 288 windows), so live refuses this mode until `ALLOW_TOUCH_QUOTING=YES`.

The Polymarket book is the execution venue. Binance BTCUSDT is the spot series the fair value is computed on, including its own 60-second TWAP as the price to beat, so a basis between venues is not baked into the quote. The screen's reference price is Polymarket's Chainlink TWAP, which is what the market resolves on.

Maker rebate on the account has been about 42 bps of sold notional. The screen and the replay show that as an estimate. The bot never sees the payment.

</details>

---

## Engine

Python, one strategy, one risk gate, one one-second loop. Simulation, replay, and live call the same engine. The venue is the only part that signs.

| Module | Role |
|---|---|
| `spork30/strategy.py` | When to mint, quote, merge, and redeem. No I/O. |
| `spork30/execution.py` | Turns that into two post-only asks and a clip size. |
| `spork30/risk.py` | Independent limits. A failed check is not sent. |
| `spork30/portfolio.py` | Per-market state machine and the decision log. |
| `spork30/loop.py` | One-second loop: reconcile, sync fills, step, submit. |
| `spork30/gateway.py` | Polymarket CLOB, post-only orders, gasless split / merge / redeem. |
| `spork30/spotfeed.py` | Binance spot for fair value. |
| `spork30/spot.py` | Chainlink TWAP for the screen. |
| `spork30/dashboard.py` | Terminal UI. |
| `spork30/fillmodel.py` | A resting ask fills when a taker print trades through it. Shared by the replay and the simulation. |
| `spork30/simulate.py` | Real Binance spot, real Polymarket taker prints, modelled queue. |
| `spork30/backtest.py` | Replay against cached public taker prints. |
| `spork30/diagnostics.py` | Markouts, regime breakdowns, PnL decomposition, bootstrap intervals. |
| `spork30/audit.py` | Frozen-dataset baseline and train/test evaluation of a change. |
| `spork30/reference.py` | The account's public tape, and a simulation compared with it. |
| `spork30/live.py` | Account check, and the only path that signs. |

Every decision is one JSON line in `data/runtime/sim/decisions.jsonl` or `data/runtime/live/decisions.jsonl`. The screen reads that file. It does not care which process wrote it.

---

## Safety

| | |
|---|---|
| **Simulation first** | `TRADING_MODE` defaults to `SIMULATION`. `python -m spork30` refuses to start when live is armed. |
| **Three gates** | A send needs `TRADING_MODE=LIVE`, `LIVE_TRADING_CONFIRM=YES`, and `POLYMARKET_PRIVATE_KEY`. |
| **Post-only** | A sell at or under its own bid is not sent. |
| **Emergency stop** | Creating `data/runtime/EMERGENCY_STOP` refuses new orders and halts the market. Delete the file to clear it. |
| **Cancel on exit** | The live loop cancels every order it still believes is resting. |
| **Preflight** | `--run` refuses touch quoting without `ALLOW_TOUCH_QUOTING=YES`, and refuses a wallet that cannot pay for a split when nothing is left to unwind. |

**Recommendations:**

| Stage | Action |
|---|---|
| First session | `python -m spork30.dashboard` in simulation. Read the health pane: prints are real, queue position is modelled. |
| Before any send | `python -m spork30.live` with no flags. Confirm collateral, funder, and signature type. |
| First live window | Keep `SPLIT_SIZE` at a size you can lose. Watch the emergency-stop file path. |
| Ongoing | A change to pricing ships only after `audit` or `simcompare` passes. Defaults stay until then. |

Signature type `3` is the deposit-wallet (POLY_1271) path: the key is the owner EOA, collateral sits in `POLYMARKET_FUNDER`, and the funder is the maker. Splits, merges, and redeems go through Polymarket's gasless relayer and need `POLYMARKET_BUILDER_API_KEY`, `POLYMARKET_BUILDER_SECRET`, and `POLYMARKET_BUILDER_PASSPHRASE`.

---

## Run

### One simulated window

Prints the mint, quotes, merge, and redeem against a synthetic book. Refuses to start if `TRADING_MODE=LIVE`.

```bash
python -m spork30
```

### Terminal

Follows the current 5-minute window on the wall clock. In simulation the engine quotes off the same Binance spot feed live uses, against the live window's real CLOB top of book polled once a second. A resting ask fills only when a real taker print trades through it after `SIM_QUEUE_AHEAD` shares (default 200, the queue that reproduces the reference account's volume) at the same price. The public print tape is indexed 15 s to minutes late, so a print may fill an ask the engine has since replaced; the health line counts those. Nothing is sent.

```bash
python -m spork30.dashboard
python -m spork30.dashboard --follow    # watch a loop that is already running
```

`--once` prints a single frame. `--pace` slows the catch-up replay (seconds per replayed second).

What the simulation measures is on its health pane: prints, prices, sizes, and the top of book are real; queue position is modelled. Logs from before Oct 15 2026 filled a synthetic 5 shares every 20 s and do not measure the strategy (`docs/audit-2026-10.md`). The feed fixes after that are in `docs/reference-gap-2026-10.md`.

In live mode an interactive dashboard starts the order loop in the same process.

### Headless, several variants at once

Each run has its own `BOT_DATA_DIR`. Touching `<BOT_DATA_DIR>/STOP` ends it.

```powershell
$env:TRADING_MODE="SIMULATION"; $env:BOT_DATA_DIR="data/backtest/ab-touch"; $env:QUOTE_MODE="touch"
python -m spork30.simulate --hours 24 --init
.\scripts\ab-sim.ps1 -Hours 24     # five variants; -Stop ends them
```

### Replay

The same engine, with a book rebuilt from public taker prints. A resting ask fills only when a print trades through it, after a configurable queue. Nothing is signed. Prints are cached under `data/backtest/`.

```bash
python -m spork30.backtest --windows 48 --queue 0,250,500,1000
```

Queue position, the reconstructed touch, and the assumption that our own asks do not move anyone else are stated in the output. Treat the PnL as a measurement of that model. `--fills path` writes one row per modelled fill with its markouts.

### Diagnostics and audit

```bash
python -m spork30.diagnostics --queue 250 --lag 2
python -m spork30.diagnostics --queue 100 --touch-only --lag 2
python -m spork30.audit baseline
python -m spork30.audit experiments --hypothesis gap
python -m spork30.audit compare --set fair_gap_withhold_ticks=1
python -m spork30.audit calibrate
```

The first `audit` run freezes the window list in `data/backtest/audit-dataset.json`; later tables use those same windows. A change is adopted only if it passes the rule in `spork30/audit.py` on the test set under both conservative fill models. The objective is cash plus `--rebate-bps` (default `MAKER_REBATE_BPS`), with the rebate reported separately, and every table ends with the reference account's own row. The Oct 2026 results are in `docs/audit-2026-10.md`.

### Reference account

`spork30/reference.py` pulls the account's public activity (read-only, cached under `data/reference/`), rebuilds every window it traded, and lines its fills up next to a replay of this bot on the same windows.

```bash
python -m spork30.reference fetch --days 10
python -m spork30.reference profile --windows-json data/reference/windows.json
python -m spork30.reference align --queue 250 --lag 1
python -m spork30.reference simcompare --log data/backtest/ab-touch/sim/decisions.jsonl --baseline data/backtest/ab-baseline/sim/decisions.jsonl
```

`simcompare` scores a simulation log against the account on the windows both settled: volume, one-sided windows, matched-pair versus leftover PnL, late-fill share, sold-winner, PnL by price and by minute, and (with `--baseline`) the paired improvement over another log. It ends with the checks a default change has to pass. The Oct 2026 comparison is in `docs/reference-gap-2026-10.md`.

### Tests

```bash
python -m pytest
```

---

## Configuration

Defaults are the policy above. Set any of these in the environment or in `spork30/.env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRADING_MODE` | `SIMULATION` | `SIMULATION` or `LIVE` |
| `LIVE_TRADING_CONFIRM` | unset | Must be `YES` before any send |
| `SPLIT_SIZE` | `750` | Complete sets minted per window |
| `SPLIT_LEAD_SECONDS` | `293` | How early the next window is minted |
| `MIN_SPLIT_LEAD_SECONDS` | `90` | Late-start floor; after this, skip the window |
| `FLATTEN_DELAY_SECONDS` | `358` | Merge remaining pairs at this offset |
| `REDEEM_DELAY_SECONDS` | `366` | Redeem the unpaired winner |
| `FAIR_EDGE` | `0.03` | Added to spot-implied fair. `0` quotes the touch |
| `FAIR_UNDERCUT` | off | Allow the fair ask to step inside the touch |
| `ALLOW_TOUCH_QUOTING` | unset | `YES` lets `--run` start with `QUOTE_MODE=touch` or `FAIR_EDGE=0` |
| `QUOTE_MODE` | `fair` | `fair` prices at fair + edge; `touch` joins the Polymarket touch |
| `FAIR_GUARD_TICKS` | `0` | Touch mode: withhold an ask this many ticks or more under its spot fair. `0` disables |
| `LEAN_SIZE_CLAMP` | off | Clamp each clip so a fill cannot take the lean past `MAX_LEAN_SHARES` (needs the cap at 5 or more) |
| `REBALANCE_AT_TOUCH` | off | While one side is ahead, the lagging side joins its touch |
| `STICKY_CLIP` | off | Keep an ask's clip size when it is replaced |
| `UNDERDOG_MIN_PRICE` | `0` | Do not sell an outcome below this price. `0` disables |
| `JUMP_PAUSE_SECONDS` | `0` | On a jump over 0.50, pull quotes this long and resume. `0` halts the window |
| `MAX_JUMPS_PER_WINDOW` | `3` | With the pause on, halt the window on this many jumps |
| `SPOT_LAG_SECONDS` | `1` | Spot age assumed by the replay |
| `FAIR_VOL_ANNUAL` | `0.45` | Vol used when realised vol cannot be measured |
| `MAX_LEAN_SHARES` | `3` | Pull an ask once that side is this far ahead. `0` disables |
| `ASK_OFFSET_TICKS` | `0` | Ticks behind the touch. `0` joins the ask |
| `IMPROVE_SPREAD_TICKS` | `0` | Step inside a wide spread. `0` disables |
| `REQUOTE_THRESHOLD_TICKS` | `0` | Minimum move away from the ask before a replace. A move up always replaces at once |
| `REQUOTE_MIN_SECONDS` | `0` | Minimum resting age before a replace, for moves away from the ask |
| `MAKER_REBATE_BPS` | `42` | Rebate estimate on the screen and in the replay |
| `FAIR_GAP_WITHHOLD_TICKS` | `0` | Withhold an ask this many ticks or more above its touch. `0` disables |
| `QUOTE_PAUSE_FINAL_SECONDS` | `0` | Pull both asks for the last N seconds. `0` disables |
| `FAIR_EDGE_VOL_MULT` | `0` | Edge at least this multiple of sigma·sqrt(seconds left). `0` disables |
| `FILL_COOLDOWN_SECONDS` | `0` | Withhold a side for N seconds after it fills. `0` disables |
| `SPOT_STALE_GRACE_SECONDS` | `2` | Keep resting asks this long through a spot outage. `0` pulls on the first stale tick |
| `SIM_QUEUE_AHEAD` | `200` | Simulation only: shares assumed ahead of each ask (replay tables use 250 and 100) |
| `MAX_POSITION` | `800` | Shares per outcome |
| `MAX_ORDER_SIZE` | `20` | Shares per order |
| `MAX_DAILY_LOSS` | `1000` | Halt new orders after this realised loss |
| `MAX_TOTAL_EXPOSURE` | `3000` | Gross exposure cap |
| `MAX_OPEN_MARKETS` | `8` | Markets that may be open at once |
| `MAX_ORDER_RATE` | `40` | Orders per 10 seconds |
| `STALE_DATA_TIMEOUT` | `5` | Seconds before a book is too old to quote |
| `BOT_DATA_DIR` | `data/runtime` | Decision log and state live under `sim/` or `live/` |
| `POLYGON_RPC` | | Polygon RPC, required for the account check and CTF reads |
| `USER_FEED` | on | User websocket for fills. Set `0` to poll only |

Risk limits are not part of the measured policy. They sit outside it so a bug cannot scale the strategy without a human change.

---

## Disclaimer

> Trading prediction markets uses real funds once live mode is armed. This software is research code, published so the policy can be read and run. It is not financial advice, and it carries no warranty. Test in simulation before `python -m spork30.live --run`. Past windows, the account's own tape, and any replay are not a forecast. You are responsible for the keys, the size, and the loss.

