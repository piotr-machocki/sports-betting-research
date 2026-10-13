"""Pinnacle -> devig -> Betclic EV comparison.

Reads data/pinnacle/odds.csv and data/betclic/odds.csv, matches the same
matches across bookmakers by normalized player names, computes EV for both
sides of every matched match, prints a table sorted from best to worst EV and
appends a full-precision CSV to data/results/history/ (skipped when identical
to the newest entry) and overwrites data/results/latest_report.md.

Names decide the match (exact, then loose). Tournaments are only an extra
confidence signal: a match is "verified" when the names match exactly, or loosely
AND the tournaments share a meaningful token. Unverified loose matches are
flagged CHECK and listed in the report. Tournament names are normalized with
the Polish->English city dictionary in src/betting/tournament_names.py.

Matching is done against ALL Betclic rows; matches that are live on Betclic are
dropped only afterwards (unless --include-live) and listed separately in the
report, so they do not show up as "without a Betclic pair".

Data age is a property of the whole run, not of a single row: it is the time
between NOW and the OLDER of the two snapshots. It is shown once (terminal
and report header) together with the gap between the snapshots. When it
exceeds --max-age the data is "stale": no row counts as a signal.

The report tables are HTML (Markdown has no syntax for vertical grid lines),
so they render with a full grid in a Markdown preview. The Flags column is
left out of a table when none of its cells has a flag.

Usage:  python scripts/compare_odds.py [--include-live] [--min-ev 0.02]
                                        [--max-age 15] [--top 20]
                                        [--color auto|always|never]
"""
import argparse
import csv
import html
import io
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from betting.margin import remove_margin  # noqa: E402
from betting.tournament_names import CITY_ALIASES, TOURNAMENT_STOPWORDS  # noqa: E402
from betting.value import calculate_ev  # noqa: E402

PINNACLE_CSV = ROOT / "data" / "pinnacle" / "odds.csv"
BETCLIC_CSV = ROOT / "data" / "betclic" / "odds.csv"
RESULTS_DIR = ROOT / "data" / "results"
HISTORY_DIR = RESULTS_DIR / "history"          # one CSV per distinct snapshot pair
LATEST_REPORT = RESULTS_DIR / "latest_report.md"  # always overwritten


# ---------- name normalization ----------

def ascii_lower(s: str) -> str:
    s = s.replace("ł", "l").replace("Ł", "L").replace("đ", "d").replace("ø", "o")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def collapse(s: str) -> str:
    """Fold German-style digraphs so Moeller == Moller (loose matching only)."""
    return re.sub(r"([aou])e", r"\1", s)


class Player:
    """Common form of a player name.

    initial  - first letter of the first name
    initials - first letters of all name tokens (loose matching: the other
               side may have extra given names, "Maria Camila Osorio Serrano")
    sq       - name squashed to letters only, no spaces/hyphens/diacritics
               ("Learner Tien" and "LearnerTien" -> "learnertien")
    last     - last token of the name (used for loose matching)
    sq_c / last_c - sq / last with ae/oe/ue folded (loose matching only)
    """

    def __init__(self, raw: str):
        tokens = re.findall(r"[a-z]+", ascii_lower(raw))
        if not tokens:
            raise ValueError(f"Empty player name: {raw!r}")
        if len(tokens) > 1 and len(tokens[-1]) == 1:
            # Betclic doubles style: "Pridankina E" (surname + initial)
            self.initial = tokens[-1]
            body = tokens[:-1]
        else:
            self.initial = tokens[0][0]
            body = tokens
        self.sq = "".join(body)
        self.last = body[-1]
        self.initials = {t[0] for t in body} | {self.initial}
        self.sq_c = collapse(self.sq)
        self.last_c = collapse(self.last)


class Team:
    def __init__(self, raw: str):
        parts = [p for p in raw.split("/") if p.strip()]
        self.players = [Player(p) for p in parts]
        self.is_double = len(self.players) == 2


def player_eq(a: Player, b: Player, exact: bool) -> bool:
    if exact:
        return a.sq == b.sq
    # loose: first-name initial matches any token of the other name and one
    # surname is contained in the other name
    # ("Thiago Seyboth Wild" ~ "ThiagoWild", "Camila Osorio" ~ "Maria Camila Osorio Serrano",
    #  "Moller" ~ "Moeller")
    return (
        (a.initial in b.initials or b.initial in a.initials)
        and (a.last_c in b.sq_c or b.last_c in a.sq_c)
    )


def team_eq(a: Team, b: Team, exact: bool) -> bool:
    if a.is_double != b.is_double:
        return False
    if not a.is_double:
        return player_eq(a.players[0], b.players[0], exact)
    if exact:  # doubles use different name formats, only loose makes sense
        return False
    return any(
        all(player_eq(x, y, False) for x, y in zip(a.players, perm))
        for perm in permutations(b.players)
    )


def match_order(p1, p2, b1, b2, exact):
    """Return 'straight', 'swapped' or None."""
    if team_eq(p1, b1, exact) and team_eq(p2, b2, exact):
        return "straight"
    if team_eq(p1, b2, exact) and team_eq(p2, b1, exact):
        return "swapped"
    return None


# ---------- tournament normalization ----------

def tournament_tokens(raw) -> set[str]:
    """Meaningful tokens of a tournament name: no generic words, no ITF grade
    (M25/W50), no short filler (di/de), Polish city names mapped to English."""
    if not raw:
        return set()
    out = set()
    for t in re.findall(r"[a-z0-9]+", ascii_lower(raw)):
        if t in TOURNAMENT_STOPWORDS or len(t) < 3 or re.fullmatch(r"[mw]?\d+", t):
            continue
        out.update(CITY_ALIASES.get(t, t).split())
    return out


def tournaments_agree(a, b) -> bool:
    """True when both names have meaningful tokens and share at least one.
    Unknown/empty tournament -> False (we cannot confirm)."""
    return bool(tournament_tokens(a) & tournament_tokens(b))


def is_verified(p, b, tier) -> bool:
    return tier == "exact" or tournaments_agree(p["tournament"], b["tournament"])


# ---------- io ----------

def read_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["odds1"], r["odds2"] = float(r["odds1"]), float(r["odds2"])
        r["t1"], r["t2"] = Team(r["player1"]), Team(r["player2"])
        r["ts"] = datetime.fromisoformat(r["captured_at"])
    return rows


# ---------- matching ----------

def match_books(pin_rows, bet_rows):
    """Return (matches, unmatched_pinnacle, ambiguous, unmatched_betclic)."""
    used = set()
    matches, unmatched, ambiguous = [], [], []

    for p in pin_rows:
        found = []
        for tier in ("exact", "loose"):
            for i, b in enumerate(bet_rows):
                if i in used:
                    continue
                order = match_order(p["t1"], p["t2"], b["t1"], b["t2"], tier == "exact")
                if order:
                    found.append((i, b, order, tier))
            if found:
                break

        if not found:
            unmatched.append(p)
        elif len(found) > 1:
            # several candidates: let the tournament break the tie
            narrowed = [f for f in found if tournaments_agree(p["tournament"], f[1]["tournament"])]
            if len(narrowed) == 1:
                i, b, order, tier = narrowed[0]
                used.add(i)
                matches.append((p, b, order, tier))
            else:
                ambiguous.append((p, found))
        else:
            i, b, order, tier = found[0]
            used.add(i)
            matches.append((p, b, order, tier))

    unmatched_bet = [b for i, b in enumerate(bet_rows) if i not in used]
    return matches, unmatched, ambiguous, unmatched_bet


# ---------- data age ----------

def snapshot_info(pin_rows, bet_rows, now, max_age) -> dict:
    """Run-level freshness. Every row of a CSV shares one captured_at, so the
    age is a property of the snapshot, not of a match.

    age_min - now minus the OLDER snapshot (what matters for placing a bet)
    gap_min - distance between the two snapshots (how well they line up)
    """
    pin_ts = min((r["ts"] for r in pin_rows), default=now)
    bet_ts = min((r["ts"] for r in bet_rows), default=now)
    age_min = (now - min(pin_ts, bet_ts)).total_seconds() / 60
    return {
        "age_min": age_min,
        "gap_min": abs((pin_ts - bet_ts).total_seconds()) / 60,
        "stale": age_min > max_age,
    }


# ---------- EV ----------

def ev_rows(matches, stale):
    out = []
    for p, b, order, tier in matches:
        probs = remove_margin([p["odds1"], p["odds2"]])
        bet_odds = (b["odds1"], b["odds2"]) if order == "straight" else (b["odds2"], b["odds1"])
        tour_ok = tournaments_agree(p["tournament"], b["tournament"])
        # Margin (overround) = sum of raw implied probabilities - 1.
        pin_margin = 1 / p["odds1"] + 1 / p["odds2"] - 1
        bet_margin = 1 / bet_odds[0] + 1 / bet_odds[1] - 1
        for side in (0, 1):
            out.append({
                "ev": calculate_ev(probs[side], bet_odds[side]),
                "player": (p["player1"], p["player2"])[side],
                "opponent": (p["player2"], p["player1"])[side],
                "betclic_odds": bet_odds[side],
                "pinnacle_odds": (p["odds1"], p["odds2"])[side],
                "pinnacle_margin": pin_margin,
                "betclic_margin": bet_margin,
                "p_ref": probs[side],
                "tournament": p["tournament"],
                "doubles": p["t1"].is_double,
                "match_type": tier,
                "tournament_match": tour_ok,
                "verified": tier == "exact" or tour_ok,
                "betclic_tournament": b["tournament"],
                "betclic_name": (b["player1"], b["player2"])[side if order == "straight" else 1 - side],
                "pinnacle_captured_at": p["captured_at"],
                "betclic_captured_at": b["captured_at"],
                "stale": stale,  # run-level: same for every row
            })
    out.sort(key=lambda r: r["ev"], reverse=True)  # full precision, rounding only on display
    return out


# ---------- output ----------

def fmt_odds(x: float) -> str:
    """Odds for display: at least 2, at most 4 decimals (1.9 -> 1.90, 1.005 -> 1.005)."""
    s = f"{x:.4f}".rstrip("0")
    whole, dec = s.split(".")
    return f"{whole}.{dec.ljust(2, '0')}"


def fmt_age(minutes: float) -> str:
    """Data age: seconds when < 1 min, then min + s, then h + min."""
    s = round(minutes * 60)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


CELL_STYLE = "border:1px solid #888;padding:4px 8px;"


def html_table(headers: list[str], rows: list[list[str]], aligns: list[str]) -> str:
    """HTML table with a full grid. Cells must already be escaped."""
    def cell(tag: str, text: str, align: str) -> str:
        return f'<{tag} align="{align}" style="{CELL_STYLE}text-align:{align}">{text}</{tag}>'

    out = ['<table border="1" cellspacing="0" style="border-collapse:collapse">']
    out.append("<tr>" + "".join(cell("th", h, a) for h, a in zip(headers, aligns)) + "</tr>")
    for row in rows:
        out.append("<tr>" + "".join(cell("td", c, a) for c, a in zip(row, aligns)) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


# ---------- colours ----------

class Style:
    enabled = False


def setup_color(mode: str) -> None:
    """mode: auto (tty only, honours NO_COLOR), always, never."""
    if mode == "always":
        Style.enabled = True
    elif mode == "never":
        Style.enabled = False
    else:
        Style.enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if Style.enabled and os.name == "nt":
        os.system("")  # enables ANSI escape codes in Windows 10+ consoles


def paint(text: str, *codes: str) -> str:
    """Wrap already-padded text in ANSI codes (pad first, colour second)."""
    if not Style.enabled or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def ev_style(r: dict, min_ev: float) -> tuple[str, ...]:
    if r["ev"] < 0:
        return ("31",)             # red: negative EV
    if r["ev"] >= min_ev and not r["stale"]:
        return ("1", "32")         # bold green: signal
    return ("33",)                 # yellow: positive but below threshold / stale


def ev_icon(r: dict, min_ev: float) -> str:
    if r["ev"] < 0:
        return "🔴"
    if r["ev"] >= min_ev and not r["stale"]:
        return "🟢"
    return "🟡"


# ---------- data age line ----------

def age_line_terminal(snap: dict, args) -> str:
    text = (
        f"Data age: {fmt_age(snap['age_min'])}\n"
        f"Snapshot time difference: {fmt_age(snap['gap_min'])} "
        f"(Betclic -> Pinnacle)"
    )

    if snap["stale"]:
        return paint(
            f"{text}\nSTALE (> {args.max_age:g} min)",
            "1",
            "31",
        )

    return paint(text, "2")


def age_line_md(snap: dict, args) -> str:
    text = (
        f"**Data age:** {fmt_age(snap['age_min'])}\n\n"
        f"**Snapshot time difference:** {fmt_age(snap['gap_min'])} "
        f"(Betclic → Pinnacle)"
    )

    if snap["stale"]:
        text += f"\n\n🟡 **STALE** (older than {args.max_age:g} min)"

    return text




# ---------- summary ----------

def summary_items(rows, stats, args) -> list[tuple[str, str]]:
    signals = [r for r in rows if r["ev"] >= args.min_ev and not r["stale"]]
    return [
        ("Pinnacle rows", str(stats["pin"])),
        ("Betclic rows", str(stats["bet_all"])),
        ("Matched matches", f"{stats['matched']} (exact {stats['exact']}, loose {stats['loose']})"),
        ("Matches to verify (loose + tournament differs)", str(len(stats["to_check"]))),
        ("Skipped (live on Betclic)", str(len(stats["live_skipped"]))),
        ("Unmatched Pinnacle", str(stats["unmatched"])),
        ("Sides with EV > 0", f"{sum(r['ev'] > 0 for r in rows)} / {len(rows)}"),
        (f"Signals (EV >= {args.min_ev * 100:.1f}%, data age <= {args.max_age:g} min)", str(len(signals))),
    ]



def summary_terminal(items: list[tuple[str, str]]) -> str:
    kw = max(len(k) for k, _ in items)
    vw = max(len(v) for _, v in items)
    sep = "+-" + "-" * kw + "-+-" + "-" * vw + "-+"
    lines = [sep, f"| {paint('Metric'.ljust(kw), '1')} | {paint('Value'.ljust(vw), '1')} |", sep]
    for k, v in items:
        lines.append(f"| {k.ljust(kw)} | {v.ljust(vw)} |")
    lines.append(sep)
    return "\n".join(lines)


def summary_md(items: list[tuple[str, str]]) -> str:
    return html_table(
        ["Metric", "Value"],
        [[html.escape(k), html.escape(v)] for k, v in items],
        ["left", "left"],
    )


# ---------- tables ----------

PW = 26  # name column width


def flag_list(r: dict, ascii_only: bool) -> list[str]:
    flags = []
    if not r["verified"]:
        flags.append("CHECK" if ascii_only else "⚠ check")
    if r["doubles"]:
        flags.append("doubles")
    return flags


def terminal_table(rows, top, min_ev):
    """One row = one side of a match. The BET ON player is highlighted: the
    odds, P(ref) and EV in that row all refer to him; the opponent is dimmed."""
    header = (
        f"{'#':>3}  {'BET ON':<{PW}} {'Betclic':>7} {'Pinnacle':>8} {'P(ref)':>7} {'EV':>8}  "
        f"{'OPPONENT':<{PW + 3}}"
    )
    lines = [paint(header, "1"), "-" * len(header)]
    flag_style = {"CHECK": ("1", "33"), "doubles": ("2",)}
    for i, r in enumerate(rows[:top], 1):
        player = paint(f"{r['player'][:PW]:<{PW}}", "1", "36")          # bold cyan
        ev = paint(f"{r['ev'] * 100:>+7.2f}%", *ev_style(r, min_ev))
        opp = paint(f"vs {r['opponent'][:PW]:<{PW}}", "2")              # dim
        flags = "".join(" " + paint(f, *flag_style[f]) for f in flag_list(r, True))
        lines.append(
            f"{i:>3}  {player} {fmt_odds(r['betclic_odds']):>7} {fmt_odds(r['pinnacle_odds']):>8} "
            f"{r['p_ref'] * 100:>6.2f}% {ev}  {opp}{flags}"
        )
    if len(rows) > top:
        lines.append(f"... {len(rows) - top} more rows in the CSV / report")
    lines.append(
        "Legend: "
        + paint("BET ON", "1", "36") + " = player the odds/EV in the row refer to | "
        + paint("green", "1", "32") + " = signal | "
        + paint("yellow", "33") + " = EV >= 0, below threshold or data stale | "
        + paint("red", "31") + " = negative EV | "
        + paint("CHECK", "1", "33") + " = loose name match, tournament not confirmed, verify by hand"
    )
    return "\n".join(lines)


def md_table(rows, min_ev):
    headers = ["#", "EV", "Bet on", "Betclic", "Pinnacle", "Margin Pinnacle",
               "Margin Betclic", "P(ref)", "Opponent", "Tournament"]
    aligns = ["right", "right", "left", "right", "right", "right",
              "right", "right", "left", "left"]

    flags = [", ".join(flag_list(r, False)) for r in rows]
    show_flags = any(flags)  # drop the Flags column when every cell is empty
    if show_flags:
        headers.append("Flags")
        aligns.append("left")

    body = []
    for i, (r, fl) in enumerate(zip(rows, flags), 1):
        cells = [
            str(i),
            f"{ev_icon(r, min_ev)} {r['ev'] * 100:+.2f}%",
            f"<b>{html.escape(r['player'])}</b>",
            fmt_odds(r["betclic_odds"]),
            fmt_odds(r["pinnacle_odds"]),
            fmt_pct(r["pinnacle_margin"]),
            fmt_pct(r["betclic_margin"]),
            fmt_pct(r["p_ref"]),
            html.escape(r["opponent"]),
            html.escape(r["tournament"] or ""),
        ]
        if show_flags:
            cells.append(html.escape(fl))
        body.append(cells)
    return html_table(headers, body, aligns)


def legend_md(args) -> list[str]:
    """Small 'Legend' heading + one row per colour. Thresholds come from args."""
    t = f"{args.min_ev * 100:g}%"
    return [
        "### Legend",
        "",
        f"- 🟢 EV >= {t} and data age <= {args.max_age:g} min (signal)",
        f"- 🟡 EV between 0% and {t}, or EV >= {t} but data older than {args.max_age:g} min",
        "- 🔴 EV < 0%",
        "",
        "**Bold player** = the side the odds and EV in that row refer to.",
        "",
    ]


def build_report(rows, stats, args, generated_at):
    signals = [r for r in rows if r["ev"] >= args.min_ev and not r["stale"]]
    parts = [
        "# Pinnacle -> Betclic EV report",
        "",
        f"Generated: {generated_at:%Y-%m-%d %H:%M:%S} UTC",
        "",
        age_line_md(stats["snap"], args),
        "",
        "## Summary",
        "",
        summary_md(summary_items(rows, stats, args)),
        "",
        *legend_md(args),
        "## Signals",
        "",
        md_table(signals, args.min_ev) if signals else "_No signals above the threshold._",
        "",
        "## All matched sides (best to worst EV)",
        "",
        md_table(rows, args.min_ev) if rows else "_No matches._",
        "",
        "## Matches to verify manually (loose name match, tournament not confirmed)",
        "",
    ]
    parts += [
        f"- {p['player1']} vs {p['player2']} ({p['tournament']}) <-> "
        f"{b['player1']} vs {b['player2']} ({b['tournament']})"
        for p, b, _order, _tier in stats["to_check"]
    ] or ["_None._"]
    parts += [
        "",
        "_If the same tournament shows up here with different names, add its city to `src/betting/tournament_names.py`._",
        "",
        "## Pinnacle matches skipped (live on Betclic)",
        "",
    ]
    parts += [f"- {p['player1']} vs {p['player2']} ({p['tournament']})"
              for p, *_ in stats["live_skipped"]] or ["_None._"]
    parts += [
        "",
        "## Pinnacle matches without a Betclic pair",
        "",
    ]
    parts += [f"- {p['player1']} vs {p['player2']} ({p['tournament']})" for p in stats["unmatched_rows"]] or ["_None._"]
    parts += [
        "",
        "## Caveats",
        "",
        "- Rows flagged `⚠ check` were paired by loose name matching and their tournaments could not be matched; verify them by hand (see the section above).",
        "- Only prematch match-winner markets are compared; Betclic live rows are dropped unless `--include-live`.",
        "- Positive EV against a devigged Pinnacle line is an estimate, not a guaranteed edge.",
        "",
    ]
    return "\n".join(parts)


def rows_to_csv_text(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def save_history(rows: list[dict], fields: list[str], stamp: str):
    """Save the EV table to history/ unless it is identical to the newest file there.

    Returns (path, saved). The rows carry both snapshot timestamps, so identical
    content means identical input data. Run-dependent values (data age, stale)
    are deliberately NOT in the CSV, otherwise no two runs would ever be equal.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    text = rows_to_csv_text(rows, fields)

    existing = sorted(HISTORY_DIR.glob("ev_*.csv"))
    if existing and existing[-1].read_text(encoding="utf-8") == text:
        return existing[-1], False

    path = HISTORY_DIR / f"ev_{stamp}.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return path, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-live", action="store_true",
                    help="keep Betclic rows marked live (default: drop them after matching)")
    ap.add_argument("--min-ev", type=float, default=0.02, help="EV threshold, default 0.02")
    ap.add_argument("--max-age", type=float, default=15.0,
                    help="max age in minutes (now minus the older snapshot) for a signal, default 15")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                    help="ANSI colours in the terminal table, default auto (tty only, honours NO_COLOR)")
    ap.add_argument("--top", type=int, default=20, help="rows in the terminal table, default 20")
    args = ap.parse_args()
    setup_color(args.color)

    pin = read_rows(PINNACLE_CSV)
    bet_all = read_rows(BETCLIC_CSV)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    snap = snapshot_info(pin, bet_all, now, args.max_age)

    # Match against ALL Betclic rows, drop live ones only afterwards, so a
    # Pinnacle match that is live on Betclic is reported as "skipped (live)"
    # instead of "without a Betclic pair".
    matches, unmatched, ambiguous, _ = match_books(pin, bet_all)
    live_skipped = []
    if not args.include_live:
        live_skipped = [m for m in matches if m[1]["status"] == "live"]
        matches = [m for m in matches if m[1]["status"] != "live"]
    rows = ev_rows(matches, snap["stale"])
    to_check = [m for m in matches if not is_verified(m[0], m[1], m[3])]

    stats = {
        "pin": len(pin), "bet_all": len(bet_all),
        "matched": len(matches),
        "exact": sum(m[3] == "exact" for m in matches),
        "loose": sum(m[3] == "loose" for m in matches),
        "unmatched": len(unmatched), "ambiguous": len(ambiguous),
        "unmatched_rows": unmatched,
        "live_skipped": live_skipped,
        "to_check": to_check,
        "snap": snap,
    }

    fields = ["ev", "player", "opponent", "betclic_odds", "pinnacle_odds",
              "pinnacle_margin", "betclic_margin", "p_ref",
              "tournament", "betclic_tournament", "doubles", "match_type", "tournament_match",
              "verified", "betclic_name",
              "pinnacle_captured_at", "betclic_captured_at"]
    csv_path, saved = save_history(rows, fields, stamp)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(build_report(rows, stats, args, now), encoding="utf-8")

    print(age_line_terminal(snap, args))
    print()
    print(summary_terminal(summary_items(rows, stats, args)))
    print()
    print(terminal_table(rows, args.top, args.min_ev))
    if saved:
        print(f"\nCSV (new history entry): {csv_path}")
    else:
        print(f"\nCSV identical to the newest history entry, not saved again: {csv_path.name}")
    print(f"Report (overwritten):    {LATEST_REPORT}")


if __name__ == "__main__":
    main()