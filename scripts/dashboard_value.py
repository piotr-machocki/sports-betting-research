"""
The dashboard's All tab, Signal value subtab (its default; since 28 Nov 2026, user's
request): every kind of signal the dashboard tracks, ranked by how likely its value is
real, so the best candidates sit on top and the ones with no value at the bottom.

Ranked by average CLV (the flagged price against Pinnacle's closing line): it shows
long before results do whether a signal beats the market. Each row is one source's
Statistics as its own tab works them out at the defaults (minimum EV 2%, every
probability, markets' spread up to 4 cents), read from the same loaders, so it keeps
itself current as runs finish and results come in:
- Tennis > Moneyline > Sharp vs soft: Betclic and Superbet (dashboard.TrackLoader).
- Tennis > Moneyline > Prediction markets: book prices flagged against Polymarket,
  Kalshi and both (dashboard.MarketTrackLoader).
- Tennis > Props > Sharp vs soft: set and games markets per book
  (dashboard_props.HistoryStore; match winner left out, it is Moneyline's market).

The verdict comes from the 95% range of the mean CLV: above zero = likely real, mean
above zero but the range still crosses it = promising, below zero = no value, else
doubtful; fewer than MIN_CLV_SIGNALS signals with a CLV = too early.
"""

from __future__ import annotations

import html
import threading
import time
from datetime import datetime, timezone
from typing import Any

from market_compare import BAND_NAMES, BAND_POINTS, FULL_BAND, in_band

MIN_EV = 0.02
MAX_SPREAD = 0.04
MIN_CLV_SIGNALS = 10
REFRESH_SECONDS = 15

# (label, CSS class, icon, tooltip). Status colours of the dataviz palette, each with
# an icon and its label, so the verdict is never colour alone.
VERDICTS = {
    "real": ("Likely real", "good", "✓",
             "The whole 95% range of the average CLV is above zero."),
    "promising": ("Promising", "warn", "↗",
                  "Average CLV above zero, but the 95% range still reaches below it: "
                  "more signals will tell."),
    "doubtful": ("Doubtful", "serious", "?",
                 "Average CLV at or below zero, the 95% range still reaches above it."),
    "none": ("No value", "critical", "✕",
             "The whole 95% range of the average CLV is below zero: the closing line "
             "keeps moving against these prices."),
    "early": ("Too early", "muted", "…",
              f"Fewer than {MIN_CLV_SIGNALS} started signals with a closing line."),
    "loading": ("Loading", "muted", "…", "Being worked out."),
}


def verdict(clv: float | None, margin: float | None, count: int) -> str:
    if clv is None or count < MIN_CLV_SIGNALS:
        return "early"
    if margin is None:
        return "promising" if clv > 0 else "doubtful"
    if clv - margin > 0:
        return "real"
    if clv + margin < 0:
        return "none"
    return "promising" if clv > 0 else "doubtful"


def track_row(path: tuple[str, ...], source: str, stats: dict | None) -> dict:
    """A row from dashboard.track_stats (both Sharp vs soft and the markets' cards)."""
    if stats is None:
        return {"path": path, "source": source, "loading": True}
    return {
        "path": path, "source": source, "loading": False,
        "signals": stats["signals"], "clv": stats["clv"],
        "clv_margin": stats.get("clv_margin"), "clv_count": stats["clv_count"],
        "clv_positive": stats["clv_positive"], "won": stats["won"], "lost": stats["lost"],
        "roi": stats["roi"], "units": stats["units"] if stats["won"] + stats["lost"] else None,
        "ev": stats["ev"],
    }


def props_row(path: tuple[str, ...], source: str, stats: dict) -> dict:
    """A row from dashboard_props.history_stats."""
    return {
        "path": path, "source": source, "loading": False,
        "signals": stats["signals"], "clv": stats["avg_clv"],
        "clv_margin": stats.get("clv_margin"), "clv_count": stats["with_close"],
        "clv_positive": stats["clv_positive"], "won": stats["won"], "lost": stats["lost"],
        "roi": stats["roi"], "units": stats["units"], "ev": stats["avg_ev"],
    }


def odds_text(point: int) -> str:
    return "\u221e" if point == 0 else f"{100 / point:.2f}"


# The four stretches of the Statistics slider, each a band (low, high) as fractions,
# exactly as the loaders precompute them (dashboard.ALL_BANDS), with its odds range.
STRETCHES = [
    (
        name,
        (BAND_POINTS[index] / 100, BAND_POINTS[index + 1] / 100),
        f"{odds_text(BAND_POINTS[index])}\u2013{odds_text(BAND_POINTS[index + 1])}",
    )
    for index, name in enumerate(BAND_NAMES)
]


def stretch_row(name: str, odds: str, stats: dict | None, held: bool) -> dict:
    """One stretch of a card: held=False while its record is still being worked out;
    stats None when held = no signal in this stretch."""
    if not held:
        return {"path": (odds,), "source": name, "loading": True}
    if stats is None:
        return {"path": (odds,), "source": name, "loading": False, "empty": True,
                "signals": 0, "clv": None, "clv_margin": None, "clv_count": 0}
    return track_row((odds,), name, stats)


def rank(rows: list[dict]) -> list[dict]:
    """Verdict filled in, best average CLV first among rows with enough closes, then
    the too-early ones the same way (one lucky close must not top the list), then
    those still loading."""
    for row in rows:
        row["verdict"] = "loading" if row["loading"] else verdict(
            row["clv"], row["clv_margin"], row["clv_count"]
        )
        if row.get("stretches"):
            row["stretches"] = rank(row["stretches"])
    order = {"loading": 2, "early": 1}
    return sorted(rows, key=lambda row: (
        order.get(row["verdict"], 0), row.get("clv") is None,
        -(row.get("clv") or 0.0), -(row.get("signals") or 0),
    ))


def axis_limit(rows: list[dict]) -> float:
    """Half the CLV axis: the widest range end, rounded up to 10%, from 10% to 50%
    (a wider whisker is cut at the edge and drawn with an arrow)."""
    ends = [
        abs(row["clv"]) + (row["clv_margin"] or 0.0)
        for row in rows if row.get("clv") is not None
    ]
    widest = max(ends, default=0.1)
    return min(max(-(-widest * 10 // 1) / 10, 0.1), 0.5)


def pct(value: float | None, signed: bool = True, digits: int = 1) -> str:
    if value is None:
        return "--"
    return f"{value:+.{digits}%}" if signed else f"{value:.{digits}%}"


def tone(value: float | None) -> str:
    if not value:
        return ""
    return " up" if value > 0 else " down"


def plot_html(row: dict, limit: float) -> str:
    """The average CLV as a dot on a shared axis around zero, its 95% range as a
    line: the further right of the zero line the whole line sits, the surer."""
    if row.get("clv") is None:
        return '<div class="sv-plot empty"><i class="zero"></i></div>'

    def place(value: float) -> float:
        return 50 + 50 * max(-1.0, min(1.0, value / limit))

    clv, margin = row["clv"], row["clv_margin"]
    parts = ['<i class="zero"></i>']
    if margin:
        low, high = clv - margin, clv + margin
        cut = (" cut-left" if low < -limit else "") + (" cut-right" if high > limit else "")
        parts.append(
            f'<i class="range{cut}" style="left:{place(low):.1f}%;'
            f'width:{place(high) - place(low):.1f}%"></i>'
        )
    parts.append(f'<i class="dot {VERDICTS[row["verdict"]][1]}" style="left:{place(clv):.1f}%"></i>')
    range_text = (
        f"95% range {pct(clv - margin)} to {pct(clv + margin)}" if margin else "no range yet"
    )
    tip = f"Average CLV {pct(clv)}, {range_text}, {row['clv_count']} signals with a close"
    return f'<div class="sv-plot" title="{html.escape(tip)}">{"".join(parts)}</div>'


def stretch_html(row: dict, limit: float) -> str:
    """A compact line inside an opened card: one odds stretch."""
    label, css, icon, help_text = VERDICTS[row["verdict"]]
    name = (
        f'<span class="sv-name"><b>{html.escape(row["source"])}</b>'
        f'<small>{html.escape(row["path"][0])}</small></span>'
    )
    if row.get("empty"):
        return (
            f'<div class="sv-sub empty"><div class="sv-head">{name}'
            '<span class="sv-none">No signals</span></div></div>'
        )
    pill = (
        f'<span class="sv-verdict {css}" title="{html.escape(help_text)}">{icon} {label}</span>'
    )
    if row["loading"]:
        return f'<div class="sv-sub"><div class="sv-head">{name}{pill}</div></div>'
    clv = pct(row["clv"])
    if row["clv_margin"]:
        clv += f' <small>\u00b1{row["clv_margin"] * 100:.1f}</small>'
    facts = (
        f'<b class="{tone(row["clv"]).strip()}">{clv}</b> CLV \u00b7 {row["signals"]} signals'
        f' \u00b7 {row["won"]}-{row["lost"]} W-L'
        f' \u00b7 ROI <b class="{tone(row["roi"]).strip()}">{pct(row["roi"])}</b>'
    )
    return (
        f'<div class="sv-sub"><div class="sv-head">{name}{pill}</div>'
        f'{plot_html(row, limit)}<div class="sv-facts">{facts}</div></div>'
    )


def row_html(number: int, row: dict, limit: float) -> str:
    label, css, icon, help_text = VERDICTS[row["verdict"]]
    path = " · ".join(row["path"])
    head = (
        f'<div class="sv-head"><span class="sv-rank">{number}</span>'
        f'<span class="sv-name"><b>{html.escape(row["source"])}</b>'
        f'<small>{html.escape(path)}</small></span>'
        f'<span class="sv-verdict {css}" title="{html.escape(help_text)}">'
        f"{icon} {label}</span></div>"
    )
    if row["loading"]:
        return f'<div class="sv-row">{head}</div>'

    clv_text = pct(row["clv"])
    if row["clv_margin"]:
        clv_text += f' <small>±{row["clv_margin"] * 100:.1f}</small>'
    settled = row["won"] + row["lost"]
    stats = [
        ("Avg CLV", clv_text, tone(row["clv"])),
        ("CLV > 0", pct(row["clv_positive"], signed=False, digits=0), ""),
        ("Signals", f'{row["signals"]} <small>({row["clv_count"]} closed)</small>', ""),
        ("Won-Lost", f'{row["won"]}-{row["lost"]}' if settled else "--", ""),
        ("ROI", pct(row["roi"]), tone(row["roi"])),
        ("Avg EV", pct(row["ev"]), ""),
    ]
    cells = "".join(
        f'<div class="sv-stat"><b class="{css_class.strip()}">{value}</b><span>{name}</span></div>'
        for name, value, css_class in stats
    )
    summary = f'{head}{plot_html(row, limit)}<div class="sv-stats">{cells}</div>'
    if not row.get("stretches"):
        return f'<div class="sv-row">{summary}</div>'
    # Tap to open (user, 28 Nov 2026): the same source by odds stretch, best first.
    inner = "".join(stretch_html(sub, limit) for sub in row["stretches"])
    return (
        f'<details class="sv-row"><summary>{summary}'
        '<div class="sv-open">By odds<i></i></div></summary>'
        f'<div class="sv-subs">{inner}</div></details>'
    )


def axis_html(limit: float) -> str:
    return (
        '<div class="sv-axis"><span>' + pct(-limit, digits=0) + "</span>"
        "<span>0 (no value)</span><span>" + pct(limit, digits=0) + "</span></div>"
    )


def page_html(rows: list[dict]) -> str:
    ranked = rank(rows)
    limit = axis_limit(ranked)
    body = "".join(row_html(number, row, limit) for number, row in enumerate(ranked, 1))
    return (
        STYLE
        + '<div class="sv">'
        '<div class="sv-intro">Is the value real? Every signal source, ranked by '
        "<b>average CLV</b> (flagged price vs Pinnacle's closing line), which shows it long "
        "before wins and losses do. The line around each dot is its 95% range: "
        "<b>likely real</b> once all of it is right of zero. Signals at EV &ge; "
        f"{MIN_EV:.0%}; updates as runs finish.</div>"
        + axis_html(limit) + body + "</div>"
    )


def tennis_rows(st: Any, dash: Any) -> tuple[list[dict], list[tuple]]:
    """Rows from the two loaders (the same ones the Tennis tabs use, held by
    st.cache_resource), and (loader, key) pairs to watch for new numbers."""
    rows = []
    track = st.cache_resource(show_spinner=False)(dash.track_loader)()
    track_key = (MIN_EV, tuple(dash.BOOKS), FULL_BAND)
    record = track.request_key(track_key)
    watched = [(track, track_key)]
    track_stretches = {}
    for _, band, _ in STRETCHES:
        key = (MIN_EV, tuple(dash.BOOKS), band)
        track_stretches[band] = track.request_key(key)
        watched.append((track, key))
    for book in dash.BOOKS:
        if record is None or book in record:
            row = track_row(
                ("Tennis", "Moneyline", "Sharp vs soft"), book, (record or {}).get(book)
            )
            row["stretches"] = [
                stretch_row(name, odds, (track_stretches[band] or {}).get(book),
                            track_stretches[band] is not None)
                for name, band, odds in STRETCHES
            ]
            rows.append(row)

    import market_track

    markets = st.cache_resource(show_spinner=False)(dash.market_loader)()
    market_key = (MIN_EV, MAX_SPREAD, FULL_BAND)
    market_record = markets.request_key(market_key)
    watched.append((markets, market_key))
    market_stretches = {}
    for _, band, _ in STRETCHES:
        key = (MIN_EV, MAX_SPREAD, band)
        market_stretches[band] = markets.request_key(key)
        watched.append((markets, key))
    for reference in market_track.REFERENCES:
        if market_record is None or reference in market_record:
            name = "Both markets" if reference == market_track.BOTH else reference
            row = track_row(
                ("Tennis", "Moneyline", "Prediction markets"), f"Books vs {name}",
                (market_record or {}).get(reference),
            )
            row["stretches"] = [
                stretch_row(stretch, odds, (market_stretches[band] or {}).get(reference),
                            market_stretches[band] is not None)
                for stretch, band, odds in STRETCHES
            ]
            rows.append(row)
    return rows, watched


def props_rows(store: Any) -> list[dict]:
    import dashboard_props

    now = datetime.now(timezone.utc)
    flags = [
        flag for flag in dashboard_props.history_flags(store.update(), MIN_EV, now)
        if flag["market"] != "match_winner"
    ]
    dashboard_props.settle_flags(flags, store.outcomes(flags, now))
    rows = []
    for book in dashboard_props.BOOKS:
        stats = dashboard_props.history_stats(flags, book)
        if stats["signals"]:
            row = props_row(PROPS_PATH, book, stats)
            row["stretches"] = []
            for name, band, odds in STRETCHES:
                # Judged on Pinnacle's fair probability, as on the Tennis page.
                inside = [
                    flag for flag in flags
                    if flag["net"] and in_band((1 + flag["ev"]) / flag["net"], band)
                ]
                part = dashboard_props.history_stats(inside, book)
                row["stretches"].append(
                    props_row((odds,), name, part) if part["signals"]
                    else stretch_row(name, odds, None, True)
                )
            rows.append(row)
    return rows


PROPS_PATH = ("Tennis", "Props", "Sharp vs soft")


class PropsLoader:
    """The props rows worked out in a thread: the history's first build reads every
    props snapshot (137 s on the VM on 28 Nov 2026), so the page never waits for it.
    Held rows are shown at once and worked out again at most every REFRESH_SECONDS."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.rows: list[dict] | None = None
        self.error: str | None = None
        self.version = 0
        self.started = 0.0
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def request(self) -> list[dict] | None:
        with self.lock:
            due = time.time() - self.started >= REFRESH_SECONDS
            if self.thread is None and due:
                self.started = time.time()
                self.thread = threading.Thread(target=self.work, daemon=True)
                self.thread.start()
            return self.rows

    def work(self) -> None:
        try:
            rows, error = props_rows(self.store), None
        except Exception as exc:  # shown under the page; the other sources still show
            rows, error = None, f"{type(exc).__name__}: {exc}"
        with self.lock:
            if rows is not None and rows != self.rows:
                self.rows = rows
                self.version += 1
            if error != self.error:
                self.error = error
                self.version += 1
            self.thread = None


def props_loader() -> PropsLoader:
    """One per server (st.cache_resource), sharing the Props tab's HistoryStore."""
    import streamlit as st
    import dashboard_props

    return PropsLoader(st.cache_resource(show_spinner=False)(dashboard_props.HistoryStore)())


def render_value(st: Any, dash: Any) -> None:
    rows, watched = tennis_rows(st, dash)
    props = st.cache_resource(show_spinner=False)(props_loader)()
    held = props.request()
    if held is None and props.error is None:
        rows.append({"path": PROPS_PATH, "source": "Betclic and Superbet", "loading": True})
    rows += held or []
    if props.error:
        st.warning(f"Props history could not be read ({props.error}).")

    drawn = [loader.version(key) for loader, key in watched] + [props.version]

    @st.fragment(run_every=REFRESH_SECONDS)
    def watch() -> None:
        props.request()  # works the props rows out again every REFRESH_SECONDS
        if [loader.version(key) for loader, key in watched] + [props.version] != drawn:
            st.rerun(scope="app")

    watch()
    st.html(page_html(rows))


STYLE = """<style>
.sv {
    --good: #0ca30c; --warn: #fab219; --serious: #ec835a; --critical: #d03b3b;
    --muted: color-mix(in srgb, currentColor 60%, transparent);
    --line: rgba(128, 128, 128, 0.3);
    max-width: 820px;
}
.sv-intro { font-size: 14px; color: var(--muted); margin: 4px 0 14px; line-height: 1.45; }
.sv-intro b { color: inherit; font-weight: 600; }
.sv-axis {
    display: flex; justify-content: space-between; font-size: 12px; color: var(--muted);
    margin: 0 0 6px; padding: 0 14px;
}
.sv-row {
    border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px 12px;
    margin-bottom: 10px;
}
.sv-head { display: flex; align-items: center; gap: 10px; }
.sv-rank {
    flex: none; width: 22px; height: 22px; border-radius: 50%; text-align: center;
    line-height: 22px; font-size: 12px; background: rgba(128, 128, 128, 0.2);
}
.sv-name { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.sv-name b { font-size: 16px; font-weight: 600; }
.sv-name small { font-size: 12px; color: var(--muted); }
.sv-verdict {
    flex: none; font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 10px;
    color: #fff; cursor: help; white-space: nowrap;
}
.sv-verdict.good { background: var(--good); }
.sv-verdict.warn { background: var(--warn); color: #0b0b0b; }
.sv-verdict.serious { background: var(--serious); color: #0b0b0b; }
.sv-verdict.critical { background: var(--critical); }
.sv-verdict.muted { background: rgba(128, 128, 128, 0.35); }
.sv-plot { position: relative; height: 18px; margin: 10px 0 8px; cursor: help; }
.sv-plot::before {
    content: ""; position: absolute; left: 0; right: 0; top: 50%; height: 1px;
    background: rgba(128, 128, 128, 0.25);
}
.sv-plot i { position: absolute; display: block; }
.sv-plot .zero { left: 50%; top: 0; bottom: 0; width: 1px; background: var(--muted); }
.sv-plot .range {
    top: 50%; height: 2px; margin-top: -1px; background: currentColor; opacity: 0.75;
}
.sv-plot .range.cut-left { border-left: 5px solid transparent; }
.sv-plot .range.cut-right { border-right: 5px solid transparent; }
.sv-plot .dot {
    top: 50%; width: 10px; height: 10px; margin: -5px 0 0 -5px; border-radius: 50%;
    box-shadow: 0 0 0 2px #0e1117;
}
.sv-plot .dot.good { background: var(--good); }
.sv-plot .dot.warn { background: var(--warn); }
.sv-plot .dot.serious { background: var(--serious); }
.sv-plot .dot.critical { background: var(--critical); }
.sv-plot .dot.muted { background: var(--muted); }
.sv-stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 6px; }
.sv-stat { display: flex; flex-direction: column; }
.sv-stat b { font-size: 15px; font-weight: 600; }
.sv-stat b small { font-size: 11px; font-weight: 400; color: var(--muted); }
.sv-stat b.up { color: #3dd56d; }
.sv-stat b.down { color: #ff6c6c; }
.sv-stat span { font-size: 12px; color: var(--muted); }
details.sv-row > summary { list-style: none; cursor: pointer; }
details.sv-row > summary::-webkit-details-marker { display: none; }
.sv-open {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    margin: 8px -14px -4px; padding-top: 6px; font-size: 12px; color: var(--muted);
    border-top: 1px solid rgba(128, 128, 128, 0.18);
}
.sv-open i {
    width: 7px; height: 7px; border: solid currentColor; border-width: 0 1.5px 1.5px 0;
    transform: rotate(45deg); margin-top: -4px; transition: transform 0.15s;
}
details[open] .sv-open i { transform: rotate(-135deg); margin-top: 3px; }
.sv-subs { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.sv-sub { border-radius: 6px; padding: 8px 10px; background: rgba(128, 128, 128, 0.08); }
.sv-sub .sv-name b { font-size: 14px; }
.sv-sub .sv-plot { margin: 6px 0 4px; height: 14px; }
.sv-facts { font-size: 12px; color: var(--muted); }
.sv-facts b { font-weight: 600; color: var(--text, inherit); }
.sv-facts b.up { color: #3dd56d; }
.sv-facts b.down { color: #ff6c6c; }
.sv-facts small { font-size: 11px; }
.sv-sub.empty { opacity: 0.55; }
.sv-none { font-size: 12px; color: var(--muted); }
@media (max-width: 700px) {
    .sv-stats { grid-template-columns: repeat(3, 1fr); row-gap: 8px; }
    .sv-axis { padding: 0 14px; }
}
</style>"""
