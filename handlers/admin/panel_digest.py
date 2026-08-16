# ───────────────────────────────────────────────
#  handlers/admin/panel_digest.py — 📊 экран недельного дайджеста группы
#  (2026-08-04)
#
#  Дайджест собирает services/group_digest.py, здесь — только показ и кнопки.
#  ⚠️ РЕШЕНИЕ МАКСИМА: дайджест приходит ВЛАДЕЛЬЦУ В ЛИЧКУ, а в группу его
#  отправляет ЧЕЛОВЕК кнопкой «📤 Отправить в группу». Автоматической отправки
#  в чат нет — сначала он смотрит, что получается на живых данных.
# ───────────────────────────────────────────────

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from database.history import get_known_chats, set_setting
from utils import register_and_clean_bot_message

from .common import _adm_back_row, _audit

logger = logging.getLogger(__name__)

_ICON = "📊"


def _back_rows():
    """Возврат двумя рядами: откуда пришли и в главную панель."""
    return [
        [InlineKeyboardButton("⬅️ Статистика", callback_data="adm_open_stats")],
        _adm_back_row(),
    ]


def digest_keyboard(target_chat: int) -> InlineKeyboardMarkup:
    """
    Кнопки под текстом дайджеста: отправка в группу + возврат.

    ЕДИНСТВЕННОЕ место, где эта клавиатура собирается: её берут и экран
    панели, и понедельничная рассылка владельцу (jobs/reports.py). Заведёшь
    вторую сборку — кнопки в личке и в панели однажды разъедутся.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📤 Отправить в группу",
                               callback_data=f"dig:send:{target_chat}")]] + _back_rows()
    )


async def send_digest_panel(bot, chat_id: int, user_id: int, target_chat: int | None = None):
    """
    Экран дайджеста. Групп несколько и не выбрана — показываем выбор; группа
    одна — сразу её дайджест (лишний тык там ни к чему).
    """
    from services import group_digest

    chats = get_known_chats()
    if not chats:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{_ICON} <b>ДАЙДЖЕСТ НЕДЕЛИ</b>\n"
                 f"───────────────────────────\n"
                 f"Бот пока не знает ни одной группы — считать нечего.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(_back_rows()),
        )
        return

    if target_chat is None and len(chats) > 1:
        rows = [[InlineKeyboardButton(f"💬 {(c.get('title') or str(c['chat_id']))[:30]}",
                                      callback_data=f"dig:chat:{c['chat_id']}")]
                for c in chats]
        sent = await bot.send_message(
            chat_id=chat_id,
            text=f"{_ICON} <b>ДАЙДЖЕСТ НЕДЕЛИ</b>\n"
                 f"───────────────────────────\n"
                 f"По какой группе показать итоги?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows + _back_rows()),
        )
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)
        return

    chat = next((c for c in chats if c["chat_id"] == target_chat), chats[0])
    title = chat.get("title") or str(chat["chat_id"])

    # ⚠️ save_quiz=False: кнопка «показать сейчас» НЕ сдвигает точку отсчёта
    # викторины — иначе нажавший её человек обнулил бы себе цифры ближайшего
    # понедельничного дайджеста. Снимок обновляет только настоящая отправка.
    text = group_digest.build(chat["chat_id"], title, save_quiz=False,
                              bot_id=bot.id)
    sent = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        reply_markup=digest_keyboard(chat["chat_id"]),
    )
    if sent:
        await register_and_clean_bot_message(bot, chat_id, sent.message_id)


async def _handle_digest_callback(query, context, data: str, chat_id: int, user_id: int):
    """
    Ветки dig:<действие>. Все владельческие (`dig:` в _CALLBACK_RULES).
      dig:show           — экран дайджеста (выбор группы или сразу итоги)
      dig:chat:<id>      — итоги конкретной группы
      dig:toggle         — тумблер понедельничной отправки в личку
      dig:send:<id>      — отправить показанный дайджест В ГРУППУ
    """
    from services import group_digest

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "toggle":
        new_val = "0" if group_digest.is_enabled() else "1"
        set_setting(group_digest.ENABLED_KEY, new_val)
        state = "включён" if new_val == "1" else "выключен"
        logger.info("%s Понедельничный дайджест %s (админ %s)", _ICON, state, user_id)
        _audit(user_id, "digest", 0, f"понедельничный дайджест {state}")
        await query.answer(
            f"📰 Дайджест по понедельникам {state}."
            + (" Придёт тебе в личку в 10:00." if new_val == "1" else ""),
            show_alert=True,
        )
        # Перерисовываем ПАНЕЛЬ СТАТИСТИКИ — кнопка живёт там.
        from .panel_main import send_stats_panel
        await send_stats_panel(context.bot, chat_id, user_id)
        return

    if action == "send":
        try:
            target = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        # Отправляем ИМЕННО ТОТ текст, который человек видит перед собой:
        # пересчёт мог бы дать другие цифры (неделя-то скользящая), и в группу
        # ушло бы не то, что он одобрил.
        text = query.message.text_html if query.message else ""
        if not text:
            await query.answer("❌ Текст дайджеста потерялся — открой экран заново.",
                               show_alert=True)
            return
        # ⚠️ На нажатие отвечаем РОВНО ОДИН раз — либо успехом, либо причиной
        # отказа. Раннего «⏳ Отправляю…» здесь намеренно НЕТ: после него
        # Telegram отбивал повторный ответ, объяснение отказа не доходило, и
        # кнопка выглядела зависшей ровно в том случае, ради которого ветка и
        # написана (бота выгнали из группы, отняли право писать). Одно сообщение
        # в одну группу — не долгая операция, 15-секундный предел ей не грозит.
        try:
            await context.bot.send_message(chat_id=target, text=text,
                                           parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("⚠️ %s Дайджест не ушёл в группу %s: %s", _ICON, target, e)
            # Текст всплывающего окна ≤200 символов, иначе оно не покажется вовсе.
            await query.answer(f"❌ Не удалось отправить: {e}"[:200], show_alert=True)
            return
        await query.answer("✅ Дайджест отправлен в группу.")
        logger.info("%s Владелец %s отправил дайджест в группу %s", _ICON, user_id, target)
        _audit(user_id, "digest", 0, f"дайджест отправлен в чат {target}")
        # Кнопку отправки убираем: второе нажатие прислало бы в группу дубль.
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(_back_rows()))
        except Exception as e:
            logger.debug("%s Не удалось убрать кнопку отправки: %s", _ICON, e)
        return

    if action == "chat":
        try:
            target = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return
        await query.answer()
        await send_digest_panel(context.bot, chat_id, user_id, target_chat=target)
        return

    await query.answer()
    await send_digest_panel(context.bot, chat_id, user_id)
