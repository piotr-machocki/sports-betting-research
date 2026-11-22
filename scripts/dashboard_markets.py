"""
The dashboard's Prediction markets subtab of Tennis: Polymarket and Kalshi next to Pinnacle and the books.

Since 20 Nov 2026 (user's request). Comparison only, like the files it reads
(scripts/tennis_markets.py, matched by scripts/market_compare.py): no Telegram, no
rechecks, nothing in the Tennis page or its Track record. It follows the newest run
that has a market file (written right after the run, so about a minute behind the
Tennis page during a run).
"""

from __future__ import annotations

import html
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from live_monitor import SESSIONS_DIR, latest_per_match, read_session_rows
from market_compare import (
    DEFAULT_MAX_SPREAD,
    SOURCE_LABELS,
    SOURCES,
    agreement,
    best_side,
    compare_session,
    latest_snapshots,
    market_stats,
    parse_time,
    read_snapshot,
    trusted_evs,
)

IDLE_REFRESH_SECONDS = 10
DEFAULT_MIN_EV_PERCENT = 2.0
MAX_MIN_EV_PERCENT = 5.0
CHECK = "✓"

COLUMN_HELP = {
    "Bet on": "The side with the best EV against Pinnacle or a tight market. "
    "Every value in the row is for this side.",
    "Book": "The book with the better price for this side (Superbet after its 12% tax).",
    "Price": "As the book shows it. Superbet's EV uses 0.88 x this.",
    "Pinnacle": "Pinnacle's probability, power devig (the Tennis page's reference).",
    "Polymarket": "Middle of the bid and ask. Hover for the book. Grey: the spread "
    "is wider than the sidebar's limit, so it is not trusted.",
    "Kalshi": "Middle of the bid and ask, scaled with the other player's market to add "
    "up to 100%. Hover for the book. Grey: spread wider than the limit.",
    "EV Pinnacle": "Book price x Pinnacle's probability - 1, as on the Tennis page.",
    "EV Polymarket": "Book price x Polymarket's probability - 1.",
    "EV Kalshi": "Book price x Kalshi's probability - 1.",
    "Agree": "Pinnacle and every tight market give this side at least the minimum EV.",
}

MARKETS_STYLE = """<style>
.markets-note { font-size: 13px; color: color-mix(in srgb, currentColor 65%, transparent); }
/* A phone gets the table too (sideways scroll): there are no cards for this tab. */
@media (max-width: 700px) { table.matches.markets { display: table; } }
/* The fork (user, 22 Nov 2026: boxes stretched to fill a row looked odd): the
   branches share a grid, so the boxes of one column are one size, as wide as the
   longest name in it, and spare room stays empty on the right. Pinnacle is as tall
   as the branches; under them on screens too narrow for both. */
.markets-flow .flow-fork { flex-wrap: wrap; gap: 6px 0; }
.markets-flow .flow-fork > .flow-node { flex: none; min-width: max-content; }
.markets-flow .flow-branches {
    display: grid;
    grid-template-columns: repeat(4, auto);
    justify-content: start;
    align-items: stretch;
    gap: 6px 0;
}
.markets-flow .flow-branch, .markets-flow .flow-step { display: contents; }
/* Each branch on its own row. */
.markets-flow .flow-step:first-child > .flow-arrow { grid-column: 1; }
.markets-flow .flow-branches .flow-node { max-width: none; min-width: max-content; padding: 6px 12px; }
@media (max-width: 700px) {
    .markets-flow .flow-branches .flow-node { padding: 6px 8px; }
    .markets-flow .flow-fork .flow-arrow { min-width: 40px; padding: 0 3px; }
    .markets-flow .flow-fork > .flow-node { padding: 6px 6px; }
}
</style>"""

GREEN = "color: #1f9d55; background-color: rgba(31, 157, 85, 0.16)"
RED = "color: #d64545"
GREY = "color: #888888"


def newest_pair(markets_dir: Path | None = None) -> tuple[str, Path] | None:
    """The newest run with both a market file and a session CSV."""
    files = latest_snapshots(markets_dir) if markets_dir else latest_snapshots()
    for path in files:
        session_id = path.name.removesuffix(".csv.gz")
        if (SESSIONS_DIR / f"{session_id}.csv").exists():
            return session_id, path
    return None


def percent(value: float | None, digits: int = 1) -> str:
    return "--" if value is None else f"{value * 100:.{digits}f}%"


def signed_percent(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:+.1f}%"


def ev_style(value: float | None, min_ev: float, trusted: bool = True) -> str:
    if value is None or not trusted:
        return GREY
    if value >= min_ev:
        return GREEN
    return RED if value < 0 else ""


def quote_text(view: dict, side: int) -> str:
    quote = view["sides"][side]
    other = view["sides"][3 - side]

    def cents(value):
        return "--" if value is None else f"{value * 100:.1f}¢"

    lines = [
        f"{view['names'][side - 1]}: bid {cents(quote['bid'])}, ask {cents(quote['ask'])}",
        f"{view['names'][2 - side]}: bid {cents(other['bid'])}, ask {cents(other['ask'])}",
        f"Wider spread: {cents(view['spread'])}",
    ]
    if view.get("volume"):
        lines.append(f"Volume: ${view['volume']:,.0f}")
    if view.get("liquidity"):
        lines.append(f"Liquidity: ${view['liquidity']:,.0f}")
    lines.append(view["event_id"])
    return "\n".join(lines)


def match_record(match: dict, min_ev: float) -> dict:
    """One table row, for the Bet on side."""
    row = match["row"]
    side = best_side(match)
    data = match["sides"][side]
    trusted = trusted_evs(match, side)
    start = parse_time(row.get("pinnacle_starts_at"))
    book = data["book"]
    shown = data["prices"].get(book)
    if book == "Superbet":
        shown = shown / 0.88 if shown else None

    record = {
        "Bet on": row.get(f"pinnacle_player{side}") or "",
        "Opponent": row.get(f"pinnacle_player{3 - side}") or "",
        "Starts": start.astimezone().strftime("%d %b %H:%M") if start else "",
        "Tournament": row.get("pinnacle_tournament") or "",
        "Book": book or "--",
        "Price": f"{shown:.2f}" if shown else "--",
        "Pinnacle": percent(data["pinnacle"]),
        "_style Pinnacle": "",
    }

    for source in SOURCES:
        label = SOURCE_LABELS[source]
        view = match["markets"].get(source)
        chance = None
        if view and view["probability1"] is not None:
            chance = view["probability1"] if side == 1 else 1 - view["probability1"]
        record[label] = percent(chance) if view else ""
        record[f"_title {label}"] = quote_text(view, side) if view else "Not on this market"
        record[f"_style {label}"] = "" if view and view["tight"] and not view["off"] else GREY

    record["EV Pinnacle"] = signed_percent(data["evs"]["pinnacle"])
    record["_style EV Pinnacle"] = ev_style(data["evs"]["pinnacle"], min_ev)
    for source in SOURCES:
        label = SOURCE_LABELS[source]
        value = data["evs"][source]
        tight = source in trusted
        record[f"EV {label}"] = signed_percent(value) if value is not None else "--"
        record[f"_style EV {label}"] = ev_style(value, min_ev, tight)
        view = match["markets"].get(source)
        if value is not None and view and view["off"]:
            record[f"_title EV {label}"] = (
                "Over 10 pp from Pinnacle: probably not a real price (e.g. a new market "
                "still at its opening 50/50), not trusted"
            )
        elif value is not None and not tight:
            record[f"_title EV {label}"] = "Spread wider than the limit: not trusted"

    record["Agree"] = CHECK if agreement(match, side, min_ev) else ""
    record["_style Agree"] = GREEN if record["Agree"] else ""
    if match["started"]:
        record["_title Bet on"] = "Started: market prices are in-play, so no EV"
    record["_dimmed"] = match["started"] or data["price"] is None
    record["_sort"] = (
        match["started"],
        -max(trusted.values(), default=-9),
        not match["markets"],
    )
    return record


def table_html(records: list[dict]) -> str:
    columns = [c for c in records[0] if not c.startswith("_")]
    text_columns = {"Bet on", "Opponent", "Starts", "Tournament", "Book"}

    def attr(name: str, value: Any) -> str:
        return f' {name}="{html.escape(str(value))}"' if value else ""

    heads = "".join(
        f"<th{attr('class', '' if c in text_columns else 'number')}"
        f"{attr('title', COLUMN_HELP.get(c))}>{html.escape(c)}</th>"
        for c in columns
    )
    rows = []
    for record in records:
        cells = []
        for column in columns:
            style = record.get(f"_style {column}", "")
            if record["_dimmed"] and column not in {"Agree"}:
                style = GREY
            cells.append(
                f"<td{attr('class', '' if column in text_columns else 'number')}"
                f"{attr('style', style)}{attr('title', record.get(f'_title {column}'))}>"
                f"{html.escape(str(record[column]))}</td>"
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        f'<table class="matches markets"><thead><tr>{heads}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


# Bar colours by kind: sharp reference, prediction market, soft book.
SHARP_COLOUR = "#e0a526"
MARKET_COLOUR, MARKET_LIGHT = "#1f9d55", "rgba(31, 157, 85, 0.4)"
BOOK_COLOUR = "#2a78d6"


def market_count(part: dict, total: int, max_spread: float = DEFAULT_MAX_SPREAD) -> str:
    """A market's count as "33 (3) / 38": matches found, the loose ones (spread over
    the limit) greyed in brackets with a tap/hover explanation, then Pinnacle's
    prematch total."""
    loose = part["matched"] - part["tight"]
    tip = (
        f"{loose} of {part['matched']} with a spread wider than the limit "
        f"({round(max_spread * 100)} cents)."
    )
    # A tap (tabindex) or hover opens the tip; it opens leftwards from the right edge.
    return (
        f'{part["matched"]} <span class="overview-note has-tip" tabindex="0">({loose})'
        '<span class="tip" style="left: auto; right: 0; text-align: left">'
        f"{html.escape(tip)}</span></span> / {total}"
    )


def meters(stats: dict, max_spread: float = DEFAULT_MAX_SPREAD) -> str:
    """Coverage of the matches on Pinnacle's prematch list: Pinnacle (the 100%, yellow),
    each market (green, bright part = tight spread), then each book that priced it (blue)."""
    total = stats["matches"]

    def share(count: int) -> float:
        return min(100.0, 100 * count / total) if total else 0

    cells = [
        "<div>Pinnacle</div>"
        f'<div class="overview-meter" title="{total} prematch matches on Pinnacle&#39;s list">'
        f'<div style="width: 100%; background-color: {SHARP_COLOUR}"></div></div>'
        f'<div class="count">{total}</div>'
    ]
    for source in SOURCES:
        part = stats["sources"][source]
        cells.append(
            f"<div>{SOURCE_LABELS[source]}</div>"
            f'<div class="overview-meter" title="{part["matched"]} of {total} '
            f'matches on {SOURCE_LABELS[source]}, {part["tight"]} with a tight spread" '
            'style="position: relative">'
            f'<div style="position: absolute; width: {share(part["matched"]):.1f}%; '
            f'background-color: {MARKET_LIGHT}"></div>'
            f'<div style="position: absolute; width: {share(part["tight"]):.1f}%; '
            f'background-color: {MARKET_COLOUR}"></div></div>'
            f'<div class="count">{market_count(part, total, max_spread)}</div>'
        )
    for book, count in stats.get("books", {}).items():
        cells.append(
            f"<div>{book}</div>"
            f'<div class="overview-meter" title="{count} of {total} matches priced on {book}">'
            f'<div style="width: {share(count):.1f}%; background-color: {BOOK_COLOUR}"></div></div>'
            f'<div class="count">{count} / {total}</div>'
        )
    return f'<div class="overview-meters">{"".join(cells)}</div>'


def pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "--"
    return f"{value:+.1%}" if signed else f"{value:.0%}"


def market_track_card(dash: Any, record: dict | None, min_ev: float, error: str | None) -> str:
    """Statistics (all runs) for the market signals (scripts/market_track.py), one row
    of tiles per reference, like the Tennis page's Track record (user, 22 Nov 2026:
    it replaced the Agreement with Pinnacle and Signals cards)."""
    if not record:
        if record is not None:
            text = f"No match has reached EV ≥ {min_ev:.1%} against a market before its start yet."
            body = f'<div class="overview-empty">{html.escape(text)}</div>'
        elif error:
            body = f'<div class="overview-empty">Could not work it out: {html.escape(error)}</div>'
        else:
            body = (
                '<div class="overview-empty overview-loading"><span class="spinner"></span>'
                "<span>Loading (a few minutes the first time)…</span></div>"
            )
        return f'<div class="overview-card">{body}</div>'

    rows = []
    for reference, stats in record.items():
        expected, margin = stats["expected"], stats["roi_margin"]
        agree_share = stats["agree"] / stats["signals"] if stats["signals"] else None
        what = (
            "Polymarket and against Kalshi at once (the lower EV counts), both within "
            "the spread limit" if reference == "Both" else f"{reference}, within the spread limit"
        )
        tiles = [
            dash.tip_tile(
                "Signals", str(stats["signals"]),
                f"Betclic or Superbet prices with EV ≥ {min_ev:.1%} against {what}; "
                "each match once, at its first flag before the start. Logged only, "
                "nothing is sent.",
            ),
            dash.tip_tile(
                "Agree w/ Pinnacle",
                f"{stats['agree']} ({pct(agree_share, signed=False)})",
                f"Signals where Pinnacle also gave the price EV ≥ {min_ev:.1%} at the flag. "
                f"Mean CLV: agreeing {pct(stats['agree_clv'])}, market only "
                f"{pct(stats['alone_clv'])}.",
            ),
            dash.overview_tile(
                "Avg CLV", pct(stats["clv"]),
                f"Mean CLV of the {stats['clv_count']} started matches: the flagged price "
                "against Pinnacle's last power-devigged price before the start.",
                dash.signed_css(stats["clv"]),
            ),
            dash.overview_tile(
                "CLV > 0", pct(stats["clv_positive"], signed=False),
                f"Share of the {stats['clv_count']} started matches whose price beat the close",
            ),
            dash.overview_tile(
                "Won-Lost", f"{stats['won']}-{stats['lost']}", "Completed matches only",
            ),
            dash.overview_tile(
                "Win rate" + (f" (exp. {expected:.0%})" if expected is not None else ""),
                pct(stats["win_rate"], signed=False),
                f"Won share of completed matches; expected = mean of {reference}'s "
                "probability for the side bet",
            ),
            dash.overview_tile(
                "ROI" + (f" ±{margin:.0%}" if margin is not None else ""), pct(stats["roi"]),
                "Profit per stake of 1 on completed matches, with its 95% range.",
                dash.signed_css(stats["roi"]),
            ),
            dash.overview_tile(
                "Units", f"{stats['units']:+.2f}",
                "Total profit at a stake of 1 per completed match",
                dash.signed_css(stats["units"]),
            ),
            dash.overview_tile(
                "Avg EV", pct(stats["ev"]), "Mean EV at the first flag, what the prices promised",
            ),
        ]
        rows.append(
            f'<div class="track-book">{html.escape(reference)}</div>'
            f'<div class="overview-tiles">{"".join(tiles)}</div>'
        )
    return f'<div class="overview-card"><div class="track-rows">{"".join(rows)}</div></div>'


def source_times(snapshot: list[dict]) -> dict[str, datetime]:
    """Each market's capture time in the file (the newest, should one differ)."""
    times: dict[str, datetime] = {}
    for row in snapshot:
        moment = parse_time(row.get("captured_at"))
        source = row.get("source")
        if moment and source in SOURCES and (source not in times or moment > times[source]):
            times[source] = moment
    return times


def flow_summary(rows: list[dict], snapshot: list[dict], now: datetime) -> tuple[dict, list]:
    """The Tennis page's Data flow and age, for the markets (user, 22 Nov 2026): the
    run's Pinnacle age, then the books and markets in the order their prices came,
    each the median time from a match's Pinnacle capture to that source's. Files before 22 Nov 2026 give both markets one time."""
    pinnacle = [t for t in (parse_time(row.get("pinnacle_captured_at")) for row in rows) if t]
    summary: dict[str, Any] = {}
    if pinnacle:
        summary["pinnacle_age"] = (now - min(pinnacle)).total_seconds()
        summary["pinnacle_captured"] = min(pinnacle).timestamp()

    gaps = []
    # The books first, per match from the session rows (as on the Tennis page)...
    for book in ("Betclic", "Superbet"):
        spans = [
            (book_time - start).total_seconds()
            for row in rows
            for start, book_time in [(
                parse_time(row.get("pinnacle_captured_at")),
                parse_time(row.get(f"{book.lower()}_captured_at")),
            )]
            if start and book_time
        ]
        gaps.append((statistics.median(spans) if spans else None, book))

    # ...then the markets, read once the run is over.
    times = source_times(snapshot)
    for source in SOURCES:
        moment = times.get(source)
        seconds = (
            statistics.median((moment - start).total_seconds() for start in pinnacle)
            if moment and pinnacle else None
        )
        gaps.append((seconds, SOURCE_LABELS[source]))
    return summary, gaps


def overview(
    stats: dict, max_spread: float = DEFAULT_MAX_SPREAD, flow: str = "", track: str = ""
) -> str:
    def card(heading: str, body: str) -> str:
        return (
            f'<div class="overview-card wide"><div class="overview-heading">'
            f"{html.escape(heading)}</div>{body}</div>"
        )

    return (
        '<section class="overview"><div class="overview-cards">'
        + card("Matches found", meters(stats, max_spread))
        + (card("Data flow and age", f'<div class="markets-flow">{flow}</div>') if flow else "")
        + "</div>"
        + (
            '<div class="overview-title">Statistics (all runs)</div>'
            f'<div class="overview-cards">{track}</div>' if track else ""
        )
        + "</section>"
    )


def render_markets(st, dash: Any = None) -> None:
    if dash is None:
        import dashboard as dash
    with st.sidebar:
        min_ev = st.slider(
            "Minimum EV", 0.0, MAX_MIN_EV_PERCENT, DEFAULT_MIN_EV_PERCENT, 0.1,
            format="%.1f%%", key="markets_min_ev",
            help="Green from here up, against any reference.",
        ) / 100
        max_spread = st.slider(
            "Maximum spread", 1, 10, round(DEFAULT_MAX_SPREAD * 100), 1,
            format="%d¢", key="markets_max_spread",
            help="A market whose wider side's bid-ask spread is above this is shown in "
            "grey and its EV is not trusted: a thin market can sit on a stale quote.",
        ) / 100
        longshots = st.checkbox(
            "Include long shots", value=True, key="markets_longshots",
            help="In Statistics, count signals whose market probability is below 15% "
            "(odds above about 6.5). Markets tend to overprice outsiders, and a cent "
            "on a 5% price is a fifth of it, so these can look like value that is not.",
        )
        on_market = st.checkbox(
            "Only matches on a market", value=True, key="markets_only",
        )
        player = st.text_input("Player", key="markets_player")
        # At the bottom of the sidebar (user, 22 Nov 2026; it was a note over the page).
        st.caption(
            "Nothing is sent to Telegram or counts on the Tennis page until the markets "
            "have proved themselves against closing lines and results. A market's "
            "probability is the middle of its bid and ask."
        )

    st.html(MARKETS_STYLE)

    loader = st.cache_resource(show_spinner=False)(dash.market_loader)()
    track_key = (min_ev, max_spread, longshots)

    @st.fragment(run_every=IDLE_REFRESH_SECONDS)
    def watch() -> None:
        pair = newest_pair()
        # A new run, or the Statistics card has finished loading.
        arrived = st.session_state.get("markets_track_loading") and loader.ready_key(track_key)
        if pair != st.session_state.get("markets_drawn") or arrived:
            st.rerun(scope="app")

    pair = newest_pair()
    st.session_state["markets_drawn"] = pair
    watch()

    if not pair:
        st.info(
            "No prediction-market file yet. run_once.py saves one after every run "
            "(scripts/tennis_markets.py; skipped with --no-markets)."
        )
        return

    session_id, path = pair
    rows = read_session_rows(SESSIONS_DIR / f"{session_id}.csv") or []
    snapshot = read_snapshot(path)
    matches = compare_session(latest_per_match(rows), snapshot, max_spread)
    stats = market_stats(matches, min_ev)
    now = datetime.now(timezone.utc)
    summary, gaps = flow_summary(latest_per_match(rows), snapshot, now)
    info = dash.session_info(session_id)
    summary["pinnacle_source"] = dash.pinnacle_source(info)
    # Superbet then Betclic, and both markets at once, all started with Superbet.
    by_name = {name: (seconds, name) for seconds, name in gaps}
    flow = dash.fork_html(summary, [
        [by_name["Superbet"], by_name["Betclic"]],
        [by_name["Polymarket"]],
        [by_name["Kalshi"]],
    ])
    record = loader.request_key(track_key, wait=dash.TRACK_WAIT_SECONDS)
    st.session_state["markets_track_loading"] = record is None and loader.error is None
    track = market_track_card(dash, record, min_ev, loader.error)
    # The Tennis page's heading (user, 22 Nov 2026): Last run overview with the run's
    # state and time; the Pinnacle age is in Data flow and age.
    summary["status"] = str(info.get("status", "running")).lower()
    dash.pill_state(summary, session_id)

    st.html(
        dash.session_bar_html(summary, session_id)
        + overview(stats, max_spread, flow, track)
    )

    shown = [m for m in matches if m["markets"]] if on_market else matches
    needle = player.strip().lower()
    if needle:
        shown = [
            m for m in shown
            if needle in (m["row"].get("pinnacle_player1") or "").lower()
            or needle in (m["row"].get("pinnacle_player2") or "").lower()
        ]
    records = sorted((match_record(m, min_ev) for m in shown), key=lambda r: r["_sort"])

    st.html(f'<div class="markets-note">Showing {len(records)} of {len(matches)} matches</div>')
    if records:
        st.html(table_html(records))
    else:
        st.info("No match fits these filters.")
