"""Runs on a schedule and says something when a box goes quiet.

Deliberately narrow: this alerts on **silence and recovery only**. A wedged scanner, a stale
price, a filling disk — MT already notices those itself and messages you directly, and two
systems shouting about the same thing is how people start ignoring alerts. The one thing MT
cannot tell you is that MT is gone, so that is the only thing this says.

Run it as a separate Railway service in the same project, with a cron schedule:

    startCommand:  python -m watch.cron
    schedule:      */10 * * * *
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import httpx

from watch import db

SILENT_AFTER_S = float(os.environ.get("SILENT_AFTER_S", 1800))


def tell(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(f"[no telegram configured] {text}")
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        # Never fail the run over a failed message: the next tick will try again, and an
        # exception here would look like the checker itself being broken.
        print(f"telegram failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    db.init()
    hosts = db.overview()
    if not hosts:
        print("no hosts have ever reported; nothing to check")
        return 0

    now = datetime.now(timezone.utc)
    for h in hosts:
        host = h["host"]
        quiet_s = (now - h["received_at"]).total_seconds()
        silent = quiet_s > SILENT_AFTER_S
        last_silent = db.last_alert(host, "silent")
        last_recovered = db.last_alert(host, "recovered")

        if silent:
            # One message per episode of silence, not one per tick. A new episode is one
            # where the box has reported since the last time we complained about it.
            already_said = last_silent is not None and last_silent > h["received_at"]
            if already_said:
                print(f"{host}: still silent ({quiet_s / 60:.0f} min), already alerted")
                continue
            tell(
                f"🔴 <b>{host}</b> has gone quiet.\n"
                f"Last report {quiet_s / 60:.0f} minutes ago. The machine itself may be down — "
                f"MT cannot tell you this, which is why something outside it is asking."
            )
            db.note_alert(host, "silent")
            print(f"{host}: SILENT for {quiet_s / 60:.0f} min — alerted")
            continue

        # Reporting again. Say so once, if the last thing we said was that it was gone.
        if last_silent is not None and (last_recovered is None or last_recovered < last_silent):
            tell(f"🟢 <b>{host}</b> is reporting again. Last heard {quiet_s / 60:.0f} minutes ago.")
            db.note_alert(host, "recovered")
            print(f"{host}: recovered — alerted")
        else:
            print(f"{host}: fine, last report {quiet_s / 60:.0f} min ago")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
