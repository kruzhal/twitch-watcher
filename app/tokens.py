"""Persist rotated OAuth credentials in the mounted data directory."""
import json
import os
import tempfile
from pathlib import Path


def load_tokens(path):
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(values, dict) or any(not isinstance(values.get(k), str) or not values[k] for k in ("access_token", "refresh_token")):
        raise RuntimeError("Повреждено хранилище Twitch-токенов; повтори OAuth-авторизацию")
    return values


def save_tokens(path, access_token, refresh_token):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".twitch-tokens-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"access_token": access_token, "refresh_token": refresh_token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
