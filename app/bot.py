import csv
import io
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from html import escape
from functools import lru_cache

from .health import summarize_health
from .notifications import format_activity
from .version import VERSION

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    BufferedInputFile,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

log = logging.getLogger(__name__)


@lru_cache(maxsize=1024)
def _phrase_pattern(normalized_phrase):
    words = re.split(r"\s+", normalized_phrase)
    pattern = r"(?<!\w)" + r"\W+".join(re.escape(word) for word in words) + r"(?!\w)"
    return re.compile(pattern)


def phrase_matches(text, phrase):
    normalized_phrase = phrase.casefold().strip()
    return bool(normalized_phrase and _phrase_pattern(normalized_phrase).search(text.casefold()))


ACTIONS = {
    "addexclude": "🛡 Пришли слово или фразу-исключение (до 200 символов). Сообщения с ней не попадут в поиск.",
    "delexclude": "🗑 Пришли точную фразу-исключение для удаления.",
    "cooldown": "⏱ Пришли интервал защиты от повторов в секундах: от 0 до 86400. 0 — отключить. История сохраняет все уникальные сообщения.",
    "addword": "🔎 Пришлите слово или фразу для поиска.\nПример: gg ez\n\nЧтобы отменить, нажмите кнопку ниже.",
    "adduser": "👤 Чьи сообщения искать в чужих чатах? Пришлите Twitch-логин без @.\nПример: ninja\n\nЧтобы отменить, нажмите кнопку ниже.",
    "addchannel": "📡 Пришлите Twitch-логин канала без @.\nПример: shroud\n\nЧтобы отменить, нажмите кнопку ниже.",
    "delword": "🗑 Пришлите точное слово или фразу, которую нужно удалить.\n\nЧтобы отменить, нажмите кнопку ниже.",
    "deluser": "🗑 Пришлите Twitch-логин пользователя без @.\n\nЧтобы отменить, нажмите кнопку ниже.",
    "delchannel": "🗑 Пришлите Twitch-логин канала без @.\n\nЧтобы отменить, нажмите кнопку ниже.",
}


NAVIGATION = {
    "🏠 Главное меню": "home", "📡 Каналы": "channels", "🔴 Эфиры": "live",
    "🔎 Фразы": "words", "👤 Пользователи": "users", "🔔 Активность": "activity",
    "🧾 История": "history", "📊 Статистика": "stats", "❔ Помощь": "help",
    "🛡 Фильтры": "filters", "💬 Поддержка": "support",
}


def navigation_keyboard():
    labels = list(NAVIGATION)
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in labels[i:i + 3]]
                  for i in range(0, len(labels), 3)],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Выберите раздел или введите значение",
    )


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Каналы", callback_data="menu:channels"),
         InlineKeyboardButton(text="🔴 Эфиры", callback_data="menu:live")],
        [InlineKeyboardButton(text="🔎 Фразы", callback_data="menu:words"),
         InlineKeyboardButton(text="👤 Пользователи", callback_data="menu:users")],
        [InlineKeyboardButton(text="🔔 Активность", callback_data="menu:activity"),
         InlineKeyboardButton(text="🧾 История", callback_data="menu:history")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
         InlineKeyboardButton(text="❔ Помощь", callback_data="menu:help")],
        [InlineKeyboardButton(text="➕ Добавить фразу", callback_data="action:addword")],
        [InlineKeyboardButton(text="🛡 Фильтры и повторы", callback_data="menu:filters")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="menu:support")],
    ])


def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")]
    ])


def cancel_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="action:cancel")]
    ])


def setup_bot(config, storage, twitch):
    session = AiohttpSession(proxy=config.telegram_proxy or None, timeout=25)
    bot = Bot(config.telegram_bot_token, session=session)
    dp = Dispatcher()
    pending_actions = {}
    history_filters = {}

    @dp.startup()
    async def clean_telegram_menu(bot: Bot):
        # Remove stale slash-command lists left by earlier versions/Bot API scopes.
        scopes = (
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeChat(chat_id=config.telegram_admin_id),
        )
        for scope in scopes:
            for language in ("", "ru"):
                try:
                    await bot.delete_my_commands(scope=scope, language_code=language)
                except Exception:
                    log.warning("Could not clear Telegram commands for scope %s", type(scope).__name__)
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        except Exception:
            log.warning("Could not reset Telegram menu button")

    def is_admin(user):
        return user is not None and user.id == config.telegram_admin_id

    async def show_home(target, *, edit=False):
        catalog_size = await storage.catalog_count()
        text = (
            f"<b>🟣 Twitch Watcher {VERSION}</b>\n"
            "Ищу фразы и сообщения выбранных пользователей только в активных чатах каналов из твоей базы.\n"
            f"Каналов в базе: <b>{catalog_size}</b>. Читаю до 100 чатов одновременно; при превышении — по рейтингу CSV.\n\n"
            "Нажмите «➕ Добавить фразу», чтобы включить поиск и получать совпадения сюда.\n"
            "«Пользователи» — чьи сообщения искать в чужих чатах. «Каналы» — чьи эфиры и изменения отслеживать.\n\n"
            "Переключайте разделы кнопками у строки ввода. 🏠 Главное меню — вернуться в начало."
        )
        if edit:
            await edit_page(target.message, text, reply_markup=main_menu(), parse_mode="HTML")
        else:
            await target.answer(text, reply_markup=main_menu(), parse_mode="HTML")

    async def edit_page(message, text, **kwargs):
        try:
            await message.edit_text(text, **kwargs)
        except TelegramBadRequest as exc:
            reason = str(exc).casefold()
            if "message is not modified" in reason:
                return
            if "message can't be edited" in reason or "message to edit not found" in reason:
                await message.answer(text, **kwargs)
                return
            raise

    async def begin_action(user_id, action, target):
        prompt = ACTIONS.get(action)
        if prompt is None:
            return False
        pending_actions[user_id] = action
        await target.answer(prompt, reply_markup=cancel_menu())
        return True

    @dp.message(Command("start", "menu"))
    async def start(message: Message):
        if is_admin(message.from_user):
            pending_actions.pop(message.from_user.id, None)
            await message.answer("Разделы всегда под рукой — используйте кнопки у строки ввода.", reply_markup=navigation_keyboard())
            await show_home(message)
        else:
            await message.answer("Управление ботом доступно только владельцу.")

    @dp.callback_query(F.data.startswith("action:"))
    async def action_callback(callback: CallbackQuery):
        if not is_admin(callback.from_user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        action = callback.data.split(":", 1)[1]
        if action != "cancel" and action not in ACTIONS:
            await callback.answer("Эта кнопка устарела. Откройте главное меню через /start.", show_alert=True)
            return
        await callback.answer()
        if action == "cancel":
            pending_actions.pop(callback.from_user.id, None)
            await edit_page(callback.message, "Действие отменено. Выберите следующий раздел:", reply_markup=main_menu())
            return
        await begin_action(callback.from_user.id, action, callback.message)

    @dp.callback_query(F.data.startswith("menu:"))
    async def menu_callback(callback: CallbackQuery):
        if not is_admin(callback.from_user):
            await callback.answer("Нет доступа", show_alert=True)
            return

        section = callback.data.split(":", 1)[1]
        if section not in {"home", "channels", "words", "users", "history", "stats", "help", "live", "activity", "support", "filters"}:
            await callback.answer("Эта кнопка устарела. Отправьте /start.", show_alert=True)
            return
        await callback.answer()
        pending_actions.pop(callback.from_user.id, None)
        await render_section(callback.message, section)

    async def render_section(message, section):
        if section == "home":
            pending_actions.pop(message.chat.id, None)
            await show_home(message)
        elif section == "channels":
            rows = await storage.channels()
            selected = [login for login, _, source in rows if source == "manual"]
            selected_body = "\n".join(f"• @{escape(login)}" for login in selected[:30]) or "Пока не выбраны."
            if len(selected) > 30:
                selected_body += f"\n…и ещё {len(selected) - 30}"
            text = (
                "<b>📡 Каналы</b>\n\n"
                "Твои выбранные каналы — по ним приходят уведомления об активности:\n"
                f"{selected_body}"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить канал", callback_data="action:addchannel"),
                 InlineKeyboardButton(text="🗑 Удалить канал", callback_data="action:delchannel")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
            ])
            await edit_page(message, text, reply_markup=keyboard, parse_mode="HTML")
        elif section == "live":
            try:
                selected = [row for row in await storage.channels() if row[2] == "manual"]
                live_by_id = await twitch._streams_for_channels([row[1] for row in selected]) if selected else {}
                streams = sorted(live_by_id.values(), key=lambda stream: int(stream.get("viewer_count") or 0), reverse=True)
                if streams:
                    entries = []
                    for stream in streams[:15]:
                        login = escape(stream.get("user_login", "unknown"))
                        title = escape((stream.get("title") or "Без названия").replace("\n", " ")[:65])
                        game = escape((stream.get("game_name") or "Категория не указана")[:40])
                        viewers = int(stream.get("viewer_count") or 0)
                        entries.append(f'🔴 <a href="https://twitch.tv/{login}">@{login}</a> · {viewers} зр.\n   {game} — {title}')
                    body = "\n\n".join(entries)
                    if len(streams) > len(entries):
                        body += f"\n\nПоказаны первые {len(entries)} из {len(streams)} найденных."
                    text = f"<b>🔴 Выбранные каналы в эфире</b> · {len(streams)} из {len(selected)}\nОбновление: по кнопке «Обновить».\n\n{body}"
                else:
                    text = ("<b>🔴 Выбранные каналы в эфире</b>\n\nСейчас ни один из выбранных тобой каналов не в эфире."
                            if selected else "<b>🔴 Выбранные каналы в эфире</b>\n\nТы пока не выбрал каналы. Добавь их в разделе «Каналы».")
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:live")],
                    [InlineKeyboardButton(text="📡 Мои каналы", callback_data="menu:channels")],
                    [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
                ])
                await edit_page(message, text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                log.exception("Could not load live streams")
                await edit_page(message, "Не удалось получить список эфиров от Twitch. Попробуйте обновить через минуту.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Повторить", callback_data="menu:live")],
                    [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
                ]))
        elif section == "activity":
            rows = await storage.channel_activity(12)
            if rows:
                lines = []
                for login, is_live, title, category, checked_at in rows:
                    status = "🔴 В эфире" if is_live else "⚫ Не в эфире"
                    details = " · ".join(item for item in (category, title) if item)
                    lines.append(f"{status} · <a href=\"https://twitch.tv/{escape(login)}\">@{escape(login)}</a>" + (f"\n   {escape(details[:160])}" if details else ""))
                body = "\n".join(lines)
                if len(rows) == 12:
                    body += "\n\nПоказаны первые 12 каналов."
            else:
                body = "Добавь каналы в разделе «Каналы». Уведомления будут приходить только по выбранным тобой каналам."
            text = (
                "<b>🔔 Активность выбранных каналов</b>\n\n"
                "Уведомляю только по каналам, которые ты добавил вручную: начало и завершение эфира, смена названия или категории. Проверка примерно раз в минуту.\n\n"
                + body
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📡 Выбрать каналы", callback_data="menu:channels")],
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu:activity")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
            ])
            await edit_page(message, text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
        elif section in {"words", "users"}:
            is_words = section == "words"
            items = await storage.rules("watched_words" if is_words else "watched_users")
            title = "🔎 Ключевые слова" if is_words else "👤 Отслеживаемые пользователи"
            body = "\n".join(("• " if is_words else "• @") + escape(item[:100]) for item in items[:20]) or "Список пока пуст."
            if len(items) > 20:
                body += f"\n…и ещё {len(items) - 20}. Для удаления пришли точное значение через кнопку удаления."
            add_action, del_action = ("addword", "delword") if is_words else ("adduser", "deluser")
            add_label, del_label = ("➕ Добавить слово", "🗑 Удалить слово") if is_words else ("➕ Добавить пользователя", "🗑 Удалить пользователя")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=add_label, callback_data=f"action:{add_action}"),
                 InlineKeyboardButton(text=del_label, callback_data=f"action:{del_action}")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
            ])
            explanation = ("Поиск отдельных слов и фраз в подключённых активных чатах из твоей базы (до 100)."
                           if is_words else "Уведомляю, когда эти пользователи пишут в чужих подключённых чатах из твоей базы. Наличие ключевой фразы не требуется.")
            await edit_page(message, f"<b>{title}</b>\n\n{explanation}\n\n{body}", reply_markup=keyboard, parse_mode="HTML")
        elif section == "filters":
            excluded = await storage.rules("excluded_words")
            cooldown = await storage.repeat_cooldown()
            body = "\n".join("• " + escape(phrase[:100]) for phrase in excluded[:20]) or "Исключений нет."
            text = (
                f"<b>🛡 Фильтры поиска</b>\n\nИсключения применяются к фразам и выбранным авторам, но не к уведомлениям об эфирах.\n\n{body}\n\n"
                f"Повтор того же текста от того же автора в том же канале по тому же правилу: не чаще раза в {cooldown} с. "
                "При 0 защита отключена. История сохраняет все уникальные сообщения."
            )
            await edit_page(message, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Исключение", callback_data="action:addexclude"), InlineKeyboardButton(text="🗑 Удалить", callback_data="action:delexclude")],
                [InlineKeyboardButton(text="⏱ Интервал повторов", callback_data="action:cooldown")],
                [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
            ]))
        elif section == "history":
            await show_history(message)
        elif section == "stats":
            count = await storage.match_count()
            channel_rows = await storage.channels()
            selected_channels = sum(1 for row in channel_rows if row[2] == "manual")
            words = len(await storage.rules("watched_words"))
            users = len(await storage.rules("watched_users"))
            catalog_size = await storage.catalog_count()
            health = summarize_health(await storage.runtime_rows(), pid=os.getpid())
            queue_size = await storage.queue_count()
            states = {"healthy": "Работает", "waiting": "Нет активных выбранных чатов", "degraded": "Есть проблема", "stale": "Нет свежей диагностики", "stopped": "Остановлен"}
            text = (
                f"<b>📊 Twitch Watcher {VERSION}</b>\n{states[health['state']]}\n\n"
                f"🗂 Каналов в базе поиска: <b>{catalog_size}</b>\n"
                f"🔴 В эфире из базы при последней проверке: <b>{twitch.catalog_live_count}</b>\n"
                f"💬 Подтверждено чатов: <b>{health['active_chats']}</b> / 100\n"
                f"📨 Очередь уведомлений: <b>{queue_size}</b>\n"
                f"🌐 Twitch: {'онлайн' if health['twitch_online'] else 'ожидание/нет связи'} · Telegram API: {'онлайн' if health['telegram_online'] else 'нет свежего подтверждения'}\n"
                f"🔔 Выбрано для уведомлений об активности: <b>{selected_channels}</b>\n"
                f"🔎 Ключевых слов: <b>{words}</b>\n"
                f"👤 Пользователей: <b>{users}</b>\n"
                f"🎯 Сохранено совпадений: <b>{count}</b>"
            )
            await edit_page(message, text, reply_markup=back_menu(), parse_mode="HTML")
        elif section == "support":
            await edit_page(
                message,
                "<b>💬 Поддержка</b>\n\nЕсть вопрос или что-то не работает?\nНапишите @egorkruzhalov — опишите, что произошло.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✉️ Написать в поддержку", url="https://t.me/egorkruzhalov")],
                    [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
                ]),
            )
        elif section == "help":
            text = (
                "<b>❔ Помощь</b>\n\n"
                "«Фразы»: ищу отдельные слова и фразы в активных чатах из импортированной базы каналов. «Пользователи»: присылаю сообщения выбранных аккаунтов в чужих подключённых чатах, даже без ключевых слов.\n\n"
                "В разделе «Каналы» добавь каналы вручную. Только по ним приходят уведомления об активности: старт и окончание эфира, смена названия или категории.\n\n"
                "Бот читает до 100 активных чатов из базы одновременно. Если их больше — выбирает первые по рейтингу CSV. Каналы вне базы не подключаются. /start возвращает в главное меню."
                "\n\n«Фильтры»: исключения и интервал повторов. Каталог каналов, CSV и приоритеты управляются через веб-панель."
            )
            await edit_page(message, text, reply_markup=back_menu(), parse_mode="HTML")

    def history_state(message):
        return history_filters.setdefault(message.chat.id, {})

    async def show_history(message, page=0):
        filters = history_state(message)
        query = {key: value for key, value in filters.items() if key in {"channel", "phrase", "since", "until"}}
        rows, total, page = await storage.history_page(**query, page=page)
        pages = max(1, (total + 4) // 5)
        labels = [f"Канал: @{escape(filters['channel'])}" for _ in [0] if filters.get('channel')]
        if filters.get("phrase"):
            labels.append("Фраза: " + escape(filters["phrase"]))
        labels.append("Период: " + escape(filters.get("period", "за всё время")))
        parts = []
        for created, channel, author, text_value, rule in rows:
            try:
                stamp = datetime.fromisoformat(created).astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")
            except ValueError:
                stamp = created[:19]
            parts.append(f"🕒 {escape(stamp)} МСК\n<b>@{escape(author[:60])}</b> · "
                         f'<a href="https://twitch.tv/{escape(channel)}">@{escape(channel[:60])}</a>\n'
                         f"{escape(rule[:140])}\n<i>{escape(text_value[:300])}</i>")
        text = (f"<b>🧾 История совпадений</b>\nНайдено: {total} · Страница {page + 1}/{pages}\n"
                + " · ".join(labels) + "\n\n" + ("\n\n".join(parts) or "По этим условиям совпадений нет. Попробуйте сбросить фильтры."))
        buttons = []
        nav = []
        if page:
            nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"hist:page:{page-1}"))
        if (page + 1) * 5 < total:
            nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"hist:page:{page+1}"))
        if nav:
            buttons.append(nav)
        buttons += [
            [InlineKeyboardButton(text="🔎 Фраза", callback_data="hist:filter:phrase"), InlineKeyboardButton(text="📡 Канал", callback_data="hist:filter:channel")],
            [InlineKeyboardButton(text="📅 Период", callback_data="hist:period"), InlineKeyboardButton(text="↩️ Сбросить", callback_data="hist:reset")],
            [InlineKeyboardButton(text="📥 Скачать CSV", callback_data="hist:export"), InlineKeyboardButton(text="🔄 Обновить", callback_data="hist:page:0")],
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="menu:home")],
        ]
        await edit_page(message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML", disable_web_page_preview=True)

    @dp.callback_query(F.data.startswith("hist:"))
    async def history_callback(callback: CallbackQuery):
        if not is_admin(callback.from_user):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        pending_actions.pop(callback.from_user.id, None)
        parts = callback.data.split(":")
        action = parts[1]
        filters = history_state(callback.message)
        if action == "filter" and parts[2] in {"phrase", "channel", "date"}:
            kind = parts[2]
            pending_actions[callback.from_user.id] = "history:" + kind
            prompt = {"phrase": "Пришлите фразу из причины совпадения (можно часть).", "channel": "Пришлите Twitch-логин канала без @.", "date": "Пришлите дату в формате ДД.ММ.ГГГГ. День считается по московскому времени."}[kind]
            await callback.message.answer(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="hist:page:0")]]))
            return
        if action == "period":
            await edit_page(callback.message, "📅 За какой период показать совпадения? Время — московское.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Сегодня", callback_data="hist:days:today"), InlineKeyboardButton(text="7 дней", callback_data="hist:days:7")],
                [InlineKeyboardButton(text="30 дней", callback_data="hist:days:30"), InlineKeyboardButton(text="Всё время", callback_data="hist:days:all")],
                [InlineKeyboardButton(text="Указать дату", callback_data="hist:filter:date")],
                [InlineKeyboardButton(text="⬅️ История", callback_data="hist:page:0")],
            ]))
            return
        if action == "days":
            value = parts[2]
            if value not in {"today", "7", "30", "all"}:
                return
            filters.pop("since", None); filters.pop("until", None)
            filters["period"] = {"today": "сегодня", "7": "последние 7 дней", "30": "последние 30 дней", "all": "за всё время"}[value]
            now = datetime.now(timezone(timedelta(hours=3)))
            if value != "all":
                start = now.replace(hour=0, minute=0, second=0, microsecond=0) if value == "today" else now - timedelta(days=int(value))
                filters["since"] = start.astimezone(timezone.utc).isoformat()
        elif action == "reset":
            filters.clear()
        elif action == "export":
            query = {key: value for key, value in filters.items() if key in {"channel", "phrase", "since", "until"}}
            rows, total, _ = await storage.history_page(**query, limit=5000)
            if not rows:
                await callback.message.answer("По выбранным условиям нечего выгружать.")
                return
            output = io.StringIO(newline="")
            writer = csv.writer(output, delimiter=";")
            writer.writerow(["Дата (UTC)", "Канал", "Автор", "Сообщение", "Причина совпадения"])
            for row in rows:
                # Spreadsheet applications must treat chat text as text, never formulas.
                writer.writerow([("'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value) for value in row])
            caption = f"История по текущим фильтрам: {len(rows)} из {total} записей."
            if total > 5000:
                caption += " Выгружены последние 5000. Уточните период для остальных."
            await callback.message.answer_document(BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="twitch_history.csv"), caption=caption)
            return
        page = int(parts[2]) if action == "page" and len(parts) == 3 and parts[2].isdigit() else 0
        await show_history(callback.message, page)

    @dp.message(F.text & ~F.text.startswith("/"))
    async def handle_plain_text(message: Message):
        if not is_admin(message.from_user):
            return
        section = NAVIGATION.get(message.text)
        if section is not None:
            pending_actions.pop(message.from_user.id, None)
            if section == "home":
                await show_home(message)
            else:
                page = await message.answer("Открываю раздел…", reply_markup=navigation_keyboard())
                await render_section(page, section)
            return
        action = pending_actions.pop(message.from_user.id, None)
        if action is None:
            await message.answer(
                "Сначала выберите действие в меню. Отправьте /start, чтобы открыть главное меню.",
                reply_markup=main_menu(),
            )
            return
        value = (message.text or "").strip()
        if not value:
            pending_actions[message.from_user.id] = action
            await message.answer("Пустое значение. Пришлите текст или нажмите «Отмена».", reply_markup=cancel_menu())
            return

        if action.startswith("history:"):
            kind = action.split(":", 1)[1]
            filters = history_state(message)
            if kind == "date":
                try:
                    day = datetime.strptime(value, "%d.%m.%Y").replace(tzinfo=timezone(timedelta(hours=3)))
                except ValueError:
                    pending_actions[message.from_user.id] = action
                    await message.answer("Нужна существующая дата в формате ДД.ММ.ГГГГ, например 02.11.2026. Повторите ввод или выберите другой раздел.")
                    return
                filters.update(since=day.astimezone(timezone.utc).isoformat(), until=(day + timedelta(days=1)).astimezone(timezone.utc).isoformat(), period=value)
            elif kind == "channel":
                login = value.lstrip("@").casefold()
                if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
                    pending_actions[message.from_user.id] = action
                    await message.answer("Пришлите логин канала: латинские буквы, цифры и подчёркивание.")
                    return
                filters["channel"] = login
            else:
                if len(value) > 100:
                    pending_actions[message.from_user.id] = action
                    await message.answer("Для поиска достаточно фразы до 100 символов.")
                    return
                filters["phrase"] = value.casefold()
            page = await message.answer("Ищу в истории…", reply_markup=navigation_keyboard())
            await show_history(page)
            return

        try:
            if action in {"addexclude", "delexclude"}:
                if len(value) > 200:
                    raise ValueError("Исключение должно быть не длиннее 200 символов")
                operation = storage.add_rule if action == "addexclude" else storage.del_rule
                await operation("excluded_words", value)
                answer = "✅ Фильтр исключений обновлён."
            elif action == "cooldown":
                if not value.isdecimal() or len(value) > 5:
                    raise ValueError("Пришли целое число от 0 до 86400")
                await storage.set_repeat_cooldown(int(value))
                answer = f"✅ Интервал повторов: {int(value)} с."
            elif action in {"addword", "delword"}:
                operation = storage.add_rule if action == "addword" else storage.del_rule
                await operation("watched_words", value)
                verb = "✅ Буду отслеживать фразу" if action == "addword" else "🗑 Удалил фразу"
                answer = f"{verb}: <b>{escape(value)}</b>"
            elif action in {"adduser", "deluser"}:
                login = value.lstrip("@").casefold()
                operation = storage.add_rule if action == "adduser" else storage.del_rule
                await operation("watched_users", login)
                verb = "✅ Теперь отслеживаю пользователя" if action == "adduser" else "🗑 Удалил пользователя"
                answer = f"{verb} <b>@{escape(login)}</b>"
            elif action == "addchannel":
                user = await twitch.resolve_user(value.lstrip("@").casefold())
                selected = [row for row in await storage.channels() if row[2] == "manual"]
                if len(selected) >= 100 and not any(row[1] == str(user["id"]) for row in selected):
                    raise ValueError("Достигнут лимит Twitch: можно выбрать не более 100 каналов.")
                await storage.add_channel(user["login"], user["id"])
                answer = f"✅ Канал <b>@{escape(user['login'])}</b> добавлен."
            else:
                login = value.lstrip("@").casefold()
                await storage.del_channel(login)
                answer = f"🗑 Канал <b>@{escape(login)}</b> удалён из списка."
            await message.answer(answer + "\n\nВыберите следующий раздел:", parse_mode="HTML", reply_markup=main_menu())
        except Exception as exc:
            log.exception("Plain-text action failed")
            await message.answer(
                f"Не получилось выполнить действие: {escape(str(exc)[:300])}",
                parse_mode="HTML", reply_markup=main_menu(),
            )

    @dp.message(Command("addchannel"))
    async def addchannel(message: Message):
        if not is_admin(message.from_user):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Формат: <code>/addchannel twitch_login</code>", parse_mode="HTML")
            return
        try:
            user = await twitch.resolve_user(parts[1].strip().lstrip("@").casefold())
            await storage.add_channel(user["login"], user["id"])
            await message.answer(
                f"✅ Канал <b>@{escape(user['login'])}</b> добавлен.",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.exception("Could not add channel")
            await message.answer(f"Не удалось добавить канал: {escape(str(exc)[:500])}", parse_mode="HTML")

    @dp.message(Command("delchannel"))
    async def delchannel(message: Message):
        if not is_admin(message.from_user):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Формат: <code>/delchannel twitch_login</code>", parse_mode="HTML")
            return
        login = parts[1].strip().lstrip("@").casefold()
        await storage.del_channel(login)
        await message.answer(f"🗑 Канал @{escape(login)} удалён из локального списка.")

    async def rule_cmd(message, table, add):
        if not is_admin(message.from_user):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await message.answer("Укажи никнейм или фразу после команды.")
            return
        value = parts[1].strip()
        operation = storage.add_rule if add else storage.del_rule
        await operation(table, value)
        verb = "✅ Добавлено" if add else "🗑 Удалено"
        await message.answer(f"{verb}: <b>{escape(value)}</b>", parse_mode="HTML")

    @dp.message(Command("adduser"))
    async def adduser(message: Message):
        await rule_cmd(message, "watched_users", True)

    @dp.message(Command("deluser"))
    async def deluser(message: Message):
        await rule_cmd(message, "watched_users", False)

    @dp.message(Command("addword"))
    async def addword(message: Message):
        await rule_cmd(message, "watched_words", True)

    @dp.message(Command("delword"))
    async def delword(message: Message):
        await rule_cmd(message, "watched_words", False)

    @dp.message(Command("channels"))
    async def channels(message: Message):
        if not is_admin(message.from_user):
            return
        rows = await storage.channels()
        body = "\n".join(f"• @{escape(login)}" for login, *_ in rows) or "Пока нет каналов."
        await message.answer("<b>📡 Каналы</b>\n\n" + body, parse_mode="HTML")

    @dp.message(Command("users"))
    async def users(message: Message):
        if not is_admin(message.from_user):
            return
        items = await storage.rules("watched_users")
        body = "\n".join("• @" + escape(item) for item in items) or "Список пуст."
        await message.answer("<b>👤 Пользователи</b>\n\n" + body, parse_mode="HTML")

    @dp.message(Command("words"))
    async def words(message: Message):
        if not is_admin(message.from_user):
            return
        items = await storage.rules("watched_words")
        body = "\n".join("• " + escape(item) for item in items) or "Список пуст."
        await message.answer("<b>🔎 Ключевые слова</b>\n\n" + body, parse_mode="HTML")

    @dp.message(Command("stats"))
    async def stats(message: Message):
        if not is_admin(message.from_user):
            return
        await message.answer(f"📊 Сохранено совпадений: <b>{await storage.match_count()}</b>", parse_mode="HTML")

    @dp.message(Command("history"))
    async def history(message: Message):
        if not is_admin(message.from_user):
            return
        pending_actions.pop(message.from_user.id, None)
        page = await message.answer("Открываю историю…", reply_markup=navigation_keyboard())
        await show_history(page)

    async def notify_match(event):
        channel = event.get("broadcaster_user_login", "unknown")
        author = event.get("chatter_user_login", "unknown")
        text_value = event.get("message", {}).get("text", "")
        if not text_value:
            return

        if any(phrase_matches(text_value, phrase) for phrase in await storage.rules("excluded_words")):
            return

        user_rules = set(await storage.rules("watched_users"))
        word_rules = await storage.rules("watched_words")
        author_key = author.casefold()
        hits = []
        if author_key in user_rules and author_key != channel.casefold():
            hits.append(f"пользователь @{author}")
        for phrase in word_rules:
            if phrase_matches(text_value, phrase):
                hits.append(f"фраза «{phrase}»")
        if not hits:
            return

        created = datetime.now(timezone.utc).isoformat()
        message_id = event.get("message_id") or event.get("message", {}).get("message_id")
        for hit in hits:
            msg = (
                "<b>🔔 TWITCH WATCHER — НАЙДЕНО СОВПАДЕНИЕ</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>Причина:</b> {escape(hit)}\n"
                f"📡 <b>Канал:</b> <a href=\"https://twitch.tv/{escape(channel)}\">@{escape(channel)}</a>\n"
                f"👤 <b>Автор:</b> @{escape(author)}\n\n"
                f"💬 <b>Сообщение:</b>\n{escape(text_value[:1200])}"
            )
            await storage.save_match(created, channel, author, text_value, hit, message_id, notification=msg)

    async def notify_activity(login, changes, is_live, title, category):
        await storage.enqueue_notification(format_activity(login, changes, is_live, title, category))

    return bot, dp, notify_match, notify_activity
