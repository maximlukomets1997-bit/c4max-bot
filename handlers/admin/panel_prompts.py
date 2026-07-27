# ───────────────────────────────────────────────
#  handlers/admin/panel_prompts.py — панель «⚙️ НАСТРОЙКИ PROMPTов» и команды /prompt_*, /news_prompt_*, /rag_prompt_*.
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import json
import logging
import os

import logging_setup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, ADMIN_IDS, GEMINI_MODEL
from config import PROACTIVE_ENABLED_DEFAULT, PROACTIVE_MIN_MSGS, PROACTIVE_CONTEXT_MSGS
from config import PROACTIVE_HANDS_DEFAULT, PROACTIVE_MUTE_MAX_SEC
from config import PROACTIVE_OFF_ANNOUNCE, PROACTIVE_OFF_MSGS_KEY
from database.history import set_setting, get_setting, append_prompt_addition, get_active_system_prompt, get_bot_stats, get_news_system_prompt, get_rag_instruction, get_proactive_instruction, get_known_chats
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
    # Дубликат кнопки живёт и в главной панели (временно, для тестов), поэтому
    # у подтверждения свои callback'и: по ним ветка узнаёт, КУДА возвращать
    # клавиатуру — в панель промптов или в /adm.
    prefix = "adm" if from_adm else "proactive"

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
        [InlineKeyboardButton("📄 Показать полные PROMPTы", callback_data="prompts_files")],
        _adm_back_row(),
    ]

    return full_text, InlineKeyboardMarkup(keyboard)


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


async def cmd_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Полностью заменяет системный промпт на кастомный."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    # Получаем текст после команды
    new_prompt = ""
    if context.args:
        new_prompt = update.message.text.split(None, 1)[1]  # Всё после /prompt_set

    # Проверяем, не приложен ли .txt файл (reply на документ)
    if not new_prompt and update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        if doc.file_name and doc.file_name.endswith(".txt"):
            try:
                file = await context.bot.get_file(doc.file_id)
                file_bytes = await file.download_as_bytearray()
                new_prompt = file_bytes.decode("utf-8").strip()
            except Exception as e:
                logger.error("⚠️ Не удалось прочитать файл промпта: %s", e)
                # Через регистрацию в уборке: иначе сообщение зависнет в чате навсегда
                sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
                    "❌ <b>Ошибка при чтении файла.</b> Убедись, что это текстовый .txt файл в кодировке UTF-8.",
                    parse_mode=ParseMode.HTML
                )
                if sent_msg:
                    await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
                return

    if not new_prompt:
        # Через регистрацию в уборке: иначе подсказка зависнет в чате навсегда
        sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
            "✏️ <b>Замена системного промпта</b>\n\n"
            "<b>Способ 1:</b> Напиши текст после команды:\n"
            "<code>/prompt_set Ты — дружелюбный бот...</code>\n\n"
            "<b>Способ 2:</b> Отправь <code>.txt</code> файл с промптом, "
            "затем ответь (Reply) на него командой <code>/prompt_set</code>",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    set_setting("custom_system_prompt", new_prompt)
    logger.info("🔧 Админ %s установил кастомный системный промпт (%d символов)", user_id, len(new_prompt))

    # Показываем первые 300 символов для подтверждения
    preview = new_prompt[:300].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(new_prompt) > 300:
        preview += "..."

    sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
        f"✅ <b>Системный промпт заменён!</b>\n\n"
        f"📊 Длина: {len(new_prompt)} символов\n\n"
        f"<b>Превью:</b>\n{preview}",
        parse_mode=ParseMode.HTML
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


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



async def cmd_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Сбрасывает промпт к заводским настройкам (с подтверждением)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    # Проверяем, есть ли вообще что сбрасывать
    custom = get_setting("custom_system_prompt", "")
    additions = get_setting("prompt_additions", "")

    if not custom and not additions:
        sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
            "ℹ️ <b>Сброс не требуется.</b>\n\n"
            "Бот уже использует заводской промпт из <code>config.py</code>.\n"
            "Кастомных изменений и дополнений не обнаружено.",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    # Формируем информацию о том, что будет удалено
    details = ""
    if custom:
        details += f"• Кастомный промпт ({len(custom)} символов)\n"
    if additions:
        details += f"• Дополнения ({len(additions)} символов)\n"

    keyboard = [
        [
            InlineKeyboardButton("✅ Да, сбросить", callback_data="prompt_reset_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="prompt_reset_cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
        f"🔄 <b>Сброс системного промпта</b>\n\n"
        f"Будет удалено:\n{details}\n"
        f"Бот вернётся к заводскому промпту из <code>config.py</code>.\n\n"
        f"⚠️ <b>Это действие нельзя отменить. Подтвердить?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_news_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Устанавливает системный промпт для форматирования новостей."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    new_prompt = ""
    if context.args:
        new_prompt = update.message.text.split(None, 1)[1]

    # Поддержка .txt файла через Reply
    if not new_prompt and update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        if doc.file_name and doc.file_name.endswith(".txt"):
            try:
                file = await context.bot.get_file(doc.file_id)
                file_bytes = await file.download_as_bytearray()
                new_prompt = file_bytes.decode("utf-8").strip()
            except Exception as e:
                logger.error("⚠️ Не удалось прочитать файл промпта новостей: %s", e)
                # Через регистрацию в уборке: иначе сообщение зависнет в чате навсегда
                sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
                    "❌ <b>Ошибка при чтении файла.</b> Убедись, что это текстовый .txt файл в кодировке UTF-8.",
                    parse_mode=ParseMode.HTML
                )
                if sent_msg:
                    await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
                return

    if not new_prompt:
        sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
            "📰 <b>Промпт для форматирования новостей</b>\n\n"
            "Этот промпт отправляется ИИ как системная инструкция при каждом форматировании новости.\n\n"
            "<b>Способ 1:</b> Напиши текст после команды:\n"
            "<code>/news_prompt_set Ты — военный корреспондент C4_Max...</code>\n\n"
            "<b>Способ 2:</b> Отправь <code>.txt</code> файл с промптом, "
            "затем ответь (Reply) на него командой <code>/news_prompt_set</code>",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    set_setting("news_system_prompt", new_prompt)
    logger.info("🔧 Админ %s установил промпт новостей (%d символов)", user_id, len(new_prompt))

    preview = new_prompt[:300].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(new_prompt) > 300:
        preview += "..."

    sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
        f"✅ <b>Промпт новостей установлен!</b>\n\n"
        f"📊 Длина: {len(new_prompt)} символов\n\n"
        f"<b>Превью:</b>\n{preview}",
        parse_mode=ParseMode.HTML
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_news_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Удаляет системный промпт для новостей (с подтверждением)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    current = get_news_system_prompt()
    if not current:
        sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
            "ℹ️ <b>Промпт новостей не задан.</b>\n\n"
            "Нечего удалять — новости уже форматируются без системного промпта.",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data="news_prompt_reset_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="news_prompt_reset_cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
        f"🗑️ <b>Удаление промпта новостей</b>\n\n"
        f"Текущий промпт ({len(current)} символов) будет удалён.\n"
        f"Новости начнут форматироваться без системного промпта.\n\n"
        f"⚠️ <b>Подтвердить?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_rag_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Задаёт «шапку»-инструкцию, уходящую модели перед статьями RAG.
    Хранится в settings под ключом 'rag_instruction'. Сами статьи бот всегда
    подставляет под инструкцией автоматически."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    new_prompt = ""
    if context.args:
        new_prompt = update.message.text.split(None, 1)[1]

    # Поддержка .txt файла через Reply (как у системного промпта и новостей)
    if not new_prompt and update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        if doc.file_name and doc.file_name.endswith(".txt"):
            try:
                file = await context.bot.get_file(doc.file_id)
                file_bytes = await file.download_as_bytearray()
                new_prompt = file_bytes.decode("utf-8").strip()
            except Exception as e:
                logger.error("⚠️ Не удалось прочитать файл RAG-инструкции: %s", e)
                # Через регистрацию в уборке: иначе сообщение зависнет в чате навсегда
                sent_msg = await context.bot.send_message(chat_id=chat_id, text=
                    "❌ <b>Ошибка при чтении файла.</b> Убедись, что это текстовый .txt файл в кодировке UTF-8.",
                    parse_mode=ParseMode.HTML
                )
                if sent_msg:
                    await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
                return

    if not new_prompt:
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=
            "🧠 <b>RAG-инструкция</b>\n\n"
            "Это «шапка», которая уходит модели ПЕРЕД найденными статьями базы "
            "знаний. Сами статьи бот всегда подставляет под ней сам — их писать "
            "не нужно.\n\n"
            "<b>Способ 1:</b> Напиши текст после команды:\n"
            "<code>/rag_prompt_set Ты эксперт по технике War Thunder Mobile...</code>\n\n"
            "<b>Способ 2:</b> Отправь <code>.txt</code> файл с текстом, "
            "затем ответь (Reply) на него командой <code>/rag_prompt_set</code>\n\n"
            "🗑️ Вернуть заводскую — /rag_prompt_reset",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    set_setting("rag_instruction", new_prompt)
    logger.info("🔧 Админ %s изменил RAG-инструкцию (%d символов)", user_id, len(new_prompt))

    preview = new_prompt[:300].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(new_prompt) > 300:
        preview += "..."

    sent_msg = await context.bot.send_message(chat_id=chat_id, text=
        f"✅ <b>RAG-инструкция обновлена!</b>\n\n"
        f"📊 Длина: {len(new_prompt)} символов\n\n"
        f"<b>Превью:</b>\n{preview}\n\n"
        f"ℹ️ Найденные статьи по-прежнему подставляются автоматически под этой инструкцией.",
        parse_mode=ParseMode.HTML
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_rag_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Возвращает заводскую RAG-инструкцию (удаляет свою, с подтверждением)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    # Своя инструкция задана? Если нет — уже действует заводская, сбрасывать нечего.
    current = get_setting("rag_instruction", "").strip()
    if not current:
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=
            "ℹ️ <b>Сейчас уже действует заводская RAG-инструкция.</b>\n\n"
            "Своя не задана — возвращать нечего.",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Да, вернуть заводскую", callback_data="rag_prompt_reset_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="rag_prompt_reset_cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent_msg = await context.bot.send_message(chat_id=chat_id, text=
        f"🗑️ <b>Возврат заводской RAG-инструкции</b>\n\n"
        f"Твоя инструкция ({len(current)} символов) будет удалена, "
        f"вернётся заводская.\n\n"
        f"⚠️ <b>Подтвердить?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_proactive_prompt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Задаёт инструкцию участия в разговоре групп (режим «Сам в разговор»).
    Хранится в settings под ключом 'proactive_instruction'. По ней модель
    решает, вступить ли в беседу без обращения к боту (services/proactive.py)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    new_prompt = ""
    if context.args:
        new_prompt = update.message.text.split(None, 1)[1]

    # Поддержка .txt файла через Reply (как у системного промпта и RAG)
    if not new_prompt and update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        if doc.file_name and doc.file_name.endswith(".txt"):
            try:
                file = await context.bot.get_file(doc.file_id)
                file_bytes = await file.download_as_bytearray()
                new_prompt = file_bytes.decode("utf-8").strip()
            except Exception as e:
                logger.error("⚠️ Не удалось прочитать файл инструкции участия: %s", e)
                # Через регистрацию в уборке: иначе сообщение зависнет в чате навсегда
                sent_msg = await context.bot.send_message(chat_id=chat_id, text=
                    "❌ <b>Ошибка при чтении файла.</b> Убедись, что это текстовый .txt файл в кодировке UTF-8.",
                    parse_mode=ParseMode.HTML
                )
                if sent_msg:
                    await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
                return

    if not new_prompt:
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=
            "🗣 <b>Инструкция участия в разговоре</b>\n\n"
            "По ней бот сам решает, вступить ли в беседу группы, когда к нему "
            "НЕ обращались (режим «Сам в разговор»). Стенограмму чата бот "
            "подставляет под ней сам — её писать не нужно.\n\n"
            "<b>Способ 1:</b> Напиши текст после команды:\n"
            "<code>/proactive_prompt_set Вступай, только если можешь пошутить...</code>\n\n"
            "<b>Способ 2:</b> Отправь <code>.txt</code> файл с текстом, "
            "затем ответь (Reply) на него командой <code>/proactive_prompt_set</code>\n\n"
            "🗑️ Вернуть заводскую — /proactive_prompt_reset",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    set_setting("proactive_instruction", new_prompt)
    logger.info("🔧 Админ %s изменил инструкцию участия в разговоре (%d символов)", user_id, len(new_prompt))

    preview = new_prompt[:300].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(new_prompt) > 300:
        preview += "..."

    sent_msg = await context.bot.send_message(chat_id=chat_id, text=
        f"✅ <b>Инструкция участия в разговоре обновлена!</b>\n\n"
        f"📊 Длина: {len(new_prompt)} символов\n\n"
        f"<b>Превью:</b>\n{preview}\n\n"
        f"ℹ️ Стенограмма чата по-прежнему подставляется автоматически под этой инструкцией.",
        parse_mode=ParseMode.HTML
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_proactive_prompt_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    """Возвращает заводскую инструкцию участия в разговоре (с подтверждением)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    # Только личка: в группе молча выходим (команда уже удалена).
    if _is_group_chat(update):
        return

    # Своя инструкция задана? Если нет — уже действует заводская, сбрасывать нечего.
    current = get_setting("proactive_instruction", "").strip()
    if not current:
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=
            "ℹ️ <b>Сейчас уже действует заводская инструкция участия в разговоре.</b>\n\n"
            "Своя не задана — возвращать нечего.",
            parse_mode=ParseMode.HTML
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Да, вернуть заводскую", callback_data="proactive_prompt_reset_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="proactive_prompt_reset_cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent_msg = await context.bot.send_message(chat_id=chat_id, text=
        f"🗑️ <b>Возврат заводской инструкции участия в разговоре</b>\n\n"
        f"Твоя инструкция ({len(current)} символов) будет удалена, "
        f"вернётся заводская.\n\n"
        f"⚠️ <b>Подтвердить?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)