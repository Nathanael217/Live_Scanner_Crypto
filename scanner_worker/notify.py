"""Telegram Bot API client. Reads creds from env vars."""
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID",   "")
TG_API_BASE  = "https://api.telegram.org"


class TelegramConfigError(RuntimeError):
    pass


def send_message(
    text: str,
    parse_mode: str | None = None,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> bool:
    """Returns True on success. Logs errors and returns False (does NOT raise)
    so a single failed message doesn't kill the whole scan."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        raise TelegramConfigError(
            "TG_BOT_TOKEN and TG_CHAT_ID must be set in environment"
        )

    url = f"{TG_API_BASE}/bot{TG_BOT_TOKEN}/sendMessage"
    payload: dict = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                return True
            elif r.status_code == 429:  # rate limited
                wait = r.json().get("parameters", {}).get("retry_after", retry_delay)
                log.warning(f"Telegram rate limit, waiting {wait}s")
                time.sleep(wait)
            else:
                log.error(f"Telegram API error {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            log.error(f"Telegram request failed (attempt {attempt}): {e}")
        time.sleep(retry_delay)

    return False


def test_credentials() -> bool:
    """getMe sanity check — returns True if bot token is valid."""
    if not TG_BOT_TOKEN:
        return False
    try:
        r = requests.get(
            f"{TG_API_BASE}/bot{TG_BOT_TOKEN}/getMe", timeout=10
        )
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception:
        return False
