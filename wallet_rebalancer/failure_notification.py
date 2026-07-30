"""Telegram alert for a failed scheduled daily portfolio run."""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from .telegram import TelegramClient


TRACKING_FAILURE_MESSAGE = """⚠️ Greetings cryptopian.

The scheduled daily performance and portfolio balance check failed.

Please investigate the logs:
journalctl --user -u hwr-tracking.service"""


def main() -> int:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print(
            "ERROR: Tracking failure alert could not be sent because Telegram "
            "is not configured.",
            file=sys.stderr,
        )
        return 2

    try:
        TelegramClient(token).send_message(
            chat_id,
            TRACKING_FAILURE_MESSAGE,
        )
    except Exception:
        print(
            "ERROR: Tracking failure alert could not be delivered; "
            "Telegram credentials omitted.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
