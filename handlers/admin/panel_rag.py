# ───────────────────────────────────────────────
#  handlers/admin/panel_rag.py — база знаний: панель /rag, карточки статей, приём файлов, режим «🔍 Проверить поиск».
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import logging
import os
import re

import logging_setup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, ADMIN_IDS, GEMINI_MODEL
from database.history import set_setting, get_setting, append_prompt_addition, get_active_system_prompt, get_bot_stats, get_news_system_prompt, get_rag_instruction
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete


logger = logging.getLogger(__name__)
from .common import _adm_back_row, _fmt_mod_time, _is_group_chat, _onoff, _reject_non_admin, _require




# ─────────────────────────────────────────────
#  Панель базы знаний RAG (/rag и кнопка в /adm)
#
#  Список статей: 🕐 — ждут одобрения (папка pending), ✅ — в базе (approved).
#  Имена файлов не влезают в callback_data (лимит Telegram 64 байта), поэтому
#  в кнопки кладутся короткие номера-токены, а соответствие токен → (папка,
#  файл) живёт в bot_data["kb_file_map"] и пересобирается при каждом
#  открытии панели. После перезапуска бота токены из старых сообщений
#  протухают — обработчик тогда просто заново открывает панель.
# ─────────────────────────────────────────────

_KB_LIST_LIMIT = 81  # максимум статей-кнопок: потолок Telegram ~100 кнопок на сообщение, 15 из них — служебные
#                      (было 84 при 12 служебных; регулятор «Запас над фоном» 2026-07-19 добавил ряд из трёх)


_KB_ACTION_ICONS = (
    ("одобрена", "✅"), ("добавлена", "➕"), ("заменена", "📝"),
    ("удалена", "🗑"), ("пересборка", "🔄"),
)


def _kb_recent_actions_block(limit: int = 5) -> str:
    """Блок «Последние действия» для панели /rag (из журнала knowledge_log)."""
    from database.history import get_recent_kb_actions
    recent = get_recent_kb_actions(limit)
    if not recent:
        return ""
    lines = []
    for a in recent:
        icon = next((ic for prefix, ic in _KB_ACTION_ICONS if a["action"].startswith(prefix)), "•")
        # Заголовок статьи приходит с сайта/из файла — экранируем, иначе «<»
        # в заголовке ломает HTML-разметку панели /rag.
        article = html.escape((a.get("article") or "")[:35])
        lines.append(f"  {icon} {_fmt_mod_time(a['ts'])} {a['action']}: {article}")
    return "\n\n<b>Последние действия:</b>\n" + "\n".join(lines)


async def _end_kb_test(bot, chat_id: int, context) -> None:
    """
    Завершает режим «🔍 Проверить поиск»: гасит флаг и удаляет из чата все
    накопленные сообщения проверки — и вопросы админа, и диагностические
    ответы бота, чтобы после теста не оставалось мусора. Вызывается на КАЖДОМ
    выходе из режима (кнопка «Завершить», переход в другой раздел, повторный
    ввод команды-панели). Работает только в личке админа, где бот вправе
    удалять входящие сообщения. Ошибки удаления глушатся: сообщение могли уже
    убрать вручную или прошло 48 ч (лимит Telegram на удаление). Если режим не
    был включён — тихо выходит, ничего не удаляя.
    """
    was_on = context.user_data.pop("kb_test_mode", None)
    msg_ids = context.user_data.pop("kb_test_msgs", None) or []
    if not was_on and not msg_ids:
        return
    for mid in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


def _build_rag_panel(context, admin_id: int):
    """
    Собирает текст и клавиатуру панели базы знаний: статус модуля RAG и общий
    тумблер базы, журнал, список статей-кнопок, кнопку проверки поиска и
    настройки ➖/➕ (порог сходства и число фрагментов — живут в settings).
    Используется и при отправке панели, и при перерисовке после кнопок.

    admin_id остаётся в подписи для совместимости с вызовами: персональных
    настроек в панели больше нет — тумблер базы знаний стал общим (2026-07-27).
    """
    from config import RAG_ENABLED
    from services.knowledge_store import list_articles, ARTICLE_KINDS
    from services.rag import _live_top_k, _live_min_similarity, _live_peak_margin

    articles = list_articles()
    pending_count = sum(1 for a in articles if a["folder"] == "pending")
    approved_count = len(articles) - pending_count

    file_map = {}
    article_buttons = []
    for i, art in enumerate(articles[:_KB_LIST_LIMIT]):
        token = str(i)
        file_map[token] = (art["folder"], art["fname"])
        # Ожидающие одобрения — часики (тип у сырых новостей не определить).
        # Одобренные — галочка + смысловой значок типа техники (🚜 наземная,
        # ✈️ авиация, 🛥️ корабли, ⚓ подлодки, 📘 механики, 📄 тип не опознан);
        # значки и порядок групп заданы в knowledge_store.ARTICLE_KINDS.
        if art["folder"] == "pending":
            icon = "🕐"
        else:
            kind = ARTICLE_KINDS.get(art.get("kind"), ARTICLE_KINDS["other"])
            icon = f"✅{kind['icon']}"
        # У статей об игровых механиках срезаем приставку «Игровая механика:» —
        # значок 📘 и так о ней говорит, а на узкой кнопке приставка съедала
        # всю подпись: разные статьи выглядели одинаково («Игровая механика…»).
        raw_title = art["title"]
        if art.get("kind") == "guide":
            raw_title = re.sub(r"^\s*игровая\s+механика\s*[:：-]\s*", "", raw_title, flags=re.I)
        # Заголовок урезаем чуть сильнее обычного: второй значок съедает
        # ширину кнопки, а статьи стоят в три столбца.
        title = raw_title[:16] + "…" if len(raw_title) > 17 else raw_title
        article_buttons.append(InlineKeyboardButton(f"{icon} {title}", callback_data=f"kb_view:{token}"))
    # Статьи — в три столбца (решение владельца 2026-07-18: главное — вместить все)
    rows = [article_buttons[i:i + 3] for i in range(0, len(article_buttons), 3)]
    # Кнопка-переключатель: пока проверка идёт, показывает «Завершить» —
    # чтобы админ всегда видел, что находится в режиме теста, и как выйти.
    test_on = bool(context.user_data.get("kb_test_mode"))
    test_label = "⏹️ Завершить проверку" if test_on else "🔍 Проверить поиск"
    rows.append([InlineKeyboardButton(test_label, callback_data="kb_test")])
    # Настройки поиска: порог сходства и число фрагментов (кнопки ➖/➕,
    # как в панели модерации; значение на средней кнопке, она «пустышка»)
    thr_pct = int(round(_live_min_similarity() * 100))
    rows.append([
        InlineKeyboardButton("➖", callback_data="kb_thr_dec"),
        InlineKeyboardButton(f"🎯 Порог: {thr_pct}%", callback_data="kb_noop"),
        InlineKeyboardButton("➕", callback_data="kb_thr_inc"),
    ])
    rows.append([
        InlineKeyboardButton("➖", callback_data="kb_topk_dec"),
        InlineKeyboardButton(f"📦 Статей в ответ: {_live_top_k()}", callback_data="kb_noop"),
        InlineKeyboardButton("➕", callback_data="kb_topk_inc"),
    ])
    # Запас «пик над полкой»: насколько статья должна оторваться от общего фона,
    # чтобы попасть в ответ. Больше запас — меньше случайных совпадений на
    # болтовне («ахахахах»), но и настоящие попадания найти труднее.
    margin_pct = int(round(_live_peak_margin() * 100))
    rows.append([
        InlineKeyboardButton("➖", callback_data="kb_margin_dec"),
        InlineKeyboardButton(f"📈 Запас над фоном: {margin_pct}%", callback_data="kb_noop"),
        InlineKeyboardButton("➕", callback_data="kb_margin_inc"),
    ])
    # ОБЩИЙ тумблер базы знаний (2026-07-27, просьба Максима — был персональным,
    # только для лички админа). Выключен — база не подмешивается никому и нигде:
    # ни в личке, ни в группах, ни в режиме «Сам в разговор». Проверяется в
    # единственной точке — services/rag.py::retrieve_relevant_context.
    # Ключ хранится ПРЯМО ("1" = включена), переворачивать не надо.
    kb_on = get_setting("rag_enabled", "1") == "1"
    kb_btn = f"📖 База знаний: {_onoff(kb_on)}"
    rows.append([InlineKeyboardButton(kb_btn, callback_data="kb_myrag")])
    rows.append([
        InlineKeyboardButton("➕ Добавить RAG", callback_data="kb_add"),
        InlineKeyboardButton("🔄 Пересобрать RAG", callback_data="kb_rebuild"),
    ])
    rows.append([InlineKeyboardButton("🧹 Очистить журнал", callback_data="kb_clearlog")] + _adm_back_row())
    context.application.bot_data["kb_file_map"] = file_map

    rag_status = "🟢 включён" if RAG_ENABLED else "🔴 выключен (RAG_ENABLED в .env)"
    kb_status = "🟢 ВКЛЮЧЕНА" if kb_on else "🔴 ВЫКЛЮЧЕНА (тумблер ниже)"
    hidden = len(articles) - _KB_LIST_LIMIT
    text = (
        "📚 <b>База знаний (RAG)</b>\n"
        "───────────────────────────\n"
        f"🌐 Модуль RAG: <b>{rag_status}</b>\n"
        f"📖 База знаний для всех: <b>{kb_status}</b>\n"
        f"🕐 Ждут одобрения: <b>{pending_count}</b>\n"
        f"✅ В базе знаний: <b>{approved_count}</b>"
        + _kb_recent_actions_block()
        + "\n\nНажми на статью, чтобы открыть её карточку."
        + (f"\n\n<i>Показаны первые {_KB_LIST_LIMIT}, скрыто ещё {hidden}.</i>" if hidden > 0 else "")
    )
    return text, InlineKeyboardMarkup(rows)


async def send_rag_panel(bot, chat_id: int, context):
    """Отправляет панель базы знаний: статус RAG, журнал и список статей-кнопок."""
    # Панель живёт только в личке админа, поэтому chat_id == id владельца
    text, markup = _build_rag_panel(context, chat_id)
    sent_msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
    )
    if sent_msg:
        await register_and_clean_bot_message(bot, chat_id, sent_msg.message_id)


def _kb_resolve(context, token: str):
    """Токен из callback → (folder, fname) или None, если карта протухла."""
    return context.application.bot_data.get("kb_file_map", {}).get(token)


def _kb_card_keyboard(token: str, folder: str, confirm_delete: bool = False):
    """Кнопки карточки статьи."""
    if confirm_delete:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❗️ Да, удалить", callback_data=f"kb_delete_yes:{token}"),
                InlineKeyboardButton("Отмена", callback_data=f"kb_cancel_del:{token}"),
            ],
        ])
    rows = []
    if folder == "pending":
        rows.append([InlineKeyboardButton("✅ Одобрить в базу", callback_data=f"kb_approve:{token}")])
    rows.append([
        InlineKeyboardButton("📝 Заменить", callback_data=f"kb_replace:{token}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"kb_delete:{token}"),
    ])
    rows.append([InlineKeyboardButton("⬅️ К списку", callback_data="kb_panel")])
    return InlineKeyboardMarkup(rows)


async def _kb_sync_in_executor():
    """Пересборка индекса RAG (сетевые вызовы) — в рабочем потоке, не блокируя бота."""
    import asyncio
    from services.rag import sync_knowledge_base
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, sync_knowledge_base)


def _kb_rebuild_running(context) -> bool:
    """
    Идёт ли сейчас фоновая пересборка индекса (защёлка в bot_data).
    Пока она идёт, операции со статьями (одобрить/удалить/добавить) закрыты —
    иначе две синхронизации будут наперегонки писать один файл индекса.
    """
    return bool(context.application.bot_data.get("kb_rebuild_running"))


async def _kb_popup(query, context, chat_id: int, text: str):
    """
    Всплывающее уведомление панели RAG — как при смене модели (show_alert).
    Если callback уже протух (долгая операция, лимит Telegram ~15 сек) —
    запасной вариант: обычное сообщение, которое само удаляется.
    """
    try:
        await query.answer(text, show_alert=True)
    except Exception:
        try:
            msg = await context.bot.send_message(chat_id=chat_id, text=text)
            if msg:
                schedule_delete(context.bot, chat_id, msg.message_id, 30)
        except Exception as e:
            logger.warning("⚠️ Не удалось показать уведомление панели RAG: %s", e)


async def _handle_kb_callback(query, context, data: str, chat_id: int, user_id: int):
    """Обрабатывает все callback-кнопки панели базы знаний (префикс kb_)."""
    from services.knowledge_store import read_article, read_title, approve_article, delete_article

    action, _, token = data.partition(":")

    # Любое действие панели, кроме «Добавить», выключает режим приёма новых
    # файлов — чтобы случайный документ позже не улетел в базу.
    if action != "kb_add":
        context.user_data.pop("kb_add_mode", None)
    # Режим «Проверить поиск» переживает подстройку порога/фрагментов и
    # персональный тумблер (удобно крутить настройки между тестовыми
    # вопросами), остальные действия панели его выключают — с уборкой
    # накопленных тестовых сообщений.
    if action not in ("kb_test", "kb_noop", "kb_thr_dec", "kb_thr_inc",
                      "kb_topk_dec", "kb_topk_inc", "kb_myrag",
                      "kb_margin_dec", "kb_margin_inc"):
        await _end_kb_test(context.bot, chat_id, context)

    # ── Кнопки без токена (не привязаны к конкретному файлу) ────────────
    if action == "kb_noop":
        # «Пустышка» — кнопка-значение между ➖ и ➕
        await query.answer()
        return

    if action == "kb_test":
        if context.user_data.get("kb_test_mode"):
            # Повторное нажатие — завершаем проверку: гасим режим и убираем
            # из чата все накопленные вопросы/ответы теста.
            await _end_kb_test(context.bot, chat_id, context)
            await query.answer("✅ Проверка завершена — тестовые сообщения убраны.")
        else:
            # Следующие текстовые сообщения этого админа в личке уходят в
            # диагностику поиска (handlers/messages.py), а не в ИИ.
            context.user_data["kb_test_mode"] = True
            context.user_data["kb_test_msgs"] = []
            context.user_data.pop("kb_replace_target", None)
            # ВАЖНО: лимит всплывающего окна Telegram — 200 символов, текст
            # длиннее молча не показывается (ошибка Message_too_long)
            await query.answer(
                "🔍 Пришли вопрос сообщением — покажу, какие разделы базы найдутся.\n\n"
                "Можно несколько подряд; для выхода снова нажми «Завершить проверку».",
                show_alert=True
            )
        # Перерисовываем панель — кнопка теста меняет метку (Проверить ↔ Завершить)
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось обновить панель RAG: %s", e)
        return

    if action in ("kb_thr_dec", "kb_thr_inc", "kb_topk_dec", "kb_topk_inc",
                  "kb_margin_dec", "kb_margin_inc"):
        from config import RAG_TOP_K, RAG_MIN_SIMILARITY, RAG_PEAK_MARGIN
        if action.startswith("kb_margin"):
            # Запас «пик над полкой»: шаг 0.01 — рабочий диапазон узкий
            # (дефолт 0.14, выверен замером 2026-07-19 — см. config.py).
            # 0 = правило выключено, проходит всё, что выше порога сходства.
            try:
                cur = float(get_setting("rag_peak_margin", str(RAG_PEAK_MARGIN)))
            except (TypeError, ValueError):
                cur = RAG_PEAK_MARGIN
            new_val = round(cur + (0.01 if action.endswith("inc") else -0.01), 2)
            new_val = max(0.0, min(0.30, new_val))
            set_setting("rag_peak_margin", f"{new_val:.2f}")
            logger.info("📚 /rag: запас «пик над полкой» = %.2f", new_val)
        elif action.startswith("kb_thr"):
            try:
                cur = float(get_setting("rag_min_similarity", str(RAG_MIN_SIMILARITY)))
            except (TypeError, ValueError):
                cur = RAG_MIN_SIMILARITY
            # Шаг 0.02: зазор между болтовнёй и реальными вопросами узкий
            # (~0.59 против ~0.60 по замерам 2026-07-05), 0.05 слишком грубо
            new_val = round(cur + (0.02 if action.endswith("inc") else -0.02), 2)
            new_val = max(0.05, min(0.95, new_val))
            set_setting("rag_min_similarity", f"{new_val:.2f}")
            logger.info("📚 /rag: порог сходства = %.2f", new_val)
        else:
            try:
                cur = int(get_setting("rag_top_k", str(RAG_TOP_K)))
            except (TypeError, ValueError):
                cur = RAG_TOP_K
            new_val = max(1, min(10, cur + (1 if action.endswith("inc") else -1)))
            set_setting("rag_top_k", str(new_val))
            logger.info("📚 /rag: фрагментов в контекст = %d", new_val)
        await query.answer()
        # Перерисовываем панель с новыми значениями на кнопках
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось обновить панель RAG: %s", e)
        return

    if action == "kb_myrag":
        # ОБЩИЙ тумблер базы знаний (2026-07-27): выключен — статьи не
        # подмешиваются НИКОМУ и НИГДЕ (личка, группы, «Сам в разговор»).
        # Проверяется в services/rag.py::retrieve_relevant_context — в одной
        # точке на всех путях сразу.
        # На кнопку «🔍 Проверить поиск» не влияет — диагностика работает всегда.
        cur_on = get_setting("rag_enabled", "1") == "1"
        new_val = "0" if cur_on else "1"
        set_setting("rag_enabled", new_val)
        state = "включена" if new_val == "1" else "выключена"
        logger.info("📚 /rag: база знаний %s для ВСЕХ (переключил админ %s)", state, user_id)
        await query.answer(f"📖 База знаний {state} для всех", show_alert=False)
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось обновить панель RAG: %s", e)
        return

    if action == "kb_add":
        # Следующий документ .md/.txt от этого админа станет новой статьёй
        # базы знаний (handle_kb_document). Режим живёт до другого действия.
        context.user_data["kb_add_mode"] = True
        context.user_data.pop("kb_replace_target", None)
        await query.answer(
            "➕ Пришли файл .md или .txt со статьёй — я добавлю её в базу знаний "
            "и сразу посчитаю вектора. Можно прислать несколько файлов подряд.",
            show_alert=True
        )
        return

    if action == "kb_rebuild":
        if _kb_rebuild_running(context):
            await _kb_popup(query, context, chat_id, "⏳ Пересборка уже идёт — дождитесь итога.")
            return
        logger.info("🔧 Админ %s запустил полную пересборку базы знаний", user_id)
        import asyncio
        from services.rag import rebuild_knowledge_base
        from database.history import add_kb_action
        bot = context.bot

        async def _rebuild_and_report():
            # Из-за пауз при лимите Google пересборка может идти несколько
            # минут — поэтому отдельной задачей: очередь апдейтов свободна,
            # бот всё это время отвечает как обычно. Повторный запуск и
            # операции со статьями на это время закрывает защёлка.
            result = None
            try:
                result = await asyncio.get_running_loop().run_in_executor(None, rebuild_knowledge_base)
            except Exception as e:
                logger.error("⚠️ Пересборка базы знаний упала: %s", e)
            finally:
                context.application.bot_data["kb_rebuild_running"] = False
            if result is None or result[1] == 0:
                text = "⚠️ Не удалось пересобрать RAG — база пуста или произошла ошибка. Детали в логе."
            else:
                indexed, total = result
                add_kb_action("пересборка базы", f"проиндексировано {indexed} из {total}", user_id)
                if indexed >= total:
                    text = f"✅ RAG пересобрана заново!\n\nПроиндексировано: {indexed} из {total}"
                else:
                    text = (f"⚠️ Пересборка упёрлась в лимит Google: проиндексировано {indexed} из {total}.\n"
                            f"Недостающее бот доберёт при следующем запуске, "
                            f"либо нажмите пересборку ещё раз чуть позже.")
            try:
                msg = await bot.send_message(chat_id=chat_id, text=text)
                if msg:
                    schedule_delete(bot, chat_id, msg.message_id, 60)
            except Exception as e:
                logger.warning("⚠️ Не удалось отправить итог пересборки: %s", e)
            await send_rag_panel(bot, chat_id, context)

        context.application.bot_data["kb_rebuild_running"] = True
        context.application.create_task(_rebuild_and_report())
        await _kb_popup(query, context, chat_id,
                        "⏳ Пересборка запущена в фоне — итог придёт отдельным сообщением. "
                        "Бот продолжает работать как обычно.")
        return

    if action == "kb_clearlog":
        # Подтверждение прямо в панели: подменяем клавиатуру на «да/отмена»
        await query.answer()
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, очистить журнал", callback_data="kb_clearlog_yes"),
                InlineKeyboardButton("Отмена", callback_data="kb_panel"),
            ]]))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение очистки журнала: %s", e)
        return

    if action == "kb_clearlog_yes":
        from database.history import clear_kb_log
        deleted = clear_kb_log()
        logger.info("🔧 Админ %s очистил журнал базы знаний (%d записей)", user_id, deleted)
        await _kb_popup(query, context, chat_id, f"🧹 Журнал действий очищен (удалено записей: {deleted}).")
        await send_rag_panel(context.bot, chat_id, context)
        return

    resolved = _kb_resolve(context, token)
    if resolved is None:
        # Карта токенов протухла (перезапуск бота) — открываем список заново
        await query.answer("Список устарел — открываю заново.", show_alert=False)
        await send_rag_panel(context.bot, chat_id, context)
        return
    folder, fname = resolved

    if action == "kb_view":
        try:
            path, content = read_article(folder, fname)
        except FileNotFoundError:
            await query.answer("Файл уже удалён.", show_alert=True)
            await send_rag_panel(context.bot, chat_id, context)
            return
        await query.answer()
        status = "🕐 Ждёт одобрения" if folder == "pending" else "✅ В базе знаний"
        caption = f"{status}\n<b>{read_title(path)}</b>\n<code>{fname}</code>"
        # Статью отправляем ФАЙЛОМ: её удобно открыть, отредактировать у себя
        # и прислать обратно через кнопку «Заменить».
        sent_msg = await context.bot.send_document(
            chat_id=chat_id,
            document=content.encode("utf-8"),
            filename=fname,
            caption=caption[:1024],
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_card_keyboard(token, folder),
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    if action == "kb_approve":
        if _kb_rebuild_running(context):
            await _kb_popup(query, context, chat_id, "⏳ Идёт пересборка базы — одобрите статью после её завершения.")
            return
        if folder != "pending":
            await query.answer("Эта статья уже в базе.", show_alert=True)
            return
        try:
            path, _ = read_article(folder, fname)
            title = read_title(path)
            approve_article(fname)
        except FileNotFoundError:
            await query.answer("Файл уже удалён.", show_alert=True)
            await send_rag_panel(context.bot, chat_id, context)
            return
        logger.info("🔧 Админ %s одобрил статью базы знаний: %s", user_id, fname)
        from database.history import add_kb_action
        add_kb_action("одобрена", title, user_id)
        await _kb_sync_in_executor()
        # Пока идёт векторизация, на кнопке крутится «часики» Telegram;
        # всплывашка приходит уже с фактом — статья В базе.
        await _kb_popup(query, context, chat_id, f"✅ Статья добавлена в базу знаний:\n\n{title}")
        await send_rag_panel(context.bot, chat_id, context)
        return

    if action == "kb_delete":
        # Первое нажатие — просим подтверждение прямо в карточке
        await query.answer()
        try:
            await query.edit_message_reply_markup(_kb_card_keyboard(token, folder, confirm_delete=True))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение удаления: %s", e)
        return

    if action == "kb_cancel_del":
        await query.answer("Отменено.")
        try:
            await query.edit_message_reply_markup(_kb_card_keyboard(token, folder))
        except Exception as e:
            logger.warning("⚠️ Не удалось вернуть кнопки карточки: %s", e)
        return

    if action == "kb_delete_yes":
        if _kb_rebuild_running(context):
            await _kb_popup(query, context, chat_id, "⏳ Идёт пересборка базы — удалите статью после её завершения.")
            return
        try:
            path, _ = read_article(folder, fname)
            title = read_title(path)
        except FileNotFoundError:
            title = fname
        try:
            delete_article(folder, fname)
        except FileNotFoundError:
            pass  # уже удалён — просто обновим список
        logger.info("🔧 Админ %s удалил статью базы знаний (%s): %s", user_id, folder, fname)
        from database.history import add_kb_action
        if folder == "approved":
            add_kb_action("удалена из базы", title, user_id)
            await _kb_sync_in_executor()
            await _kb_popup(query, context, chat_id, f"🗑 Статья удалена из базы знаний:\n\n{title}")
        else:
            add_kb_action("удалена из ожидания", title, user_id)
            await _kb_popup(query, context, chat_id, f"🗑 Статья удалена из ожидания:\n\n{title}")
        await send_rag_panel(context.bot, chat_id, context)
        return

    if action == "kb_replace":
        # Запоминаем, какой файл ждёт замену, — следующий документ .md/.txt
        # от этого админа в личке станет новым содержимым (handle_kb_document).
        context.user_data["kb_replace_target"] = (folder, fname)
        await query.answer(
            f"📝 Пришли файлом (.md или .txt) новое содержимое статьи {fname} — я заменю её целиком.",
            show_alert=True
        )
        return

    await query.answer()


async def cmd_rag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель базы знаний. Только в личке и только у ВЛАДЕЛЬЦА:
    модераторам база знаний не выдаётся ни при каких галочках."""
    await delete_user_message_safe(update.message)
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    if _is_group_chat(update):
        return

    await _end_kb_test(context.bot, update.effective_chat.id, context)  # выход из проверки поиска — с уборкой
    await send_rag_panel(context.bot, update.effective_chat.id, context)


async def handle_kb_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает документ для базы знаний в двух режимах:
      • замена содержимого статьи (после кнопки «Заменить», kb_replace_target);
      • добавление новой статьи (после кнопки «➕ Добавить RAG», kb_add_mode —
        режим живёт до другого действия в панели, можно слать несколько подряд).
    Срабатывает только у админа в личке и только если режим был включён —
    во всех остальных случаях молча пропускает документ (как и раньше,
    документы бот не обрабатывает).
    """
    if not update.message or not update.message.document:
        return
    user_id = update.effective_user.id
    from services import roles
    if not roles.is_owner(user_id) or _is_group_chat(update):
        return
    replace_target = context.user_data.get("kb_replace_target")
    add_mode = bool(context.user_data.get("kb_add_mode"))
    if not replace_target and not add_mode:
        return

    from services.knowledge_store import replace_article, add_article

    chat_id = update.effective_chat.id
    if _kb_rebuild_running(context):
        msg = await update.message.reply_text("⏳ Идёт пересборка базы — пришлите файл после её завершения.")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return
    doc = update.message.document
    fname_low = (doc.file_name or "").lower()
    # Служебные сообщения об ошибках не должны копиться в чате —
    # они удаляются сами через schedule_delete.
    if not (fname_low.endswith(".md") or fname_low.endswith(".txt")):
        msg = await update.message.reply_text("⚠️ Нужен текстовый файл .md или .txt — попробуй ещё раз.")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return
    if doc.file_size and doc.file_size > 1_000_000:
        msg = await update.message.reply_text("⚠️ Файл слишком большой (лимит 1 МБ).")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return

    try:
        tg_file = await doc.get_file()
        raw = await tg_file.download_as_bytearray()
        new_text = bytes(raw).decode("utf-8", errors="replace")
        if replace_target:
            folder, fname = replace_target
            replace_article(folder, fname, new_text)
        else:
            folder = "approved"
            fname = add_article(doc.file_name or "article.md", new_text)
    except Exception as e:
        verb = "заменить статью" if replace_target else "добавить статью"
        logger.error("⚠️ Не удалось %s: %s", verb, e)
        msg = await update.message.reply_text(f"⚠️ Не получилось {verb} — подробности в логе.")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return

    await delete_user_message_safe(update.message)
    from database.history import add_kb_action
    from services.knowledge_store import read_article, read_title
    try:
        article_path, _ = read_article(folder, fname)
        article_title = read_title(article_path)
    except Exception:
        article_title = fname
    if replace_target:
        context.user_data.pop("kb_replace_target", None)
        logger.info("🔧 Админ %s заменил содержимое статьи (%s): %s", user_id, folder, fname)
        add_kb_action("заменена", article_title, user_id)
        note = "✅ Статья <code>" + fname + "</code> заменена"
        note += " — вектора пересчитаны." if folder == "approved" else "."
    else:
        # Режим добавления НЕ сбрасываем — можно прислать следующий файл
        logger.info("🔧 Админ %s добавил новую статью в базу знаний: %s", user_id, fname)
        add_kb_action("добавлена", article_title, user_id)
        note = (f"✅ Статья <code>{fname}</code> добавлена в базу знаний — вектора посчитаны.\n"
                f"Можно прислать ещё файл или нажать любую кнопку панели.")
    if folder == "approved":
        # Файл в базе — пересчитываем вектора сразу
        await _kb_sync_in_executor()
    await send_rag_panel(context.bot, chat_id, context)
    # Подтверждение под обновлённой панелью; исчезает само, чтобы не мусорить
    confirm = await context.bot.send_message(chat_id=chat_id, text=note, parse_mode=ParseMode.HTML)
    if confirm:
        schedule_delete(context.bot, chat_id, confirm.message_id, 15)


async def handle_kb_test_query(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    """
    Режим «🔍 Проверить поиск» панели RAG: вопрос админа в личке уходит в
    диагностику семантического поиска вместо ИИ. Показывает найденные разделы
    с процентом сходства и отметкой, попали бы они в контекст модели.
    Вызывается из handlers/messages.py, когда включён user_data["kb_test_mode"].
    """
    import asyncio
    from services import rag

    message = update.message
    user_id = update.effective_user.id
    logger.info("📚 /rag: проверка поиска — «%s»", user_text[:80])

    # Эмбеддинг запроса — сетевой вызов, уводим в рабочий поток
    report = await asyncio.get_running_loop().run_in_executor(None, rag.test_search, user_text)

    if report.get("error"):
        text = f"⚠️ {html.escape(report['error'])}"
    else:
        thr_pct = int(round(report["threshold"] * 100))
        base_pct = int(round(report.get("baseline", 0) * 100))
        lines = [
            "🔍 <b>Проверка поиска</b>",
            f"Вопрос: <i>{html.escape(user_text[:200])}</i>",
            f"🎯 Порог: {thr_pct}% · 📦 Статей: {report['top_k']} · 📉 Полка (медиана): {base_pct}%",
            "",
        ]
        any_passed = False
        for r in report["results"]:
            mark = "✅" if r["passes"] else "▫️"
            any_passed = any_passed or r["passes"]
            # Балл = смысл + слова; вклад слов показываем отдельно, чтобы было
            # видно работу гибридного поиска. Для отсеянных — причина.
            lex_note = f" (слова +{r['lex'] * 100:.0f}%)" if r.get("lex") else ""
            reason_note = "" if r["passes"] else f" — {r.get('reason', '')}"
            lines.append(f"{mark} {r['similarity'] * 100:.0f}%{lex_note} — "
                         f"{html.escape(r['title'])}{html.escape(reason_note)}")
        if any_passed:
            lines.append("\n✅ — эти статьи уйдут модели вместе с вопросом.")
        else:
            lines.append("\n⚠️ Ничего не прошло отбор — модель ответит без базы знаний.")
        lines.append("<i>Пришли следующий вопрос или открой панель: /rag</i>")
        text = "\n".join(lines)

    from database.history import add_kb_action
    try:
        add_kb_action("проверка поиска", user_text[:35], user_id)
    except Exception:
        pass
    sent = await message.reply_text(text, parse_mode=ParseMode.HTML)
    # Запоминаем и вопрос, и ответ, чтобы убрать их при выходе из проверки
    # (см. _end_kb_test). Список создаётся при включении режима, но на всякий
    # случай подстраховываемся setdefault.
    msgs = context.user_data.setdefault("kb_test_msgs", [])
    if message.message_id:
        msgs.append(message.message_id)
    if sent:
        msgs.append(sent.message_id)
