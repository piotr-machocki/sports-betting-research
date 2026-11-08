"""Telegram message for each new positive-EV bet in a session.

run_once.py calls run_step() after the session is saved. It reads the session's
rows the way the monitor and dashboard do (both books), keeps prematch matches
whose EV reaches the minimum and that have not been sent before, and sends one
message per match. Sent bets are kept in data/notify/sent.json, so a match is
announced once per book and side, not every run. A failed send is not recorded,
so the next run tries again. No retry, and nothing here can fail a run.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env; without them it does
nothing. Uses no PinnAPI requests.

Usage:
    python scripts/notify.py --chat-id                   # print the chat ID after you message the bot
    python scripts/notify.py --test                      # send a test message
    python scripts/notify.py --session-id ID --dry-run   # print what would be sent
    python scripts/notify.py [--session-id ID] [--min-ev 0.02]   # latest session by default
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_monitor  # noqa: E402

load_dotenv(ROOT / ".env")

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_S = 10

MIN_EV = 0.02
SENT_FILE = ROOT / "data" / "notify" / "sent.json"
SENT_KEEP_DAYS = 3  # older entries are dropped; a match is long over by then
LOCAL_TZ = "Europe/Warsaw"


# ----------------------------------------------------------------- Telegram ---

class TelegramError(Exception):
    """A failed call. Its text never contains the token (it is in the URL)."""


def call(method: str, token: str, payload: dict | None = None) -> dict:
    try:
        r = requests.post(
            API.format(token=token, method=method), json=payload or {}, timeout=TIMEOUT_S
        )
    except requests.RequestException as exc:
        # The exception text includes the URL, so only its type is shown.
        raise TelegramError(type(exc).__name__) from None

    try:
        body = r.json()
    except ValueError:
        body = {}

    if r.status_code != 200 or not body.get("ok"):
        raise TelegramError(f"HTTP {r.status_code}: {body.get('description', '')}".strip())

    return body


def send(token: str, chat_id: str, text: str) -> None:
    call("sendMessage", token, {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    })


def credentials() -> tuple[str, str] | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat_id) if token and chat_id else None


# ------------------------------------------------------------------- picking ---

def number(value) -> float | None:
    return live_monitor.number(value)


def candidates(rows: list[dict], min_ev: float, now: datetime) -> list[dict]:
    """One bet per match and book whose EV reaches min_ev, best EV first.

    The worker computes Betclic EV only for prematch FOUND rows; Superbet has EV
    only for FOUND rows (VERIFY has prices but no EV). A start already passed is
    skipped, since such a row may be live by now.
    """
    bets = []

    for row in live_monitor.latest_per_match(rows):
        start = live_monitor.parse_timestamp(row.get("pinnacle_starts_at"))

        if start is not None and start <= now:
            continue

        for book, ev_prefix, price_field, net_field in (
            ("Betclic", "ev", "betclic_odds_for_pin_p{side}", "betclic_odds_for_pin_p{side}"),
            ("Superbet", "superbet_ev", "superbet_price{side}", "superbet_odds{side}"),
        ):
            evs = {side: number(row.get(f"{ev_prefix}{side}")) for side in (1, 2)}

            if book == "Betclic" and row.get("status") != "FOUND":
                continue

            if book == "Superbet" and row.get("superbet_status") != "FOUND":
                continue

            if None in evs.values():
                continue

            side = 1 if evs[1] >= evs[2] else 2

            if evs[side] < min_ev:
                continue

            probability = number(row.get(f"probability{side}"))

            bets.append({
                "book": book,
                "side": side,
                "ev": evs[side],
                "price": number(row.get(price_field.format(side=side))),
                "net": number(row.get(net_field.format(side=side))),
                "fair": 1 / probability if probability else None,
                "player": row.get(f"pinnacle_player{side}", ""),
                "player1": row.get("pinnacle_player1", ""),
                "player2": row.get("pinnacle_player2", ""),
                "tournament": row.get("pinnacle_tournament", ""),
                "starts_at": start,
            })

    return sorted(bets, key=lambda bet: -bet["ev"])


def bet_key(bet: dict) -> str:
    # Name pair + tournament text (it holds the round), like evaluate_ev.py:
    # event_id is missing in older rows.
    return "|".join((bet["player1"], bet["player2"], bet["tournament"], bet["book"], str(bet["side"])))


def local_time(moment: datetime | None) -> str:
    if moment is None:
        return "start unknown"

    try:
        from zoneinfo import ZoneInfo
        local = moment.astimezone(ZoneInfo(LOCAL_TZ))
        label = ""
    except Exception:  # no tz database (Windows without tzdata)
        local, label = moment.astimezone(timezone.utc), " UTC"

    today = datetime.now(local.tzinfo).date()
    day = "" if local.date() == today else local.strftime("%a %d %b ")
    return f"starts {day}{local:%H:%M}{label}"


def message(bet: dict) -> str:
    price = f"{bet['price']:.2f}" if bet["price"] else "?"

    if bet["book"] == "Superbet" and bet["net"]:
        price += f" ({bet['net']:.2f} after tax)"

    fair = f" · Pinnacle fair {bet['fair']:.2f}" if bet["fair"] else ""

    return (
        f"🎾 EV {bet['ev']:+.1%} · {bet['book']} {price} on {bet['player']}\n"
        f"{bet['player1']} vs {bet['player2']}\n"
        f"{bet['tournament']} · {local_time(bet['starts_at'])}{fair}"
    )


# --------------------------------------------------------------- sent memory ---

def load_sent(path: Path = SENT_FILE) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_sent(sent: dict[str, str], now: datetime, path: Path = SENT_FILE) -> None:
    cutoff = now - timedelta(days=SENT_KEEP_DAYS)
    kept = {
        key: stamp
        for key, stamp in sent.items()
        if (live_monitor.parse_timestamp(stamp) or now) >= cutoff
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(kept, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------- step ---

def latest_session_id() -> str | None:
    paths = sorted(live_monitor.SESSIONS_DIR.glob("session_*_status.json"))
    return paths[-1].name.removesuffix("_status.json") if paths else None


def notify_session(
    session_id: str,
    min_ev: float = MIN_EV,
    dry_run: bool = False,
    now: datetime | None = None,
    sent_path: Path = SENT_FILE,
) -> dict:
    now = now or datetime.now(timezone.utc)
    creds = credentials()

    if creds is None and not dry_run:
        return {"status": "no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in .env", "sent": 0}

    rows = live_monitor.read_session_rows(live_monitor.SESSIONS_DIR / f"{session_id}.csv") or []
    sent = load_sent(sent_path)
    new = [bet for bet in candidates(rows, min_ev, now) if bet_key(bet) not in sent]

    if dry_run:
        for bet in new:
            print(message(bet), end="\n\n")
        return {"status": "dry run", "sent": 0, "new": len(new)}

    token, chat_id = creds
    count, error = 0, None

    for bet in new:
        try:
            send(token, chat_id, message(bet))
        except TelegramError as exc:
            error = str(exc)
            break  # the rest go next run

        sent[bet_key(bet)] = now.isoformat()
        count += 1

    if count:
        save_sent(sent, now, sent_path)

    status = f"failed: {error}" if error else "ok"
    return {"status": status, "sent": count, "new": len(new)}


def run_step(session_id: str, min_ev: float = MIN_EV) -> None:
    """run_once.py's hook: prints one line, never raises."""
    try:
        result = notify_session(session_id, min_ev)
    except Exception as exc:  # noqa: BLE001 - a run never fails here
        print(f"WARNING: Telegram step failed: {type(exc).__name__}: {exc}", flush=True)
        return

    print(f"Telegram: {result['status']}, {result['sent']} sent", flush=True)


# ----------------------------------------------------------------------- CLI ---

def print_chat_ids() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env first.")

    updates = call("getUpdates", token).get("result", [])
    chats = {}

    for update in updates:
        # my_chat_member: the bot was added to a group or channel, even with no message since.
        event = (
            update.get("message") or update.get("channel_post")
            or update.get("my_chat_member") or {}
        )
        chat = event.get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name", "")

    if not chats:
        sys.exit("No messages yet: send your bot any message in Telegram, then run this again.")

    for chat_id, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}   ({name})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--session-id", help="Default: the latest session.")
    parser.add_argument("--min-ev", type=float, default=MIN_EV)
    parser.add_argument("--dry-run", action="store_true", help="Print, send nothing, record nothing.")
    parser.add_argument("--test", action="store_true", help="Send a test message.")
    parser.add_argument("--chat-id", action="store_true", help="Print chat IDs that messaged the bot.")
    args = parser.parse_args()

    # The Windows console's code page cannot print the emoji in --dry-run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    if args.chat_id:
        print_chat_ids()
        return

    if args.test:
        creds = credentials()
        if creds is None:
            sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        send(*creds, "🎾 Test from sports-betting-research: notifications work.")
        print("Sent.")
        return

    session_id = args.session_id or latest_session_id()

    if session_id is None:
        sys.exit("No session found.")

    print(session_id, notify_session(session_id, args.min_ev, args.dry_run))


if __name__ == "__main__":
    main()
