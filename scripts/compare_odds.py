"""Pinnacle -> devig -> Betclic EV comparison.

Reads data/pinnacle/odds.csv and data/betclic/odds.csv, matches the same
matches across bookmakers by normalized player names, computes EV for both
sides of every matched match (proportional = reference, plus power / Shin /
odds ratio), prints a table and appends a full-precision CSV to
data/results/history/ (skipped when identical to the newest entry) and
overwrites data/results/latest_report.md.

Names decide the match (exact, then loose). Tournaments are only an extra
confidence signal: a match is "verified" when the names match exactly, or loosely
AND the tournaments share a meaningful token. Unverified loose matches are
flagged CHECK and listed in the report. Tournament names are normalized with
the Polish->English city dictionary in src/betting/tournament_names.py.

Matching is done against ALL Betclic rows; matches that are live on Betclic are
dropped only afterwards (unless --include-live) and listed separately in the
report, so they do not show up as "without a Betclic pair".

Signals: a side is a signal when EV > 0% for ANY devig method (reference,
power, Shin, odds ratio) and the data is not stale. --min-ev no longer decides
what a signal is; it only separates yellow (0% < EV < min-ev) from green
(EV >= min-ev) in the colours and in the ranking below. Exactly zero EV is gray.

Row order (terminal and report), three tiers:
  1. EV(ref) > 0% (proportional normalization) - sorted by EV(ref), best first
  2. EV(ref) <= 0% but another method has EV > 0% - sorted by the total
     colour score across all four EV columns (green = 3 points,
     yellow = 2 points, gray = 1 point, red = 0 points), highest first;
     ties broken by the highest alternative EV
  3. The rest - sorted by EV(ref), best first

Data age is a property of the whole run, not of a single row: it is the time
between NOW and the OLDER of the two snapshots. It is shown once (terminal
and report header) together with the gap between the snapshots. When it
exceeds --max-age the data is "stale": no row counts as a signal.

The report tables are HTML (Markdown has no syntax for vertical grid lines),
so they render with a full grid in a Markdown preview.

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
HISTORY_DIR = RESULTS_DIR / "history"
LATEST_REPORT = RESULTS_DIR / "latest_report.md"

# Alternative devig methods. The default/reference method remains proportional.
# The dictionary maps the method argument to the suffix used in CSV column names.
ALT_METHODS = {
    "power": "power",
    "shin": "shin",
    "odds_ratio": "or",
}

# Row keys holding EV for every method: reference first, then the alternatives.
EV_KEYS = ("ev", "ev_power", "ev_shin", "ev_or")


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

    initials - first letters of all name tokens (loose matching: the other side may have extra given names, "Maria Camila Osorio Serrano")

    sq - name squashed to letters only, no spaces/hyphens/diacritics("Learner Tien" and "LearnerTien" -> "learnertien")

    last - last token of the name (used for loose matching)

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

    # Loose: first-name initial matches any token of the other name and one
    # surname is contained in the other name.
    # ("Thiago Seyboth Wild" ~ "ThiagoWild",
    #  "Camila Osorio" ~ "Maria Camila Osorio Serrano",
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
    if exact:
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
    """Meaningful tournament tokens: no generic words, ITF grades or short
    filler; Polish city names are mapped to English."""
    if not raw:
        return set()

    out = set()
    for token in re.findall(r"[a-z0-9]+", ascii_lower(raw)):
        if (
            token in TOURNAMENT_STOPWORDS
            or len(token) < 3
            or re.fullmatch(r"[mw]?\d+", token)
        ):
            continue
        out.update(CITY_ALIASES.get(token, token).split())

    return out


def tournaments_agree(a, b) -> bool:
    """True when both names share at least one token."""
    return bool(tournament_tokens(a) & tournament_tokens(b))


def is_verified(p, b, tier) -> bool:
    return tier == "exact" or tournaments_agree(
        p["tournament"], b["tournament"]
    )


# ---------- io ----------

def read_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        row["odds1"] = float(row["odds1"])
        row["odds2"] = float(row["odds2"])
        row["t1"] = Team(row["player1"])
        row["t2"] = Team(row["player2"])
        row["ts"] = datetime.fromisoformat(row["captured_at"])

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

                order = match_order(
                    p["t1"],
                    p["t2"],
                    b["t1"],
                    b["t2"],
                    tier == "exact",
                )

                if order:
                    found.append((i, b, order, tier))

            if found:
                break

        if not found:
            unmatched.append(p)

        elif len(found) > 1:
            # Several candidates: let the tournament break the tie.
            narrowed = [
                candidate
                for candidate in found
                if tournaments_agree(
                    p["tournament"], candidate[1]["tournament"]
                )
            ]

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

    unmatched_bet = [
        b for i, b in enumerate(bet_rows) if i not in used
    ]

    return matches, unmatched, ambiguous, unmatched_bet


# ---------- data age ----------

def snapshot_info(pin_rows, bet_rows, now, max_age) -> dict:
    """Calculate freshness from the older of the two snapshots.

    age_min - now minus the older snapshot
    gap_min - distance between the two snapshots
    """
    pin_ts = min((r["ts"] for r in pin_rows), default=now)
    bet_ts = min((r["ts"] for r in bet_rows), default=now)

    age_min = (now - min(pin_ts, bet_ts)).total_seconds() / 60

    return {
        "age_min": age_min,
        "gap_min": abs((pin_ts - bet_ts).total_seconds()) / 60,
        "stale": age_min > max_age,
    }


# ---------- EV classification and ordering ----------

def ev_points(value: float, min_ev: float) -> int:
    """Colour score: green = 3, yellow = 2, gray = 1, red = 0.

    Uses the same thresholds as the colours in the terminal and the report.
    """
    if value >= min_ev:
        return 3
    if value > 0:
        return 2
    if value == 0:
        return 1
    return 0


def any_pos(row: dict) -> bool:
    """True when EV > 0 for at least one method (reference or alternative)."""
    return any(row[key] > 0 for key in EV_KEYS)


def is_signal(row: dict) -> bool:
    """Signal = EV > 0 for any method, and the data is not stale."""
    return any_pos(row) and not row["stale"]


def sort_key(row: dict, min_ev: float) -> tuple:
    """Three tiers, see the module docstring.

    Tier 0: reference EV > 0           -> by reference EV, descending.
    Tier 1: only an alternative is > 0 -> by colour score over all methods,
            descending, then by the best alternative EV, descending.
    Tier 2: no method has EV > 0       -> by reference EV, descending.
    """
    if row["ev"] > 0:
        return (0, -row["ev"], 0.0)

    alt_best = max(row[key] for key in EV_KEYS[1:])
    if alt_best > 0:
        score = sum(ev_points(row[key], min_ev) for key in EV_KEYS)
        return (1, -score, -alt_best)

    return (2, -row["ev"], 0.0)


# ---------- EV ----------

def ev_rows(matches, stale, min_ev):
    """Calculate EV using proportional devig plus alternative methods.

    'ev' and 'p_ref' use proportional normalization.
    Alternative values are stored as p_power / ev_power, p_shin / ev_shin,
    and p_or / ev_or.

    Rows are returned in the tiered order described by sort_key().
    """
    out = []

    for p, b, order, tier in matches:
        pin_odds = [p["odds1"], p["odds2"]]

        # Proportional remains the reference method.
        probs = remove_margin(pin_odds)

        # Calculate probabilities using alternative methods.
        alt_probs = {
            method: remove_margin(pin_odds, method)
            for method in ALT_METHODS
        }

        bet_odds = (
            (b["odds1"], b["odds2"])
            if order == "straight"
            else (b["odds2"], b["odds1"])
        )

        tour_ok = tournaments_agree(
            p["tournament"], b["tournament"]
        )

        # Margin (overround) = sum of raw implied probabilities - 1.
        pin_margin = 1 / p["odds1"] + 1 / p["odds2"] - 1
        bet_margin = 1 / bet_odds[0] + 1 / bet_odds[1] - 1

        for side in (0, 1):
            row = {
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
                "betclic_name": (
                    (b["player1"], b["player2"])[
                        side if order == "straight" else 1 - side
                    ]
                ),
                "pinnacle_captured_at": p["captured_at"],
                "betclic_captured_at": b["captured_at"],
                "stale": stale,
            }

            # Store alternative probabilities and EV values.
            for method, suffix in ALT_METHODS.items():
                probability = alt_probs[method][side]
                row[f"p_{suffix}"] = probability
                row[f"ev_{suffix}"] = calculate_ev(
                    probability, bet_odds[side]
                )

            out.append(row)

    out.sort(key=lambda row: sort_key(row, min_ev))
    return out


# ---------- output formatting ----------

def fmt_odds(x: float) -> str:
    """Display odds with at least 2 and at most 4 decimal places."""
    s = f"{x:.4f}".rstrip("0")
    whole, dec = s.split(".")
    return f"{whole}.{dec.ljust(2, '0')}"


def fmt_age(minutes: float) -> str:
    """Display seconds, minutes + seconds, or hours + minutes."""
    seconds = round(minutes * 60)

    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s"

    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


CELL_STYLE = "border:1px solid #888;padding:4px 8px;"


def html_table(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str],
) -> str:
    """Build an HTML table with a full grid. Cells must already be escaped."""

    def cell(tag: str, text: str, align: str) -> str:
        return (
            f'<{tag} align="{align}" '
            f'style="{CELL_STYLE}text-align:{align}">{text}</{tag}>'
        )

    out = [
        '<table border="1" cellspacing="0" '
        'style="border-collapse:collapse">'
    ]

    out.append(
        "<tr>"
        + "".join(
            cell("th", header, align)
            for header, align in zip(headers, aligns)
        )
        + "</tr>"
    )

    for row in rows:
        out.append(
            "<tr>"
            + "".join(
                cell("td", value, align)
                for value, align in zip(row, aligns)
            )
            + "</tr>"
        )

    out.append("</table>")
    return "\n".join(out)


# ---------- terminal colours ----------

class Style:
    enabled = False


def setup_color(mode: str) -> None:
    """Set terminal colour mode; auto honours TTY and NO_COLOR."""
    if mode == "always":
        Style.enabled = True
    elif mode == "never":
        Style.enabled = False
    else:
        Style.enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    if Style.enabled and os.name == "nt":
        os.system("")


def paint(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes."""
    if not Style.enabled or not codes:
        return text

    return f"\033[{';'.join(codes)}m{text}\033[0m"


def ev_style(value: float, min_ev: float) -> tuple[str, ...]:
    """Style EV by sign and threshold, independently of freshness."""
    if value < 0:
        return ("31",)
    if value == 0:
        return ("90",)
    if value < min_ev:
        return ("33",)
    return ("1", "32")


def ev_icon(r: dict) -> str:
    """Show a yellow dot when any method has EV > 0 but the data is stale."""
    if r["stale"] and any_pos(r):
        return "🟡"
    return ""


# ---------- HTML EV colours ----------

def ev_html(value: float, min_ev: float) -> str:
    """Colour EV using the same sign/threshold logic for every EV column."""
    text = f"{value * 100:+.2f}%"

    if value < 0:
        color = "#d00000"  # red
    elif value == 0:
        color = "#808080"  # gray
    elif value < min_ev:
        color = "#b88600"  # yellow
    else:
        color = "#008000"  # green

    return f'<span style="color:{color}">{text}</span>'


def ev_ref_html(row: dict, min_ev: float, max_age: float) -> str:
    """Format reference EV with threshold colours and a stale-data marker.

    The marker is shown when ANY method has EV > 0 on stale data, so a row
    that is only positive for an alternative method is flagged as well.
    """
    icon = "🟡 " if row["stale"] and any_pos(row) else ""
    return icon + ev_html(row["ev"], min_ev)


# ---------- data age line ----------

def age_line_terminal(snap: dict, args) -> str:
    text = (
        f"Data age: {fmt_age(snap['age_min'])}\n"
        f"Snapshot time difference: {fmt_age(snap['gap_min'])} "
        "(Betclic -> Pinnacle)"
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
        "(Betclic → Pinnacle)"
    )

    if snap["stale"]:
        text += f"\n\n🟡 **STALE** (older than {args.max_age:g} min)"

    return text


# ---------- summary ----------

def summary_items(rows, stats, args) -> list[tuple[str, str]]:
    signals = [row for row in rows if is_signal(row)]

    return [
        ("Pinnacle rows", str(stats["pin"])),
        ("Betclic rows", str(stats["bet_all"])),
        (
            "Matched matches",
            f"{stats['matched']} "
            f"(exact {stats['exact']}, loose {stats['loose']})",
        ),
        (
            "Matches to verify (loose + tournament differs)",
            str(len(stats["to_check"])),
        ),
        ("Skipped (live on Betclic)", str(len(stats["live_skipped"]))),
        ("Unmatched Pinnacle", str(stats["unmatched"])),
        (
            "Sides with EV(ref) > 0",
            f"{sum(row['ev'] > 0 for row in rows)} / {len(rows)}",
        ),
        (
            "Sides with EV > 0 for any method",
            f"{sum(any_pos(row) for row in rows)} / {len(rows)}",
        ),
        (
            f"Signals (EV > 0% for any method, "
            f"data age <= {args.max_age:g} min)",
            str(len(signals)),
        ),
    ]


def summary_terminal(items: list[tuple[str, str]]) -> str:
    key_width = max(len(key) for key, _ in items)
    value_width = max(len(value) for _, value in items)

    separator = (
        "+-" + "-" * key_width + "-+-" + "-" * value_width + "-+"
    )

    lines = [
        separator,
        (
            f"| {paint('Metric'.ljust(key_width), '1')} "
            f"| {paint('Value'.ljust(value_width), '1')} |"
        ),
        separator,
    ]

    for key, value in items:
        lines.append(
            f"| {key.ljust(key_width)} | {value.ljust(value_width)} |"
        )

    lines.append(separator)
    return "\n".join(lines)


def summary_md(items: list[tuple[str, str]]) -> str:
    return html_table(
        ["Metric", "Value"],
        [
            [html.escape(key), html.escape(value)]
            for key, value in items
        ],
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


def terminal_table(rows, top, min_ev, max_age):
    """Print one row per side of a match."""
    header = (
        f"{'#':>3}  {'BET ON':<{PW}} {'Betclic':>7} {'Pinnacle':>8} "
        f"{'P(ref)':>7} {'EV(ref)':>8} {'EV(pow)':>8} {'EV(shin)':>8} "
        f"{'EV(OR)':>8}     {'OPPONENT':<{PW + 3}}"
    )

    lines = [paint(header, "1"), "-" * len(header)]
    flag_style = {"CHECK": ("1", "33"), "doubles": ("2",)}

    for i, row in enumerate(rows[:top], 1):
        player = paint(
            f"{row['player'][:PW]:<{PW}}", "1", "36"
        )

        ev_cells = " ".join(
            paint(f"{row[key] * 100:>+7.2f}%", *ev_style(row[key], min_ev))
            for key in EV_KEYS
        )

        # The dot always takes 3 characters so the opponent column stays aligned.
        icon = ev_icon(row)
        ev_cells += (" " + icon) if icon else "   "

        opponent = paint(
            f"vs {row['opponent'][:PW]:<{PW}}", "2"
        )

        flags = "".join(
            " " + paint(flag, *flag_style[flag])
            for flag in flag_list(row, True)
        )

        lines.append(
            f"{i:>3}  {player} "
            f"{fmt_odds(row['betclic_odds']):>7} "
            f"{fmt_odds(row['pinnacle_odds']):>8} "
            f"{row['p_ref'] * 100:>6.2f}% "
            f"{ev_cells}  {opponent}{flags}"
        )

    if len(rows) > top:
        lines.append(
            f"... {len(rows) - top} more rows in the CSV / report"
        )

    threshold = f"{min_ev * 100:g}%"

    lines.append(
        "Legend: "
        + paint("BET ON", "1", "36")
        + " = player the odds/EV in the row refer to | "
        + paint("red", "31")
        + " = EV < 0% | "
        + paint("gray", "90")
        + " = EV = 0% | "
        + paint("yellow", "33")
        + f" = 0% < EV < {threshold} | "
        + paint("green", "1", "32")
        + f" = EV >= {threshold} | "
        + "🟡 = EV > 0% for any method and data older than "
        + f"{max_age:g} min | "
        + paint("CHECK", "1", "33")
        + " = loose name match, tournament not confirmed; verify by hand"
    )
    lines.append(
        "Order: EV(ref) (proportional normalization) > 0% first, "
        "sorted by EV(ref) descending; then rows where EV(ref) <= 0% "
        "but another method has EV > 0%, sorted by the total colour score "
        "across all four EV columns (green = 3 points, yellow = 2 points, "
        "gray = 1 point, red = 0 points; ties broken by the highest "
        "alternative EV); then the rest, sorted by EV(ref) descending."
    )

    return "\n".join(lines)


def md_table(rows, min_ev, max_age):
    """HTML report table with probabilities grouped before alternative EVs."""
    headers = [
        "#",
        "EV(ref)",
        "Bet on",
        "Opponent",
        "Betclic",
        "Pinnacle",
        "Margin Pinnacle",
        "Margin Betclic",
        "Tournament",
        "P(ref)",
        "P(power)",
        "P(Shin)",
        "P(odds ratio)",
        "EV(power)",
        "EV(Shin)",
        "EV(odds ratio)",
    ]

    aligns = [
        "right",
        "right",
        "left",
        "left",
        "right",
        "right",
        "right",
        "right",
        "left",
        "right",
        "right",
        "right",
        "right",
        "right",
        "right",
        "right",
    ]

    body = []

    for i, row in enumerate(rows, 1):
        cells = [
            str(i),
            ev_ref_html(row, min_ev, max_age),
            f"<b>{html.escape(row['player'])}</b>",
            html.escape(row["opponent"]),
            fmt_odds(row["betclic_odds"]),
            fmt_odds(row["pinnacle_odds"]),
            fmt_pct(row["pinnacle_margin"]),
            fmt_pct(row["betclic_margin"]),
            html.escape(row["tournament"] or ""),
            fmt_pct(row["p_ref"]),
            fmt_pct(row["p_power"]),
            fmt_pct(row["p_shin"]),
            fmt_pct(row["p_or"]),
            ev_html(row["ev_power"], min_ev),
            ev_html(row["ev_shin"], min_ev),
            ev_html(row["ev_or"], min_ev),
        ]

        body.append(cells)

    return html_table(headers, body, aligns)


def legend_md(args) -> list[str]:
    """Explain EV colours, the stale-data dot and row order."""
    threshold = f"{args.min_ev * 100:g}%"

    return [
        "### Legend",
        "",
        f"- <span style=\"color:#d00000\">**Red text**</span>: "
        "EV < 0%.",
        "- <span style=\"color:#808080\">**Gray text**</span>: EV = 0%.",
        f"- <span style=\"color:#b88600\">**Yellow text**</span>: "
        f"0% < EV < {threshold}.",
        f"- <span style=\"color:#008000\">**Green text**</span>: "
        f"EV >= {threshold}.",
        f"- 🟡 **Yellow dot**: EV > 0% for any method and data age > "
        f"{args.max_age:g} min.",
        "- **Bold player**: the side the odds and EV in that row refer to.",
        "- **Signal**: EV > 0% for any method (reference, power, Shin, "
        f"odds ratio) and data age <= {args.max_age:g} min.",
        "- **Row order**: rows with EV(ref) (proportional normalization) "
        "> 0% first, sorted by EV(ref) descending. Then rows where "
        "EV(ref) <= 0% but another method has EV > 0%, sorted by the "
        "total colour score across all four EV columns (green = 3 points, "
        "yellow = 2 points, gray = 1 point, red = 0 points; ties broken "
        "by the highest alternative EV). Finally, the rest, sorted by "
        "EV(ref) descending.",
        "",
    ]


# ---------- Markdown report ----------

def build_report(rows, stats, args, generated_at):
    signals = [row for row in rows if is_signal(row)]

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
        (
            md_table(signals, args.min_ev, args.max_age)
            if signals
            else "_No signals._"
        ),
        "",
        (
            "## All matched sides"
        ),
        "",
        (
            md_table(rows, args.min_ev, args.max_age)
            if rows
            else "_No matches._"
        ),
        "",
        "## Matches to verify manually "
        "(loose name match and tournament not confirmed)",
        "",
    ]

    parts += [
        f"- {p['player1']} vs {p['player2']} ({p['tournament']}) "
        f"<-> {b['player1']} vs {b['player2']} ({b['tournament']})"
        for p, b, _order, _tier in stats["to_check"]
    ] or ["_None._"]

    parts += [
        "",
        (
            "_If the same tournament shows up here with different names, "
            "add its city to `src/betting/tournament_names.py`._"
        ),
        "",
        "## Pinnacle matches skipped (live on Betclic)",
        "",
    ]

    parts += [
        f"- {p['player1']} vs {p['player2']} ({p['tournament']})"
        for p, *_ in stats["live_skipped"]
    ] or ["_None._"]

    parts += [
        "",
        "## Pinnacle matches without a Betclic pair",
        "",
    ]

    parts += [
        f"- {p['player1']} vs {p['player2']} ({p['tournament']})"
        for p in stats["unmatched_rows"]
    ] or ["_None._"]

    parts += [
        "",
        "## About",
        "",
        (
            "- Only prematch match-winner markets are compared; Betclic live "
            "rows are dropped unless `--include-live`."
        ),
        (
            "- Positive EV against a devigged Pinnacle line is an estimate, "
            "not a guaranteed edge."
        ),
        (
            "- A signal fires when ANY of four correlated devig methods gives "
            "EV > 0%, so it is more permissive than the reference EV alone; "
            "look at how many methods agree before betting."
        ),
        "",
    ]

    return "\n".join(parts)


# ---------- CSV history ----------

def rows_to_csv_text(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def save_history(rows: list[dict], fields: list[str], stamp: str):
    """Save to history unless identical to the newest file.

    Run-dependent values such as age and stale are deliberately not saved,
    because they would prevent identical runs from being deduplicated.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    text = rows_to_csv_text(rows, fields)

    existing = sorted(HISTORY_DIR.glob("ev_*.csv"))

    if existing and existing[-1].read_text(encoding="utf-8") == text:
        return existing[-1], False

    path = HISTORY_DIR / f"ev_{stamp}.csv"
    path.write_text(text, encoding="utf-8", newline="")
    return path, True


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--include-live",
        action="store_true",
        help="keep Betclic rows marked live (default: drop them after matching)",
    )
    ap.add_argument(
        "--min-ev",
        type=float,
        default=0.02,
        help=(
            "green/yellow colour threshold and ranking weight, default 0.02 "
            "(does not decide what a signal is: any method EV > 0 does)"
        ),
    )
    ap.add_argument(
        "--max-age",
        type=float,
        default=15.0,
        help="max age in minutes for a signal, default 15",
    )
    ap.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="ANSI colours in the terminal table",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=20,
        help="rows in the terminal table, default 20",
    )

    args = ap.parse_args()
    setup_color(args.color)

    pin = read_rows(PINNACLE_CSV)
    bet_all = read_rows(BETCLIC_CSV)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    snap = snapshot_info(pin, bet_all, now, args.max_age)

    # Match against ALL Betclic rows, then drop live rows if requested.
    matches, unmatched, ambiguous, _ = match_books(pin, bet_all)

    live_skipped = []

    if not args.include_live:
        live_skipped = [
            match for match in matches if match[1]["status"] == "live"
        ]
        matches = [
            match for match in matches if match[1]["status"] != "live"
        ]

    rows = ev_rows(matches, snap["stale"], args.min_ev)

    to_check = [
        match for match in matches
        if not is_verified(match[0], match[1], match[3])
    ]

    stats = {
        "pin": len(pin),
        "bet_all": len(bet_all),
        "matched": len(matches),
        "exact": sum(match[3] == "exact" for match in matches),
        "loose": sum(match[3] == "loose" for match in matches),
        "unmatched": len(unmatched),
        "ambiguous": len(ambiguous),
        "unmatched_rows": unmatched,
        "live_skipped": live_skipped,
        "to_check": to_check,
        "snap": snap,
    }

    fields = [
        "ev",
        "player",
        "opponent",
        "betclic_odds",
        "pinnacle_odds",
        "pinnacle_margin",
        "betclic_margin",
        "p_ref",
        "p_power",
        "p_shin",
        "p_or",
        "ev_power",
        "ev_shin",
        "ev_or",
        "tournament",
        "betclic_tournament",
        "doubles",
        "match_type",
        "tournament_match",
        "verified",
        "betclic_name",
        "pinnacle_captured_at",
        "betclic_captured_at",
    ]

    csv_path, saved = save_history(rows, fields, stamp)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(
        build_report(rows, stats, args, now),
        encoding="utf-8",
    )

    print(age_line_terminal(snap, args))
    print()
    print(summary_terminal(summary_items(rows, stats, args)))
    print()
    print(terminal_table(rows, args.top, args.min_ev, args.max_age))

    if saved:
        print(f"\nCSV (new history entry): {csv_path}")
    else:
        print(
            "\nCSV identical to the newest history entry, "
            f"not saved again: {csv_path.name}"
        )

    print(f"Report (overwritten):    {LATEST_REPORT}")


if __name__ == "__main__":
    main()