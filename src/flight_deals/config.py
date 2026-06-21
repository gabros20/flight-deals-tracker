"""
Configuration management for Flight Deals Tracker.
Supports env vars, user config file, and defaults.
"""

import json
import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class FlightDealsConfig(BaseModel):
    """Main configuration model"""
    default_origin: str = Field(default="BUD", description="Default departure airport")
    currency: str = Field(default="EUR")
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    cache_ttl_hours: int = Field(default=6, ge=0)
    max_workers: int = Field(default=8, ge=1, le=20)
    data_dir: str = Field(default="data")
    enable_cache: bool = True

    # Apify multi-source config (for connections + broader coverage)
    apify_token: Optional[str] = None
    apify_actor_id: str = Field(default="makework36/flight-price-scraper")
    apify_enabled: bool = True
    apify_cache_ttl_hours: int = Field(default=12, ge=0)

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def cache_dir(self) -> Path:
        p = self.data_path / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def history_path(self) -> Path:
        return self.data_path / "price_history.csv"

    @property
    def has_apify(self) -> bool:
        return bool(self.apify_token) and self.apify_enabled


def get_config_path() -> Path:
    """Return the user config path"""
    config_dir = Path.home() / ".config" / "flight-deals"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"


def load_config() -> FlightDealsConfig:
    """
    Load configuration with precedence:
    1. Environment variables
    2. User config file (~/.config/flight-deals/config.json)
    3. Project data/config.json
    4. Defaults
    """
    # Start with defaults
    config_data = {}

    # 1. Load from project data/config.json if exists
    project_config = Path("data/config.json")
    if project_config.exists():
        try:
            config_data.update(json.loads(project_config.read_text()))
        except Exception:
            pass

    # 2. Load from user config
    user_config_path = get_config_path()
    if user_config_path.exists():
        try:
            config_data.update(json.loads(user_config_path.read_text()))
        except Exception:
            pass

    # 3. Override with environment variables
    env_mapping = {
        "FLIGHT_DEALS_DEFAULT_ORIGIN": "default_origin",
        "FLIGHT_DEALS_CURRENCY": "currency",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "FLIGHT_DEALS_CACHE_TTL_HOURS": "cache_ttl_hours",
        "FLIGHT_DEALS_MAX_WORKERS": "max_workers",
        "FLIGHT_DEALS_DATA_DIR": "data_dir",
        "FLIGHT_DEALS_ENABLE_CACHE": "enable_cache",
        # Apify
        "APIFY_TOKEN": "apify_token",
        "APIFY_ACTOR_ID": "apify_actor_id",
        "FLIGHT_DEALS_APIFY_ENABLED": "apify_enabled",
        "FLIGHT_DEALS_APIFY_CACHE_TTL_HOURS": "apify_cache_ttl_hours",
    }

    for env_var, field in env_mapping.items():
        if env_var in os.environ:
            val = os.environ[env_var]
            if field in ("cache_ttl_hours", "max_workers", "apify_cache_ttl_hours"):
                try:
                    config_data[field] = int(val)
                except ValueError:
                    pass
            elif field == "enable_cache" or field == "apify_enabled":
                config_data[field] = val.lower() in ("true", "1", "yes")
            else:
                config_data[field] = val

    return FlightDealsConfig(**config_data)


def save_user_config(config: FlightDealsConfig) -> None:
    """Save config to user config file (excludes secrets in some cases)"""
    path = get_config_path()
    # Only save non-secret fields or let user manage token via env
    data = config.model_dump(exclude={"apify_token", "telegram_bot_token"})
    path.write_text(json.dumps(data, indent=2))


def get_config() -> FlightDealsConfig:
    return load_config()