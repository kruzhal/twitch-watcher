"""Validate a Twitch channel CSV and import it into the existing bot database."""
import argparse
import asyncio
from pathlib import Path

from .config import Config
from .storage import Storage
from .catalog import parse_catalog_csv, MAX_CSV_BYTES


def read_catalog(path):
    with Path(path).open("rb") as source:
        raw = source.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise ValueError("CSV должен быть не больше 2 МБ")
    return parse_catalog_csv(raw.decode("utf-8-sig"))


async def import_catalog(path, database, *, replace=False):
    channels = read_catalog(path)  # Validate the entire file before touching SQLite.
    storage = Storage(database)
    await storage.init()
    if await storage.catalog_count() and not replace:
        raise ValueError("Каталог уже заполнен; для замены добавь --replace")
    await storage.replace_catalog(channels)
    return len(channels)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="CSV с колонкой login и необязательной колонкой rank")
    parser.add_argument("--database", default=Config().database_path)
    parser.add_argument("--replace", action="store_true", help="Заменить ранее импортированный каталог")
    args = parser.parse_args()
    try:
        count = asyncio.run(import_catalog(args.csv, args.database, replace=args.replace))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Импорт не выполнен: {exc}\n")
    print(f"Импортировано каналов: {count}. Меньший rank означает больший приоритет.")


if __name__ == "__main__":
    main()
