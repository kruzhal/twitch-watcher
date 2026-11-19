import os
from dataclasses import dataclass

from dotenv import load_dotenv
from pathlib import Path
from .tokens import load_tokens

load_dotenv()


@dataclass
class Config:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_admin_id: int = int(os.getenv("TELEGRAM_ADMIN_ID", "0") or 0)
    telegram_proxy: str = os.getenv("TELEGRAM_PROXY", "").strip()
    twitch_client_id: str = os.getenv("TWITCH_CLIENT_ID", "")
    twitch_client_secret: str = os.getenv("TWITCH_CLIENT_SECRET", "")
    twitch_user_access_token: str = os.getenv("TWITCH_USER_ACCESS_TOKEN", "")
    twitch_refresh_token: str = os.getenv("TWITCH_REFRESH_TOKEN", "")
    database_path: str = os.getenv("DATABASE_PATH", "./data/watcher.sqlite3")
    monitor_max_channels: int = int(os.getenv("MONITOR_MAX_CHANNELS", "100"))
    monitor_refresh_seconds: int = int(os.getenv("MONITOR_REFRESH_SECONDS", "60"))

    twitch_token_path: str = os.getenv("TWITCH_TOKEN_PATH", "")

    def __post_init__(self):
        if not self.twitch_token_path:
            self.twitch_token_path = str(Path(self.database_path).parent / "twitch-tokens.json")
        tokens = load_tokens(self.twitch_token_path)
        if tokens:
            self.twitch_user_access_token = tokens["access_token"]
            self.twitch_refresh_token = tokens["refresh_token"]

    def validate(self):
        required = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_ADMIN_ID": str(self.telegram_admin_id) if self.telegram_admin_id else "",
            "TWITCH_CLIENT_ID": self.twitch_client_id,
            "TWITCH_CLIENT_SECRET": self.twitch_client_secret,
            "TWITCH_USER_ACCESS_TOKEN": self.twitch_user_access_token,
            "TWITCH_REFRESH_TOKEN": self.twitch_refresh_token,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Не заполнены настройки: " + ", ".join(missing))
        if not 1 <= self.monitor_max_channels <= 100:
            raise RuntimeError("MONITOR_MAX_CHANNELS должен быть от 1 до 100.")
        if self.monitor_refresh_seconds < 30:
            raise RuntimeError("MONITOR_REFRESH_SECONDS должен быть не меньше 30 секунд.")
