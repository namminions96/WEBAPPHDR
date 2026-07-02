"""
core/telegram_notifier.py — Gửi thông báo Telegram
"""
import json
import threading
import urllib.request

from core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_telegram_notify(message: str) -> None:
    def _send():
        try:
            url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
            payload = json.dumps({'chat_id': TELEGRAM_CHAT_ID, 'text': message}).encode()
            req = urllib.request.Request(
                url, data=payload, method='POST',
                headers={'Content-Type': 'application/json'},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()
