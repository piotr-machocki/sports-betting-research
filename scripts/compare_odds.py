"""Pinnacle -> devig -> Betclic EV comparison.

Reads data/pinnacle/odds.csv and data/betclic/odds.csv, matches the same
matches across bookmakers by normalized player names, computes EV for both
sides of every matched match, prints a table sorted from best to worst EV and
appends a full-precision CSV to data/results/history/ (skipped when identical
to the newest entry) and overwrites data/results/latest_report.md.

Usage:  python scripts/compare_odds.py [--include-live] [--min-ev 0.02]
                                        [--max-age 15] [--top 20]
"""
import argparse
import csv
import io
import re
import sys
import unicodedata
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from betting.margin import remove_margin  # noqa: E402
from betting.value import calculate_ev  # noqa: E402

PINNACLE_CSV = ROOT / "data" / "pinnacle" / "odds.csv"
BETCLIC_CSV = ROOT / "data" / "betclic" / "odds.csv"
RESULTS_DIR = ROOT / "data" / "results"
HISTORY_DIR = RESULTS_DIR / "history"          # one CSV per distinct snapshot pair
LATEST_REPORT = RESULTS_DIR / "latest_report.md"  # always overwritten


# ---------- name normalization ----------

class Player:
    """Common form of a player name.

    initial - first letter of the first name
    sq      - name squashed to letters only, no spaces/hyphens/diacritics
              ("Learner Tien" and "LearnerTien" -> "learnertien")
    last    - last token of the name (used for loose matching)
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


def ascii_lower(s: str) -> str:
    s = s.replace("ł", "l").replace("Ł", "L").replace("đ", "d").replace("ø", "o")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


class Team:
    def __init__(self, raw: str):
        parts = [p for p in raw.split("/") if p.strip()]
        self.players = [Player(p) for p in parts]
        self.is_double = len(self.players) == 2


def player_eq(a: Player, b: Player, exact: bool) -> bool:
    if exact:
        return a.sq == b.sq
    # loose: same first-name initial and one surname contained in the other name
    # ("Thiago Seyboth Wild" ~ "ThiagoWild", "Joao Dinis Silva" ~ "Joao Silva")
    return a.initial == b.initial and (a.last in b.sq or b.last in a.sq)


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
    """Return (matches, unmatched_pinnacle, ambiguous)."""
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
            ambiguous.append((p, found))
        else:
            i, b, order, tier = found[0]
            used.add(i)
            matches.append((p, b, order, tier))

    unmatched_bet = [b for i, b in enumerate(bet_rows) if i not in used]
    return matches, unmatched, ambiguous, unmatched_bet


# ---------- EV ----------

def ev_rows(matches, max_age):
    out = []
    for p, b, order, tier in matches:
        probs = remove_margin([p["odds1"], p["odds2"]])
        bet_odds = (b["odds1"], b["odds2"]) if order == "straight" else (b["odds2"], b["odds1"])
        age_min = abs((p["ts"] - b["ts"]).total_seconds()) / 60
        for side in (0, 1):
            out.append({
                "ev": calculate_ev(probs[side], bet_odds[side]),
                "player": (p["player1"], p["player2"])[side],
                "opponent": (p["player2"], p["player1"])[side],
                "betclic_odds": bet_odds[side],
                "pinnacle_odds": (p["odds1"], p["odds2"])[side],
                "p_ref": probs[side],
                "tournament": p["tournament"],
                "doubles": p["t1"].is_double,
                "match_type": tier,
                "betclic_name": (b["player1"], b["player2"])[side if order == "straight" else 1 - side],
                "pinnacle_captured_at": p["captured_at"],
                "betclic_captured_at": b["captured_at"],
                "age_min": age_min,
                "stale": age_min > max_age,
            })
    out.sort(key=lambda r: r["ev"], reverse=True)  # full precision, rounding only on display
    return out


# ---------- output ----------

def fmt_odds(x: float) -> str:
    """Odds for display: at least 2, at most 4 decimals (1.9 -> 1.90, 1.005 -> 1.005)."""
    s = f"{x:.4f}".rstrip("0")
    whole, dec = s.split(".")
    return f"{whole}.{dec.ljust(2, '0')}"


def terminal_table(rows, top):
    lines = [
        f"{'#':>3} {'EV':>8} {'Betclic':>8} {'Pinnacle':>9} {'P(ref)':>8}  {'Player':<30} {'Opponent':<30} {'Age':>4}",
        "-" * 108,
    ]
    for i, r in enumerate(rows[:top], 1):
        lines.append(
            f"{i:>3} {r['ev'] * 100:>+7.2f}% {fmt_odds(r['betclic_odds']):>8} {fmt_odds(r['pinnacle_odds']):>9} "
            f"{r['p_ref'] * 100:>7.2f}%  {r['player'][:30]:<30} {r['opponent'][:30]:<30} {r['age_min']:>3.0f}m"
        )
    if len(rows) > top:
        lines.append(f"... {len(rows) - top} more rows in the CSV / report")
    return "\n".join(lines)


def md_table(rows):
    out = [
        "| # | EV | Player | Opponent | Betclic | Pinnacle | P(ref) | Data age | Match | Tournament |",
        "|--:|--:|---|---|--:|--:|--:|--:|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        flags = r["match_type"] + (", doubles" if r["doubles"] else "") + (", STALE" if r["stale"] else "")
        out.append(
            f"| {i} | {r['ev'] * 100:+.2f}% | {r['player']} | {r['opponent']} | {fmt_odds(r['betclic_odds'])} | "
            f"{fmt_odds(r['pinnacle_odds'])} | {r['p_ref'] * 100:.2f}% | {r['age_min']:.0f} min | {flags} | {r['tournament'] or ''} |"
        )
    return "\n".join(out)


def build_report(rows, stats, args, generated_at):
    signals = [r for r in rows if r["ev"] >= args.min_ev and not r["stale"]]
    positive = [r for r in rows if r["ev"] > 0]
    ages = [r["age_min"] for r in rows]
    parts = [
        "# Pinnacle -> Betclic EV report",
        "",
        f"Generated: {generated_at:%Y-%m-%d %H:%M:%S} UTC",
        "",
        "## Summary",
        "",
        f"- Pinnacle rows: {stats['pin']}, Betclic rows: {stats['bet_all']} (after live filter: {stats['bet']})",
        f"- Matched matches: {stats['matched']} (exact: {stats['exact']}, loose: {stats['loose']})",
        f"- Unmatched Pinnacle: {stats['unmatched']}, ambiguous: {stats['ambiguous']}",
        f"- Sides with EV > 0: {len(positive)} / {len(rows)}",
        f"- Signals (EV >= {args.min_ev * 100:.1f}% and data age <= {args.max_age:g} min): {len(signals)}",
        f"- Data age between snapshots: min {min(ages, default=0):.1f}, max {max(ages, default=0):.1f} min "
        f"(stale = older than {args.max_age:g} min: {sum(r['stale'] for r in rows)} sides)",
        "",
        "## Signals",
        "",
        md_table(signals) if signals else "_No signals above the threshold._",
        "",
        "## All matched sides (best to worst EV)",
        "",
        md_table(rows) if rows else "_No matches._",
        "",
        "## Pinnacle matches without a Betclic pair",
        "",
    ]
    parts += [f"- {p['player1']} vs {p['player2']} ({p['tournament']})" for p in stats["unmatched_rows"]] or ["_None._"]
    parts += [
        "",
        "## Caveats",
        "",
        "- Loose matches (`match` column) were paired by initial + surname and should be verified by hand.",
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
    content means identical input data.
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
                    help="keep Betclic rows marked live (default: drop them)")
    ap.add_argument("--min-ev", type=float, default=0.02, help="EV threshold, default 0.02")
    ap.add_argument("--max-age", type=float, default=15.0,
                    help="max minutes between the two snapshots for a signal, default 15")
    ap.add_argument("--top", type=int, default=20, help="rows in the terminal table, default 20")
    args = ap.parse_args()

    pin = read_rows(PINNACLE_CSV)
    bet_all = read_rows(BETCLIC_CSV)
    bet = bet_all if args.include_live else [b for b in bet_all if b["status"] != "live"]

    matches, unmatched, ambiguous, _ = match_books(pin, bet)
    rows = ev_rows(matches, args.max_age)

    stats = {
        "pin": len(pin), "bet_all": len(bet_all), "bet": len(bet),
        "matched": len(matches),
        "exact": sum(m[3] == "exact" for m in matches),
        "loose": sum(m[3] == "loose" for m in matches),
        "unmatched": len(unmatched), "ambiguous": len(ambiguous),
        "unmatched_rows": unmatched,
    }
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    fields = ["ev", "player", "opponent", "betclic_odds", "pinnacle_odds", "p_ref",
              "tournament", "doubles", "match_type", "betclic_name",
              "pinnacle_captured_at", "betclic_captured_at", "age_min", "stale"]
    csv_path, saved = save_history(rows, fields, stamp)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(build_report(rows, stats, args, now), encoding="utf-8")

    signals = [r for r in rows if r["ev"] >= args.min_ev and not r["stale"]]
    print(f"Matched {stats['matched']} matches ({stats['exact']} exact, {stats['loose']} loose), "
          f"unmatched Pinnacle: {stats['unmatched']}, ambiguous: {stats['ambiguous']}")
    print(f"EV > 0: {sum(r['ev'] > 0 for r in rows)} / {len(rows)} sides | "
          f"signals (EV >= {args.min_ev * 100:.1f}%, age <= {args.max_age:g} min): {len(signals)}\n")
    print(terminal_table(rows, args.top))
    if saved:
        print(f"\nCSV (new history entry): {csv_path}")
    else:
        print(f"\nCSV identical to the newest history entry, not saved again: {csv_path.name}")
    print(f"Report (overwritten):    {LATEST_REPORT}")


if __name__ == "__main__":
    main()