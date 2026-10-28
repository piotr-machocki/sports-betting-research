"""Load tennis-data.co.uk files, clean historical matches and calculate devigged probabilities.

Outputs:
data/historical/processed/matches.csv - one row per match, for probability evaluation.
data/historical/processed/sides.csv - two rows per match with valid Pinnacle and Bet365
odds, for EV analysis.
"""

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from betting.margin import METHODS, remove_margin  # noqa: E402

HIST_DIR = ROOT / "data" / "historical"
RAW_DIR = HIST_DIR / "raw"
ATP_DIR = RAW_DIR / "atp"
WTA_DIR = RAW_DIR / "wta"

PROCESSED_DIR = HIST_DIR / "processed"
MATCHES_OUT = PROCESSED_DIR / "matches.csv"
SIDES_OUT = PROCESSED_DIR / "sides.csv"

REQUIRED_COLUMNS = [
    "Date",
    "Tournament",
    "Round",
    "Winner",
    "Loser",
    "PSW",
    "PSL",
    "Comment",
]

ODDS_COLUMNS = ["PSW", "PSL", "B365W", "B365L"]

VALID_COMMENTS = {"Completed"}


def identify_tour(path: Path) -> str | None:
    """Identify ATP or WTA from the parent folder."""
    folder = path.parent.name.lower()

    if folder == "atp":
        return "atp"

    if folder == "wta":
        return "wta"

    return None


def load_all() -> pd.DataFrame:
    """Load all historical Excel files and identify their tours and years."""
    files = sorted(
        list(ATP_DIR.glob("*.xlsx"))
        + list(WTA_DIR.glob("*.xlsx"))
    )

    if not files:
        raise FileNotFoundError(
            "No Excel files found. Expected files in:\n"
            f"  {ATP_DIR}\n"
            f"  {WTA_DIR}"
        )

    frames = []

    for path in files:
        tour = identify_tour(path)

        if tour is None:
            print(f"[SKIP] {path.name}: cannot identify ATP/WTA")
            continue

        try:
            year = int(path.stem[-4:])
        except ValueError:
            print(f"[SKIP] {path.name}: cannot identify year")
            continue

        try:
            df = pd.read_excel(path)
        except Exception as exc:
            print(f"[ERROR] {path.name}: {exc}")
            continue

        missing = [
            column
            for column in REQUIRED_COLUMNS
            if column not in df.columns
        ]

        if missing:
            print(f"[SKIP] {path.name}: missing columns {missing}")
            continue

        # Bet365 columns are optional for probability evaluation.
        if "B365W" not in df.columns:
            df["B365W"] = float("nan")

        if "B365L" not in df.columns:
            df["B365L"] = float("nan")

        df = df[REQUIRED_COLUMNS + ["B365W", "B365L"]].copy()

        df["tour"] = tour
        df["year"] = year
        df["source_file"] = path.name

        frames.append(df)

        print(
            f"[LOADED] {path.name}: {len(df)} matches "
            f"({tour.upper()}, {year})"
        )

    if not frames:
        raise ValueError(
            "No valid ATP/WTA files were loaded. "
            "Check filenames and required columns."
        )

    matches = pd.concat(frames, ignore_index=True)

    print(f"\nFiles loaded: {len(frames)} / {len(files)}")
    print(f"Total loaded: {len(matches)} rows")

    return matches


def clean_matches(matches: pd.DataFrame) -> pd.DataFrame:
    """Clean match data for historical probability evaluation."""
    before = len(matches)

    matches = matches.copy()

    matches["Date"] = pd.to_datetime(matches["Date"], errors="coerce")

    for column in ODDS_COLUMNS:
        matches[column] = pd.to_numeric(matches[column], errors="coerce")

    # Keep completed matches only.
    matches = matches[matches["Comment"].isin(VALID_COMMENTS)].copy()

    after_completed = len(matches)

    # Valid Pinnacle odds are required for devigging.
    valid_pinnacle = (
        matches["PSW"].notna()
        & matches["PSL"].notna()
        & matches["PSW"].gt(1)
        & matches["PSL"].gt(1)
    )

    matches = matches[valid_pinnacle].copy()

    after_pinnacle = len(matches)

    # Valid date and player names are required for evaluation.
    matches = matches.dropna(
        subset=["Date", "Winner", "Loser", "Tournament"]
    )

    matches = matches[
        matches["Winner"].astype(str).str.strip().ne("")
        & matches["Loser"].astype(str).str.strip().ne("")
        & matches["Tournament"].astype(str).str.strip().ne("")
    ].copy()

    after_metadata = len(matches)

    # Remove exact duplicate rows.
    matches = matches.drop_duplicates(
        subset=["tour", "Date", "Tournament", "Round", "Winner", "Loser"],
        keep="first",
    ).copy()

    after_duplicates = len(matches)

    print("\n=== MATCH CLEANING ===")
    print(f"Loaded rows:                     {before}")
    print(f"Completed matches:               {after_completed}")
    print(f"Valid Pinnacle odds:             {after_pinnacle}")
    print(f"Valid date/player/tournament:    {after_metadata}")
    print(f"After duplicate removal:         {after_duplicates}")
    print(f"Removed during cleaning:         {before - after_duplicates}")

    return matches.reset_index(drop=True)


def add_probabilities(matches: pd.DataFrame) -> pd.DataFrame:
    """Calculate winner and loser probabilities using every devig method."""
    matches = matches.copy()

    expected_methods = {"proportional", "power", "shin", "odds_ratio"}
    missing_methods = expected_methods - set(METHODS)

    if missing_methods:
        raise ValueError(
            f"Missing devig methods in betting.margin.METHODS: "
            f"{sorted(missing_methods)}. Available methods: {list(METHODS)}"
        )

    for method in METHODS:
        winner_probabilities = []
        loser_probabilities = []

        for winner_odds, loser_odds in zip(matches["PSW"], matches["PSL"]):
            probabilities = remove_margin(
                [float(winner_odds), float(loser_odds)],
                method,
            )

            p_winner = float(probabilities[0])
            p_loser = float(probabilities[1])

            if (
                not 0 < p_winner < 1
                or not 0 < p_loser < 1
                or abs(p_winner + p_loser - 1) > 1e-8
            ):
                raise ValueError(
                    f"Invalid probabilities for method {method}: "
                    f"{probabilities} (odds {winner_odds}, {loser_odds})"
                )

            winner_probabilities.append(p_winner)
            loser_probabilities.append(p_loser)

        matches[f"p_{method}_w"] = winner_probabilities
        matches[f"p_{method}_l"] = loser_probabilities

    return matches


def to_sides(matches: pd.DataFrame) -> pd.DataFrame:
    """Create two rows per match for EV analysis.

    Includes only matches with valid Pinnacle and Bet365 odds.
    Each row represents one player side:
        y = 1 if the player won
        y = 0 if the player lost
    """
    matches = matches.copy()

    valid_bet365 = (
        matches["B365W"].notna()
        & matches["B365L"].notna()
        & matches["B365W"].gt(1)
        & matches["B365L"].gt(1)
    )

    ev_matches = matches[valid_bet365].copy()

    print("\n=== EV DATA ===")
    print(f"Matches available for probability evaluation: {len(matches)}")
    print(f"Matches with valid Bet365 odds:               {len(ev_matches)}")
    print(
        f"Matches excluded from EV dataset:             "
        f"{len(matches) - len(ev_matches)}"
    )

    base_columns = [
        "Date",
        "Tournament",
        "Round",
        "tour",
        "year",
        "Winner",
        "Loser",
        "source_file",
    ]

    sides = []

    for suffix, player, opponent, pin_col, bet_col, outcome in (
        ("w", "Winner", "Loser", "PSW", "B365W", 1),
        ("l", "Loser", "Winner", "PSL", "B365L", 0),
    ):
        side = ev_matches[base_columns].copy()

        side["player"] = ev_matches[player]
        side["opponent"] = ev_matches[opponent]
        side["pinnacle_odds"] = ev_matches[pin_col]
        side["soft_odds"] = ev_matches[bet_col]
        side["y"] = outcome

        for method in METHODS:
            side[f"p_{method}"] = ev_matches[f"p_{method}_{suffix}"]

        sides.append(side)

    return pd.concat(sides, ignore_index=True)


def main() -> None:
    # Create the output directory if it does not exist.
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    matches = load_all()
    matches = clean_matches(matches)
    matches = add_probabilities(matches)

    # Dataset for probability evaluation: does not require Bet365 odds.
    matches.to_csv(
        MATCHES_OUT,
        index=False,
        date_format="%Y-%m-%d",
    )
    print(
        f"\n[SAVED] {len(matches)} match rows "
        f"-> {MATCHES_OUT.relative_to(ROOT)}"
    )

    # Dataset for EV analysis: requires valid Bet365 odds.
    sides = to_sides(matches)
    sides.to_csv(
        SIDES_OUT,
        index=False,
        date_format="%Y-%m-%d",
    )
    print(
        f"[SAVED] {len(sides)} side rows "
        f"-> {SIDES_OUT.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()