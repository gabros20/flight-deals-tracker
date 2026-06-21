import os
from typing import Optional


class TelegramNotifier:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    def send_deal(self, message: str):
        if not self.token or not self.chat_id:
            print("[TelegramNotifier] Would send:", message)
            return
        # Real implementation would use requests to Telegram API
        print(f"[Telegram] Sent to {self.chat_id}: {message}")