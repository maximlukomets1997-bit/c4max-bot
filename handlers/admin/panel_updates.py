# ───────────────────────────────────────────────
#  handlers/admin/panel_updates.py — ⬇️ экран «ОБНОВЛЕНИЯ» (2026-08-04)
#
#  Просьба Максима: в статистике — кнопка со сводкой обновлений, и каждое
#  обновление нажимается, открывая свой pull request (или сам коммит) на
#  GitHub.
#
#  Историю собирает services/update_log.py (журнал git той папки, из которой
#  бот работает), здесь — только показ и листание.
#
#  ⚠️ КНОПКИ ОБНОВЛЕНИЙ — ССЫЛОЧНЫЕ (url=…), а не callback: Telegram открывает
#  их сам, боту нажатие вообще не приходит. Поэтому в роутере разбирается
#  только листание (`upd:`), а самих правок там нет и быть не должно.
# ───────────────────────────────────────────────

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import AUTO_UPDATE_ENABLED_DEFAULT, BOT_VERSION_HTML
from database.history import get_setting

from .common import _adm_back_row, _send_panel_message

logger = logging.getLogger(__name__)

_ICON = "⬇️"

# Сколько обновлений показываем на одной странице. Восемь — чтобы экран
# читался целиком: у кнопки-ссылки надпись длинная, и десяток таких уже
# превращается в стену.
_PAGE_SIZE = 8

# Предел длины надписи на кнопке. Длиннее Telegram не отбивает, но переносит
# в несколько строк, и список перестаёт читаться.
_LABEL_MAX = 46


def _back_rows():
    """Возврат двумя рядами: откуда пришли и в главную панель.
    Тот же порядок, что у экрана дайджеста, — оба живут в статистике."""
    return [
        [InlineKeyboardButton("⬅️ Статистика", callback_data="adm_open_stats")],
        _adm_back_row(),
    ]


def _label(item: dict) -> str:
    """
    Надпись кнопки: «04.08 · v3.82 · Каталог техники: выход в главное меню» —
    порядок задан Максимом: сначала дата, потом версия, потом название.

    Версию спрашиваем у git ТОЛЬКО для показанной страницы (см. `version_of`).
    Её может не быть вовсе (файла VERSION в том коммите ещё не было) — тогда
    надпись собирается из даты и названия, без пустого разделителя.

    ⚠️ ВЕРСИЯ НЕ ПОВТОРЯЕТСЯ ДВАЖДЫ (2026-08-11, просьба Максима). Названия
    коммитов с некоторых пор сами начинаются с номера — «v4.10 — карта проекта
    приведена…», так их пишет и `Отправить изменения на GitHub.bat`. Со старой
    сборкой выходило «10.08 · v4.10 · v4.10 — карта проекта…». Теперь: название
    начинается с номера → показываем только дату и название («10.08 · v4.10 —
    карта проекта…»), иначе всё как было («04.08 · v3.82 · Каталог техники…»).
    Так номер виден ВСЕГДА и ровно один раз — в том числе у старых записей и
    у слияний pull request, где номера в названии нет вовсе.
    """
    import re
    from services import update_log

    title = item["title"]
    day = update_log.fmt_day(item["ts"])
    if re.match(r"^v\d+\.\d+\b", title):
        head = day                       # номер уже внутри названия
    else:
        version = update_log.version_of(item["sha"])
        head = f"{day} · v{version}" if version else day
    room = _LABEL_MAX - len(head) - 3  # 3 символа на разделитель « · »
    if len(title) > room:
        title = title[:max(1, room - 1)].rstrip() + "…"
    return f"{head} · {title}"


def _build_updates_panel(page: int = 0):
    """
    Текст и клавиатура экрана «ОБНОВЛЕНИЯ». Отдельной функцией — её собирает
    и preflight, меряя панель по лимитам Telegram.

    page — страница списка (0 = самые свежие).
    """
    from services import update_log

    items = update_log.recent()
    auto_on = get_setting("auto_update_enabled", AUTO_UPDATE_ENABLED_DEFAULT) == "1"

    head = (
        f"{_ICON} <b>ОБНОВЛЕНИЯ</b>\n"
        f"───────────────────────────\n"
        f"🔢 <b>Сейчас:</b> {BOT_VERSION_HTML}\n"
    )

    if not items:
        # Историю читаем из журнала git; его может не быть вовсе (код
        # разложен из архива) или git может быть не установлен — это не
        # поломка, просто рассказывать нечего.
        why = ("журнала git рядом с ботом нет"
               if not update_log.available() else "не удалось прочитать журнал git")
        text = (f"{head}"
                f"🤖 <b>Самообновление:</b> {'🟢 ВКЛЮЧЕНО' if auto_on else '🔴 ВЫКЛЮЧЕНО'}\n\n"
                f"История обновлений недоступна: {why}.")
        return text, InlineKeyboardMarkup(_back_rows())

    st = update_log.stats()
    pages = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]

    last = items[0]
    ago = update_log.fmt_ago(last["ts"])
    # Полное название свежей правки — только на первой странице: на остальных
    # человек листает старое, и строка «свежее всех» там сбивала бы с толку.
    # На кнопке оно всё равно обрезано, а тут видно целиком.
    fresh = f"\n<b>{html.escape(last['title'])}</b> — свежее всех.\n" if page == 0 else "\n"
    text = (
        f"{head}"
        f"📅 <b>Последнее:</b> {update_log.fmt_time(last['ts'])}"
        f"{f' ({ago})' if ago else ''}\n"
        f"📦 <b>Правок:</b> за сутки {st['day']} · за неделю {st['week']} · "
        f"за месяц {st['month']}\n"
        f"🗂 <b>В списке:</b> {st['total']}\n"
        f"🤖 <b>Самообновление:</b> {'🟢 ВКЛЮЧЕНО' if auto_on else '🔴 ВЫКЛЮЧЕНО'}\n"
        f"───────────────────────────\n"
        f"<i>Нажми на обновление — откроется его страница на GitHub "
        f"(слияние или коммит).</i>\n"
        f"{fresh}"
        f"Страница {page + 1} из {pages}."
    )

    rows = [[InlineKeyboardButton(_label(it), url=it["url"])] for it in chunk]

    # Листание: «Новее» слева, «Раньше» справа — как в списке пользователей.
    # Ряд появляется, только если листать есть куда: одинокая мёртвая стрелка
    # выглядит поломкой.
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Новее", callback_data=f"upd:page:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Раньше ➡️", callback_data=f"upd:page:{page + 1}"))
    if nav:
        rows.append(nav)

    return text, InlineKeyboardMarkup(rows + _back_rows())


async def send_updates_panel(bot, chat_id: int, user_id: int, page: int = 0):
    """Экран «⬇️ ОБНОВЛЕНИЯ» (кнопка в панели статистики)."""
    text, markup = _build_updates_panel(page)
    await _send_panel_message(bot, chat_id, text, markup)


async def _handle_updates_callback(query, context, data: str, chat_id: int, user_id: int):
    """
    Ветки upd:<действие>. Все владельческие (`upd:` в _CALLBACK_RULES).
      upd:list        — открыть экран
      upd:page:<n>    — страница списка

    Сами обновления сюда НЕ приходят: их кнопки — ссылки (url=…), Telegram
    открывает их без участия бота.
    """
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    page = 0
    if action == "page":
        try:
            page = int(parts[2])
        except (IndexError, ValueError):
            page = 0

    await query.answer()
    await send_updates_panel(context.bot, chat_id, user_id, page)
