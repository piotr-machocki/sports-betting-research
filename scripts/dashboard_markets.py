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
    snapshot_time,
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
.markets-bar { display: flex; flex-wrap: wrap; gap: 4px 28px; margin: 6px 0 10px; }
.markets-bar .value { font-size: 17px; font-weight: 600; }
.markets-bar .label { font-size: 12px; color: color-mix(in srgb, currentColor 65%, transparent); }
.markets-source { font-weight: 600; margin: 2px 0 0; }
.markets-tiles + .markets-source { margin-top: 6px; }
/* A phone gets the table too (sideways scroll): there are no cards for this tab. */
@media (max-width: 700px) { table.matches.markets { display: table; } }
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
        record[f"_style {label}"] = "" if view and view["tight"] else GREY

    record["EV Pinnacle"] = signed_percent(data["evs"]["pinnacle"])
    record["_style EV Pinnacle"] = ev_style(data["evs"]["pinnacle"], min_ev)
    for source in SOURCES:
        label = SOURCE_LABELS[source]
        value = data["evs"][source]
        tight = source in trusted
        record[f"EV {label}"] = signed_percent(value) if value is not None else "--"
        record[f"_style EV {label}"] = ev_style(value, min_ev, tight)
        if value is not None and not tight:
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


def tile(label: str, value: str, help_text: str, style: str = "") -> str:
    css = f' style="{style}"' if style else ""
    return (
        f'<div class="overview-tile" title="{html.escape(help_text)}">'
        f'<div class="value"{css}>{html.escape(value)}</div>'
        f'<div class="label">{html.escape(label)}</div></div>'
    )


def meters(stats: dict) -> str:
    """Coverage of the matches on Pinnacle's prematch list: each book that priced it,
    then each market (dark = tight spread). Pinnacle itself is the 100% and has no bar."""
    total = stats["matches"]

    def share(count: int) -> float:
        return min(100.0, 100 * count / total) if total else 0

    cells = []
    for book, count in stats.get("books", {}).items():
        cells.append(
            f"<div>{book}</div>"
            f'<div class="overview-meter" title="{count} of {total} matches priced on {book}">'
            f'<div style="width: {share(count):.1f}%; background-color: #2a78d6"></div></div>'
            f'<div class="count">{count}</div>'
        )
    for source in SOURCES:
        part = stats["sources"][source]
        cells.append(
            f"<div>{SOURCE_LABELS[source]}</div>"
            f'<div class="overview-meter" title="{part["matched"]} of {total} '
            f'matches on {SOURCE_LABELS[source]}, {part["tight"]} with a tight spread" '
            'style="position: relative">'
            f'<div style="position: absolute; width: {share(part["matched"]):.1f}%; '
            'background-color: rgba(42, 120, 214, 0.45)"></div>'
            f'<div style="position: absolute; width: {share(part["tight"]):.1f}%; '
            'background-color: #2a78d6"></div></div>'
            f'<div class="count">{part["matched"]} ({part["tight"]} tight)</div>'
        )
    note = (
        f'<div class="overview-note">Of {total} prematch matches on Pinnacle&#39;s list. '
        f'Markets dark: spread within the limit. {stats["started"]} started, not compared.</div>'
    )
    return f'<div class="overview-meters">{"".join(cells)}</div>{note}'


def agreement_tiles(stats: dict) -> str:
    parts = []
    for source in SOURCES:
        part = stats["sources"][source]
        compared = part["compared"]
        gap = part["median_abs_gap"]
        tiles = [
            tile(
                "Median gap",
                "--" if gap is None else f"{gap * 100:.1f} pp",
                "Median distance between the market's probability and Pinnacle's, "
                f"over {compared} prematch matches with a tight spread.",
            ),
            tile(
                "Within 2 pp",
                f"{part['within_2pp'] / compared:.0%}" if compared else "--",
                f"{part['within_2pp']} of {compared} matches within 2 percentage points.",
            ),
            tile(
                "Favourite differs",
                str(part["flips"]),
                "Matches where the market and Pinnacle pick a different favourite.",
            ),
        ]
        parts.append(
            f'<div class="markets-source">{SOURCE_LABELS[source]}</div>'
            f'<div class="overview-tiles markets-tiles">{"".join(tiles)}</div>'
        )
    return "".join(parts)


def signal_tiles(stats: dict, min_ev: float) -> str:
    threshold = f"EV ≥ {min_ev * 100:.1f}%"
    tiles = [
        tile("vs Pinnacle", str(stats["pinnacle_positive"]),
             f"Matches where a book's price has {threshold} against Pinnacle."),
    ]
    for source in SOURCES:
        tiles.append(
            tile(f"vs {SOURCE_LABELS[source]}", str(stats["sources"][source]["positive"]),
                 f"Matches where a book's price has {threshold} against "
                 f"{SOURCE_LABELS[source]} (tight spreads only).")
        )
    tiles.append(
        tile("Agree", str(stats["agreeing"]),
             f"{threshold} against Pinnacle and every tight market, on the same side.",
             "color: #1f9d55" if stats["agreeing"] else "")
    )
    return f'<div class="overview-tiles">{"".join(tiles)}</div>'


def overview(stats: dict, min_ev: float) -> str:
    def card(heading: str, body: str) -> str:
        return (
            f'<div class="overview-card wide"><div class="overview-heading">'
            f"{html.escape(heading)}</div>{body}</div>"
        )

    return (
        '<section class="overview"><div class="overview-cards">'
        + card("Matches found", meters(stats))
        + card("Agreement with Pinnacle", agreement_tiles(stats))
        + "</div><div class=\"overview-cards\">"
        + card("Signals (comparison only, nothing is sent)", signal_tiles(stats, min_ev))
        + "</div></section>"
    )


def age_text(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "--"
    seconds = max(0, (now - moment).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60)} s ago"
    return f"{seconds / 3600:.1f} h ago"


def render_markets(st) -> None:
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
        on_market = st.checkbox(
            "Only matches on a market", value=True, key="markets_only",
        )
        player = st.text_input("Player", key="markets_player")

    st.html(MARKETS_STYLE)
    st.html(
        '<div class="markets-note">Polymarket and Kalshi against Pinnacle, for '
        "comparison only: no Telegram alerts, and nothing here counts in the Tennis "
        "page or its Track record until the markets have been checked against closing "
        "lines and results. A market's probability is the middle of its bid and ask.</div>"
    )

    @st.fragment(run_every=IDLE_REFRESH_SECONDS)
    def watch() -> None:
        pair = newest_pair()
        if pair != st.session_state.get("markets_drawn"):
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
    started = datetime.strptime(
        "".join(session_id.split("_")[1:3]), "%Y%m%d%H%M%S"
    ).replace(tzinfo=timezone.utc)

    st.html(
        '<div class="markets-bar">'
        f'<div><div class="value">{started.astimezone():%Y-%m-%d %H:%M}</div>'
        '<div class="label">Run on</div></div>'
        f'<div><div class="value">{age_text(snapshot_time(snapshot), now)}</div>'
        '<div class="label">Markets read</div></div></div>'
        + overview(stats, min_ev)
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
