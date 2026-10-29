"""Shared formatting for the run's performance summary."""

from __future__ import annotations

REASON_LABELS = {
    "child_event": "child event (games market)",
    "no_odds": "no odds available",
    "no_full_match_market": "no full-match market",
    "no_money_line": "no match-winner odds",
    "missing_players": "missing player name",
    "invalid_odds": "unparseable or <= 1.0 odds",
}


def format_duration(seconds) -> str:
    if seconds is None:
        return "n/a"

    seconds = float(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)

    if seconds < 60:
        return f"{sign}{seconds:.2f} s"

    minutes, secs = divmod(seconds, 60)

    if minutes < 60:
        return f"{sign}{int(minutes)} min {secs:05.2f} s"

    hours, minutes = divmod(int(minutes), 60)
    return f"{sign}{hours} h {minutes:02d} min {secs:05.2f} s"


def format_timestamp(value) -> str:
    if not value:
        return "n/a"

    return str(value).replace("T", " ").replace("Z", " UTC")


def quality_lines(pin: dict) -> list[str]:
    """Pinnacle data-quality block; empty when the breakdown is unavailable.

    Excluded = not a full-match winner market (expected).
    Invalid  = looked like a match but its data was unusable.
    """
    events = pin.get("events")
    valid = pin.get("matches")
    excluded = pin.get("excluded")
    invalid = pin.get("invalid")

    if events is None or valid is None:
        return []

    if excluded is None and invalid is None:
        return []

    excluded = excluded or {}
    invalid = invalid or {}
    n_excluded = sum(excluded.values())
    n_invalid = sum(invalid.values())

    def row(label: str, count: int, indent: int = 2) -> str:
        return f"{' ' * indent}{label:<{34 - indent}}{count:>6}"

    lines = [
        "Pinnacle data quality",
        row("Events received", events),
        row("Valid matches", valid),
        row("Excluded (not a match market)", n_excluded),
    ]
    lines += [
        row(REASON_LABELS.get(reason, reason), count, 4)
        for reason, count in excluded.items()
    ]
    lines.append(row("Invalid (bad data)", n_invalid))
    lines += [
        row(REASON_LABELS.get(reason, reason), count, 4)
        for reason, count in invalid.items()
    ]

    if valid + n_excluded + n_invalid != events:
        lines.append(
            f"  WARNING: totals do not reconcile "
            f"({valid} + {n_excluded} + {n_invalid} != {events})"
        )

    return lines

def performance_lines(
    perf: dict | None,
    started_at=None,
    pinnacle_data_age_seconds=None,
) -> list[str]:
    """Format performance and Pinnacle data-quality sections."""
    if not perf:
        return []

    pin = perf.get("pinnacle") or {}
    bet = perf.get("betclic") or {}

    events = pin.get("events")
    parsed = pin.get("matches")

    lines = [
        "=" * 60,
        "PERFORMANCE SUMMARY",
        "=" * 60,
        f"Started at:                {format_timestamp(started_at)}",
        f"Total runtime:             {format_duration(perf.get('total_seconds'))}",
        f"Pinnacle data age:         {format_duration(pinnacle_data_age_seconds)}",
        "",
        "Pinnacle",
        f"  Collection time:         {format_duration(pin.get('total_seconds'))}"
        f"  (API request: {format_duration(pin.get('api_seconds'))})",
        f"  Events in response:      {events if events is not None else 'n/a'}"
        + (f"  ({parsed} parsed matches)" if parsed is not None else ""),
        f"  Completed at:            {format_timestamp(pin.get('completed_at'))}",
        "",
        "Betclic",
        f"  Scraping time:           {format_duration(bet.get('seconds'))}",
        f"  Completed at:            {format_timestamp(bet.get('completed_at'))}",
        f"  Outcome:                 {bet.get('outcome', 'n/a')}",
        "",
    ]

    gap = perf.get("completion_gap_seconds")

    if gap is None:
        lines.append("Completion gap:            n/a")
    else:
        direction = "after" if gap >= 0 else "before"
        lines.append(
            f"Completion gap:            Betclic finished "
            f"{format_duration(abs(gap))} {direction} Pinnacle"
        )

    quality = quality_lines(pin)
    if quality:
        lines.extend(["", "=" * 60, "PINNACLE DATA QUALITY", "=" * 60])
        lines.extend(quality[1:])

    return lines
