# ───────────────────────────────────────────────
#  handlers/admin/panel_prompts.py — панель «⚙️ НАСТРОЙКИ PROMPTов» и команды /prompt_*, /news_prompt_*, /rag_prompt_*.
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import json
import logging
import os
import re
import time

import logging_setup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, ADMIN_IDS, GEMINI_MODEL
from config import PROACTIVE_ENABLED_DEFAULT, PROACTIVE_MIN_MSGS, PROACTIVE_CONTEXT_MSGS
from config import PROACTIVE_HANDS_DEFAULT, PROACTIVE_MUTE_MAX_SEC
from config import PROACTIVE_OFF_ANNOUNCE, PROACTIVE_OFF_MSGS_KEY
from database.history import set_setting, get_setting, append_prompt_addition, get_active_system_prompt, get_bot_stats, get_news_system_prompt, get_rag_instruction, get_proactive_instruction, get_proactive_media_prompt, get_known_chats
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete


logger = logging.getLogger(__name__)
from .common import _adm_back_row, _audit, _is_group_chat, _onoff, _reject_non_admin, _require, _send_panel_message


def _int_setting(key: str, default: int) -> int:
    """settings хранит строки — безопасно приводим к int с фолбэком на дефолт."""
    try:
        return int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


# Шаги и границы регуляторов «Сам в разговор» (кнопки ➖/➕ этой панели);
# тот же паттерн, что _MOD_LIMITS в panel_mod.py.
_PROACTIVE_LIMITS = {
    # Пауз по времени здесь БОЛЬШЕ НЕТ (обе убраны 2026-07-20, см. proactive.py):
    # они дублировали друг друга. Остались порог сообщений и размер стенограммы.
    "proactive_min_msgs":      {"step": 1,  "min": 1, "max": 20},
    "proactive_context_msgs":  {"step": 5,  "min": 5, "max": 50},
}


def _adjust_proactive_setting(key: str, default: int, delta_steps: int) -> int:
    """Меняет числовую настройку на delta_steps шагов в пределах _PROACTIVE_LIMITS."""
    lim = _PROACTIVE_LIMITS[key]
    new_val = _int_setting(key, default) + lim["step"] * delta_steps
    new_val = max(lim["min"], min(lim["max"], new_val))
    set_setting(key, str(new_val))
    return new_val


async def _announce_proactive_off(bot):
    """
    Объявление в группы при ВЫКЛЮЧЕНИИ режима «Сам в разговор» (2026-07-24,
    просьба Максима): шлёт PROACTIVE_OFF_ANNOUNCE во ВСЕ известные группы
    (known_chats) и запоминает координаты сообщений в settings.

    Три правила, которые нельзя «чинить»:
      • НЕ регистрируем в гигиене панелей (register_and_clean_bot_message) —
        иначе объявление затрёт первая же панель, а оно должно висеть до тех
        пор, пока Максим не включит режим обратно;
      • координаты пишем в БД, а не в память — переживают перезапуск бота,
        иначе удалить объявление станет нечем;
      • группа, куда отправить не вышло (бота выгнали, нет прав), молча
        пропускается: сбой рассылки не должен ронять нажатие кнопки.
    """
    sent = []
    for chat in get_known_chats():
        chat_id = chat.get("chat_id")
        try:
            msg = await bot.send_message(chat_id=chat_id, text=PROACTIVE_OFF_ANNOUNCE,
                                         parse_mode=ParseMode.HTML)
            sent.append([chat_id, msg.message_id])
        except Exception as e:
            logger.warning("⚠️ Объявление о выключении не ушло в чат %s: %s", chat_id, e)
    set_setting(PROACTIVE_OFF_MSGS_KEY, json.dumps(sent))
    logger.info("🔧 Объявление о выключении «Сам в разговор» отправлено в %d групп(ы)", len(sent))


async def _remove_proactive_off_announce(bot):
    """
    Удаляет ранее отправленные объявления при ВКЛЮЧЕНИИ режима. Обратного
    сообщения («бан снят») Максим НЕ хочет — только тихое удаление.
    Сообщение, удалённое кем-то вручную или устаревшее (Telegram не даёт
    ботам удалять старше 48 часов), просто пропускается.
    """
    raw = get_setting(PROACTIVE_OFF_MSGS_KEY, "")
    if not raw:
        return
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        items = []
    removed = 0
    for pair in items:
        try:
            chat_id, message_id = pair
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            removed += 1
        except Exception as e:
            logger.debug("🔧 Не удалось удалить объявление %s: %s", pair, e)
    set_setting(PROACTIVE_OFF_MSGS_KEY, "")
    logger.info("🔧 Объявлений о выключении удалено: %d из %d", removed, len(items))


# ─────────────────────────────────────────────
#  Итоги участия в разговоре (2026-07-31)
#
#  Режим «Сам в разговор» настраивается двумя рычагами — порогом и промптом
#  (таймеры убраны дважды и не возвращаются). До этих цифр оба крутились
#  вслепую: «тараторит» и «молчит» — это ощущение, а не число.
#
#  Считаются РАЗНЫЕ вещи, и путать их нельзя:
#    • ПРОВЕРКИ — сколько раз дошло до модели (это расход);
#    • ВСТУПЛЕНИЯ — сколько раз модель решила заговорить;
#    • ОТСЕВ — сколько сообщений до модели не дошло и почему.
#  Ключевое — ДОЛЯ вступлений от проверок: она отличает «модель не умеет
#  молчать» (лечится промптом) от «проверок слишком много» (лечится порогом).
# ─────────────────────────────────────────────

# Подписи исходов для экрана подробностей. ⚠️ Новый исход в
# database/history.py::log_proactive_check добавлять И СЮДА, иначе он
# покажется голым кодом (те же грабли, что с типами мутов в /mod).
_OUTCOME_TITLES = {
    "reply": "💬 вступил в разговор",
    "reply_mute": "🔇 вступил и выдал мут",
    "silent": "🤐 промолчал",
    "empty": "📭 сказать было нечего",
    "error": "⚠️ запрос сорвался",
}

_TRIGGER_TITLES = {
    "text": "текст",
    "photo": "фото",
    "voice": "голос",
    "video": "видео",
}


def _utc_since(hours: int) -> str:
    """Момент «N часов назад» строкой UTC — в том же виде, что ts в журнале."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _share(part: int, whole: int) -> str:
    """Доля в процентах, без деления на ноль."""
    return f"{round(part * 100 / whole)}%" if whole else "—"


def _proactive_stats_lines() -> str:
    """
    Две строки итогов за сутки для панели промптов. Держать КОРОТКИМИ: текст
    панели уже за три тысячи символов из 4096, подробности живут на своём
    экране. Своих ошибок наружу не отдаём — статистика не должна мешать
    открыть панель (без неё панель бесполезна, а без цифр — просто беднее).
    """
    try:
        from database.history import proactive_stats
        from services.proactive import skip_counts

        day = proactive_stats(_utc_since(24))
        checks = day["checks"]
        replies = day["reply"] + day["reply_mute"]
        skipped, _since = skip_counts()
        skipped_total = sum(skipped.values())

        if not checks and not skipped_total:
            return ("\n───────────────────────────\n"
                    "📊 <b>УЧАСТИЕ ЗА СУТКИ:</b> <i>проверок не было</i>")

        return (
            "\n───────────────────────────\n"
            f"📊 <b>УЧАСТИЕ ЗА СУТКИ:</b> проверок {checks} · "
            f"вступил {replies} ({_share(replies, checks)}) · промолчал {day['silent']}\n"
            f"<i>Отсеяно до модели: {skipped_total} — подробности по кнопке ниже</i>"
        )
    except Exception as e:
        logger.debug("🔧 Не удалось собрать итоги участия для панели: %s", e)
        return ""


def _build_proactive_stats_panel():
    """
    Экран «📊 Подробнее об участии»: сутки и неделя, разбивка по исходам,
    по дням, по чатам и по виду триггера, плюс отсев дешёвыми фильтрами.

    ⚠️ Две половины экрана живут по РАЗНЫМ правилам, и это сказано прямо
    в тексте: проверки берутся из журнала в базе (переживают перезапуск),
    отсев — из счётчиков в памяти (обнуляется перезапуском, как у антиспама).
    Не «привести к общему виду»: считать отсев в базе значит писать в неё
    на каждое сообщение группы.
    """
    from database.history import proactive_by_chat, proactive_by_day, proactive_stats
    from services.proactive import skip_counts

    sep = "───────────────────────────\n"
    day = proactive_stats(_utc_since(24))
    week = proactive_stats(_utc_since(24 * 7))

    parts = ["📊 <b>УЧАСТИЕ В РАЗГОВОРЕ</b>\n", sep]

    day_replies = day["reply"] + day["reply_mute"]
    week_replies = week["reply"] + week["reply_mute"]
    parts.append(
        f"<b>За сутки:</b> проверок {day['checks']} · "
        f"вступил {day_replies} ({_share(day_replies, day['checks'])})\n"
        f"<b>За неделю:</b> проверок {week['checks']} · "
        f"вступил {week_replies} ({_share(week_replies, week['checks'])})\n"
    )
    if day["avg_sec"]:
        parts.append(f"<i>Модель думает в среднем {day['avg_sec']:.1f} с</i>\n")

    # ── Исходы за неделю ──
    parts.append(sep + "<b>Чем кончались проверки за неделю</b>\n")
    for code, title in _OUTCOME_TITLES.items():
        n = week.get(code, 0)
        if n:
            parts.append(f"{title}: <b>{n}</b> ({_share(n, week['checks'])})\n")
    if not week["checks"]:
        parts.append("<i>проверок не было</i>\n")

    # ── По дням ──
    try:
        by_day = proactive_by_day(7)
    except Exception:
        by_day = []
    if by_day:
        parts.append(sep + "<b>По дням</b> <i>(проверок → вступлений)</i>\n")
        for day_label, checks, replies in by_day:
            parts.append(f"{day_label}: {checks} → <b>{replies}</b> ({_share(replies, checks)})\n")

    # ── По чатам ──
    try:
        by_chat = proactive_by_chat(_utc_since(24 * 7), limit=5)
    except Exception:
        by_chat = []
    if by_chat:
        parts.append(sep + "<b>По чатам за неделю</b>\n")
        for chat_id, checks, replies in by_chat:
            title = _chat_title(chat_id)
            parts.append(f"{title}: {checks} → <b>{replies}</b>\n")

    # ── По виду триггера ──
    if week["by_trigger"]:
        kinds = ", ".join(
            f"{_TRIGGER_TITLES.get(k, k)} {n}"
            for k, n in sorted(week["by_trigger"].items(), key=lambda kv: -kv[1])
        )
        parts.append(sep + f"<b>Триггеры за неделю:</b> {kinds}\n"
                           "<i>Медиа дороже: вложение сначала разбирает отдельная модель.</i>\n")

    # ── Отсев в памяти ──
    skipped, since = skip_counts()
    total = sum(skipped.values())
    minutes = max(0, (time.time() - since) / 60)
    uptime = f"{minutes / 60:.0f} ч" if minutes >= 60 else f"{minutes:.0f} мин"
    parts.append(sep + f"<b>Отсеяно до модели:</b> {total} "
                       f"<i>(с последнего запуска, {uptime})</i>\n")
    if total:
        for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
            parts.append(f"• {reason}: <b>{n}</b>\n")
    parts.append("<i>Эти числа живут в памяти и обнуляются перезапуском — "
                 "на каждое сообщение группы бот в базу не ходит.</i>\n")

    parts.append(sep + "ℹ️ <i>Вступает часто при малом числе проверок — правь "
                       "PROMPT участия. Проверок много — правь порог.</i>")

    keyboard = [
        [InlineKeyboardButton("⬅️ К промптам", callback_data="back_to_stats")],
        _adm_back_row(),
    ]
    return "".join(parts), InlineKeyboardMarkup(keyboard)


def _chat_title(chat_id: int) -> str:
    """Название группы из известных боту чатов; не нашлось — голый номер."""
    try:
        for chat in get_known_chats():
            if chat["chat_id"] == chat_id:
                return html.escape(chat["title"] or str(chat_id))
    except Exception:
        pass
    return str(chat_id)


async def _handle_proactive_callback(query, user_id, data):
    """
    Ветки proactive:<действие> — тумблер и регуляторы режима «Сам в разговор»
    (services/proactive.py). Гейт ADMIN_IDS уже пройден в handle_callback_query.
    Панель обновляется ТОЛЬКО клавиатурой (edit_message_reply_markup): живые
    цифры и статус живут на кнопках, текст не трогаем — нет риска лимита 4096.
    """
    action = data.split(":", 1)[1] if ":" in data else ""

    if action == "noop":
        await query.answer()
        return

    if action in ("wipe", "wipe_yes"):
        await _handle_proactive_wipe(query, user_id, action == "wipe_yes")
        return

    if action == "stats":
        # Экран подробностей ЗАМЕНЯЕТ панель в том же сообщении (как экран прав
        # модератора): возврат — кнопкой «⬅️ К промптам», она соберёт панель
        # заново со свежими цифрами.
        await query.answer()
        try:
            text, markup = _build_proactive_stats_panel()
            await query.edit_message_text(
                text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось показать итоги участия: %s", e)
        return

    if action == "hands":
        new_val = "0" if get_setting("proactive_hands", PROACTIVE_HANDS_DEFAULT) == "1" else "1"
        set_setting("proactive_hands", new_val)
        state = "включены" if new_val == "1" else "выключены"
        logger.info("🔧 Панель промптов: руки «Сам в разговор» %s", state)
        _audit(user_id, "proactive_hands", 0, state)
        await query.answer(
            f"Руки {state}." + (" Бот сможет сам выдавать мут до "
                                f"{PROACTIVE_MUTE_MAX_SEC // 60} мин." if new_val == "1" else ""),
            show_alert=(new_val == "1"),
        )
    elif action == "toggle":
        new_val = "0" if get_setting("proactive_enabled", PROACTIVE_ENABLED_DEFAULT) == "1" else "1"
        set_setting("proactive_enabled", new_val)
        state = "включён" if new_val == "1" else "выключен"
        logger.info("🔧 Панель промптов: режим «Сам в разговор» %s", state)
        # Объявление в группы (2026-07-24, просьба Максима): выключил — бот
        # сообщает группам о «бане», включил — сообщение молча удаляется.
        # Рассылка не должна ломать саму кнопку: ошибки уже проглочены внутри,
        # но страхуемся и здесь — тумблер обязан переключиться в любом случае.
        try:
            if new_val == "0":
                await _announce_proactive_off(query.get_bot())
            else:
                await _remove_proactive_off_announce(query.get_bot())
        except Exception as e:
            logger.warning("⚠️ Объявление о смене режима «Сам в разговор» не отработало: %s", e)
        await query.answer(f"Сам в разговор {state}", show_alert=False)
    elif action in ("mm_inc", "mm_dec"):
        new_mm = _adjust_proactive_setting("proactive_min_msgs", PROACTIVE_MIN_MSGS,
                                           1 if action.endswith("inc") else -1)
        logger.info("🔧 Панель промптов: порог «Сам в разговор» = %d сообщ.", new_mm)
        await query.answer()
    elif action in ("ctx_inc", "ctx_dec"):
        new_ctx = _adjust_proactive_setting("proactive_context_msgs", PROACTIVE_CONTEXT_MSGS,
                                            1 if action.endswith("inc") else -1)
        logger.info("🔧 Панель промптов: стенограмма «Сам в разговор» = %d сообщ.", new_ctx)
        await query.answer()
    elif action == "wipe_no":
        # Отмена подтверждения — общая перерисовка ниже вернёт обычные кнопки.
        await query.answer()
    else:
        await query.answer()
        return

    # Перерисовываем только клавиатуру (цифры и статус — на кнопках)
    try:
        _, markup = _build_prompt_panel_text_and_keyboard(user_id)
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception as e:
        logger.debug("🔧 Не удалось обновить клавиатуру панели промптов: %s", e)


async def _handle_proactive_wipe(query, user_id: int, confirmed: bool, from_adm: bool = False):
    """
    Ветки proactive:wipe (спросить) и proactive:wipe_yes (забыть) — кнопка
    «🧹Очистить РАЗГОВОРЫ» блока «Сам в разговор».

    Бот забывает стенограмму бесед ВО ВСЕХ группах и у всех людей сразу,
    включая владельцев и модераторов. Архив при этом НЕ удаляется: в settings
    ставится метка времени, старше которой стенограмма не читается
    (database/history.py::set_proactive_reset_mark) — счётчик архива в /stats
    и запасные имена для /users остаются целы, а решение обратимо.

    Действие заметное, поэтому сначала подменяем клавиатуру на «да / отмена»
    — тем же приёмом, что очистка журнала в панели модерации.
    """
    # ⚠️ ПОДТВЕРЖДЕНИЕ ОСТАЛОСЬ ТОЛЬКО У КНОПКИ ИЗ ПАНЕЛИ ПРОМПТОВ (2026-08-10,
    # просьба Максима убрать его в /adm). Поэтому callback'и подтверждения
    # всегда «proactive:…»: из /adm сюда приходят уже с confirmed=True, и
    # ветка ниже не выполняется вовсе. Пары «adm:wipe_yes/no» больше НЕТ —
    # заведёшь её обратно, не добавив ветку в роутер, и preflight это поймает
    # (проверка «кнопки ↔ роутер»), как поймал при этой правке.
    prefix = "proactive"

    if not confirmed:
        await query.answer()
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, очистить РАЗГОВОРЫ", callback_data=f"{prefix}:wipe_yes"),
                InlineKeyboardButton("Отмена", callback_data=f"{prefix}:wipe_no"),
            ]]))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение очистки стенограммы: %s", e)
        return

    from database.history import set_proactive_reset_mark
    from services.proactive import forget_conversations

    mark = set_proactive_reset_mark()
    forget_conversations()   # счётчики в памяти — иначе проверка по пустой стенограмме
    logger.info("🔧 Владелец %s: стенограмма «Сам в разговор» забыта (черта %s UTC)", user_id, mark)
    _audit(user_id, "proactive_wipe", 0, "бот забыл разговоры во всех группах")
    await query.answer("🧹 Готово: бот забыл разговоры во всех группах.", show_alert=True)

    # Возвращаем кнопки ТОЙ панели, из которой нажали (текст не трогаем — он
    # не изменился). Перепутаешь — в админ-панели окажутся кнопки промптов.
    try:
        if from_adm:
            from .panel_main import build_adm_keyboard
            markup = build_adm_keyboard(user_id)
        else:
            _, markup = _build_prompt_panel_text_and_keyboard(user_id)
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception as e:
        logger.debug("🔧 Не удалось вернуть клавиатуру после очистки разговоров: %s", e)


# ─────────────────────────────────────────────
#  Страховка от лимита Telegram (2026-08-05)
# ─────────────────────────────────────────────
#
#  У панели промптов ШЕСТЬ превью (система, дополнения, новости, RAG, участие,
#  разбор медиа) по 600 символов каждое — в сумме 3600 при постоянной части
#  ~1400. Задай Максим все промпты длинными, и панель перевалит за 4096:
#  Telegram отобьёт отправку целиком, и панель просто перестанет открываться.
#
#  Поэтому перед отправкой текст прогоняется через `_fit_panel_text`: он
#  ужимает СОДЕРЖИМОЕ свёрнутых цитат (самое длинное — первым), пока текст
#  не влезет. Режем именно внутри `<blockquote expandable>…</blockquote>`,
#  а не текст целиком: обрезка «по живому» рассекла бы HTML-тег, и отправка
#  упала бы всё равно, только уже с непонятной ошибкой разметки.
#
#  ⚠️ НЕ «оптимизировать» до обычного text[:4096] — см. абзац выше.
_PANEL_TEXT_MAX = 4000       # запас к лимиту 4096 на служебную разметку
_PREVIEW_FLOOR = 80          # короче этого превью ужимать бессмысленно


def _fit_panel_text(text: str, limit: int = _PANEL_TEXT_MAX) -> str:
    """
    Ужимает превью в тексте панели, пока он не влезет в лимит Telegram.

    Возвращает текст как есть, если он и так короче лимита (обычный случай —
    ужимание включается только на длинных кастомных промптах).
    """
    if len(text) <= limit:
        return text

    for _ in range(20):   # потолок проходов: страховка от вечного цикла
        blocks = list(re.finditer(r"<blockquote expandable>(.*?)</blockquote>",
                                  text, re.DOTALL))
        # Самая длинная цитата — её и режем: так текст выравнивается сам собой.
        longest = max((b for b in blocks if len(b.group(1)) > _PREVIEW_FLOOR),
                      key=lambda b: len(b.group(1)), default=None)
        if longest is None:
            break
        body = longest.group(1)
        # Режем ровно на лишнее (плюс «…»), но не ниже пола.
        keep = max(_PREVIEW_FLOOR, len(body) - (len(text) - limit) - 1)
        # ⚠️ Внутри превью текст УЖЕ экранирован (&amp;, &lt;): обрезка может
        # рассечь мнемонику пополам — хвост вида «&am» убираем целиком,
        # иначе Telegram отобьёт сообщение ошибкой разметки.
        clipped = re.sub(r"&[a-z]*$", "", body[:keep]) + "…"
        text = text[:longest.start(1)] + clipped + text[longest.end(1):]
        if len(text) <= limit:
            break
    return text


def _build_prompt_panel_text_and_keyboard(user_id, bot_username=None):
    additions = get_setting("prompt_additions")
    prompt_text, _, _ = get_active_system_prompt()

    # Счётчик символов — синей ссылкой на чат с ботом (решение 2026-07-05;
    # без bot_username — запасной копируемый <code>).
    def _num(n):
        if bot_username:
            return f'<a href="https://t.me/{bot_username}">{n}</a>'
        return f"<code>{n}</code>"

    # Превью текста промпта выводим в сворачиваемой цитате (expandable blockquote).
    # Текст обрезаем до PROMPT_PREVIEW_MAX: свёрнутый блок всё равно целиком
    # считается в лимит Telegram (4096 симв.), поэтому без обрезки огромный промпт
    # сломал бы отправку. Полный текст доступен через команды /prompt_*, /news_prompt_*.
    PROMPT_PREVIEW_MAX = 600

    def _expandable_preview(raw: str) -> str:
        if not raw:
            return "<i>(не задан)</i>"
        clipped = raw[:PROMPT_PREVIEW_MAX]
        safe = clipped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if len(raw) > PROMPT_PREVIEW_MAX:
            safe += "…"
        return f"<blockquote expandable>{safe}</blockquote>"

    header = f"📝 <b>SYSTEM PROMPT:</b> {_num(len(prompt_text))} <i>символов</i>\n"

    # Основной промпт без дополнений (они уже вшиты в конец prompt_text)
    if additions:
        base_prompt = prompt_text[:-(len(additions) + 2)]  # -2 за "\n\n"
    else:
        base_prompt = prompt_text

    full_text = header + _expandable_preview(base_prompt)

    if additions:
        full_text += (
            "\n"
            "➕ <b>ДОПОЛНЕНИЯ:</b>\n"
            f"{_expandable_preview(additions)}"
        )

    # Метка кнопки переключения промпта (сам статус в тексте больше не выводится).
    # ВНИМАНИЕ: настройка хранится НАОБОРОТ — "1" в admin_no_prompt_<id> значит
    # «промпт ВЫКЛЮЧЕН», поэтому в _onoff уходит перевёрнутое значение.
    no_prompt_val = get_setting(f"admin_no_prompt_{user_id}", "0")
    btn_prompt_label = f"⚙️ PROMPT: {_onoff(no_prompt_val != '1')}"

    # Команды — обычным текстом (НЕ <code>): Telegram сам подсвечивает их синим
    # и делает кликабельными, как /unsubscribe в сообщении о подписке.
    full_text += (
        "\n"
        "✏️ /prompt_add &lt;текст&gt; — Добавить новые инструкции к промпту\n"
        "🔄 /prompt_set &lt;текст&gt; — Полностью заменить текущий промпт\n"
        "🗑️ /prompt_reset — Сбросить промпт к заводским настройкам"
    )

    # ── Промпт для новостей ──────────────────────────────────────────────
    news_prompt = get_news_system_prompt()
    if news_prompt:
        news_section = (
            "\n───────────────────────────\n"
            f"📰<b>NEWS PROMPT:</b> {_num(len(news_prompt))} <i>символов</i>\n"
            f"{_expandable_preview(news_prompt)}\n"
            "✏️ /news_prompt_set &lt;текст&gt; — Изменить промпт новостей\n"
            "🗑️ /news_prompt_reset — Удалить промпт новостей"
        )
    else:
        # Промпт новостей не задан — показываем счётчик и "(не задан)",
        # в том же стиле, что и у SYSTEM PROMPT.
        news_section = (
            "\n───────────────────────────\n"
            f"📰<b>NEWS PROMPT:</b> {_num(0)} <i>символов</i>\n"
            f"{_expandable_preview(news_prompt)}\n"
            "✏️ /news_prompt_set &lt;текст&gt; — Задать промпт новостей"
        )
    full_text += news_section

    # ── RAG-инструкция ───────────────────────────────────────────────────
    # «Шапка», которая уходит модели ПЕРЕД найденными статьями базы знаний
    # (сами статьи бот подставляет под ней автоматически). Инструкция задана
    # ВСЕГДА — своя или заводская, поэтому ветки «(не задан)» тут нет.
    rag_instruction = get_rag_instruction()
    rag_is_custom = bool(get_setting("rag_instruction", "").strip())
    rag_origin = "своя" if rag_is_custom else "заводская"
    rag_section = (
        "\n───────────────────────────\n"
        f"🧠<b>RAG-PROMPT:</b> {_num(len(rag_instruction))} <i>символов ({rag_origin})</i>\n"
        f"{_expandable_preview(rag_instruction)}\n"
        "✏️ /rag_prompt_set &lt;текст&gt; — Изменить RAG-инструкцию\n"
        "🗑️ /rag_prompt_reset — Вернуть заводскую"
    )
    full_text += rag_section

    # ── Справка об авторе («[С кем ты говоришь]») ────────────────────────
    # Единственный кусок проактивного запроса, который бот СОБИРАЕТ САМ из
    # карточки участника, а не берёт из настроек. Показываем дословно — иначе
    # владелец никак не может увидеть, что именно бот рассказывает модели
    # о людях (просьба Максима 2026-07-26).
    # Пример собирается НА СМОТРЯЩЕГО (решение Максима): живые цифры нагляднее
    # шаблона с прочерками. Источник — services/gemini.py::author_brief, тот же,
    # что уходит модели: показанное и отправленное разъехаться не могут.
    # Стоит ПЕРЕД блоком участия, потому что в запросе идёт в этом же порядке
    # (характер → база знаний → справка → правила участия → стенограмма).
    try:
        from services.gemini import author_brief
        who_text = author_brief(user_id) or ""
    except Exception as e:
        logger.debug("🔧 Не удалось собрать справку об авторе для панели: %s", e)
        who_text = ""
    who_body = _expandable_preview(who_text) if who_text else "<i>(нет данных о тебе)</i>"
    who_section = (
        "\n───────────────────────────\n"
        f"🪪<b>СПРАВКА ОБ АВТОРЕ:</b> {_num(len(who_text))} <i>символов (собирает бот)</i>\n"
        f"{who_body}\n"
        "ℹ️ <i>Пример собран на тебе — у каждого участника он свой.</i>\n"
        "📋 <i>Берётся из карточки: имя · ник · роль · почётное звание</i>\n"
        "⚠️ <i>Уходит модели в режиме «Сам в разговор» и при обращении к боту "
        "в группе. В личке — нет.</i>"
    )
    full_text += who_section

    # ── Инструкция участия в разговоре («Сам в разговор») ────────────────
    # По ней модель решает, вступить ли в беседу группы без обращения к боту
    # (services/proactive.py). Задана всегда — своя или заводская.
    proactive_instruction = get_proactive_instruction()
    proactive_origin = "своя" if get_setting("proactive_instruction", "").strip() else "заводская"
    proactive_section = (
        "\n───────────────────────────\n"
        f"🗣<b>PROMPT УЧАСТИЯ В РАЗГОВОРЕ:</b> {_num(len(proactive_instruction))} <i>символов ({proactive_origin})</i>\n"
        f"{_expandable_preview(proactive_instruction)}\n"
        "✏️ /proactive_prompt_set &lt;текст&gt; — Изменить инструкцию\n"
        "🗑️ /proactive_prompt_reset — Вернуть заводскую"
    )
    full_text += proactive_section

    # ── Промпт разбора медиа (2026-08-05) ────────────────────────────────
    # ⚠️ ОПИСАНИЕ ЗДЕСЬ ДВАЖДЫ УСТАРЕВАЛО — сверяй при каждой правке разбора
    # медиа (2026-08-10, Максим поймал ложь в панели). Как есть на сегодня:
    #   • В ГРУППЕ («Сам в разговор») промпт уходит НЕ активной модели, а
    #     цепочке разбора PROACTIVE_MEDIA_CHAIN — с 2026-08-10 это четыре
    #     модели, а не одна зашитая flash-lite. Это ОТДЕЛЬНЫЙ запрос, и в
    #     постоянную часть проактивного он не входит — отсюда своя секция.
    #   • В ЛИЧКЕ (с 2026-08-10) тот же промпт идёт АКТИВНОЙ модели вместе с
    #     файлом — в ask_gemini / ask_gemini_audio / ask_gemini_video.
    # ⚠️ Промпт ОДИН на все три вида медиа и по умолчанию ПУСТ — пустой
    # означает «модели уходит только файл» (решение Максима 2026-07-24),
    # поэтому здесь, как у промпта новостей, есть ветка «(не задан)».
    # Превью — общей длины PROMPT_PREVIEW_MAX (600), как у остальных блоков
    # (просьба Максима 2026-08-05; в первой сборке было ужато до 200 ради
    # запаса по лимиту Telegram). ⚠️ Запас теперь держит `_fit_panel_text`
    # в конце сборки: шесть превью по 600 в сумме выбивают 4096, и без обрезки
    # панель просто не открылась бы.
    media_prompt = get_proactive_media_prompt()
    media_section = (
        "\n───────────────────────────\n"
        f"🖼<b>PROMPT РАЗБОРА МЕДИА:</b> {_num(len(media_prompt))} <i>символов</i>\n"
        f"{_expandable_preview(media_prompt)}\n"
        "🤖 <i>Уходит вместе с фото, голосовым или видео — и в группе, и в личке. "
        "В группе файл разбирает цепочка Gemini (3.5 Flash → 3.6 Flash → "
        "3.1 Flash-Lite → 3 Flash Preview), разбор попадает в стенограмму; "
        "в личке промпт идёт активной модели вместе с файлом.</i>\n"
        "⚠️ <i>Не задан — модель получает голый файл и отвечает как хочет.</i>\n"
        + ("✏️ /media_prompt_set &lt;текст&gt; — Изменить промпт разбора\n"
           "🗑️ /media_prompt_reset — Удалить промпт разбора"
           if media_prompt else
           "✏️ /media_prompt_set &lt;текст&gt; — Задать промпт разбора")
    )
    full_text += media_section

    # ── Сводка: из чего складывается проактивный запрос ───────────────────
    # Отвечает на вопрос «что вообще уходит модели» одним взглядом (просьба
    # Максима 2026-07-26). Порядок строк = ПОРЯДОК СБОРКИ в
    # services/gemini.py::_build_proactive_parts — меняешь там, поправь здесь,
    # иначе сводка начнёт врать. Цифры живые, считаются при каждом открытии.
    # Стенограмма и статьи базы знаний размером не считаются: их длина зависит
    # от конкретного чата и вопроса, постоянного числа у них нет.
    who_len = len(who_text)
    fixed_total = len(prompt_text) + who_len + len(proactive_instruction)
    # Размер стенограммы — тот же регулятор «📜 контекст», что и на кнопке ниже.
    proactive_ctx = _int_setting("proactive_context_msgs", PROACTIVE_CONTEXT_MSGS)
    full_text += (
        "\n───────────────────────────\n"
        "📦 <b>ЧТО УХОДИТ МОДЕЛИ В РЕЖИМЕ «САМ В РАЗГОВОР»</b>\n"
        f"1. 📝 SYSTEM PROMPT — {_num(len(prompt_text))} <i>симв.</i>\n"
        "2. 🧠 Статьи базы знаний — <i>если тема совпала</i>\n"
        f"3. 🪪 Справка об авторе — {_num(who_len)} <i>симв.</i>\n"
        f"4. 🗣 PROMPT участия — {_num(len(proactive_instruction))} <i>симв.</i>\n"
        f"5. 💬 Стенограмма чата — <i>последние {proactive_ctx} сообщ.</i>\n"
        f"<i>Постоянная часть (1+3+4): {fixed_total} символов</i>"
    )

    # ── Итог участия за сутки (2026-07-31) ───────────────────────────────
    # Обратная связь для регулятора «💬 порог», который стоит кнопкой ниже:
    # без неё режим настраивается вслепую. ГЛАВНОЕ ЧИСЛО — ДОЛЯ ВСТУПЛЕНИЙ,
    # а не их количество: 38 вступлений из 40 проверок и 3 из 40 выглядят
    # в чате похоже («бот болтлив»), но лечатся ПРОТИВОПОЛОЖНО — первое
    # промптом (модель не умеет молчать), второе порогом (проверки редки).
    # Полная раскладка — на экране «📊 Подробнее», здесь только две строки:
    # текст панели и так занимает больше трёх тысяч символов из 4096.
    full_text += _proactive_stats_lines()

    # Метка кнопки тумблера ответов ИИ (глобальная настройка, по умолчанию ВКЛ)
    ai_replies_val = get_setting("ai_replies_enabled", "1")
    btn_ai_label = f"💬 ОТВЕТЫ ИИ: {_onoff(ai_replies_val != '0')}"

    # Кнопки режима «Сам в разговор»: живые цифры (пауза/порог) и статус
    # НАМЕРЕННО только на кнопках, а не в тексте панели — регуляторы обновляют
    # панель дешёвым edit_message_reply_markup, не трогая текст (нет риска
    # упереться в лимит 4096 символов).
    proactive_on = get_setting("proactive_enabled", PROACTIVE_ENABLED_DEFAULT) == "1"
    btn_proactive_label = f"🗣 САМ В РАЗГОВОР: {_onoff(proactive_on)}"
    proactive_mm = _int_setting("proactive_min_msgs", PROACTIVE_MIN_MSGS)
    # proactive_ctx уже посчитан выше — для строки стенограммы в сводке
    # «Руки» — отдельным рядом, а не рядом с тумблером режима: это единственная
    # кнопка панели, которая даёт боту право наказывать живых людей.
    hands_on = get_setting("proactive_hands", PROACTIVE_HANDS_DEFAULT) == "1"
    btn_hands_label = f"🤚 РУКИ (мут до {PROACTIVE_MUTE_MAX_SEC // 60} мин): {_onoff(hands_on)}"

    # ── КНОПКИ УПРАВЛЕНИЯ ────────────────────────────────────────────────
    # Тумблеры промпта/ответов в один ряд, ниже — блок «Сам в разговор»
    # (тумблер и очистка стенограммы, отдельный ряд «руки», два ряда
    # регуляторов ➖/➕ — порог и контекст), в конце — возврат в /adm.
    keyboard = [
        [
            InlineKeyboardButton(btn_prompt_label, callback_data="toggle_admin_prompt"),
            InlineKeyboardButton(btn_ai_label, callback_data="toggle_ai_replies"),
        ],
        [
            InlineKeyboardButton(btn_proactive_label, callback_data="proactive:toggle"),
            InlineKeyboardButton("🧹Очистить РАЗГОВОРЫ", callback_data="proactive:wipe"),
        ],
        [InlineKeyboardButton(btn_hands_label, callback_data="proactive:hands")],
        [
            InlineKeyboardButton("➖ порог", callback_data="proactive:mm_dec"),
            InlineKeyboardButton(f"💬 {proactive_mm} сообщ.", callback_data="proactive:noop"),
            InlineKeyboardButton("➕ порог", callback_data="proactive:mm_inc"),
        ],
        [
            InlineKeyboardButton("➖ контекст", callback_data="proactive:ctx_dec"),
            InlineKeyboardButton(f"📜 {proactive_ctx} сообщ.", callback_data="proactive:noop"),
            InlineKeyboardButton("➕ контекст", callback_data="proactive:ctx_inc"),
        ],
        # Подробности участия — своим рядом сразу под регуляторами, которые
        # эти цифры и настраивают: посмотрел итог, тут же поправил порог.
        [InlineKeyboardButton("📊 Подробнее об участии", callback_data="proactive:stats")],
        [InlineKeyboardButton("📄 Показать полные PROMPTы", callback_data="prompts_files")],
        _adm_back_row(),
    ]

    # Последним делом — страховка от лимита Telegram: на обычных промптах
    # ничего не меняет, на длинных ужимает превью (см. _fit_panel_text).
    return _fit_panel_text(full_text), InlineKeyboardMarkup(keyboard)


# ─────────────────────────────────────────────
#  Кнопка «📄 Показать полные PROMPTы» (роутер: prompts_files)
# ─────────────────────────────────────────────

# Через сколько секунд присланные файлы промптов сами удаляются из чата.
_PROMPT_FILES_TTL = 600  # 10 минут (решение Максима 2026-07-20)


def _collect_prompt_files():
    """Собирает промпты для выгрузки файлами.

    Возвращает список кортежей (имя файла, подпись-заголовок, пометка
    «своя/заводская», команда замены, текст). ПУСТЫЕ промпты пропускаются —
    файл с одной строкой «(не задан)» пользы не даёт (решение Максима).
    """
    system_prompt, _, _ = get_active_system_prompt()
    news_prompt = get_news_system_prompt()
    rag_instruction = get_rag_instruction()
    proactive_instruction = get_proactive_instruction()
    media_prompt = get_proactive_media_prompt()

    # В файл SYSTEM PROMPT идёт ЦЕЛИКОМ — вместе с дополнениями /prompt_add
    # (в панели они показаны отдельным блоком). В файле должно быть ровно то,
    # что реально уходит модели, иначе файл будет врать.
    items = [
        ("1_SYSTEM_PROMPT.txt", "📝 SYSTEM PROMPT",
         "своя" if get_setting("custom_system_prompt", "").strip() else "заводская",
         "/prompt_set", system_prompt),
        ("2_NEWS_PROMPT.txt", "📰 NEWS PROMPT",
         "своя", "/news_prompt_set", news_prompt),
        ("3_RAG_PROMPT.txt", "🧠 RAG-PROMPT",
         "своя" if get_setting("rag_instruction", "").strip() else "заводская",
         "/rag_prompt_set", rag_instruction),
        ("4_PROACTIVE_PROMPT.txt", "🗣 PROMPT УЧАСТИЯ В РАЗГОВОРЕ (включает блок рук)",
         "своя" if get_setting("proactive_instruction", "").strip() else "заводская",
         "/proactive_prompt_set", proactive_instruction),
        # Заводского текста у промпта разбора медиа нет: он либо свой, либо
        # его нет вовсе — и тогда файл сюда не попадёт (пустые пропускаются).
        ("5_MEDIA_PROMPT.txt", "🖼 PROMPT РАЗБОРА МЕДИА (фото/голос/видео)",
         "своя", "/media_prompt_set", media_prompt),
    ]
    return [it for it in items if it[4] and it[4].strip()]


async def send_prompt_files(bot, chat_id: int, user_id: int) -> int:
    """Присылает полные тексты промптов отдельными .txt файлами.

    Зачем файлами: превью в панели обрезано (PROMPT_PREVIEW_MAX), а лимит
    Telegram 4096 символов не даёт показать длинный промпт целиком.

    ВАЖНО, не «чинить»:
    • файлы НЕ регистрируются в гигиене панелей (register_and_clean_bot_message) —
      иначе каждый следующий файл удалял бы предыдущий, и остался бы только один,
      да ещё и сама панель промптов исчезла бы;
    • внутри файла ТОЛЬКО текст промпта, без шапок — файл задуман обратно
      загружаемым: Reply на него командой /prompt_set (и т.п.) заменяет промпт.
      Любая служебная строка внутри вклеилась бы прямо в промпт;
    • вся справка (счётчик, «своя/заводская», команда замены) — в ПОДПИСИ к файлу.

    Возвращает число отправленных файлов.
    """
    sent = 0
    for fname, title, origin, cmd, text in _collect_prompt_files():
        try:
            msg = await bot.send_document(
                chat_id=chat_id,
                document=text.encode("utf-8"),
                filename=fname,
                caption=(
                    f"{html.escape(title)} · <code>{len(text)}</code> символов · <i>{origin}</i>\n"
                    f"✏️ Заменить: Reply на этот файл командой {cmd}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось отправить файл промпта %s: %s", fname, e)
            continue
        sent += 1
        if msg:
            # Самоудаление: файлы копились бы в чате, а гигиеной панелей
            # их убирать нельзя (см. выше).
            schedule_delete(bot, chat_id, msg.message_id, _PROMPT_FILES_TTL)

    logger.info("🔧 Админ %s запросил полные промпты — отправлено файлов: %d", user_id, sent)
    return sent


async def cmd_prompt_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Дописывает правила/инструкции к текущему промпту."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    if not context.args:
        # Через регистрацию в уборке: иначе подсказка зависнет в чате навсегда
        sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
            "➕ <b>Дополнение системного промпта</b>\n\n"
            "Напиши правило после команды:\n"
            "<code>/prompt_add Никогда не упоминай танк Т-90М</code>\n\n"
            "Дополнения накапливаются и добавляются в конец промпта.",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    addition_text = update.message.text.split(None, 1)[1]
    append_prompt_addition(addition_text)
    logger.info("🔧 Админ %s добавил дополнение к промпту (%d символов)", user_id, len(addition_text))

    safe_text = addition_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Кнопка возврата к панели промптов (её обрабатывает handle_callback_query → back_to_stats).
    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад к панели", callback_data="back_to_stats")
    ]])
    sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
        f"<b><u>✅ ДОПОЛНЕНИЕ ДОБАВЛЕНО!</u></b>\n\n"
        f"<b>➕ ДОПОЛНЕНИЯ:</b>\n"
        f"<blockquote expandable>{safe_text}</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb,
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def send_prompts_panel(bot, chat_id: int, user_id: int):
    """Панель «⚙️ НАСТРОЙКИ PROMPTов»: системный промпт, промпт новостей, тумблеры."""
    prompt_text, prompt_markup = _build_prompt_panel_text_and_keyboard(user_id, bot.username)
    text = (
        "⚙️ <b>НАСТРОЙКИ PROMPTов</b>\n"
        "───────────────────────────\n"
        + prompt_text
    )
    await _send_panel_message(bot, chat_id, text, prompt_markup)


# ─────────────────────────────────────────────
#  ПОДТВЕРЖДЕНИЕ СБРОСА ПРОМПТОВ (2026-08-03)
# ─────────────────────────────────────────────
#  Четыре промпта — системный, новостной, RAG-инструкция и инструкция
#  участия в разговоре — сбрасываются ОДИНАКОВО: стереть ключ (или два)
#  в settings → попап → переписать сообщение с подтверждением. Различаются
#  только имена ключей и тексты.
#
#  До 2026-08-03 это лежало в router.py восемью почти дословно
#  повторяющимися ветками на ~110 строк: логика (попап, edit_message_text,
#  try/except, строка лога) была скопирована восемь раз. Теперь тексты —
#  в таблице ниже, логика — одна, в handle_prompt_reset.
#
#  ⚠️ ДИСПЕТЧЕР В РОУТЕРЕ ОСТАЛСЯ СПИСКОМ ЛИТЕРАЛОВ, и это НЕ небрежность,
#  которую надо «причесать»: preflight.py::_router_patterns читает router.py
#  разбором ast и ищет ровно `data == "…"`, `data in (…)` и
#  `data.startswith("…")`. Спрячешь восемь callback'ов за вычисляемый ключ —
#  проверка «кнопки ↔ роутер» перестанет их видеть и объявит все восемь
#  необработанными.
#
#  Разъезд списка с таблицей ловится с двух сторон и молча не проходит:
#  забыл вписать callback в роутер — его поймает та самая проверка
#  «кнопки ↔ роутер» (кнопка в панели есть, ветки нет); попал в роутер
#  лишний callback без записи в таблице — сработает ветка «кнопка устарела»
#  в handle_prompt_reset, с предупреждением в лог.
_PROMPTS = {
    # ключ = приставка callback'ов «<приставка>_reset_confirm/cancel»
    "prompt": {
        # ── команда /prompt_set ──
        "set_key": "custom_system_prompt",
        "file_what": "промпта",
        "set_log":  "установил кастомный системный промпт",
        "set_done": "✅ <b>Системный промпт заменён!</b>",
        "set_note": "",
        "set_usage": "✏️ <b>Замена системного промпта</b>\n\n"
                     "<b>Способ 1:</b> Напиши текст после команды:\n"
                     "<code>/prompt_set Ты — дружелюбный бот...</code>\n\n"
                     "<b>Способ 2:</b> Отправь <code>.txt</code> файл с промптом, "
                     "затем ответь (Reply) на него командой <code>/prompt_set</code>",
        # ── команда /prompt_reset ──
        # ЕДИНСТВЕННЫЙ промпт из двух ключей: свой текст и дополнения /prompt_add.
        # Поэтому подтверждение перечисляет их списком, а не одной длиной.
        "reset_reader": lambda: [("Кастомный промпт", get_setting("custom_system_prompt", "")),
                                 ("Дополнения", get_setting("prompt_additions", ""))],
        "reset_btn": "✅ Да, сбросить",
        "reset_empty": "ℹ️ <b>Сброс не требуется.</b>\n\n"
                       "Бот уже использует заводской промпт из <code>config.py</code>.\n"
                       "Кастомных изменений и дополнений не обнаружено.",
        "reset_body": "🔄 <b>Сброс системного промпта</b>\n\n"
                      "Будет удалено:\n{details}\n"
                      "Бот вернётся к заводскому промпту из <code>config.py</code>.\n\n"
                      "⚠️ <b>Это действие нельзя отменить. Подтвердить?</b>",
        "keys":   ("custom_system_prompt", "prompt_additions"),
        "what":   "промпта",
        "log":    "сбросил системный промпт к заводским настройкам",
        "popup":  "✅ Промпт сброшен к заводским настройкам!",
        "done":   "✅ <b>Системный промпт сброшен!</b>\n\n"
                  "Кастомный промпт и все дополнения удалены.\n"
                  "Бот снова использует заводской промпт из <code>config.py</code>.",
        "cancel": "🔄 <b>Сброс промпта отменён.</b>\n\nТекущий промпт остался без изменений.",
    },
    "news_prompt": {
        "set_key": "news_system_prompt",
        "file_what": "промпта новостей",
        "set_log":  "установил промпт новостей",
        "set_done": "✅ <b>Промпт новостей установлен!</b>",
        "set_note": "",
        "set_usage": "📰 <b>Промпт для форматирования новостей</b>\n\n"
                     "Этот промпт отправляется ИИ как системная инструкция при каждом "
                     "форматировании новости.\n\n"
                     "<b>Способ 1:</b> Напиши текст после команды:\n"
                     "<code>/news_prompt_set Ты — военный корреспондент C4_Max...</code>\n\n"
                     "<b>Способ 2:</b> Отправь <code>.txt</code> файл с промптом, "
                     "затем ответь (Reply) на него командой <code>/news_prompt_set</code>",
        "reset_reader": lambda: [(None, get_news_system_prompt())],
        "reset_btn": "✅ Да, удалить",
        "reset_empty": "ℹ️ <b>Промпт новостей не задан.</b>\n\n"
                       "Нечего удалять — новости уже форматируются без системного промпта.",
        "reset_body": "🗑️ <b>Удаление промпта новостей</b>\n\n"
                      "Текущий промпт ({length} символов) будет удалён.\n"
                      "Новости начнут форматироваться без системного промпта.\n\n"
                      "⚠️ <b>Подтвердить?</b>",
        "keys":   ("news_system_prompt",),
        "what":   "промпта новостей",
        "log":    "удалил промпт новостей",
        "popup":  "✅ Промпт новостей удалён!",
        "done":   "✅ <b>Промпт новостей удалён!</b>\n\n"
                  "Новости теперь форматируются без системного промпта.",
        "cancel": "🔄 <b>Сброс промпта новостей отменён.</b>\n\nТекущий промпт остался без изменений.",
    },
    # Своя инструкция стирается, а не заменяется текстом: get_rag_instruction
    # и get_proactive_instruction сами вернутся к заводской из config.py.
    "rag_prompt": {
        "set_key": "rag_instruction",
        "file_what": "RAG-инструкции",
        "set_log":  "изменил RAG-инструкцию",
        "set_done": "✅ <b>RAG-инструкция обновлена!</b>",
        "set_note": "\n\nℹ️ Найденные статьи по-прежнему подставляются автоматически "
                    "под этой инструкцией.",
        "set_usage": "🧠 <b>RAG-инструкция</b>\n\n"
                     "Это «шапка», которая уходит модели ПЕРЕД найденными статьями базы "
                     "знаний. Сами статьи бот всегда подставляет под ней сам — их писать "
                     "не нужно.\n\n"
                     "<b>Способ 1:</b> Напиши текст после команды:\n"
                     "<code>/rag_prompt_set Ты эксперт по технике War Thunder Mobile...</code>\n\n"
                     "<b>Способ 2:</b> Отправь <code>.txt</code> файл с текстом, "
                     "затем ответь (Reply) на него командой <code>/rag_prompt_set</code>\n\n"
                     "🗑️ Вернуть заводскую — /rag_prompt_reset",
        "reset_reader": lambda: [(None, get_setting("rag_instruction", "").strip())],
        "reset_btn": "✅ Да, вернуть заводскую",
        "reset_empty": "ℹ️ <b>Сейчас уже действует заводская RAG-инструкция.</b>\n\n"
                       "Своя не задана — возвращать нечего.",
        "reset_body": "🗑️ <b>Возврат заводской RAG-инструкции</b>\n\n"
                      "Твоя инструкция ({length} символов) будет удалена, "
                      "вернётся заводская.\n\n"
                      "⚠️ <b>Подтвердить?</b>",
        "keys":   ("rag_instruction",),
        "what":   "RAG-инструкции",
        "log":    "вернул заводскую RAG-инструкцию",
        "popup":  "✅ Заводская RAG-инструкция возвращена!",
        "done":   "✅ <b>Заводская RAG-инструкция возвращена.</b>\n\n"
                  "Своя удалена — модель снова получает стандартную «шапку» перед статьями.",
        "cancel": "🔄 <b>Возврат заводской RAG-инструкции отменён.</b>\n\nТвоя инструкция осталась без изменений.",
    },
    "proactive_prompt": {
        "set_key": "proactive_instruction",
        "file_what": "инструкции участия",
        "set_log":  "изменил инструкцию участия в разговоре",
        "set_done": "✅ <b>Инструкция участия в разговоре обновлена!</b>",
        "set_note": "\n\nℹ️ Стенограмма чата по-прежнему подставляется автоматически "
                    "под этой инструкцией.",
        "set_usage": "🗣 <b>Инструкция участия в разговоре</b>\n\n"
                     "По ней бот сам решает, вступить ли в беседу группы, когда к нему "
                     "НЕ обращались (режим «Сам в разговор»). Стенограмму чата бот "
                     "подставляет под ней сам — её писать не нужно.\n\n"
                     "<b>Способ 1:</b> Напиши текст после команды:\n"
                     "<code>/proactive_prompt_set Вступай, только если можешь пошутить...</code>\n\n"
                     "<b>Способ 2:</b> Отправь <code>.txt</code> файл с текстом, "
                     "затем ответь (Reply) на него командой <code>/proactive_prompt_set</code>\n\n"
                     "🗑️ Вернуть заводскую — /proactive_prompt_reset",
        "reset_reader": lambda: [(None, get_setting("proactive_instruction", "").strip())],
        "reset_btn": "✅ Да, вернуть заводскую",
        "reset_empty": "ℹ️ <b>Сейчас уже действует заводская инструкция участия в разговоре.</b>\n\n"
                       "Своя не задана — возвращать нечего.",
        "reset_body": "🗑️ <b>Возврат заводской инструкции участия в разговоре</b>\n\n"
                      "Твоя инструкция ({length} символов) будет удалена, "
                      "вернётся заводская.\n\n"
                      "⚠️ <b>Подтвердить?</b>",
        "keys":   ("proactive_instruction",),
        "what":   "инструкции участия",
        "log":    "вернул заводскую инструкцию участия в разговоре",
        "popup":  "✅ Заводская инструкция участия возвращена!",
        "done":   "✅ <b>Заводская инструкция участия в разговоре возвращена.</b>\n\n"
                  "Своя удалена — режим «Сам в разговор» снова работает по стандартным правилам.",
        "cancel": "🔄 <b>Возврат заводской инструкции участия отменён.</b>\n\nТвоя инструкция осталась без изменений.",
    },
    # Промпт разбора медиа (2026-08-05). Ведёт себя как промпт НОВОСТЕЙ,
    # а не как две инструкции выше: заводского текста нет вовсе, поэтому
    # сброс тут именно УДАЛЯЕТ промпт — модель снова получает голый файл.
    "media_prompt": {
        "set_key": "proactive_media_prompt",
        "file_what": "промпта разбора медиа",
        "set_log":  "задал промпт разбора медиа",
        "set_done": "✅ <b>Промпт разбора медиа установлен!</b>",
        "set_note": "\n\nℹ️ Действует сразу и на фото, и на голосовые, и на видео "
                    "в режиме «Сам в разговор». В личных ответах бота не участвует.",
        "set_usage": "🖼 <b>Промпт разбора медиа</b>\n\n"
                     "Уходит вспомогательной модели (<code>gemini-3.1-flash-lite</code>) "
                     "ВМЕСТЕ с фото, голосовым или видео из группы, когда бот решает, "
                     "вступать ли в разговор. Её разбор попадает в стенограмму беседы "
                     "вместо вложения — по нему активная модель и понимает, что прислали.\n\n"
                     "Промпт ОДИН на все три вида медиа. Не задан — модель получает "
                     "голый файл без единого слова и отвечает как хочет "
                     "(может ответить на другом языке, списком или советами).\n\n"
                     "<b>Способ 1:</b> Напиши текст после команды:\n"
                     "<code>/media_prompt_set Опиши одним предложением по-русски, что на "
                     "картинке. Без советов и списков.</code>\n\n"
                     "<b>Способ 2:</b> Отправь <code>.txt</code> файл с текстом, "
                     "затем ответь (Reply) на него командой <code>/media_prompt_set</code>\n\n"
                     "🗑️ Удалить промпт — /media_prompt_reset",
        "reset_reader": lambda: [(None, get_proactive_media_prompt())],
        "reset_btn": "✅ Да, удалить",
        "reset_empty": "ℹ️ <b>Промпт разбора медиа не задан.</b>\n\n"
                       "Нечего удалять — вспомогательная модель уже получает только файл.",
        "reset_body": "🗑️ <b>Удаление промпта разбора медиа</b>\n\n"
                      "Текущий промпт ({length} символов) будет удалён.\n"
                      "Фото, голосовые и видео снова уйдут модели голым файлом, "
                      "без единого слова от бота.\n\n"
                      "⚠️ <b>Подтвердить?</b>",
        "keys":   ("proactive_media_prompt",),
        "what":   "промпта разбора медиа",
        "log":    "удалил промпт разбора медиа",
        "popup":  "✅ Промпт разбора медиа удалён!",
        "done":   "✅ <b>Промпт разбора медиа удалён!</b>\n\n"
                  "Фото, голосовые и видео снова уходят вспомогательной модели "
                  "голым файлом.",
        "cancel": "🔄 <b>Удаление промпта разбора медиа отменено.</b>\n\nПромпт остался без изменений.",
    },
}

# Все восемь callback'ов таблицы одной строкой — для проверок и подсказки
# читателю. В САМ РОУТЕР их подставлять нельзя (см. предупреждение выше).
_PROMPT_RESET_CALLBACKS = tuple(
    f"{name}_reset_{action}"
    for name in _PROMPTS
    for action in ("confirm", "cancel")
)


async def handle_prompt_reset(query, user_id: int, data: str):
    """
    Кнопки «✅ Да, сбросить» / «❌ Отмена» под вопросом о сбросе промпта.
    Одна на все четыре промпта: что стереть и что написать — в _PROMPTS.

    На callback отвечает РОВНО ОДИН раз (правило карты про кнопки карточки
    действует и здесь): попап здесь, вызывающая ветка роутера молчит.
    """
    name, _, action = data.rpartition("_reset_")
    spec = _PROMPTS.get(name)
    if spec is None:
        # Кнопка из старого сообщения на промпт, которого больше нет в таблице.
        # Молчать нельзя — кнопка будет выглядеть зависшей.
        logger.warning("⚠️ Неизвестная кнопка сброса промпта: %s", data)
        await query.answer("Кнопка устарела — открой панель промптов заново.", show_alert=True)
        return

    if action == "confirm":
        for key in spec["keys"]:
            set_setting(key, "")
        logger.info("🔧 Админ %s %s", user_id, spec["log"])
        await query.answer(spec["popup"], show_alert=True)
        text, whose = spec["done"], f"сброса {spec['what']}"
    else:
        await query.answer("Отменено.", show_alert=False)
        text, whose = spec["cancel"], f"отмены сброса {spec['what']}"

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("⚠️ Не удалось обновить сообщение %s: %s", whose, e)


# ─────────────────────────────────────────────
#  КОМАНДЫ УПРАВЛЕНИЯ ПРОМПТАМИ (2026-08-04)
# ─────────────────────────────────────────────
#  Четыре промпта правятся одинаково: /X_prompt_set задаёт текст (аргументом
#  команды или .txt-файлом через Reply), /X_prompt_reset спрашивает
#  подтверждение и вешает кнопки, которые обслуживает handle_prompt_reset.
#
#  До 2026-08-04 это были ВОСЕМЬ отдельных функций на 455 строк, совпадавших
#  каркасом на 78–95%: различались имя ключа и тексты сообщений. Теперь тексты
#  живут в таблице _PROMPTS (там же, где тексты кнопок подтверждения), а логика
#  одна — _prompt_set_command и _prompt_reset_command. Команды остались
#  отдельными именами: их регистрирует handlers/__init__.py, и PTB зовёт
#  каждую по своему CommandHandler.
#
#  ⚠️ Пятый промпт заводится ОДНОЙ записью в _PROMPTS плюс парой тонких
#  обёрток ниже — и не забыть зарегистрировать команды в handlers/__init__.py
#  и добавить их в re-export handlers/admin/__init__.py.


async def _prompt_send(context, chat_id: int, text: str, reply_markup=None):
    """
    Ответ команды промпта. ВСЕГДА через гигиену панелей: подсказки и
    подтверждения — служебные сообщения, без регистрации они копились бы
    в чате навсегда (так было задумано с самого начала во всех восьми
    командах, здесь это правило записано один раз).
    """
    sent_msg = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def _prompt_text_from_message(update, context, spec) -> tuple:
    """
    Достаёт новый текст промпта из команды: аргументом после команды ИЛИ
    из .txt-файла, на который сделан Reply. Возвращает (текст, ошибка_прочтения).

    Файл читается только с расширением .txt и только в UTF-8 — так было
    во всех четырёх командах; чужую кодировку ловим и честно об этом пишем.
    """
    message = update.message
    if context.args:
        return message.text.split(None, 1)[1], False

    reply = message.reply_to_message
    if reply and reply.document:
        doc = reply.document
        if doc.file_name and doc.file_name.endswith(".txt"):
            try:
                file = await context.bot.get_file(doc.file_id)
                file_bytes = await file.download_as_bytearray()
                return file_bytes.decode("utf-8").strip(), False
            except Exception as e:
                logger.error("⚠️ Не удалось прочитать файл %s: %s", spec["file_what"], e)
                return "", True
    return "", False


async def _prompt_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str):
    """
    Общий исполнитель команд /X_prompt_set. Что именно писать и куда —
    в записи `name` таблицы _PROMPTS.
    """
    await delete_user_message_safe(update.message)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    spec = _PROMPTS[name]

    if not await _require(update, context, "owner"):
        return
    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    new_prompt, read_failed = await _prompt_text_from_message(update, context, spec)
    if read_failed:
        await _prompt_send(context, chat_id,
                           "❌ <b>Ошибка при чтении файла.</b> Убедись, что это текстовый "
                           ".txt файл в кодировке UTF-8.")
        return
    if not new_prompt:
        await _prompt_send(context, chat_id, spec["set_usage"])
        return

    set_setting(spec["set_key"], new_prompt)
    logger.info("🔧 Админ %s %s (%d символов)", user_id, spec["set_log"], len(new_prompt))

    # Превью первых 300 символов: чужой текст в HTML-панели обязателен
    # к экранированию — «<» в промпте иначе сломает отправку.
    preview = new_prompt[:300].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(new_prompt) > 300:
        preview += "..."
    await _prompt_send(context, chat_id,
                       f"{spec['set_done']}\n\n"
                       f"📊 Длина: {len(new_prompt)} символов\n\n"
                       f"<b>Превью:</b>\n{preview}{spec['set_note']}")


async def _prompt_reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str):
    """
    Общий исполнитель команд /X_prompt_reset: спрашивает подтверждение и вешает
    две кнопки. Само стирание делает handle_prompt_reset по той же таблице.
    Нечего сбрасывать — говорим об этом и кнопок не показываем.
    """
    await delete_user_message_safe(update.message)
    chat_id = update.effective_chat.id
    spec = _PROMPTS[name]

    if not await _require(update, context, "owner"):
        return
    if _is_group_chat(update):
        return

    filled = [(label, text) for label, text in spec["reset_reader"]() if text]
    if not filled:
        await _prompt_send(context, chat_id, spec["reset_empty"])
        return

    details = "".join(f"• {label} ({len(text)} символов)\n" for label, text in filled if label)
    body = spec["reset_body"].format(details=details, length=len(filled[0][1]))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(spec["reset_btn"], callback_data=f"{name}_reset_confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"{name}_reset_cancel"),
    ]])
    await _prompt_send(context, chat_id, body, keyboard)


# ── Тонкие обёртки: их регистрирует handlers/__init__.py ──────────────
async def cmd_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полностью заменяет системный промпт на кастомный."""
    await _prompt_set_command(update, context, "prompt")


async def cmd_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает системный промпт к заводским настройкам (с подтверждением)."""
    await _prompt_reset_command(update, context, "prompt")


async def cmd_news_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Задаёт системный промпт форматирования новостей."""
    await _prompt_set_command(update, context, "news_prompt")


async def cmd_news_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет промпт новостей (с подтверждением)."""
    await _prompt_reset_command(update, context, "news_prompt")


async def cmd_rag_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Задаёт «шапку»-инструкцию, уходящую модели перед статьями RAG.
    Хранится в settings под ключом 'rag_instruction'. Сами статьи бот всегда
    подставляет под инструкцией автоматически."""
    await _prompt_set_command(update, context, "rag_prompt")


async def cmd_rag_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает заводскую RAG-инструкцию (удаляет свою, с подтверждением)."""
    await _prompt_reset_command(update, context, "rag_prompt")


async def cmd_proactive_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Задаёт инструкцию участия в разговоре (режим «Сам в разговор»)."""
    await _prompt_set_command(update, context, "proactive_prompt")


async def cmd_proactive_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает заводскую инструкцию участия (с подтверждением)."""
    await _prompt_reset_command(update, context, "proactive_prompt")


async def cmd_media_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Задаёт промпт разбора медиа (фото/голос/видео) для режима «Сам в разговор».
    Хранится в settings под ключом 'proactive_media_prompt'; уходит
    вспомогательной модели вместе с самим файлом."""
    await _prompt_set_command(update, context, "media_prompt")


async def cmd_media_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет промпт разбора медиа (с подтверждением) — модель снова получает
    голый файл, как было до 2026-08-05."""
    await _prompt_reset_command(update, context, "media_prompt")
