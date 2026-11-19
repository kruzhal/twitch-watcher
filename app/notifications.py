"""Pure notification formatting, callable inside a SQLite transaction."""
from html import escape


def activity_changes(previous, is_live, title, category):
    if previous is None:
        return []
    was_live, old_title, old_category = previous
    changes = []
    if not was_live and is_live:
        changes.append({"type": "started"})
    elif was_live and not is_live:
        changes.append({"type": "ended"})
    if old_title != title:
        changes.append({"type": "title", "old": old_title, "new": title})
    if old_category != category:
        changes.append({"type": "category", "old": old_category, "new": category})
    return changes


def format_activity(login, changes, is_live, title, category):
    labels = {"started": "🔴 Начался эфир", "ended": "⚫ Эфир завершился"}
    lines = [f"<b>🔔 Активность канала @{escape(login)}</b>"]
    for change in changes:
        kind = change["type"]
        if kind in labels:
            lines.append(labels[kind])
        elif kind == "title":
            lines.append(f"📝 Название: {escape(change['old'][:180])} → <b>{escape(change['new'][:180])}</b>")
        elif kind == "category":
            old = escape(change["old"][:120]) or "Без категории"
            new = escape(change["new"][:120]) or "Без категории"
            lines.append(f"🎮 Категория: {old} → <b>{new}</b>")
    if title and is_live:
        lines.append(f"\n<b>Сейчас:</b> {escape(title[:180])}")
    if category and is_live:
        lines.append(f"<b>Категория:</b> {escape(category[:120])}")
    lines.append(f'\n<a href="https://twitch.tv/{escape(login)}">Открыть канал</a>')
    return "\n".join(lines)
