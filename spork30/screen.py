"""Pane layout for the terminal. Pure rendering: reads a `Book`, draws rich renderables.

    header   mode | gates | clock | window countdown
    market   window, phase, reference, both asks and the pair sum, inventory
    orders   every resting ask with its age
    cycles   last six windows: sold Up/Down at VWAP, pnl, winner
    risk     realized / today / daily-loss bar / exposure bar / refusals
    health   loop tick age, errors, fill feed, chain worker, resolution source
    tape     what happened, newest last
    footer   keys and the log path

Nothing here touches the engine or the venue. Behaviour changes live in the
decision log; this module only decides how to show them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from spork30.activity import Book, MarketView, short_slug
from spork30.config import TradingMode

EVENT_STYLE = {
    "split": "cyan",
    "open": "bright_blue",
    "reprice": "blue",
    "fill": "bright_green",
    "merge": "magenta",
    "redeem": "green",
    "confirm": "green",
    "halt": "bright_red",
    "reject": "bright_red",
    "error": "bright_red",
    "refuse": "yellow",
    "unwind": "yellow",
    "day": "dim",
}

PHASE_STYLE = {
    "SEEDED": "yellow",
    "DISCOVERED": "cyan",
    "QUOTING": "bright_green",
    "FLATTEN_PENDING": "yellow",
    "REDEEM_PENDING": "yellow",
    "CLOSED": "dim",
    "HALTED": "bright_red",
}

WINDOW = 300


def build(
    book: Book,
    *,
    mode: str,
    status: str,
    log_path: str,
    emergency: bool,
    btc: Decimal | None = None,
    beat: Decimal | None = None,
    clock: int | None = None,
    notice: str = "",
    sends: bool = False,
    limits: dict | None = None,
) -> Layout:
    now = book.last_ts if clock is None else clock
    live = mode == TradingMode.LIVE.value
    limits = limits or {}
    root = Layout()
    root.split_column(
        Layout(name="header", size=4),
        Layout(name="body", ratio=3, minimum_size=22),
        Layout(name="tape", ratio=2, minimum_size=8),
        Layout(name="footer", size=4),
    )
    root["body"].split_row(Layout(name="left", ratio=3), Layout(name="right", ratio=2, minimum_size=38))
    root["left"].split_column(
        Layout(name="market", size=7),
        Layout(name="orders", size=7),
        Layout(name="cycles", minimum_size=8),
    )
    root["right"].split_column(
        Layout(name="risk", size=10),
        Layout(name="health", minimum_size=8),
    )
    root["header"].update(header(book, mode=mode, status=status, emergency=emergency, sends=sends, now=now, notice=notice))
    root["market"].update(market_pane(book, now, btc, beat, notice))
    root["orders"].update(Panel(orders_table(book, now), title="resting asks", border_style="bright_blue"))
    root["cycles"].update(Panel(cycles_table(book, now), title="cycles", border_style="cyan"))
    root["risk"].update(risk_pane(book, now, limits))
    root["health"].update(health_pane(book, now, live=live, sends=sends))
    root["tape"].update(Panel(tape_table(book, notice), title="tape", border_style="green"))
    root["footer"].update(footer(log_path, mode, sends))
    return root


def snapshot_text(layout: Layout, width: int = 140, height: int = 50) -> str:
    console = Console(width=width, height=height, force_terminal=True, color_system=None)
    with console.capture() as capture:
        console.print(layout)
    return capture.get()


# --- panes ----------------------------------------------------------------------


def header(book: Book, *, mode: str, status: str, emergency: bool, sends: bool, now: int, notice: str = "") -> Panel:
    live = mode == TradingMode.LIVE.value
    title = Text()
    if emergency:
        title.append(" EMERGENCY STOP ", style="bold white on red")
        title.append(" ")
    title.append(" spork30 ", style="bold white on grey23")
    title.append(" ")
    if live:
        title.append(" LIVE ", style="bold white on red")
        title.append("  this screen runs the order loop" if sends else "  monitor only: nothing is sent from here", style="red")
    else:
        title.append(" SIMULATION ", style="bold black on bright_green")
        title.append("  orders stay on this machine", style="green")
    title.append("    ")
    title.append(_hms(now), style="bold yellow")
    title.append(" UTC", style="dim")
    if now:
        start = now - (now % WINDOW)
        left = start + WINDOW - now
        title.append(f"    window {_span(start)}  ", style="dim")
        title.append(f"{left // 60:02d}:{left % 60:02d} left", style="yellow")
    sub = Text(status, style="dim")
    if notice:
        sub.append("    ")
        sub.append(notice, style="yellow")
    return Panel(Group(title, sub), border_style="red" if live else "green")


def market_pane(book: Book, now: int, btc: Decimal | None, beat: Decimal | None, notice: str = "") -> Panel:
    market = book.active(now) if now else (book.traded()[-1] if book.traded() else None)
    line1 = Text(no_wrap=True, overflow="crop")
    line2 = Text(no_wrap=True, overflow="crop")
    line3 = Text(no_wrap=True, overflow="crop")
    if market is None or market.start is None:
        line1.append("no minted window", style="dim")
        if notice:
            line2.append(notice, style="yellow")
    else:
        phase = _phase_shown(market, now)
        line1.append(_span(market.start), style="bold")
        line1.append(f"  {short_slug(market.slug)}  ")
        line1.append(phase, style=PHASE_STYLE.get(phase, ""))
        if market.start <= now < market.start + WINDOW:
            left = market.start + WINDOW - now
            line1.append(f"    {left // 60:02d}:{left % 60:02d} left", style="yellow")
        elif now < market.start:
            line1.append(f"    arms in {market.start - now}s", style="yellow")
        else:
            line1.append(f"    +{now - market.start - WINDOW}s after close", style="dim")
        if market.halt_reason:
            line1.append(f"    {market.halt_reason}", style="bright_red")
        up = market.last_quote.get("Up")
        down = market.last_quote.get("Down")
        line2.append("ref Up ask ", style="dim")
        line2.append(_px4(market.reference), style="bold white")
        line2.append("    ask Up ", style="dim")
        line2.append(_px4(up), style="bright_green")
        line2.append("  Down ", style="dim")
        line2.append(_px4(down), style="bright_red")
        if up is not None and down is not None:
            pair = up + down
            line2.append("    pair ", style="dim")
            line2.append(f"{pair:.2f}", style="bold bright_green" if pair > 1 else "bold bright_red")
            line2.append(f" ({(pair - 1) * 100:+.0f}c)", style="dim")
        line2.append(f"    replaces {market.replaces}", style="dim")
        line3.append("left ", style="dim")
        line3.append(f"Up {_money(market.up)}  Down {_money(market.down)}")
        line3.append("    sold ", style="dim")
        line3.append(f"Up {_money(market.sold_up)}" + (f" @ {market.vwap('Up'):.3f}" if market.vwap("Up") else ""), style="bright_green")
        line3.append("  ")
        line3.append(f"Down {_money(market.sold_down)}" + (f" @ {market.vwap('Down'):.3f}" if market.vwap("Down") else ""), style="bright_red")
        line3.append("    cash ", style="dim")
        line3.append(_money(market.cash), style="bold")
    spot = Text(no_wrap=True, overflow="crop")
    spot.append("BTC TWAP ", style="dim")
    spot.append(_px(btc), style="bold white")
    spot.append("   to beat ", style="dim")
    spot.append(_px(beat), style="bold")
    if btc is not None and beat is not None:
        delta = btc - beat
        spot.append(f"   {delta:+,.2f}", style="bright_green" if delta >= 0 else "bright_red")
    return Panel(Group(line1, line2, line3, spot), title="market", border_style="white")


def orders_table(book: Book, now: int) -> Table:
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("window", style="bold", no_wrap=True)
    table.add_column("outcome", no_wrap=True)
    table.add_column("price", justify="right", no_wrap=True)
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("left", justify="right", no_wrap=True)
    table.add_column("age", justify="right", no_wrap=True)
    rows = book.resting_orders()
    if not rows:
        seeded = [m for m in book.traded() if m.phase == "QUOTING" and m.start is not None and now < m.start]
        table.add_row(short_slug(seeded[-1].slug) if seeded else "-", "", "", "", "", "seeded, arms at open" if seeded else "none resting")
        return table
    for market, order in rows[-4:]:
        age = f"{now - order.placed_ts}s" if order.placed_ts and now else "-"
        style = "bright_green" if order.outcome == "Up" else "bright_red"
        table.add_row(short_slug(market.slug), Text(order.outcome, style=style), order.price, order.size, order.remaining, age)
    return table


def cycles_table(book: Book, now: int) -> Table:
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("cycle", style="bold", no_wrap=True)
    table.add_column("phase", no_wrap=True)
    table.add_column("sold Up", justify="right", no_wrap=True)
    table.add_column("sold Down", justify="right", no_wrap=True)
    table.add_column("left U/D", justify="right", no_wrap=True)
    table.add_column("pnl", justify="right", no_wrap=True)
    table.add_column("winner", no_wrap=True)
    rows = book.traded()[-6:]
    if not rows:
        table.add_row("-", Text("waiting", style="dim"), "", "", "", "", "")
        return table
    for market in rows:
        phase = _phase_shown(market, now)
        pnl = market.marked()
        pnl_style = "dim" if phase not in {"CLOSED", "HALTED"} else ("bright_green" if pnl >= 0 else "bright_red")
        table.add_row(
            _hhmm(market.start),
            Text(phase, style=PHASE_STYLE.get(phase, "yellow")),
            _sold(market, "Up"),
            _sold(market, "Down"),
            f"{market.up:.0f}/{market.down:.0f}",
            Text(_money(pnl), style=pnl_style),
            market.winner or (market.halt_reason[:12] if market.halt_reason else "-"),
        )
    return table


def risk_pane(book: Book, now: int, limits: dict) -> Panel:
    realized = book.realized()
    closed = book.closed_count()
    day, daily = book.daily_pnl(now)
    today = book.realized_today if book.realized_today is not None else realized
    max_loss = Decimal(str(limits.get("max_daily_loss") or "0"))
    max_exposure = Decimal(str(limits.get("max_total_exposure") or "0"))
    exposure = book.exposure()
    lines: list[Text] = []

    def line() -> Text:
        t = Text(no_wrap=True, overflow="crop")
        lines.append(t)
        return t

    t = line()
    t.append("realized   ", style="dim")
    t.append(_money(realized), style="bold bright_green" if realized >= 0 else "bold bright_red")
    t.append(f"  / {closed} settled cycles", style="dim")
    t = line()
    t.append("open mark  ", style="dim")
    t.append(_money(book.open_marked()), style="yellow")
    t.append(f"  pairs at merge value, cash {_money(book.open_cash())}", style="dim")
    t = line()
    t.append(f"today      ", style="dim")
    t.append(_money(daily), style="bold")
    t.append(f"  windows opened {day}", style="dim")
    t = line()
    t.append("booked     ", style="dim")
    t.append(_money(today), style="bold bright_green" if today >= 0 else "bold bright_red")
    t.append("  counts toward MAX_DAILY_LOSS", style="dim")
    rebate_bps = Decimal(str(limits.get("maker_rebate_bps") or "0"))
    if rebate_bps > 0:
        # Estimate only: the rebate is paid outside the CLOB and never appears
        # in this log. 42 bps of sold notional is the reference account's rate.
        rebate = book.sold_notional() * rebate_bps / Decimal("10000")
        t = line()
        t.append("rebate est ", style="dim")
        t.append(_money(rebate), style="cyan")
        t.append(f"  {rebate_bps} bps of {_money(book.sold_notional())} sold, not booked", style="dim")
    if max_loss > 0:
        used = max(Decimal("0"), -today)
        t = line()
        t.append("daily loss ", style="dim")
        t.append(_bar(used, max_loss), style="bright_red" if used / max_loss > Decimal("0.8") else "yellow")
        t.append(f" {_money(used)} of {_money(max_loss)}", style="dim")
    if max_exposure > 0:
        t = line()
        t.append("exposure   ", style="dim")
        t.append(_bar(exposure, max_exposure), style="cyan")
        t.append(f" {_money(exposure)} of {_money(max_exposure)}  {len(book.open_markets())} open", style="dim")
    won, sold = book.sold_winner()
    if sold > 0:
        share = won / sold * 100
        t = line()
        t.append("adverse    ", style="dim")
        t.append(f"sold winner {share:.0f}%",
                 style="bright_red" if share >= 60 else ("yellow" if share > 52 else "bright_green"))
        t.append(f"  of {sold:.0f} sh settled; 50% is uninformed flow", style="dim")
    t = line()
    refusals = book.counts.get("risk_refuse", 0)
    rejects = book.counts.get("send_fail", 0)
    halts = book.counts.get("halt", 0)
    t.append("fills ", style="dim")
    t.append(str(book.counts.get("fill", 0)))
    t.append("  places ", style="dim")
    t.append(str(book.counts.get("place", 0)))
    t.append("  refusals ", style="dim")
    t.append(str(refusals), style="yellow" if refusals else "")
    t.append("  rejects ", style="dim")
    t.append(str(rejects), style="bright_red" if rejects else "")
    t.append("  halts ", style="dim")
    t.append(str(halts), style="bright_red" if halts else "")
    mismatched = len(book.mismatches()) + book.counts.get("pnl_mismatch", 0)
    if mismatched:
        t.append(f"  pnl mismatch {mismatched}", style="bold bright_red")
    return Panel(Group(*lines), title="pnl and limits", border_style="grey50")


def health_pane(book: Book, now: int, *, live: bool, sends: bool) -> Panel:
    h = book.health
    text = Text(no_wrap=True, overflow="crop")
    age = (now - book.last_ts) if (now and book.last_ts) else None
    text.append("loop   ", style="dim")
    if age is None:
        text.append("no events yet", style="yellow")
    else:
        text.append(f"last event {age}s ago", style="bright_green" if age <= 3 else ("yellow" if age <= 15 else "bright_red"))
    errors = book.counts.get("loop_error", 0)
    text.append("    errors ", style="dim")
    text.append(str(errors), style="bright_red" if errors else "")
    if book.counts.get("loop_abort"):
        text.append("  ABORTED", style="bold bright_red")
    text.append("\n")
    if live:
        text.append("fills  ", style="dim")
        feed = h.get("feed")
        if not h:
            text.append("waiting for health record", style="dim")
        elif feed is None:
            text.append("REST polling only (USER_FEED off)", style="yellow")
        elif feed:
            text.append(f"websocket connected, {h.get('feed_events', 0)} events", style="bright_green")
        else:
            text.append("websocket down: REST polling every tick", style="bright_red")
        text.append("\n")
        text.append("chain  ", style="dim")
        pending = int(h.get("chain_pending") or 0)
        text.append(f"{pending} pending", style="yellow" if pending else "")
        text.append(f"    confirmed {book.counts.get('tx_confirmed', 0)}", style="dim")
        text.append("\n")
        text.append("oracle ", style="dim")
        if not h:
            text.append("-", style="dim")
        else:
            text.append("CTF payout via RPC" if h.get("rpc") else "gamma closed prices (no POLYGON_RPC)", style="" if h.get("rpc") else "yellow")
        text.append("\n")
        text.append("tick   ", style="dim")
        text.append(f"{h.get('tick_ms', '-')} ms" if h else "-", style="")
        text.append(f"    resting {h.get('live_orders', 0)}" if h else "", style="dim")
    else:
        text.append("fills  ", style="dim")
        text.append(f"real taker prints, modelled queue {h.get('queue_ahead', '-')} ahead", style="dim")
        text.append("\n")
        text.append("book   ", style="dim")
        source = h.get("book_source")
        if source == "clob":
            text.append(f"real CLOB top, {h.get('book_age_s')}s old", style="bright_green")
        elif h.get("book_error"):
            text.append(f"book poll down ({h.get('book_error')}), print-implied touch", style="bright_red")
        else:
            text.append("print-implied touch", style="yellow" if source == "prints" else "dim")
        if h.get("tape_error"):
            text.append(f"  print tape down: {h.get('tape_error')}", style="bright_red")
        else:
            polled = h.get("tape_age_s")
            text.append(f"  {h.get('prints', 0)} prints this window", style="dim")
            text.append(f", polled {polled}s ago" if polled is not None else "", style="dim")
        text.append("\n")
        text.append("spot   ", style="dim")
        spot_age = h.get("spot_age_s")
        if spot_age is None:
            text.append(f"{h.get('spot_source', '-')} no price yet", style="yellow")
        else:
            text.append(f"{h.get('spot_source', '-')} age {spot_age}s",
                        style="bright_green" if float(spot_age) <= 1.5 else "yellow")
        text.append("\n")
        text.append("quoted ", style="dim")
        if h.get("open_seconds"):
            quoted, open_s = int(h.get("quoted_seconds") or 0), int(h["open_seconds"])
            share = quoted / open_s if open_s else 0.0
            text.append(f"{quoted}/{open_s}s of this window", style="bright_green" if share >= 0.9 else "yellow")
            text.append(f"  refusals {h.get('refusals', 0)}  pulls {h.get('pulls', 0)}  held {h.get('holds', 0)}",
                        style="dim")
            if "places" in h:
                text.append(f"  places {h.get('places', 0)}  fills {h.get('window_fills', 0)}"
                            f" ({h.get('late_fills', 0)} after a replace)", style="dim")
        else:
            text.append("window not open", style="dim")
        text.append("\n")
        text.append("note   ", style="dim")
        text.append("queue position is modelled: run spork30.backtest for a range", style="dim")
    return Panel(text, title="health", border_style="grey50")


def tape_table(book: Book, notice: str = "", rows: int = 14) -> Table:
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("time", width=8, no_wrap=True)
    table.add_column("event", width=8, no_wrap=True)
    table.add_column("window", width=10, no_wrap=True)
    table.add_column("detail", no_wrap=True, overflow="crop")
    lines = list(book.tape)[-rows:]
    if not lines:
        table.add_row("-", "", "", notice or "nothing yet")
        return table
    for line in lines:
        table.add_row(
            _hms(line.ts) if line.ts else "-",
            Text(line.event, style=EVENT_STYLE.get(line.event, "")),
            short_slug(line.slug),
            line.detail,
        )
    return table


def footer(log_path: str, mode: str, sends: bool = False) -> Panel:
    hint = "q quit    Ctrl+C quit    redraws once a second"
    if sends:
        hint += "    q also stops the order loop and cancels resting asks"
    elif mode == TradingMode.LIVE.value:
        hint += "    this screen does not send orders"
    else:
        hint += "    TWAP and price-to-beat are the Chainlink values on the Polymarket page"
    text = Text()
    text.append(hint + "\n", style="dim")
    text.append(log_path, style="grey70")
    return Panel(text, border_style="grey37")


# --- formatting -------------------------------------------------------------------


def _phase_shown(market: MarketView, now: int) -> str:
    if market.phase == "QUOTING" and market.start is not None and now and now < market.start:
        return "SEEDED"
    return market.phase


def _sold(market: MarketView, outcome: str) -> str:
    size = market.sold_up if outcome == "Up" else market.sold_down
    vwap = market.vwap(outcome)
    if size <= 0 or vwap is None:
        return "-"
    return f"{size:.0f} @ {vwap:.3f}"


def _bar(value: Decimal, limit: Decimal, width: int = 12) -> str:
    if limit <= 0:
        return " " * width
    share = min(Decimal("1"), max(Decimal("0"), value / limit))
    filled = int(share * width)
    # ASCII on purpose: legacy Windows consoles are cp1252 and reject block glyphs.
    return "#" * filled + "-" * (width - filled)


def _hhmm(start: int | None) -> str:
    if not start:
        return "-"
    return datetime.fromtimestamp(start, tz=timezone.utc).strftime("%H:%M")


def _span(start: int) -> str:
    end = datetime.fromtimestamp(start + WINDOW, tz=timezone.utc).strftime("%H:%M")
    return f"{_hhmm(start)}-{end}"


def _hms(ts: int) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _px(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value.quantize(Decimal('0.01')):,}"


def _px4(value: Decimal | None) -> str:
    """Outcome prices sit on the 0.01 grid."""
    if value is None:
        return "-"
    return f"{value:.2f}"
