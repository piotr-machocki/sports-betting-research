"""
The Props subtab of Tennis: set and games markets of the soft books against Pinnacle.

Since 25 Nov 2026 (user's request). Reads the snapshots of scripts/tennis_props.py
(data/tennis_props/snapshots/), nothing else; nothing is sent or counted elsewhere.

- Newest run: for every match, market and line Pinnacle prices on both sides, its
  power-devigged probability (market_compare.power_devig, no scipy) against the
  books' price for the same line: EV = price x probability - 1, Superbet's price
  x 0.88, Betclic only from 1.14 (results_store.BETCLIC_MIN_ODDS). A handicap is
  keyed by player 1's line, so "p2 +5.5" meets "p1 -5.5". Polymarket and Kalshi
  show their mid for the same outcome, for comparison only.
- All runs (`History`): each book offer counts once, at its first run at or above
  the minimum EV before the start; its CLV = that price x Pinnacle's probability for
  the same line at the last run before the start - 1. Kept incrementally in
  data/cache/props_history.pkl (one closing price per line, one first flag per
  offer), so it does not grow with the number of runs. Started flags are settled from
  final set scores (props_results.py: Matchstat plus Superbet's finished feed);
  W-L, ROI and units count completed matches only, at the price paid.
"""

from __future__ import annotations

import csv
import gzip
import html
import pickle
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import market_compare
import match_results
import props_results
from results_store import BETCLIC_MIN_ODDS

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = ROOT / "data" / "tennis_props" / "snapshots"
HISTORY_FILE = ROOT / "data" / "cache" / "props_history.pkl"
LOCAL_TIME = ZoneInfo("Europe/Warsaw")
SUPERBET_TAX_FACTOR = market_compare.SUPERBET_TAX_FACTOR

BOOKS = ("Superbet", "Betclic")
EXCHANGES = ("Polymarket", "Kalshi")
DEFAULT_MIN_EV_PERCENT = 2.0
REFRESH_SECONDS = 30

# Markets Pinnacle prices (the comparable ones), in display order.
MARKET_LABELS = {
    "match_winner": "Match winner",
    "set1_winner": "Set 1 winner",
    "set_handicap": "Set handicap",
    "total_sets": "Total sets",
    "game_handicap": "Game handicap",
    "total_games": "Total games",
    "set1_game_handicap": "Set 1 game handicap",
    "set1_total_games": "Set 1 total games",
}
HANDICAPS = {"set_handicap", "game_handicap", "set1_game_handicap"}

GREEN = "color: #1f9d55; background-color: rgba(31, 157, 85, 0.16)"
RED = "color: #d64545"

PROPS_STYLE = """<style>
.props-note { font-size: 13px; color: color-mix(in srgb, currentColor 65%, transparent); }
@media (max-width: 700px) { table.matches.props { display: table; } }
table.props-summary { border-collapse: collapse; font-size: 13px; }
table.props-summary th, table.props-summary td { padding: 3px 10px; text-align: right; white-space: nowrap; }
table.props-summary th:first-child, table.props-summary td:first-child { text-align: left; }
table.props-summary thead th { font-weight: 600; border-bottom: 1px solid rgba(128,128,128,0.35); }
.props-summary-wrap { overflow-x: auto; }
</style>"""


# ---------------------------------------------------------------- reading --


def snapshots(snapshot_dir: Path = SNAPSHOT_DIR) -> list[Path]:
    """Every snapshot, oldest first (names hold the UTC time)."""
    return sorted(snapshot_dir.glob("*/props_*.csv.gz"), key=lambda p: p.name)


def read_snapshot(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def number(value: Any) -> float | None:
    return market_compare.number(value)


def key_line(row: dict) -> str:
    """The line an offer is grouped by: player 1's handicap for handicaps (p2 +5.5 is
    p1 -5.5), the total for totals, "" for winners."""
    line = number(row.get("line"))
    if line is None:
        return ""
    if row.get("market") in HANDICAPS and row.get("side") == "p2":
        line = -line
    return f"{line:g}"


def group_key(row: dict) -> tuple[str, str, str]:
    return str(row.get("pinnacle_event_id")), row.get("market", ""), key_line(row)


def pinnacle_fair(rows: list[dict]) -> dict[tuple, dict[str, float]]:
    """(event, market, line) -> {side: fair probability} for every two-way Pinnacle pair."""
    prices: dict[tuple, dict[str, float]] = {}
    for row in rows:
        if row.get("source") == "pinnacle" and row.get("market") in MARKET_LABELS:
            price = number(row.get("price"))
            if price and price > 1:
                prices.setdefault(group_key(row), {})[row["side"]] = price
    fair = {}
    for key, sides in prices.items():
        pair = ("over", "under") if "over" in sides or "under" in sides else ("p1", "p2")
        if all(side in sides for side in pair):
            first, second = market_compare.power_devig(sides[pair[0]], sides[pair[1]])
            fair[key] = {pair[0]: first, pair[1]: second}
    return fair


def net_price(row: dict) -> float | None:
    """What a bet pays per unit staked: Superbet after its tax, Betclic as shown but
    only from its lowest single price."""
    price = number(row.get("price"))
    if not price:
        return None
    if row.get("book") == "Superbet":
        return price * SUPERBET_TAX_FACTOR
    if row.get("book") == "Betclic" and price < BETCLIC_MIN_ODDS:
        return None
    return price


def exchange_mids(rows: list[dict]) -> dict[tuple, dict[str, float]]:
    """(event, market, line, side) -> {exchange: mid probability}."""
    mids: dict[tuple, dict[str, float]] = {}
    for row in rows:
        if row.get("book") not in EXCHANGES:
            continue
        bid, ask = number(row.get("bid")), number(row.get("ask"))
        if bid is None or ask is None:
            continue
        mids.setdefault((*group_key(row), row["side"]), {})[row["book"]] = (bid + ask) / 2
    return mids


def offers(rows: list[dict], books: tuple[str, ...] = BOOKS) -> list[dict]:
    """Every book price that Pinnacle prices the same line of, with its EV."""
    fair = pinnacle_fair(rows)
    mids = exchange_mids(rows)
    out = []
    for row in rows:
        if row.get("book") not in books:
            continue
        key = group_key(row)
        chance = (fair.get(key) or {}).get(row.get("side"))
        price = net_price(row)
        if chance is None or price is None:
            continue
        out.append({
            "key": key, "side": row["side"], "book": row["book"], "market": row["market"],
            "line": number(row.get("line")), "price": number(row["price"]), "net": price,
            "probability": chance, "ev": price * chance - 1,
            "player1": row.get("player1", ""), "player2": row.get("player2", ""),
            "tournament": row.get("tournament", ""), "starts_at": row.get("starts_at", ""),
            "captured_at": row.get("captured_at", ""),
            "exchanges": mids.get((*key, row["side"]), {}),
        })
    return out


# ---------------------------------------------------------------- summary --


def market_summary(rows: list[dict], found: list[dict], min_ev: float) -> list[dict]:
    """One row per market: Pinnacle's priced matches, then per book how many of them
    it prices on the same line, its median EV and how many offers reach the minimum."""
    fair = pinnacle_fair(rows)
    summary = []
    for market, label in MARKET_LABELS.items():
        matches = {key[0] for key in fair if key[1] == market}
        entry = {"market": market, "label": label, "pinnacle": len(matches), "books": {}}
        for book in BOOKS:
            mine = [o for o in found if o["market"] == market and o["book"] == book]
            entry["books"][book] = {
                "matches": len({o["key"][0] for o in mine}),
                "offers": len(mine),
                "median_ev": statistics.median(o["ev"] for o in mine) if mine else None,
                "signals": sum(o["ev"] >= min_ev for o in mine),
            }
        summary.append(entry)
    return summary


# ---------------------------------------------------------------- history --


def empty_history() -> dict:
    return {"files": set(), "close": {}, "flags": {}, "runs": 0, "first": "", "last": ""}


def add_snapshot(history: dict, name: str, rows: list[dict]) -> None:
    """Fold one run into the history: Pinnacle's probability per line as the newest
    one before the start (the close), and each book offer's first run with EV >= 0.
    Runs must come oldest first."""
    history["files"].add(name)
    history["runs"] += 1
    captured = rows[0].get("captured_at", "") if rows else ""
    if captured:
        history["first"] = history["first"] or captured
        history["last"] = captured
    starts = {str(r.get("pinnacle_event_id")): r.get("starts_at", "") for r in rows}
    for key, sides in pinnacle_fair(rows).items():
        if captured and captured < starts.get(key[0], ""):
            history["close"][key] = (captured, sides)
    for offer in offers(rows):
        if offer["ev"] < 0 or not offer["starts_at"] or offer["captured_at"] >= offer["starts_at"]:
            continue
        flag_key = (*offer["key"], offer["side"], offer["book"])
        flags = history["flags"].setdefault(flag_key, [])
        # The first sighting at each half percent of EV (the slider's step) is enough to
        # find, later, the first one at or above any minimum on the sidebar.
        if not flags or ev_step(offer["ev"]) > max(ev_step(f["ev"]) for f in flags):
            flags.append({k: offer[k] for k in (
                "market", "price", "net", "probability", "ev", "starts_at", "captured_at",
                "player1", "player2", "line")})


def ev_step(ev: float) -> int:
    return int(ev * 200 + 1e-9)


def history_flags(history: dict, min_ev: float, now: datetime) -> list[dict]:
    """Each offer once, at its first run at or above `min_ev`, with CLV once started."""
    out = []
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for flag_key, flags in history["flags"].items():
        first = next((f for f in flags if f["ev"] >= min_ev), None)
        if first is None:
            continue
        key, side, book = flag_key[:3], flag_key[3], flag_key[4]
        record = {**first, "book": book, "side": side, "clv": None, "event": key[0],
                  "started": first["starts_at"] <= stamp, "outcome": None, "profit": None}
        close = history["close"].get(key)
        if first["starts_at"] <= stamp and close and close[1].get(side):
            record["clv"] = first["net"] * close[1][side] - 1
        out.append(record)
    return out


# A started match with no result this long after its start is "no result", not pending.
RESULT_WAIT_HOURS = 72


def settle_flags(flags: list[dict], outcomes: dict[str, tuple[str, Any]]) -> None:
    """Fill outcome and profit from each event's (state, sets)."""
    for flag in flags:
        state, sets = outcomes.get(flag["event"], ("pending", None))
        flag["outcome"] = None
        if state == "settled":
            flag["outcome"] = props_results.settle(flag["market"], flag["side"], flag["line"], sets)
        elif state == "void":
            flag["outcome"] = "void"
        flag["profit"] = props_results.profit(flag["outcome"], flag["net"]) if flag["outcome"] else None


def history_stats(flags: list[dict], book: str, market: str | None = None) -> dict:
    mine = [f for f in flags if f["book"] == book and (market is None or f["market"] == market)]
    clvs = [f["clv"] for f in mine if f["clv"] is not None]
    settled = [f for f in mine if f.get("profit") is not None]
    outcomes = [f.get("outcome") for f in mine]
    units = sum(f["profit"] for f in settled)
    return {
        "won": outcomes.count("won"), "lost": outcomes.count("lost"),
        "push": outcomes.count("push"), "void": outcomes.count("void"),
        "pending": sum(f.get("started", False) and f.get("outcome") is None for f in mine),
        "settled": len(settled), "units": units if settled else None,
        "roi": units / len(settled) if settled else None,
        "signals": len(mine),
        "avg_ev": statistics.mean(f["ev"] for f in mine) if mine else None,
        "with_close": len(clvs),
        "avg_clv": statistics.mean(clvs) if clvs else None,
        "clv_positive": sum(c > 0 for c in clvs) / len(clvs) if clvs else None,
    }


class HistoryStore:
    """The history, updated with new runs only, saved after each change."""

    def __init__(self, path: Path = HISTORY_FILE, snapshot_dir: Path = SNAPSHOT_DIR) -> None:
        self.path, self.snapshot_dir = path, snapshot_dir
        self.lock = threading.Lock()
        self.checked: set[str] = set()
        self.history = empty_history()
        try:
            with path.open("rb") as file:
                self.history = pickle.load(file)
        except (OSError, pickle.PickleError, EOFError, AttributeError):
            pass

    results_signature: tuple = ()
    results: Any = None

    def outcomes(self, flags: list[dict], now: datetime) -> dict[str, tuple[str, Any]]:
        """(state, sets) per started event of `flags`; settled and void ones are kept,
        pending ones looked up again whenever a results file changes."""
        files = [match_results.RESULTS_CSV, *match_results.extra_files()]
        signature = tuple((p.name, p.stat().st_mtime) for p in files if p.exists())
        with self.lock:
            known = self.history.setdefault("outcomes", {})
            if signature != self.results_signature:
                self.results = props_results.Results()
                self.results_signature = signature
                self.checked = set()
            changed = False
            for flag in flags:
                event = flag["event"]
                if not flag["started"] or event in self.checked:
                    continue
                self.checked.add(event)
                if known.get(event, ("pending",))[0] != "pending":
                    continue
                state, value = self.results.sets(flag["player1"], flag["player2"], flag["starts_at"])
                start = market_compare.parse_time(flag["starts_at"])
                if state == "pending" and start and (now - start).total_seconds() > RESULT_WAIT_HOURS * 3600:
                    state, value = "void", "no result"
                if state != "pending":
                    known[event] = (state, value)
                    changed = True
            if changed:
                self.save()
            return dict(known)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("wb") as file:
            pickle.dump(self.history, file)
        temporary.replace(self.path)

    def update(self) -> dict:
        with self.lock:
            new = [p for p in snapshots(self.snapshot_dir) if p.name not in self.history["files"]]
            for path in new:
                try:
                    add_snapshot(self.history, path.name, read_snapshot(path))
                except (OSError, EOFError, csv.Error):
                    continue  # a file still being written: next time
            if new:
                self.save()
            return self.history


# ---------------------------------------------------------------- display --


def surname(name: str) -> str:
    """Last word of each partner: "Solana Sierra" -> "Sierra", doubles "A / B"."""
    return " / ".join(part.split()[-1] for part in name.split("/") if part.split())


def bet_text(offer: dict) -> str:
    market, side, line = offer["market"], offer["side"], offer.get("line")
    who = surname(offer["player1"] if side == "p1" else offer["player2"]) if side in ("p1", "p2") else ""
    if market == "match_winner":
        return who
    if market == "set1_winner":
        return f"{who} wins set 1"
    if market in HANDICAPS:
        unit = "sets" if market == "set_handicap" else "games"
        suffix = " in set 1" if market.startswith("set1") else ""
        return f"{who} {line:+g} {unit}{suffix}"
    unit = "sets" if market == "total_sets" else "games"
    prefix = "Set 1: " if market.startswith("set1") else ""
    return f"{prefix}{side.capitalize()} {line:g} {unit}"


def local_time(text: str) -> str:
    moment = market_compare.parse_time(text)
    return moment.astimezone(LOCAL_TIME).strftime("%d %b %H:%M") if moment else "--"


def pct(value: float | None, signed: bool = True, digits: int = 1) -> str:
    if value is None:
        return "--"
    return f"{value * 100:+.{digits}f}%" if signed else f"{value * 100:.{digits}f}%"


def offer_record(offer: dict, min_ev: float) -> dict:
    record = {
        "Bet": bet_text(offer),
        "Match": f"{offer['player1']} vs {offer['player2']}",
        "Market": MARKET_LABELS.get(offer["market"], offer["market"]),
        "Starts": local_time(offer["starts_at"]),
        "Book": offer["book"],
        "Price": f"{offer['price']:.2f}",
        "Pinnacle": pct(offer["probability"], signed=False),
        "EV": pct(offer["ev"]),
    }
    for exchange in EXCHANGES:
        mid = offer["exchanges"].get(exchange)
        record[exchange] = pct(mid, signed=False) if mid is not None else ""
    record["_style EV"] = GREEN if offer["ev"] >= min_ev else (RED if offer["ev"] < 0 else "")
    record["_title Price"] = (
        f"After the 12% tax: {offer['net']:.3f}" if offer["book"] == "Superbet" else ""
    )
    record["_title Match"] = offer["tournament"]
    return record


COLUMN_HELP = {
    "Pinnacle": "Pinnacle's probability for this outcome on this line, power devig.",
    "EV": "Price x Pinnacle's probability - 1 (Superbet after its 12% tax).",
    "Polymarket": "Middle of the bid and ask for the same outcome, comparison only.",
    "Kalshi": "Middle of the bid and ask for the same outcome, comparison only.",
}
TEXT_COLUMNS = {"Bet", "Match", "Market", "Starts", "Book"}


def table_html(records: list[dict], help_text: dict | None = None) -> str:
    columns = [c for c in records[0] if not c.startswith("_")]

    def attr(name: str, value: Any) -> str:
        return f' {name}="{html.escape(str(value))}"' if value else ""

    heads = "".join(
        f"<th{attr('class', '' if c in TEXT_COLUMNS else 'number')}"
        f"{attr('title', (help_text or COLUMN_HELP).get(c))}>{html.escape(c)}</th>" for c in columns
    )
    body = "".join(
        "<tr>" + "".join(
            f"<td{attr('class', '' if c in TEXT_COLUMNS else 'number')}"
            f"{attr('style', r.get(f'_style {c}'))}{attr('title', r.get(f'_title {c}'))}>"
            f"{html.escape(str(r[c]))}</td>" for c in columns
        ) + "</tr>"
        for r in records
    )
    return f'<table class="matches props"><thead><tr>{heads}</tr></thead><tbody>{body}</tbody></table>'


def summary_html(summary: list[dict], min_ev: float, books: tuple[str, ...]) -> str:
    heads = "<th>Market</th><th title='Matches Pinnacle prices on both sides'>Pinnacle</th>"
    for book in books:
        heads += (f"<th title='Matches {book} prices on one of Pinnacle&#39;s lines'>{book}</th>"
                  f"<th title='Median EV of those prices'>Median EV</th>"
                  f"<th title='Prices at or above the minimum EV'>&ge; {min_ev:.1%}</th>")
    body = ""
    for entry in summary:
        cells = f"<td>{html.escape(entry['label'])}</td><td>{entry['pinnacle']}</td>"
        for book in books:
            part = entry["books"][book]
            cells += (f"<td>{part['matches']}</td><td>{pct(part['median_ev'])}</td>"
                      f"<td>{part['signals']}</td>")
        body += f"<tr>{cells}</tr>"
    return (f'<div class="props-summary-wrap"><table class="props-summary"><thead><tr>{heads}'
            f"</tr></thead><tbody>{body}</tbody></table></div>")


def history_html(flags: list[dict], books: tuple[str, ...], dash: Any) -> str:
    rows = []
    for book in books:
        stats = history_stats(flags, book)
        tiles = [
            dash.tip_tile("Signals", str(stats["signals"]),
                          "Book offers at or above the minimum EV, each counted once at its "
                          "first run before the start."),
            dash.tip_tile("Avg EV", pct(stats["avg_ev"]), "Mean EV at the first flag."),
            dash.tip_tile("Avg CLV", pct(stats["avg_clv"]),
                          f"Price x Pinnacle's probability for the same line at its last run "
                          f"before the start - 1. {stats['with_close']} of {stats['signals']} "
                          "have one (started, and Pinnacle still listed that line)."),
            dash.tip_tile("CLV > 0", pct(stats["clv_positive"], signed=False, digits=0),
                          "Share of those with a CLV above zero."),
            dash.tip_tile("W-L", f"{stats['won']}-{stats['lost']}",
                          f"Completed matches, from the final set score. Push {stats['push']}, "
                          f"void {stats['void']} (retired, walkover or no result), "
                          f"waiting for a result {stats['pending']}."),
            dash.tip_tile("ROI", pct(stats["roi"]),
                          "Profit per unit staked on the settled bets (pushes included, at 0), "
                          "at the price paid: Superbet after its tax."),
            dash.tip_tile("Units", "--" if stats["units"] is None else f"{stats['units']:+.2f}",
                          "Profit in units, 1 unit a bet."),
        ]
        rows.append(f'<div class="track-book">{html.escape(book)}</div>'
                    f'<div class="overview-tiles">{"".join(tiles)}</div>')
    by_market = ""
    for market, label in MARKET_LABELS.items():
        cells = ""
        for book in books:
            stats = history_stats(flags, book, market)
            cells += (f"<td>{stats['signals']}</td><td>{pct(stats['avg_ev'])}</td>"
                      f"<td>{pct(stats['avg_clv'])}</td><td>{stats['won']}-{stats['lost']}</td>"
                      f"<td>{pct(stats['roi'])}</td>")
        by_market += f"<tr><td>{html.escape(label)}</td>{cells}</tr>"
    heads = "<th>Market</th>" + "".join(
        f"<th>{book}</th><th>Avg EV</th><th>Avg CLV</th><th>W-L</th><th>ROI</th>" for book in books)
    table = (f'<div class="props-summary-wrap"><table class="props-summary"><thead><tr>{heads}'
             f"</tr></thead><tbody>{by_market}</tbody></table></div>")
    return '<div class="track-rows">' + "".join(rows) + "</div>" + table


def card(heading: str, body: str) -> str:
    return (f'<div class="overview-card wide"><div class="overview-heading">'
            f"{html.escape(heading)}</div>{body}</div>")


def source_tiles(rows: list[dict], dash: Any) -> str:
    tiles = []
    for source in ("Pinnacle", *BOOKS, *EXCHANGES):
        mine = [r for r in rows if r.get("book") == source]
        matches = len({r["pinnacle_event_id"] for r in mine})
        tiles.append(dash.tip_tile(source, str(matches),
                                   f"Matches with at least one price ({len(mine)} prices)."))
    return f'<div class="overview-tiles">{"".join(tiles)}</div>'


# ---------------------------------------------------------------- page --


def render_props(st, dash: Any) -> None:
    with st.sidebar:
        min_ev = st.slider("Minimum EV", 0.0, 10.0, DEFAULT_MIN_EV_PERCENT, 0.5,
                           format="%.1f%%", key="props_min_ev",
                           help="Green from here up; Signals and History count from here.") / 100
        chosen = st.pills("Bookmakers", BOOKS, selection_mode="multi", default=list(BOOKS),
                          key="props_books")
        books = tuple(b for b in BOOKS if b in (chosen or BOOKS))
        markets = st.multiselect("Markets", list(MARKET_LABELS.values()),
                                 default=[v for k, v in MARKET_LABELS.items() if k != "match_winner"],
                                 key="props_markets",
                                 help="Match winner is the Tennis page's own market.")
        player = st.text_input("Player", key="props_player")
        st.caption("Logged for a later EV study (scripts/tennis_props.py, every 15 minutes). "
                   "Nothing here is sent to Telegram or counted on the Tennis page.")

    st.html(PROPS_STYLE)
    files = snapshots()

    @st.fragment(run_every=REFRESH_SECONDS)
    def watch() -> None:
        newest = snapshots()[-1:] if SNAPSHOT_DIR.exists() else []
        if newest != st.session_state.get("props_drawn"):
            st.rerun(scope="app")

    st.session_state["props_drawn"] = files[-1:]
    watch()
    if not files:
        st.info("No props snapshot yet. scripts/tennis_props.py writes one every 15 minutes.")
        return

    path = files[-1]
    rows = read_snapshot(path)
    captured = market_compare.parse_time(rows[0]["captured_at"]) if rows else None
    now = datetime.now(timezone.utc)
    age = round((now - captured).total_seconds() / 60) if captured else None
    found = offers(rows, books)
    wanted = {k for k, v in MARKET_LABELS.items() if v in markets} or set(MARKET_LABELS)

    store = st.cache_resource(show_spinner=False)(HistoryStore)()
    history = store.update()
    flags = [f for f in history_flags(history, min_ev, now) if f["book"] in books]
    settle_flags(flags, store.outcomes(flags, now))

    first_run = local_time(history["first"]) if history["first"] else "--"
    st.html(
        '<section class="overview">'
        f'<div class="overview-title">Last props run · {local_time(rows[0]["captured_at"]) if rows else "--"}'
        f'{f" ({age} min ago)" if age is not None else ""}</div>'
        '<div class="overview-cards">'
        + card("Matches priced", source_tiles(rows, dash))
        + "</div><div class=\"overview-cards\">"
        + card("Same line as Pinnacle", summary_html(market_summary(rows, found, min_ev), min_ev, books))
        + "</div>"
        f'<div class="overview-title">History · {history["runs"]} runs since {first_run}</div>'
        '<div class="overview-cards">'
        + card("First flags", history_html(flags, books, dash))
        + "</div></section>"
    )

    needle = player.strip().lower()
    shown = [
        o for o in found
        if o["market"] in wanted and o["ev"] >= min_ev
        and (not needle or needle in o["player1"].lower() or needle in o["player2"].lower())
    ]
    shown.sort(key=lambda o: -o["ev"])
    st.html(f'<div class="props-note">{len(shown)} prices at or above {min_ev:.1%} EV '
            f"in the newest run, of {len(found)} on a Pinnacle line</div>")
    if shown:
        st.html(table_html([offer_record(o, min_ev) for o in shown]))
    else:
        st.info("No price reaches the minimum EV in the newest run.")


# ------------------------------------------------- props vs prediction markets --

DEFAULT_MAX_SPREAD = market_compare.DEFAULT_MAX_SPREAD
AGREE_WITHIN = 0.02
PAIRS = {"p1": "p2", "p2": "p1", "over": "under", "under": "over"}


def exchange_quotes(rows: list[dict]) -> dict[tuple, dict[str, dict]]:
    """(event, market, line, side) -> {exchange: {bid, ask, mid, spread, line}}. A
    side's probability is the middle of its bid and ask; Kalshi's two sides are
    separate books, so their mids are scaled to add up to 1 when both are quoted (as
    on the moneyline Prediction markets page). The spread is the wider side's."""
    quotes: dict[tuple, dict[str, dict]] = {}
    for row in rows:
        if row.get("book") not in EXCHANGES:
            continue
        bid, ask = number(row.get("bid")), number(row.get("ask"))
        if bid is None or ask is None:
            continue
        quotes.setdefault((*group_key(row), row["side"]), {})[row["book"]] = {
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "spread": ask - bid,
            "line": number(row.get("line")),
        }
    scaled = {}
    for key, by_exchange in quotes.items():
        other = quotes.get((*key[:3], PAIRS.get(key[3], "")), {})
        out = {}
        for exchange, quote in by_exchange.items():
            quote = dict(quote)
            mate = other.get(exchange)
            if exchange == "Kalshi" and mate and quote["mid"] + mate["mid"] > 0:
                quote["mid"] = quote["mid"] / (quote["mid"] + mate["mid"])
            if mate:
                quote["spread"] = max(quote["spread"], mate["spread"])
            out[exchange] = quote
        scaled[key] = out
    return scaled


def market_outcomes(rows: list[dict], max_spread: float = DEFAULT_MAX_SPREAD) -> list[dict]:
    """One record per outcome a prediction market quotes: the markets' probabilities,
    Pinnacle's on the same line, the best book price for it and the EVs against each."""
    fair = pinnacle_fair(rows)
    info = {group_key(r)[0]: r for r in rows}
    best: dict[tuple, tuple[float, dict]] = {}
    for row in rows:
        if row.get("book") in BOOKS:
            net = net_price(row)
            key = (*group_key(row), row["side"])
            if net and (key not in best or net > best[key][0]):
                best[key] = (net, row)
    out = []
    for key, quotes in exchange_quotes(rows).items():
        match = info[key[0]]
        pinnacle = (fair.get(key[:3]) or {}).get(key[3])
        net, book_row = best.get(key, (None, None))
        markets = {}
        for exchange, quote in quotes.items():
            markets[exchange] = {
                **quote, "tight": quote["spread"] <= max_spread + 1e-9,
                "ev": net * quote["mid"] - 1 if net else None,
                "gap": quote["mid"] - pinnacle if pinnacle is not None else None,
            }
        line = next((q["line"] for q in quotes.values() if q["line"] is not None), None)
        out.append({
            "key": key[:3], "side": key[3], "market": key[1], "line": line,
            "player1": match.get("player1", ""), "player2": match.get("player2", ""),
            "tournament": match.get("tournament", ""), "starts_at": match.get("starts_at", ""),
            "pinnacle": pinnacle, "markets": markets,
            "book": book_row.get("book") if book_row else None,
            "price": number(book_row["price"]) if book_row else None, "net": net,
            "ev_pinnacle": net * pinnacle - 1 if net and pinnacle is not None else None,
        })
    return out


def best_trusted_ev(outcome: dict) -> float | None:
    evs = [m["ev"] for m in outcome["markets"].values() if m["tight"] and m["ev"] is not None]
    if outcome["ev_pinnacle"] is not None:
        evs.append(outcome["ev_pinnacle"])
    return max(evs) if evs else None


def agreement_stats(outcomes: list[dict]) -> dict[str, dict]:
    """Per exchange: matches and outcomes quoted, tight ones, and against Pinnacle on
    the same line (tight only, one side per pair): median absolute gap and the share
    within AGREE_WITHIN."""
    stats = {}
    for exchange in EXCHANGES:
        quoted = [o for o in outcomes if exchange in o["markets"]]
        tight = [o for o in quoted if o["markets"][exchange]["tight"]]
        gaps = [abs(o["markets"][exchange]["gap"]) for o in tight
                if o["markets"][exchange]["gap"] is not None and o["side"] in ("p1", "over")]
        stats[exchange] = {
            "matches": len({o["key"][0] for o in quoted}),
            "outcomes": len(quoted), "tight": len(tight), "compared": len(gaps),
            "median_gap": statistics.median(gaps) if gaps else None,
            "within": sum(g <= AGREE_WITHIN for g in gaps) / len(gaps) if gaps else None,
        }
    return stats


def coverage_html(outcomes: list[dict], rows: list[dict]) -> str:
    """Per market: matches Pinnacle prices, then matches each exchange quotes."""
    fair = pinnacle_fair(rows)
    heads = "<th>Market</th><th>Pinnacle</th>" + "".join(f"<th>{e}</th>" for e in EXCHANGES)
    body = ""
    for market, label in MARKET_LABELS.items():
        cells = f"<td>{html.escape(label)}</td><td>{len({k[0] for k in fair if k[1] == market})}</td>"
        for exchange in EXCHANGES:
            mine = [o for o in outcomes if o["market"] == market and exchange in o["markets"]]
            cells += f"<td>{len({o['key'][0] for o in mine})}</td>"
        body += f"<tr>{cells}</tr>"
    return (f'<div class="props-summary-wrap"><table class="props-summary"><thead><tr>{heads}'
            f"</tr></thead><tbody>{body}</tbody></table></div>")


def agreement_html(stats: dict[str, dict], dash: Any) -> str:
    rows = []
    for exchange, part in stats.items():
        gap = "--" if part["median_gap"] is None else f"{part['median_gap'] * 100:.1f} pp"
        tiles = [
            dash.tip_tile("Matches", str(part["matches"]),
                          f"{part['outcomes']} outcomes quoted, {part['tight']} with a tight spread."),
            dash.tip_tile("Median gap", gap,
                          "Middle of bid and ask against Pinnacle's devigged probability on the "
                          f"same line, tight spreads only ({part['compared']} pairs)."),
            dash.tip_tile(f"Within {AGREE_WITHIN:.0%}", pct(part["within"], signed=False, digits=0),
                          "Share of those pairs within 2 points of Pinnacle."),
        ]
        rows.append(f'<div class="track-book">{html.escape(exchange)}</div>'
                    f'<div class="overview-tiles">{"".join(tiles)}</div>')
    return '<div class="track-rows">' + "".join(rows) + "</div>"


MARKETS_COLUMN_HELP = {
    "Pinnacle": "Pinnacle's probability for this outcome on this line, power devig.",
    "Polymarket": "Middle of the bid and ask. Grey: spread wider than the sidebar's limit.",
    "Kalshi": "Middle of the bid and ask, scaled with the other side to add up to 100%. "
              "Grey: spread wider than the limit.",
    "Price": "The better book price for this outcome (Superbet's EV after its 12% tax).",
    "EV Pinnacle": "Book price x Pinnacle's probability - 1.",
    "EV Polymarket": "Book price x Polymarket's probability - 1.",
    "EV Kalshi": "Book price x Kalshi's probability - 1.",
}
GREY = "color: #888888"


def market_record(outcome: dict, min_ev: float) -> dict:
    def ev_style(value, trusted=True):
        if value is None or not trusted:
            return GREY
        return GREEN if value >= min_ev else (RED if value < 0 else "")

    record = {
        "Bet": bet_text(outcome),
        "Match": f"{outcome['player1']} vs {outcome['player2']}",
        "Market": MARKET_LABELS.get(outcome["market"], outcome["market"]),
        "Starts": local_time(outcome["starts_at"]),
        "Pinnacle": pct(outcome["pinnacle"], signed=False),
    }
    for exchange in EXCHANGES:
        quote = outcome["markets"].get(exchange)
        record[exchange] = pct(quote["mid"], signed=False) if quote else ""
        if quote:
            record[f"_title {exchange}"] = (f"bid {quote['bid'] * 100:.1f}¢, ask {quote['ask'] * 100:.1f}¢, "
                                            f"wider spread {quote['spread'] * 100:.1f}¢")
            if not quote["tight"]:
                record[f"_style {exchange}"] = GREY
    record["Book"] = outcome["book"] or ""
    record["Price"] = f"{outcome['price']:.2f}" if outcome["price"] else ""
    record["EV Pinnacle"] = pct(outcome["ev_pinnacle"])
    record["_style EV Pinnacle"] = ev_style(outcome["ev_pinnacle"])
    for exchange in EXCHANGES:
        quote = outcome["markets"].get(exchange)
        record[f"EV {exchange}"] = pct(quote["ev"]) if quote else ""
        record[f"_style EV {exchange}"] = ev_style(quote["ev"], quote["tight"]) if quote else ""
    record["_title Match"] = outcome["tournament"]
    return record


def render_props_markets(st, dash: Any) -> None:
    with st.sidebar:
        min_ev = st.slider("Minimum EV", 0.0, 10.0, DEFAULT_MIN_EV_PERCENT, 0.5,
                           format="%.1f%%", key="props_markets_min_ev",
                           help="Green from here up, against any reference.") / 100
        max_spread = st.slider("Maximum spread", 1, 10, round(DEFAULT_MAX_SPREAD * 100), 1,
                               format="%d¢", key="props_markets_spread",
                               help="A market whose wider side's bid-ask spread is above this "
                               "is grey and its EV not trusted.") / 100
        markets = st.multiselect("Markets", list(MARKET_LABELS.values()),
                                 default=[v for k, v in MARKET_LABELS.items() if k != "match_winner"],
                                 key="props_markets_markets")
        player = st.text_input("Player", key="props_markets_player")
        st.caption("Comparison only: Polymarket and Kalshi on the set and games lines of "
                   "the props log. Nothing here is sent or counted elsewhere.")

    st.html(PROPS_STYLE)
    files = snapshots()

    @st.fragment(run_every=REFRESH_SECONDS)
    def watch() -> None:
        if snapshots()[-1:] != st.session_state.get("props_markets_drawn"):
            st.rerun(scope="app")

    st.session_state["props_markets_drawn"] = files[-1:]
    watch()
    if not files:
        st.info("No props snapshot yet. scripts/tennis_props.py writes one every 15 minutes.")
        return

    rows = read_snapshot(files[-1])
    outcomes = market_outcomes(rows, max_spread)
    captured = market_compare.parse_time(rows[0]["captured_at"]) if rows else None
    age = round((datetime.now(timezone.utc) - captured).total_seconds() / 60) if captured else None
    st.html(
        '<section class="overview">'
        f'<div class="overview-title">Last props run · {local_time(rows[0]["captured_at"]) if rows else "--"}'
        f'{f" ({age} min ago)" if age is not None else ""}</div>'
        '<div class="overview-cards">'
        + card("Matches per market", coverage_html(outcomes, rows))
        + card("Agreement with Pinnacle", agreement_html(agreement_stats(outcomes), dash))
        + "</div></section>"
    )

    wanted = {k for k, v in MARKET_LABELS.items() if v in markets} or set(MARKET_LABELS)
    needle = player.strip().lower()
    shown = [
        o for o in outcomes if o["market"] in wanted
        and (not needle or needle in o["player1"].lower() or needle in o["player2"].lower())
    ]

    def order(outcome: dict) -> float:
        value = best_trusted_ev(outcome)
        return -value if value is not None else 9.0

    shown.sort(key=order)
    st.html(f'<div class="props-note">{len(shown)} outcomes quoted on a prediction market</div>')
    if shown:
        st.html(table_html([market_record(o, min_ev) for o in shown], MARKETS_COLUMN_HELP))
    else:
        st.info("No prediction-market quote for these markets in the newest run.")
