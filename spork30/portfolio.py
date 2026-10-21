"""Per-market state machine.

Transitions are explicit and written to the decision log. A second split on
the same slug is refused. Missing or stale book data cancels quotes and
does not guess a price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from spork30.config import AppConfig
from spork30.execution import ClipSchedule, desired_asks, reference_up
from spork30.models import BookTop, DesiredOrder, MarketPosition, Phase, Side, WorkingOrder, window_start_for
from spork30.risk import RiskRefusal, RiskState, check_order, note_order
from spork30.strategy import (
    fair_up,
    flatten_method,
    in_fill_cooldown,
    in_final_pause,
    lagging_outcome,
    lean_allowance,
    lean_blocked,
    market_slug,
    quantize_price,
    quote_edge,
    quoting_open,
    redeem_due,
    rests_above_touch,
    should_flatten,
    should_requote,
    should_split,
    sigma_per_second,
    split_amount,
)

Resolver = Callable[[int], tuple[bool, str | None]]

# Phases that still hold tokens or an obligation on-chain. A market in one of
# these must keep being stepped until it reaches CLOSED or HALTED, even after
# it has rotated out of the previous/current/upcoming trio.
OPEN_PHASES = frozenset({Phase.QUOTING, Phase.FLATTEN_PENDING, Phase.REDEEM_PENDING})

# Halts caused by a chain operation whose outcome is uncertain. The engine
# does not retry these on its own: local inventory may not match the chain,
# and a blind retry could double-book. They stay HALTED for an operator.
CHAIN_FAILURES = frozenset({"SPLIT_FAILED", "MERGE_FAILED", "REDEEM_FAILED"})


def _utc_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def cash_identity_gap(market: MarketPosition) -> Decimal:
    """cash - (proceeds + merged + redeemed - split_size). Zero when every leg was booked."""
    return market.cash - (market.proceeds + market.merged + market.redeemed - market.split_size)


@dataclass
class Action:
    kind: str
    slug: str
    detail: dict


@dataclass
class Engine:
    config: AppConfig
    clips: ClipSchedule
    risk_state: RiskState = field(default_factory=RiskState)
    markets: dict[str, MarketPosition] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    pnl_day: str = ""

    def _log(self, record: dict) -> None:
        self.decisions.append(record)
        path = Path(self.config.decision_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    def _transition(self, position: MarketPosition, phase: Phase, reason: str) -> None:
        previous = position.phase
        position.phase = phase
        note = f"{previous.value}->{phase.value}:{reason}"
        position.transitions.append(note)
        self._log({"event": "transition", "slug": position.slug, "note": note})

    def open_count(self) -> int:
        """Minted markets still holding tokens or an obligation on-chain.

        Windows only DISCOVERED (every restart registers a few) hold nothing
        and must not count toward `max_open_markets`.
        """
        return sum(
            1 for market in self.markets.values()
            if market.split_submitted and (market.phase in OPEN_PHASES or self.recoverable(market))
        )

    def gross_exposure(self) -> Decimal:
        total = Decimal("0")
        for market in self.markets.values():
            if market.phase in {Phase.CLOSED, Phase.HALTED}:
                continue
            total += market.split_size
        return total

    def ensure(self, start: int) -> MarketPosition:
        slug = market_slug(start, self.config.strategy)
        market = self.markets.get(slug)
        if market is None:
            market = MarketPosition(slug=slug, start=start)
            self.markets[slug] = market
            market.transitions.append("DISCOVERED:window_known")
            self._log({"event": "transition", "slug": slug, "note": "DISCOVERED:window_known"})
        return market

    def halt(self, market: MarketPosition, reason: str) -> Action:
        market.halt_reason = reason
        self._cancel_all(market, reason)
        self._transition(market, Phase.HALTED, reason)
        self._log({"event": "halt", "slug": market.slug, "reason": reason})
        return Action("HALT", market.slug, {"reason": reason})

    def recoverable(self, market: MarketPosition) -> bool:
        """A risk halt (emergency stop, price deviation, limits) that still holds
        tokens from a confirmed mint. Quoting stays off, but the merge at +358
        and the winner redeem still run so the tokens are not stranded on-chain.
        """
        return (
            market.phase is Phase.HALTED
            and market.split_submitted
            and market.halt_reason not in CHAIN_FAILURES
            and (market.up > 0 or market.down > 0)
        )

    def live_orders(self) -> list[tuple[MarketPosition, WorkingOrder]]:
        return [
            (market, order)
            for market in self.markets.values()
            for order in market.orders.values()
            if order.live
        ]

    def _roll_day(self, now: int) -> None:
        day = _utc_day(now)
        if day != self.pnl_day:
            if self.pnl_day:
                self._log({"event": "day_roll", "slug": "", "from": self.pnl_day, "to": day,
                           "realized": str(self.risk_state.realized_pnl_today)})
            self.pnl_day = day
            self.risk_state.realized_pnl_today = Decimal("0")

    def _book_cycle(self, market: MarketPosition) -> None:
        """Count a closed cycle's cash toward today's realized PnL exactly once."""
        if market.pnl_booked:
            return
        market.pnl_booked = True
        self.risk_state.realized_pnl_today += market.cash
        self._log({"event": "cycle_closed", "slug": market.slug, "cash": str(market.cash),
                   "proceeds": str(market.proceeds), "sold_up": str(market.sold_up), "sold_down": str(market.sold_down),
                   "merged": str(market.merged), "redeemed": str(market.redeemed),
                   "split_size": str(market.split_size), "winner": market.winner,
                   "realized_today": str(self.risk_state.realized_pnl_today), "day": self.pnl_day})
        gap = cash_identity_gap(market)
        if gap != 0:
            self._log({"event": "pnl_mismatch", "slug": market.slug, "gap": str(gap), "cash": str(market.cash)})

    def daily_loss_breached(self) -> bool:
        return self.risk_state.realized_pnl_today <= -self.config.risk.max_daily_loss

    def step(
        self,
        *,
        now: int,
        book: BookTop | None,
        last_up_trade: Decimal | None,
        resolved: bool,
        winner: str | None,
        emergency_stop: bool,
        resolve: Resolver | None = None,
        spot: Decimal | None = None,
        beat: Decimal | None = None,
        sigma: float | None = None,
    ) -> list[Action]:
        """Advance the next window, the live window, the previous window, and any
        older market that still holds tokens.

        `resolved`/`winner` describe the previous window, as before. `resolve`,
        when given, answers the same question for an arbitrary window start and
        lets markets that resolved late still be redeemed instead of being
        stranded in REDEEM_PENDING.

        `spot` (BTC) and `beat` (the live window's price to beat) feed the
        spot-referenced fair value when `fair_edge` > 0; `sigma` is the
        per-second volatility (default from `fair_vol_annual`).
        """
        if Path(self.config.emergency_stop_path).exists():
            emergency_stop = True
        self.risk_state.emergency_stop = emergency_stop
        self._roll_day(now)
        if emergency_stop:
            actions = []
            for market in list(self.markets.values()):
                if market.phase not in {Phase.CLOSED, Phase.HALTED}:
                    actions.append(self.halt(market, "EMERGENCY_STOP"))
            return actions

        strategy = self.config.strategy
        actions: list[Action] = []
        # Three windows matter at once: the next one (mint), the live one
        # (quote), and the previous one (merge at +58 s, redeem at +66 s).
        current = window_start_for(now, strategy.window_seconds)
        previous = current - strategy.window_seconds
        upcoming = current + strategy.window_seconds
        trio = (upcoming, current, previous)
        fair = self._fair(now, current, spot, beat, sigma)
        edge = quote_edge(
            strategy,
            sigma if sigma is not None else sigma_per_second(strategy.fair_vol_annual),
            current + strategy.window_seconds - now,
        )
        for start in trio:
            if start <= 0:
                continue
            market = self.ensure(start)
            if market.phase is Phase.CLOSED or (market.phase is Phase.HALTED and not self.recoverable(market)):
                continue
            live = start == current
            ended = start == previous
            actions.extend(
                self._step_market(
                    market,
                    now=now,
                    book=book if live else None,
                    last_up_trade=last_up_trade if live else None,
                    resolved=resolved if ended else False,
                    winner=winner if ended else None,
                    fair=fair if live else None,
                    edge=edge,
                )
            )
        # Older markets still holding tokens: the oracle came in after the
        # window left the trio. Without this they never close, their split
        # stays in gross exposure, and MAX_TOTAL_EXPOSURE halts every new mint.
        for market in list(self.markets.values()):
            if market.start in trio or not market.split_submitted:
                continue
            if market.phase not in OPEN_PHASES and not self.recoverable(market):
                continue
            late_resolved, late_winner = (resolve(market.start) if resolve else (market.resolved, market.winner))
            actions.extend(
                self._step_market(
                    market,
                    now=now,
                    book=None,
                    last_up_trade=None,
                    resolved=bool(late_resolved),
                    winner=late_winner,
                )
            )
        return actions

    def _fair(
        self, now: int, current: int, spot: Decimal | None, beat: Decimal | None, sigma: float | None
    ) -> Decimal | None:
        """Spot-implied P(Up) for the live window, or None when U6 is off or
        either input is missing (the quote then falls back to the touch)."""
        strategy = self.config.strategy
        wanted = strategy.fair_edge > 0 or (strategy.quote_mode == "touch" and strategy.fair_guard_ticks > 0)
        if not wanted or spot is None or beat is None or spot <= 0 or beat <= 0:
            return None
        seconds_left = current + strategy.window_seconds - now
        return fair_up(spot, beat, seconds_left, sigma if sigma is not None else sigma_per_second(strategy.fair_vol_annual))

    def stale_open_markets(self, now: int) -> list[MarketPosition]:
        """Markets past their flatten time that still hold inventory."""
        strategy = self.config.strategy
        limit = max(strategy.redeem_delay_seconds, strategy.flatten_delay_seconds)
        return [
            m for m in self.markets.values()
            if m.phase in OPEN_PHASES and m.split_submitted and now > m.start + limit and (m.up > 0 or m.down > 0)
        ]

    def _step_market(
        self,
        market: MarketPosition,
        *,
        now: int,
        book: BookTop | None,
        last_up_trade: Decimal | None,
        resolved: bool,
        winner: str | None,
        fair: Decimal | None = None,
        edge: Decimal | None = None,
    ) -> list[Action]:
        strategy = self.config.strategy
        actions: list[Action] = []
        # Resolution is sticky. A later tick that cannot see the oracle must
        # not un-resolve a market that was already known to be resolved.
        if resolved:
            market.resolved = True
        if winner:
            market.winner = winner

        if market.phase is Phase.DISCOVERED and should_split(now, market.start, strategy, market.split_submitted):
            actions.append(self._split(market, now))

        # The mint is ~212s before the window. `quoting_open` is false then
        # because the window has not started, which is not the same as it ending.
        if market.phase is Phase.QUOTING and now >= market.start + strategy.window_seconds:
            self._cancel_all(market, "window_closed")
            self._transition(market, Phase.FLATTEN_PENDING, "window_closed")

        if market.phase is Phase.QUOTING and quoting_open(now, market.start, strategy):
            actions.extend(self._refresh_quotes(market, now, book, last_up_trade, fair, edge))

        recover = self.recoverable(market)
        if (
            market.split_submitted
            and should_flatten(now, market.start, strategy, market.flatten_done, market.resolved)
            and (market.phase in {Phase.QUOTING, Phase.FLATTEN_PENDING} or recover)
        ):
            if recover:
                self._log({"event": "unwind", "slug": market.slug, "ts": now, "halt_reason": market.halt_reason,
                           "up": str(market.up), "down": str(market.down)})
            actions.extend(self._flatten(market, now))
        elif (
            (market.phase is Phase.REDEEM_PENDING or recover)
            and market.resolved
            and market.winner
            and redeem_due(now, market.start, strategy)
        ):
            # The merge at +358 leaves the unpaired winner (or nothing). The
            # account redeems that residual at +366, once the oracle is in.
            if market.inventory(market.winner) > 0:
                actions.append(self._redeem(market, now))
            else:
                self._transition(market, Phase.CLOSED, "merged_flat")
                self._book_cycle(market)
        return actions

    def _split(self, market: MarketPosition, now: int) -> Action:
        if market.split_submitted:
            return Action("SPLIT_SKIPPED", market.slug, {"reason": "already_split"})
        amount = split_amount(self.config.strategy)
        # A split mints `amount` of each outcome and spends `amount` USDC.
        if self.daily_loss_breached():
            return self.halt(market, "MAX_DAILY_LOSS")
        if amount > self.config.risk.max_position_per_outcome:
            return self.halt(market, "MAX_POSITION")
        if self.open_count() >= self.config.risk.max_open_markets:
            return self.halt(market, "MAX_OPEN_MARKETS")
        if self.gross_exposure() + amount > self.config.risk.max_total_exposure:
            return self.halt(market, "MAX_TOTAL_EXPOSURE")
        market.split_submitted = True
        market.split_size = amount
        market.up += amount
        market.down += amount
        market.cash -= amount
        self._transition(market, Phase.QUOTING, "split_750")
        detail = {
            "size": str(amount),
            "lead_seconds": market.start - now,
            "up": str(market.up),
            "down": str(market.down),
            "rule": "regime_B_split_fixed_size_on_timer",
            "why": "mint equal Up and Down before the window; no directional signal",
        }
        self._log({"event": "split", "slug": market.slug, "ts": now, **detail})
        return Action("SPLIT", market.slug, detail)

    def _refresh_quotes(
        self,
        market: MarketPosition,
        now: int,
        book: BookTop | None,
        last_up_trade: Decimal | None,
        fair: Decimal | None = None,
        edge: Decimal | None = None,
    ) -> list[Action]:
        actions: list[Action] = []
        if book is None and last_up_trade is None:
            self.risk_state.data_stale = True
            self._cancel_all(market, "no_reference_price")
            actions.append(Action("QUOTES_PULLED", market.slug, {"reason": "no_reference_price"}))
            return actions
        if book is not None:
            age = now - book.observed_ts
            market.last_book_ts = book.observed_ts
            if age > self.config.risk.stale_data_timeout_seconds:
                self.risk_state.data_stale = True
                self._cancel_all(market, "stale_book")
                actions.append(Action("QUOTES_PULLED", market.slug, {"reason": "stale_book", "age": age}))
                return actions
        strategy = self.config.strategy
        if strategy.quote_mode == "fair" and strategy.fair_edge > 0 and fair is None:
            # U6 is on but spot (or the price to beat) is missing or stale.
            # Quoting off the touch alone is the behaviour the replay measured
            # at -22 per window; place nothing new until the reference is
            # back. Asks already resting are held for a short grace so a
            # quiet second on the spot feed does not cost the queue position.
            self.risk_state.data_stale = True
            if market.spot_lost_since is None:
                market.spot_lost_since = now
            held = now - market.spot_lost_since
            if held < strategy.spot_stale_grace_seconds and any(o.live for o in market.orders.values()):
                actions.append(Action("QUOTES_HELD", market.slug, {"reason": "spot_grace", "seconds": held}))
                return actions
            self._cancel_all(market, "no_spot_reference")
            actions.append(Action("QUOTES_PULLED", market.slug, {"reason": "no_spot_reference"}))
            return actions
        market.spot_lost_since = None
        self.risk_state.data_stale = False
        if in_final_pause(now, market.start, strategy):
            self._cancel_all(market, "final_pause")
            actions.append(Action("QUOTES_PULLED", market.slug, {"reason": "final_pause"}))
            return actions
        ref = reference_up(book, last_up_trade)
        if ref is None:
            return actions
        risk = self.config.risk
        if market.jump_paused_until is not None:
            if now < market.jump_paused_until:
                market.last_reference = ref
                return actions
            market.jump_paused_until = None
        if market.last_reference is not None and abs(ref - market.last_reference) > risk.max_price_jump:
            market.jumps += 1
            if risk.jump_pause_seconds > 0 and market.jumps < risk.max_jumps_per_window:
                self._cancel_all(market, "price_jump")
                market.jump_paused_until = now + risk.jump_pause_seconds
                self._log({"event": "jump_pause", "slug": market.slug, "ts": now, "from": str(market.last_reference),
                           "to": str(ref), "seconds": risk.jump_pause_seconds, "jumps": market.jumps})
                market.last_reference = ref
                actions.append(Action("QUOTES_PULLED", market.slug, {"reason": "price_jump"}))
                return actions
            actions.append(self.halt(market, "MAX_PRICE_DEVIATION"))
            return actions
        market.last_reference = ref
        tick = (book.tick if book is not None and book.tick else None) or strategy.price_tick
        live = {order.outcome: order for order in market.orders.values() if order.live and order.side is Side.SELL}
        # Pair-balance cap: withdraw the ask on the side that has run ahead.
        # The fill cooldown (H4) withdraws a side that has just been hit.
        withheld: dict[str, str] = {}
        for outcome in ("Up", "Down"):
            if lean_blocked(outcome, market.up, market.down, strategy):
                withheld[outcome] = "lean_cap"
            elif in_fill_cooldown(now, market.last_fill_ts.get(outcome), strategy):
                withheld[outcome] = "fill_cooldown"
        for outcome, reason in withheld.items():
            self._withdraw(market, live, outcome, reason, actions)
        blocked = frozenset(withheld)
        # Size each ask off inventory not already committed to another sell the
        # exchange may still fill. The ask being replaced is excluded: its
        # cancel goes out before the new ask, and if it fills first the
        # exchange refuses the new sell for lack of balance.
        reserved = {
            outcome: market.resting(outcome, exclude=live[outcome].order_id if outcome in live else None)
            for outcome in ("Up", "Down")
        }
        size_caps = {
            outcome: cap
            for outcome in ("Up", "Down")
            if (cap := lean_allowance(outcome, market.up, market.down, reserved[outcome], strategy)) is not None
        }
        lagging = lagging_outcome(market.up, market.down, strategy)
        sizes = {outcome: order.size for outcome, order in live.items()} if strategy.sticky_clip else None
        desired = desired_asks(
            up_reference=ref,
            inventory_up=market.up - reserved["Up"],
            inventory_down=market.down - reserved["Down"],
            strategy=strategy,
            clips=self.clips,
            up_bid=book.bid if book is not None else None,
            tick=book.tick if book is not None else None,
            skip=blocked,
            fair=fair,
            edge=edge,
            at_touch=frozenset({lagging}) if lagging else frozenset(),
            size_caps=size_caps or None,
            sizes=sizes,
        )
        if fair is not None:
            market.last_fair = fair
        # H1: an ask resting well above its own touch only fills when the
        # touch runs through it, i.e. on the adverse move.
        touches = {
            "Up": quantize_price(ref, tick),
            "Down": strategy.complement_sum - book.bid if book is not None and book.bid is not None else None,
        }
        kept = []
        for order in desired:
            if rests_above_touch(order.price, touches[order.outcome], tick, strategy):
                self._withdraw(market, live, order.outcome, "above_touch", actions)
                continue
            kept.append(order)
        desired = kept
        for order in desired:
            # The per-order deviation check compares the order with the
            # reference for *its own* outcome. The Down reference is the
            # complement of the Up reference. (Passing `order.price` here, as
            # before, made |price - reference| identically zero and the check
            # could never fire.)
            side_reference = ref if order.outcome == "Up" else self.config.strategy.complement_sum - ref
            if order.touch_bid is not None and order.price <= order.touch_bid:
                # The window is decided (the other ask is at the tick floor):
                # a post-only sell here would cross. Skip it without a
                # refusal record; ~30 of those per window were noise.
                continue
            try:
                checks = check_order(
                    now=now,
                    position=market,
                    outcome=order.outcome,
                    side=order.side,
                    price=order.price,
                    size=order.size,
                    open_markets=self.open_count(),
                    gross_exposure=self.gross_exposure(),
                    risk=self.config.risk,
                    state=self.risk_state,
                    reference=side_reference,
                    bid=order.touch_bid,
                    reserved=reserved[order.outcome],
                )
            except RiskRefusal as refusal:
                self._log(
                    {
                        "event": "risk_refuse",
                        "slug": market.slug,
                        "outcome": order.outcome,
                        "reason": refusal.reason,
                        "price": str(order.price),
                        "size": str(order.size),
                    }
                )
                actions.append(Action("RISK_REFUSE", market.slug, {"reason": refusal.reason, "outcome": order.outcome}))
                continue
            order.risk_checks = checks
            current = live.get(order.outcome)
            # Keep the resting clip while the touch has not really left it
            # (should_requote). Resampling the size every second would cancel
            # the queue position; replacing on every tick sold at the leading
            # edge of every move.
            if current and not should_requote(current.price, order.price, now - current.placed_ts, tick, strategy):
                continue
            if current:
                current.live = False
                self._log({"event": "cancel", "slug": market.slug, "order_id": current.order_id, "reason": "replace"})
                actions.append(Action("CANCEL", market.slug, {"order_id": current.order_id, "reason": "replace"}))
            note_order(self.risk_state, now)
            order_id = f"{market.slug}:{order.outcome}:{order.price}:{now}"
            working = WorkingOrder(
                order_id=order_id,
                outcome=order.outcome,
                side=order.side,
                price=order.price,
                size=order.size,
                placed_ts=now,
            )
            market.orders[order_id] = working
            detail = _explain(market, order, order_id, now)
            self._log(detail)
            actions.append(Action("PLACE", market.slug, detail))
        return actions

    def _flatten(self, market: MarketPosition, now: int) -> list[Action]:
        """Merge every remaining pair; redeem the residual too if it is already due.

        Returns every action to send, in order. (Returning the REDEEM with the
        MERGE tucked inside its detail, as before, meant the MERGE was never
        dispatched when both fell on one tick.)
        """
        self._cancel_all(market, "flatten")
        strategy = self.config.strategy
        method = flatten_method(now, market.start, strategy, market.resolved)
        if method is None:
            return [Action("FLATTEN_WAIT", market.slug, {"reason": "not_due"})]
        market.flatten_done = True
        out: list[Action] = []
        overlap = market.overlap()
        if overlap > 0:
            market.up -= overlap
            market.down -= overlap
            market.cash += overlap
            market.merged += overlap
            self._transition(market, Phase.REDEEM_PENDING, "merged_overlap")
            detail = {
                "method": "MERGE",
                "size": str(overlap),
                "offset": now - market.start,
                "rule": "flatten_merge_first_at_358",
                "up": str(market.up),
                "down": str(market.down),
            }
            self._log({"event": "merge", "slug": market.slug, "ts": now, **detail})
            out.append(Action("MERGE", market.slug, detail))
        else:
            # One side sold out completely: nothing pairs, only a redeem is left.
            self._transition(market, Phase.REDEEM_PENDING, "nothing_to_merge")
        if market.resolved and market.winner and redeem_due(now, market.start, strategy):
            if market.inventory(market.winner) > 0:
                out.append(self._redeem(market, now))
            else:
                self._transition(market, Phase.CLOSED, "merged_flat")
                self._book_cycle(market)
        return out

    def _redeem(self, market: MarketPosition, now: int) -> Action:
        if not market.resolved or not market.winner:
            if market.phase is not Phase.REDEEM_PENDING:
                self._transition(market, Phase.REDEEM_PENDING, "waiting_for_resolution")
            return Action("REDEEM_WAIT", market.slug, {"reason": "unresolved"})
        held = market.inventory(market.winner)
        amount = held if held >= self.config.strategy.dust_shares else Decimal("0")
        if amount > 0:
            market.add(market.winner, -amount)
            market.cash += amount
            market.redeemed += amount
        # Loser tokens are left in place. That matches redeem-only markets.
        detail = {
            "method": "REDEEM",
            "winner": market.winner,
            "size": str(amount),
            "offset": now - market.start,
            "rule": "redeem_winner_abandon_loser",
            "cash": str(market.cash),
        }
        if amount == 0:
            detail["dust"] = str(held)
        # Logged before the CLOSED transition and cycle_closed so a replay of
        # the log has the redeemed cash in hand when it meets the booked total.
        self._log({"event": "redeem", "slug": market.slug, "ts": now, **detail})
        self._transition(market, Phase.CLOSED, "redeemed_winner")
        self._book_cycle(market)
        return Action("REDEEM", market.slug, detail)

    def apply_fill(
        self, order_id: str, size: Decimal, price: Decimal, ts: int | None = None, *, late: bool = False
    ) -> Action:
        """Apply a maker fill. Partial fills keep the order live.

        `late` books a fill the exchange reports for an order we already
        marked dead (it matched before our cancel landed). The order stays
        dead; only inventory and cash move.
        """
        market = None
        order = None
        for candidate in self.markets.values():
            if order_id in candidate.orders:
                market = candidate
                order = candidate.orders[order_id]
                break
        if market is None or order is None:
            raise KeyError(order_id)
        if not order.live and not late:
            raise RuntimeError(f"fill on dead order {order_id}")
        if size <= 0 or size > order.remaining + Decimal("0.000001"):
            raise RuntimeError(f"bad fill size {size} remaining {order.remaining}")
        if order.side is not Side.SELL:
            raise RuntimeError("regime B does not buy")
        order.filled += size
        market.add(order.outcome, -size)
        market.cash += size * price
        market.proceeds += size * price
        if order.outcome == "Up":
            market.sold_up += size
        else:
            market.sold_down += size
        if ts is not None:
            market.last_fill_ts[order.outcome] = ts
        if order.remaining <= Decimal("0.000001"):
            order.live = False
            # Nothing is resting on the exchange any more; there is nothing to
            # cancel. Leaving this False made the live loop send a CANCEL for
            # every fully filled order on every tick, forever.
            order.cancel_sent = True
            order.reconciled = True
        detail = {
            "order_id": order_id,
            "outcome": order.outcome,
            "price": str(price),
            "size": str(size),
            "remaining_order": str(max(order.remaining, Decimal("0"))),
            "inventory": str(market.inventory(order.outcome)),
        }
        if late:
            detail["late"] = True
        if ts is not None:
            detail["ts"] = ts
        self._log({"event": "fill", "slug": market.slug, **detail})
        return Action("FILL", market.slug, detail)

    def _withdraw(
        self, market: MarketPosition, live: dict[str, WorkingOrder], outcome: str, reason: str, actions: list[Action]
    ) -> None:
        current = live.pop(outcome, None)
        if current is None:
            return
        current.live = False
        self._log({"event": "cancel", "slug": market.slug, "order_id": current.order_id, "reason": reason,
                   "up": str(market.up), "down": str(market.down)})
        actions.append(Action("CANCEL", market.slug, {"order_id": current.order_id, "reason": reason}))

    def _cancel_all(self, market: MarketPosition, reason: str) -> None:
        for order in market.orders.values():
            if order.live:
                order.live = False
                self._log({"event": "cancel", "slug": market.slug, "order_id": order.order_id, "reason": reason})

    def save(self) -> None:
        path = Path(self.config.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pnl_day": self.pnl_day,
            "realized_pnl_today": str(self.risk_state.realized_pnl_today),
            "markets": [
                {
                    "slug": market.slug,
                    "start": market.start,
                    "phase": market.phase.value,
                    "split_size": str(market.split_size),
                    "up": str(market.up),
                    "down": str(market.down),
                    "cash": str(market.cash),
                    "proceeds": str(market.proceeds),
                    "sold_up": str(market.sold_up),
                    "sold_down": str(market.sold_down),
                    "merged": str(market.merged),
                    "redeemed": str(market.redeemed),
                    "split_submitted": market.split_submitted,
                    "flatten_done": market.flatten_done,
                    "winner": market.winner,
                    "resolved": market.resolved,
                    "halt_reason": market.halt_reason,
                    "pnl_booked": market.pnl_booked,
                    "orders": [
                        {
                            "order_id": order.order_id,
                            "outcome": order.outcome,
                            "side": order.side.value,
                            "price": str(order.price),
                            "size": str(order.size),
                            "filled": str(order.filled),
                            "live": order.live,
                            "exchange_id": order.exchange_id,
                            "cancel_sent": order.cancel_sent,
                            "reconciled": order.reconciled,
                            "sync_attempts": order.sync_attempts,
                            "placed_ts": order.placed_ts,
                        }
                        for order in market.orders.values()
                    ],
                }
                for market in self.markets.values()
            ]
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load(self) -> None:
        path = Path(self.config.state_path)
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.pnl_day = str(payload.get("pnl_day") or "")
        self.risk_state.realized_pnl_today = Decimal(str(payload.get("realized_pnl_today") or "0"))
        for row in payload.get("markets", []):
            market = MarketPosition(
                slug=row["slug"],
                start=int(row["start"]),
                phase=Phase(row["phase"]),
                split_size=Decimal(row["split_size"]),
                up=Decimal(row["up"]),
                down=Decimal(row["down"]),
                cash=Decimal(row["cash"]),
                proceeds=Decimal(row.get("proceeds") or "0"),
                sold_up=Decimal(row.get("sold_up") or "0"),
                sold_down=Decimal(row.get("sold_down") or "0"),
                merged=Decimal(row.get("merged") or "0"),
                redeemed=Decimal(row.get("redeemed") or "0"),
                split_submitted=bool(row["split_submitted"]),
                flatten_done=bool(row["flatten_done"]),
                winner=row.get("winner"),
                resolved=bool(row["resolved"]),
                halt_reason=row.get("halt_reason"),
                # Older state files predate this flag. A CLOSED market from
                # before the upgrade is treated as already booked so it is not
                # counted again on reload.
                pnl_booked=bool(row.get("pnl_booked", row["phase"] == Phase.CLOSED.value)),
            )
            for item in row.get("orders") or []:
                order = WorkingOrder(
                    order_id=item["order_id"],
                    outcome=item["outcome"],
                    side=Side(item["side"]),
                    price=Decimal(item["price"]),
                    size=Decimal(item["size"]),
                    filled=Decimal(item.get("filled") or "0"),
                    live=bool(item.get("live", True)),
                    exchange_id=str(item.get("exchange_id") or ""),
                    cancel_sent=bool(item.get("cancel_sent", False)),
                    reconciled=bool(item.get("reconciled", False)),
                    sync_attempts=int(item.get("sync_attempts") or 0),
                    placed_ts=int(item.get("placed_ts") or 0),
                )
                market.orders[order.order_id] = order
            self.markets[market.slug] = market


def _explain(market: MarketPosition, order: DesiredOrder, order_id: str, now: int) -> dict:
    return {
        "event": "place",
        "order_id": order_id,
        "slug": market.slug,
        "ts": now,
        "why": "rest a maker ask on both outcomes while the 5-minute window is open",
        "rule": order.rule,
        "outcome": order.outcome,
        "side": order.side.value,
        "price": str(order.price),
        "size": str(order.size),
        "reference_price": str(order.reference_price),
        "position": str(order.position),
        "phase": market.phase.value,
        "order_type": order.kind.value,
        "risk_checks": order.risk_checks,
        "market_state": {
            "start": market.start,
            "seconds_into_window": now - market.start,
            "up": str(market.up),
            "down": str(market.down),
        },
    }
