"""Minimal Telegram Bot API client with mandatory bot-mode allowlisting."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import requests


class TelegramError(RuntimeError):
    """Telegram failure sanitized so the bot token cannot appear in logs."""


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
    ) -> None:
        if not token or ":" not in token:
            raise ValueError("TELEGRAM_BOT_TOKEN is missing or malformed")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._session = session or requests.Session()

    def _call(
        self,
        method: str,
        *,
        payload: dict[str, object] | None = None,
        timeout: float = 20.0,
    ) -> Any:
        try:
            response = self._session.post(
                f"{self._base_url}/{method}",
                json=payload or {},
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError(
                f"Telegram {method} failed; token omitted"
            ) from exc
        if not isinstance(body, dict) or not body.get("ok"):
            raise TelegramError(f"Telegram {method} returned an API error")
        return body.get("result")

    def send_message(self, chat_id: int | str, text: str) -> None:
        """Send plain text, splitting safely below Telegram's message limit."""

        remaining = text
        while remaining:
            if len(remaining) <= 3900:
                chunk, remaining = remaining, ""
            else:
                split_at = remaining.rfind("\n", 0, 3900)
                if split_at < 1:
                    split_at = 3900
                chunk, remaining = remaining[:split_at], remaining[split_at:]
                remaining = remaining.lstrip("\n")
            self._call(
                "sendMessage",
                payload={"chat_id": chat_id, "text": chunk},
            )

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 0,
    ) -> list[dict[str, Any]]:
        payload: dict[str, object] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call(
            "getUpdates",
            payload=payload,
            timeout=max(20.0, timeout_seconds + 10.0),
        )
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates returned malformed data")
        return [item for item in result if isinstance(item, dict)]


def discover_chats(client: TelegramClient) -> list[dict[str, str]]:
    """Extract unique chat IDs from pending updates after the user messages bot."""

    chats: dict[int, dict[str, str]] = {}
    for update in client.get_updates():
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
            continue
        chat_id = chat["id"]
        chats[chat_id] = {
            "chat_id": str(chat_id),
            "type": str(chat.get("type", "unknown")),
            "label": str(
                chat.get("username")
                or chat.get("title")
                or chat.get("first_name")
                or "unknown"
            ),
        }
    return list(chats.values())


def run_bot(
    client: TelegramClient,
    *,
    allowed_chat_ids: set[int],
    check_callback: Callable[[Decimal], str],
) -> None:
    """Long-poll for /check [EUR] from explicitly allowlisted chats."""

    if not allowed_chat_ids:
        raise ValueError("Bot mode requires TELEGRAM_ALLOWED_CHAT_IDS")
    # Discard commands queued before startup so a restart cannot unexpectedly
    # repeat an old financial check or top-up plan.
    pending = client.get_updates()
    pending_ids = [
        update["update_id"]
        for update in pending
        if isinstance(update.get("update_id"), int)
    ]
    offset: int | None = max(pending_ids) + 1 if pending_ids else None
    while True:
        updates = client.get_updates(offset=offset, timeout_seconds=30)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            text = message.get("text")
            if (
                not isinstance(chat, dict)
                or not isinstance(chat.get("id"), int)
                or not isinstance(text, str)
            ):
                continue
            chat_id = chat["id"]
            if chat_id not in allowed_chat_ids:
                continue

            parts = text.strip().split()
            command = parts[0].split("@", 1)[0].lower() if parts else ""
            if command in {"/start", "/help"}:
                client.send_message(
                    chat_id,
                    "Commands:\n/check\n/check 1000\n\n"
                    "The optional number is new EUR top-up capital.",
                )
                continue
            if command != "/check":
                continue
            if len(parts) > 2:
                client.send_message(chat_id, "Usage: /check [top_up_eur]")
                continue
            try:
                top_up = Decimal(parts[1]) if len(parts) == 2 else Decimal("0")
                if top_up < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                client.send_message(
                    chat_id,
                    "Top-up must be a non-negative number, e.g. /check 1000",
                )
                continue
            try:
                order_message = check_callback(top_up)
            except Exception as exc:  # sanitized domain errors are user-facing
                client.send_message(chat_id, f"Check failed: {exc}")
                continue
            client.send_message(chat_id, order_message)
