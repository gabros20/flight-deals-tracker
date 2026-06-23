import os
import requests
from typing import Optional
from flight_deals.config import get_config, FlightDealsConfig
from flight_deals.formatters import format_results


class TelegramNotifier:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, config: Optional[FlightDealsConfig] = None):
        cfg = config or get_config()
        self.token = token or cfg.telegram_bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or cfg.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)

    def send_deal(self, message: str, parse_mode: str = "Markdown") -> bool:
        if not self.enabled:
            print(f"[TelegramNotifier] (dry-run) Would send: {message}")
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                print(f"[Telegram] Message sent successfully to {self.chat_id}")
                return True
            else:
                print(f"[Telegram] Failed to send: {resp.text}")
                return False
        except Exception as e:
            print(f"[Telegram] Error sending message: {e}")
            return False


    def send_deals(self, deals: list, title: str = "Flight Deals") -> bool:
        """Send a list of deals using the enforced emoji + link format"""
        formatted = format_results(deals, title)
        return self.send_deal(formatted)

    def send_price_alert(self, origin: str, destination: str, date: str, price: float, currency: str, change_pct: float, booking_link: str = ""):
        """Convenience method for price drop alerts"""
        msg = (
            f"✈️ *PRICE ALERT*\n"
            f"{origin} → {destination} on {date}\n"
            f"New price: *{price} {currency}*\n"
            f"Change: {change_pct:+.1f}%\n"
        )
        if booking_link:
            msg += f"[Book now]({booking_link})"
        return self.send_deal(msg)