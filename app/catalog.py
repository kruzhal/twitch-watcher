"""CSV parsing and validation without bot/web dependencies."""
import csv
import io
import re

MAX_CSV_BYTES = 2_000_000
MAX_CHANNELS = 20_000


def normalize_login(value):
    if not isinstance(value, str):
        raise ValueError("Нужен Twitch-логин")
    value = value.strip().lstrip("@").casefold()
    if not re.fullmatch(r"[a-z0-9_]{1,25}", value):
        raise ValueError("Логин: латинские буквы, цифры и подчёркивание, до 25 символов")
    return value


def validate_priority(value):
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValueError("Приоритет должен быть целым числом от 1 до 2147483647")
    return value


def parse_catalog_csv(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("CSV должен быть не больше 2 МБ")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    if not reader.fieldnames or "login" not in reader.fieldnames:
        raise ValueError("В CSV нужна колонка login")
    channels = {}
    try:
        for position, row in enumerate(reader, start=1):
            if position > MAX_CHANNELS:
                raise ValueError("В CSV допускается не более 20000 строк каналов")
            try:
                login = normalize_login(row.get("login") or "")
                raw = (row.get("rank") or "").strip()
                priority = validate_priority(int(raw) if raw else position)
            except (ValueError, AttributeError) as exc:
                raise ValueError(f"Некорректный login или rank в строке {position + 1}") from exc
            channels[login] = min(priority, channels.get(login, priority))
    except csv.Error as exc:
        raise ValueError("Некорректный формат CSV") from exc
    if not channels:
        raise ValueError("CSV не содержит каналов")
    return sorted(channels.items(), key=lambda row: (row[1], row[0]))
