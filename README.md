# spork30

spork30 is a maker bot for [Polymarket](https://polymarket.com) Bitcoin 5-minute Up/Down markets (`btc-updown-5m`). It mints a complete set before each window, rests post-only asks on both outcomes, then merges what is left and redeems the unpaired winner.

This repository is the public version of the bot behind the Polymarket account **spork30**.

| | |
| --- | --- |
| Account | [spork30](https://polymarket.com/profile/0x1dd2a69e73ba444ecd5d87f0073d51a670ad51c2) |
| Address | `0x1dd2a69e73ba444ecd5d87f0073d51a670ad51c2` |

It is written for two readers. Traders can see the policy the account actually runs: when it mints, how it quotes, and when it flattens. Builders get the same loop as code: a pure strategy, a risk gate, a one-second engine, a Polymarket venue, a terminal screen, and a replay against the public trade tape.

Simulation is the default. Nothing in this tree sends an order unless live mode is armed on purpose.

## What it trades

Each `btc-updown-5m` market is a 300-second window. Settlement compares the 60-second Chainlink TWAP at the close with the window’s price to beat (the TWAP open). Up pays $1 if the close is at or above that open. Down pays $1 otherwise. One Up token plus one Down token is always worth $1, which is why the bot can mint both by splitting USDC and later merge a pair back into USDC.

The bot only makes. Every quote is a post-only sell. A price that would cross the bid is refused.

## The window

Times below are measured on the account’s Oct 5–12 2026 tape (2,012 settled windows) and are the defaults in `StrategyConfig`.

1. **Mint.** About 293 seconds before the window opens, split 750 USDC into 750 Up and 750 Down. If the process wakes late it still mints while at least 90 seconds of lead remain. Later than that, the window is skipped. A slug is split once.
2. **Quote.** From the open until the close, rest a maker ask on Up and a maker ask on Down. Clip size is drawn from the account’s on-grid sizes: 5, 10, 15, and 20 shares (about 33%, 42%, 23%, and 2% of fills).
3. **Stay paired.** If one outcome has been sold more than 3 shares ahead of the other, that side’s ask is pulled until the other side catches up. On the account, the median absolute lean per window is about 6 shares.
4. **Merge.** At +358 seconds, merge every remaining Up/Down pair back to USDC. This happens before the oracle is in. On that tape it was 1,953 of 2,012 windows.
5. **Redeem.** At +366 seconds, redeem the unpaired winner.

A stale book, a missing spot price, or the emergency-stop file pulls quotes. The bot does not invent a price.

## How the asks are priced

Two pricers live in the strategy. The default is the spot one.

**Spot fair value.** P(Up) is the probability implied by BTC spot versus the window’s price to beat, the time left, and realised volatility. The settlement TWAP still has to average the last 60 seconds, so a move with less than that left only partly counts. There is no directional view in the number: it is only the distance already travelled. Each ask is `fair + 0.03`, rounded up to the tick, and never through the touch. If fair is below the best ask, the quote joins the ask. If spot is older than 1.5 seconds, the bot rests nothing; the replay turns negative at 3 seconds of spot latency.

**Touch quoting.** With fair value off, the Up ask joins the Up ask and the Down ask is the complement, one tick above the Down bid, so both orders stay post-only. The pair sums to `1 + spread`.

The Polymarket book is the execution venue. Binance BTCUSDT is the spot series the fair value is computed on, including its own 60-second TWAP as the price to beat, so a basis between venues is not baked into the quote. The screen’s reference price is Polymarket’s Chainlink TWAP, which is what the market resolves on.

Maker rebate on the account has been about 42 bps of sold notional. The screen and the replay show that as an estimate. The bot never sees the payment.

## Layout

| Module | Role |
| --- | --- |
| `spork30/strategy.py` | When to mint, quote, merge, and redeem. No I/O. |
| `spork30/execution.py` | Turns that into two post-only asks and a clip size. |
| `spork30/risk.py` | Independent limits. A failed check is not sent. |
| `spork30/portfolio.py` | Per-market state machine and the decision log. |
| `spork30/loop.py` | One-second loop: reconcile, sync fills, step, submit. |
| `spork30/gateway.py` | Polymarket CLOB, post-only orders, gasless split / merge / redeem. |
| `spork30/spotfeed.py` | Binance spot for fair value. |
| `spork30/spot.py` | Chainlink TWAP for the screen. |
| `spork30/dashboard.py` | Terminal UI. |
| `spork30/fillmodel.py` | Print-driven fills: a resting ask fills when a taker print trades through it. Shared by the replay and the simulation. |
| `spork30/simulate.py` | Simulation mode: real Binance spot, real Polymarket taker prints, modelled queue. |
| `spork30/backtest.py` | Replay against cached public taker prints. |
| `spork30/diagnostics.py` | Fill-level markouts, regime breakdowns, PnL decomposition, bootstrap intervals. |
| `spork30/audit.py` | Frozen-dataset baseline and train/test evaluation of strategy changes. |
| `spork30/live.py` | Account check, and the only path that signs. |

Every decision is one JSON line in `data/runtime/sim/decisions.jsonl` or `data/runtime/live/decisions.jsonl`. The screen reads that file. It does not care which process wrote it.

## Install

Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

Optional overrides go in `spork30/.env`, which is gitignored. Variables already set in the process win. A template is in [Configuration](#configuration).

## Run

### One simulated window

Prints the mint, quotes, merge, and redeem against a synthetic book. Refuses to start if `TRADING_MODE=LIVE`.

```bash
python -m spork30
```

### Terminal

Follows the current 5-minute window on the wall clock. In simulation mode the engine quotes off the same Binance spot feed live uses (trade and book-ticker streams), against the live window's real CLOB top of book polled once a second, and a resting ask fills only when a real taker print trades through it after `SIM_QUEUE_AHEAD` shares (default 200, the queue that reproduces the reference account's volume) at the same price. The public print tape is indexed 15 s to minutes late, so a print may fill an ask the engine has since replaced; the window's health line counts those. Nothing is sent; the mint, fills, merge and redeem are local bookkeeping.

```bash
python -m spork30.dashboard
```

`q` or Ctrl+C quits. `--once` prints a single frame after catching up on the current window's prints. `--init` clears this mode’s decision log and state first. `--pace` slows the catch-up replay (seconds per replayed second).

What the simulation measures is stated on its health pane: the prints, their prices, sizes and the top of book are real; the queue position is modelled (the print-implied touch stands in only while the book poll is down). Before Oct 15 2026 the simulation filled a synthetic 5 shares every 20 s on whichever side BTC was moving toward; the losses in logs from before then measure that construction, not the strategy (see `docs/audit-2026-10.md`). Until later that day its touch came from the lagging print tape and every stale spot tick cancelled both asks (see `docs/reference-gap-2026-10.md`).

In live mode the dashboard starts the order loop in the same process. To watch a loop that is already running:

```bash
python -m spork30.dashboard --follow
```

### Replay

Drives the same engine with a book rebuilt from public taker prints. A resting ask fills only when a print trades through it, after a configurable queue of shares ahead. Nothing is signed. Prints are cached under `data/backtest/`.

```bash
python -m spork30.backtest --windows 48 --queue 0,250,500,1000
```

Queue position, the reconstructed touch, and the assumption that our own asks do not move anyone else are stated in the output. Treat the PnL as a measurement of that model. `--fills path` writes one row per modelled fill with its markouts.

### Diagnostics and audit

```bash
python -m spork30.diagnostics --queue 250 --lag 2            # markouts, regimes, decomposition, bootstrap CI
python -m spork30.diagnostics --queue 100 --touch-only --lag 2
python -m spork30.audit baseline                              # frozen windows, queue 0/100/250 x lag 1/2/3, train/test
python -m spork30.audit experiments --hypothesis gap          # train-chosen candidate, judged on the test set
python -m spork30.audit compare --set fair_gap_withhold_ticks=1
python -m spork30.audit calibrate                             # which SIM_QUEUE_AHEAD reproduces the reference account's volume
```

The first `audit` run freezes the window list in `data/backtest/audit-dataset.json`; later tables are on identical windows. A change is adopted only if it passes the rule written in `spork30/audit.py` on the test set under both conservative fill models; the objective is cash plus `--rebate-bps` (default `MAKER_REBATE_BPS`), with the rebate reported separately, and every table ends with the reference account's own row on the same windows. The Oct 2026 results are in `docs/audit-2026-10.md`.

### Reference account

The strategy is forked from the maker account `0x1dd2a69e73ba444ecd5d87f0073d51a670ad51c2`. `spork30/reference.py` pulls its public activity (read-only data-api, cached under `data/reference/`), rebuilds every window it traded, and aligns its fills with the cached prints and Binance spot next to a replay of this bot on the same windows:

```bash
python -m spork30.reference fetch --days 10
python -m spork30.reference profile --windows-json data/reference/windows.json
python -m spork30.reference align --queue 250 --lag 1
```

The Oct 2026 comparison and what it changed are in `docs/reference-gap-2026-10.md`.

### Tests

```bash
python -m pytest
```

## Live trading

`python -m spork30.live` with no flags authenticates, reads collateral, and sends nothing.

Sending requires all three gates:

- `TRADING_MODE=LIVE`
- `LIVE_TRADING_CONFIRM=YES`
- `POLYMARKET_PRIVATE_KEY` set, and the owner of `POLYMARKET_FUNDER`

Signature type `3` is the deposit-wallet (POLY_1271) path: the key is the owner EOA, collateral sits in the funder, and the funder is the maker.

```bash
python -m spork30.live          # check only
python -m spork30.live --run    # sign and run the loop
```

`--init` clears the live decision log and engine state. It does not touch the emergency-stop file.

`--run` also refuses to start when `FAIR_EDGE` is 0 (touch quoting replayed at -12.4 USDC per window on 288 out-of-sample windows; `ALLOW_TOUCH_QUOTING=YES` overrides), or when the funder holds less USDC than `SPLIT_SIZE` and there is no open market left to unwind. The check-only run prints the same preflight.

Splits, merges, and redeems go through Polymarket’s gasless relayer and need builder credentials (`POLYMARKET_BUILDER_API_KEY`, `POLYMARKET_BUILDER_SECRET`, `POLYMARKET_BUILDER_PASSPHRASE`). Orders are post-only on the CLOB. On exit the loop cancels every order it still believes is live.

**Stop.** Create the file `data/runtime/EMERGENCY_STOP` (or whatever `EMERGENCY_STOP_FILE` points at). New orders are refused and the market halts. Delete the file to clear the stop.

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
| `FAIR_EDGE` | `0.03` | Cents added to spot-implied fair. `0` quotes the touch |
| `FAIR_UNDERCUT` | off | Allow the fair ask to step inside the touch |
| `ALLOW_TOUCH_QUOTING` | unset | `YES` lets `--run` start with `FAIR_EDGE=0` |
| `SPOT_LAG_SECONDS` | `1` | Spot age assumed by the replay |
| `FAIR_VOL_ANNUAL` | `0.45` | Vol used when realised vol cannot be measured |
| `MAX_LEAN_SHARES` | `3` | Pull an ask once that side is this far ahead. `0` disables |
| `ASK_OFFSET_TICKS` | `0` | Ticks behind the touch. `0` joins the ask |
| `IMPROVE_SPREAD_TICKS` | `0` | Step inside a wide spread. `0` disables |
| `REQUOTE_THRESHOLD_TICKS` | `0` | Minimum touch move before a replace |
| `REQUOTE_MIN_SECONDS` | `0` | Minimum resting age before a replace |
| `MAKER_REBATE_BPS` | `42` | Rebate estimate on the screen and in the replay |
| `FAIR_GAP_WITHHOLD_TICKS` | `0` | Withhold an ask that would rest this many ticks or more above its touch. `1` only ever joins the touch. `0` disables |
| `QUOTE_PAUSE_FINAL_SECONDS` | `0` | Pull both asks for the last N seconds of the window. `0` disables |
| `FAIR_EDGE_VOL_MULT` | `0` | Edge at least this multiple of sigma·sqrt(seconds left). `0` disables |
| `FILL_COOLDOWN_SECONDS` | `0` | Withhold a side for N seconds after it fills. `0` disables |
| `SPOT_STALE_GRACE_SECONDS` | `2` | Keep resting asks this long through a spot outage before pulling them. `0` pulls on the first stale tick |
| `SIM_QUEUE_AHEAD` | `200` | Simulation only: shares assumed ahead of each ask at its price (calibrated to the reference account; replay tables use 250 and 100) |
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

## Disclaimer

This is research code for a live prediction market, published so traders and builders can read the policy and run it themselves. It is not financial advice. Live mode uses real funds. Past windows, including the account’s own tape and any replay, are not a forecast. You are responsible for the keys, the size, and the loss.
