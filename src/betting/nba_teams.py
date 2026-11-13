"""The 30 NBA teams and the names each odds source uses for them.

Every source is mapped to one three-letter code (the one Kalshi tickers,
Action Network and Polymarket slugs use), so a game can be joined across
sources as ``<US Eastern date>_<AWAY>@<HOME>``.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

US_EASTERN = ZoneInfo("America/New_York")

# code: (full name, Kalshi's short name, other abbreviations seen)
TEAMS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "ATL": ("Atlanta Hawks", "Atlanta", ()),
    "BKN": ("Brooklyn Nets", "Brooklyn", ("BK", "BRK", "NJN")),
    "BOS": ("Boston Celtics", "Boston", ()),
    "CHA": ("Charlotte Hornets", "Charlotte", ("CHO",)),
    "CHI": ("Chicago Bulls", "Chicago", ()),
    "CLE": ("Cleveland Cavaliers", "Cleveland", ()),
    "DAL": ("Dallas Mavericks", "Dallas", ()),
    "DEN": ("Denver Nuggets", "Denver", ()),
    "DET": ("Detroit Pistons", "Detroit", ()),
    "GSW": ("Golden State Warriors", "Golden State", ("GS",)),
    "HOU": ("Houston Rockets", "Houston", ()),
    "IND": ("Indiana Pacers", "Indiana", ()),
    "LAC": ("Los Angeles Clippers", "Los Angeles C", ("LA Clippers",)),
    "LAL": ("Los Angeles Lakers", "Los Angeles L", ("LA Lakers",)),
    "MEM": ("Memphis Grizzlies", "Memphis", ()),
    "MIA": ("Miami Heat", "Miami", ()),
    "MIL": ("Milwaukee Bucks", "Milwaukee", ()),
    "MIN": ("Minnesota Timberwolves", "Minnesota", ()),
    "NOP": ("New Orleans Pelicans", "New Orleans", ("NO", "NOR")),
    "NYK": ("New York Knicks", "New York", ("NY", "NY Knicks")),
    "OKC": ("Oklahoma City Thunder", "Oklahoma City", ()),
    "ORL": ("Orlando Magic", "Orlando", ()),
    "PHI": ("Philadelphia 76ers", "Philadelphia", ()),
    "PHX": ("Phoenix Suns", "Phoenix", ("PHO",)),
    "POR": ("Portland Trail Blazers", "Portland", ()),
    "SAC": ("Sacramento Kings", "Sacramento", ()),
    "SAS": ("San Antonio Spurs", "San Antonio", ("SA",)),
    "TOR": ("Toronto Raptors", "Toronto", ()),
    "UTA": ("Utah Jazz", "Utah", ("UTAH", "UTH")),
    "WAS": ("Washington Wizards", "Washington", ("WSH",)),
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _nickname(full: str) -> str:
    return "Trail Blazers" if full.endswith("Trail Blazers") else full.rsplit(" ", 1)[1]


_LOOKUP: dict[str, str] = {}
for _code, (_full, _short, _aliases) in TEAMS.items():
    for _name in (_code, _full, _short, _nickname(_full), *_aliases):
        _LOOKUP[_norm(_name)] = _code
_LOOKUP[_norm("Sixers")] = "PHI"
_LOOKUP[_norm("Blazers")] = "POR"
_LOOKUP[_norm("Timberwolves")] = "MIN"


def team_code(name: str | None) -> str | None:
    """The code for any full name, nickname, city or abbreviation; None if unknown.

    City names shared by two teams ("Los Angeles") are not resolved.
    """
    if not name:
        return None
    return _LOOKUP.get(_norm(name))


def game_date(starts_at: datetime) -> str:
    """The US Eastern date of a start time (NBA schedules are dated that way)."""
    return starts_at.astimezone(US_EASTERN).strftime("%Y-%m-%d")


def game_key(date: str, away: str, home: str) -> str:
    return f"{date}_{away}@{home}"
