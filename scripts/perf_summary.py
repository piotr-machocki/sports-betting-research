"""Shared formatting for the run's performance summary."""

from __future__ import annotations


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


def performance_lines(
    perf: dict | None,
    started_at=None,
    pinnacle_data_age_seconds=None,
) -> list[str]:
    """Plain-text summary (no ANSI), safe for terminal and TXT report."""
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

    lines.append("=" * 60)
    return lines
