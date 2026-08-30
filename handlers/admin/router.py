# ───────────────────────────────────────────────
#  handlers/admin/router.py — handle_callback_query — ЕДИНСТВЕННЫЙ роутер ВСЕХ callback-кнопок бота.
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import asyncio
import html
import logging
import os

from telegram import Update, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import (AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, GEMINI_MODEL,
                    PROVIDERS, THINKING_LEVELS, THINKING_SETTING_PREFIX, THINKING_PHASES)
from database.history import set_setting, get_setting
from utils import register_and_clean_bot_message
from utils import schedule_delete


logger = logging.getLogger(__name__)
from .common import (_LOG_FILE_TTL, _TG_FILE_MAX, _adm_back_row, _audit,
                     _build_chat_log_header,
                     _build_log_text, _build_logs_menu_text, _chat_log_files_row,
                     _count_archive_sessions, _log_files_row, _logs_back_row,
                     _logs_menu_rows, _read_archive_log, _read_current_log,
                     _read_file_bytes)
from .panel_balance import _handle_balance_callback
from .panel_digest import _handle_digest_callback
from .panel_main import (_build_api_keyboard, build_adm_keyboard,
                         send_adm_panel, send_api_panel,
                         send_daily_report_panel, send_weekly_report_panel,
                         send_stats_panel)
from .panel_mod import _handle_mod_callback, send_mod_panel
from .panel_prompts import (_build_prompt_panel_text_and_keyboard, _handle_proactive_callback,
                            _handle_proactive_wipe, handle_prompt_reset,
                            send_prompts_panel, send_prompt_files)
from .panel_quiz import _handle_quiz_callback
from .panel_rag import _end_kb_test, _handle_kb_callback, send_rag_panel
from .panel_updates import _handle_updates_callback
from .panel_users import _handle_users_callback, send_users_panel




async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    chat_id = query.message.chat_id

    # ── Callbacks доступные всем пользователям ──────────────────────────
    if data == "quiz_start":
        await query.answer()
        from handlers.quiz import send_quiz_question
        await send_quiz_question(chat_id, context)
        return

    # 👋 Кнопка «Я не бот» из приветствия новичков (2026-08-04). Стоит ЗДЕСЬ,
    # ДО гейта прав, намеренно: её жмёт обычный участник группы, а не персонал
    # (как quiz_start). Чужому нажатию отказывает сам обработчик — кнопка
    # именная, в её данных лежит id того, кому она адресована.
    if data.startswith("join:"):
        from services.greeter import handle_join_callback
        await handle_join_callback(query, context, data)
        return

    # 📊 Кнопки карточки справочника техники (2026-08-04): разделы статьи,
    # возврат к карточке, файл. Тоже ДО гейта прав — /ttx публичная команда,
    # и её карточкой пользуются обычные участники группы.
    if data.startswith("ttx:"):
        from handlers.tech import handle_ttx_callback
        await handle_ttx_callback(query, context, data)
        return

    # 🏠 Кнопки главного экрана /start (2026-08-04): справочник, звание,
    # тумблер новостей, полный список команд и возврат. Тоже ДО гейта прав —
    # это экран для всех, а не для персонала.
    if data.startswith("menu:"):
        from handlers.commands import handle_menu_callback
        await handle_menu_callback(query, context, data)
        return

    if data == "clear_history_btn":
        from database.history import clear_history
        clear_history(user_id)
        logger.info("🔧 История диалога очищена кнопкой (пользователь %s)", user_id)
        await query.answer("✅ История диалога очищена!", show_alert=True)
        return

    # ── Гейт доступа: у каждой кнопки своё нужное право ──────────────────
    # ЕДИНАЯ таблица «кнопка → право» живёт в services/roles.py. Прятать
    # кнопки от модератора недостаточно: старое сообщение с панелью остаётся
    # рабочим, поэтому право проверяется здесь, в момент нажатия.
    # Кнопки, которой нет в таблице, доступна только владельцу (запрет по
    # умолчанию) — забытая новая кнопка не откроется модераторам случайно.
    from services import roles
    if not roles.may_press(user_id, data):
        if roles.is_moderator(user_id):
            await query.answer("⛔ У тебя нет права на это действие.", show_alert=True)
        else:
            await query.answer("Пошел нахуй❗️ Эта команда доступна только Администрации.", show_alert=True)
        return

    # ── Ожидание числа для экрана «💰 Счета и квоты» ─────────────────────
    # Пока владелец вводит остаток счёта или квоту, его следующее сообщение в
    # личке перехватывается (handlers/messages.py). Ушёл с экрана любой другой
    # кнопкой — ожидание гаснет ЗДЕСЬ, одной проверкой на все кнопки бота:
    # иначе следующий вопрос боту был бы съеден как «не число».
    if not data.startswith("bal:"):
        context.user_data.pop("balance_edit", None)

    # ── Кнопка «⬅️ Назад к панели» (из служебных сообщений промптов) ─────
    # Удаляет текущее сообщение с кнопкой и заново открывает панель промптов
    # (callback-имя историческое: раньше вело в общую панель /stats).
    # Намеренно БЕЗ записи в лог — это обычная навигация по служебным сообщениям.
    if data == "back_to_stats":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        await send_prompts_panel(context.bot, chat_id, user_id)
        return

    # ── Кнопки главной админ-панели /adm ────────────────────────────────
    # Открытие раздела идёт через register_and_clean_bot_message внутри
    # панелей, поэтому сообщение /adm автоматически удаляется — в чате
    # остаётся только открытая панель.
    if data in ("adm_open_stats", "adm_open_prompts", "adm_open_api",
                "adm_open_mod", "adm_open_rag", "adm_open_users",
                "adm_daily_report", "adm_weekly_report",
                "adm_back", "kb_panel"):
        # Переход между разделами отменяет ожидание файлов и режим
        # проверки поиска базы знаний (с уборкой тестовых сообщений)
        context.user_data.pop("kb_add_mode", None)
        context.user_data.pop("kb_replace_target", None)
        await _end_kb_test(context.bot, chat_id, context)
        await query.answer()
        if data == "adm_open_stats":
            await send_stats_panel(context.bot, chat_id, user_id)
        elif data == "adm_open_prompts":
            await send_prompts_panel(context.bot, chat_id, user_id)
        elif data == "adm_open_api":
            await send_api_panel(context.bot, chat_id, user_id)
        elif data == "adm_open_mod":
            await send_mod_panel(context.bot, chat_id, user_id)
        elif data == "adm_open_users":
            await send_users_panel(context.bot, chat_id, user_id)
        elif data == "adm_daily_report":
            # Суточный расход по счётчикам панели API (сохранённый ночной текст)
            await send_daily_report_panel(context.bot, chat_id, user_id)
        elif data == "adm_weekly_report":
            # Недельный расход (сохранённый текст последнего понедельника)
            await send_weekly_report_panel(context.bot, chat_id, user_id)
        elif data == "adm_back":
            await send_adm_panel(context.bot, chat_id, user_id)
        else:  # adm_open_rag / kb_panel
            await send_rag_panel(context.bot, chat_id, context)
        return

    # ── Кнопка «📄 Показать полные PROMPTы»: промпты .txt файлами ─────────
    # Панель НЕ трогаем: файлы идут мимо гигиены и сами удаляются через
    # 10 минут (см. send_prompt_files) — панель остаётся на месте.
    if data == "prompts_files":
        # Отвечаем СРАЗУ: у Telegram ~15 сек на ответ кнопке, а отправка
        # нескольких документов может занять заметное время.
        await query.answer("⏳ Отправляю файлы…")
        sent = await send_prompt_files(context.bot, chat_id, user_id)
        if not sent:
            # Самоудаляемое, а не через гигиену панелей: гигиена снесла бы
            # саму панель промптов, из которой нажали кнопку.
            warn = await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Ни один промпт не задан — отправлять нечего.",
            )
            if warn:
                schedule_delete(context.bot, chat_id, warn.message_id, 15)
        return

    # ── Кнопка «📜 Логи бота»: развилка из двух веток ─────────────────────
    # ⚠️ РАНЬШЕ ЭТА КНОПКА СРАЗУ ПРИСЫЛАЛА ТЕКСТ ЛОГА (до 2026-08-16). Логов
    # стало два вида — работа бота и дословный разговор в группе, — и один
    # экран их не вмещает: у разговора свои цифры, свои файлы и свой счёт
    # записей. Прежний экран целиком переехал в ветку «adm_logs_bot» ниже.
    # Сюда же возвращает кнопка «⬅️ К логам» с обоих экранов.
    if data == "adm_logs":
        # Переход из панели отменяет ожидание файлов и режим проверки поиска
        context.user_data.pop("kb_add_mode", None)
        context.user_data.pop("kb_replace_target", None)
        await _end_kb_test(context.bot, chat_id, context)
        await query.answer()
        sent_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_build_logs_menu_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(_logs_menu_rows() + [_adm_back_row()]),
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    # ── «⚙️ Работа бота»: текст с максимумом последних строк общего лога ──
    if data == "adm_logs_bot":
        log_path, raw = _read_current_log()
        if not raw:
            await query.answer("⚠️ Файл лога текущей сессии пуст или не найден", show_alert=True)
            return
        await query.answer()
        logger.info("🔧 Админ %s запросил логи текущей сессии", user_id)
        fname = os.path.basename(log_path)
        keyboard = InlineKeyboardMarkup([
            _log_files_row(),
            _logs_back_row(),
        ])
        sent_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_build_log_text(fname, raw),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    # ── «💬 Разговор в группе»: дословный лог проактивного режима ─────────
    # Экран показывается ВСЕГДА, даже когда текущей записи нет: в архиве при
    # этом обычно лежат прошлые, и отбивать нажатие табличкой было бы враньём
    # («логов нет» вместо «сегодняшних логов нет»).
    if data == "adm_logs_chat":
        from services import chat_log
        stats = chat_log.stats()
        await query.answer()
        logger.info("🔧 Админ %s запросил лог разговора", user_id)
        keyboard = InlineKeyboardMarkup([
            _chat_log_files_row(),
            _logs_back_row(),
        ])
        _, raw = _read_file_bytes(stats["path"])
        if raw:
            text = _build_log_text(stats["name"], raw,
                                   header=_build_chat_log_header(stats))
        else:
            text = (
                "💬 <b>РАЗГОВОР В ГРУППЕ</b>\n"
                "───────────────────────────\n"
                "Записей пока нет: после очистки разговоров в группе ещё "
                "никто не писал — либо выключен режим «Сам в разговор».\n\n"
                "<i>Прошлые записи, если они были, лежат в архиве.</i>"
            )
        sent_msg = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    # ── «💬 Текущая запись» и «🗄 Архив разговоров»: файлы ────────────────
    # Отправка — как у логов бота: мимо гигиены панелей, без клавиатуры и с
    # минутным самоудалением. Экран разговора остаётся на месте.
    if data in ("adm_logs_chat_file", "adm_logs_chat_archive"):
        from services import chat_log
        is_archive = data == "adm_logs_chat_archive"
        path = chat_log.archive_path() if is_archive else chat_log.current_path()
        _, raw = _read_file_bytes(path)
        if not raw:
            await query.answer(
                "🗄 Архив разговоров пуст: он наполняется по кнопке "
                "«🧹 Очистить РАЗГОВОРЫ»." if is_archive else
                "💬 Записей пока нет: после очистки разговоров в группе ещё не писали.",
                show_alert=True,
            )
            return
        # ⚠️ ПОТОЛОК ТЕЛЕГРАМА. Запись растёт до следующей очистки разговоров,
        # и ограничить её нечем, кроме этой кнопки. Файл больше потолка Telegram
        # просто не уйдёт — лучше сказать словами, чем свалиться непонятной
        # ошибкой отправки.
        if len(raw) > _TG_FILE_MAX:
            await query.answer(
                f"⚠️ Файл слишком велик для Telegram ({len(raw) // (1024 * 1024)} МБ).\n"
                f"Нажми «🧹 Очистить РАЗГОВОРЫ» — запись уедет в архив, "
                f"и начнётся новая.",
                show_alert=True,
            )
            return
        await query.answer()
        fname = os.path.basename(path)
        logger.info("🔧 Админ %s скачал %s разговора", user_id,
                    "архив" if is_archive else "текущую запись")
        if is_archive:
            sessions = chat_log.stats()["archive_sessions"]
            caption = (f"🗄 <b>Архив разговоров</b>\n"
                       f"<code>{html.escape(fname)}</code> · "
                       f"{max(1, round(len(raw) / 1024))} КБ · записей: {sessions}\n")
        else:
            caption = (f"💬 <b>Текущая запись разговора</b>\n"
                       f"<code>{html.escape(fname)}</code> · "
                       f"{max(1, round(len(raw) / 1024))} КБ\n")
        sent_msg = await context.bot.send_document(
            chat_id=chat_id, document=raw, filename=fname,
            caption=caption + "<i>Сообщение исчезнет через минуту — успейте "
                              "открыть или сохранить.</i>",
            parse_mode=ParseMode.HTML,
        )
        if sent_msg:
            schedule_delete(context.bot, chat_id, sent_msg.message_id, _LOG_FILE_TTL)
        return

    # ── Кнопка «💾 Текущий лог»: полный файл лога документом ──────────────
    # ⚠️ ФАЙЛ ИДЁТ МИМО ГИГИЕНЫ ПАНЕЛЕЙ и БЕЗ клавиатуры (решение Максима
    # 2026-07-28). Раньше он отправлялся как панель: затирал собой экран логов
    # и приносил те же две кнопки — получался лишний переход и дубль кнопок.
    # Теперь экран логов остаётся на месте со своими кнопками, а файл просто
    # падает в чат следующим сообщением: нажал — скачал, второй файл берётся
    # тем же экраном сверху. Не «чинить» возвратом register_and_clean_bot_message:
    # он удалил бы экран, из которого нажали.
    if data == "adm_logs_file":
        log_path, raw = _read_current_log()
        if not raw:
            await query.answer("⚠️ Файл лога текущей сессии пуст или не найден", show_alert=True)
            return
        await query.answer()
        logger.info("🔧 Админ %s скачал файл лога текущей сессии", user_id)
        fname = os.path.basename(log_path)
        size_kb = max(1, round(len(raw) / 1024))
        sent_msg = await context.bot.send_document(
            chat_id=chat_id,
            document=raw,
            filename=fname,
            caption=(
                f"📜 <b>Полный лог текущей сессии</b>\n"
                f"<code>{html.escape(fname)}</code> · {size_kb} КБ\n"
                f"<i>Сообщение исчезнет через минуту — успейте открыть или сохранить.</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
        if sent_msg:
            schedule_delete(context.bot, chat_id, sent_msg.message_id, _LOG_FILE_TTL)
        return

    # ── Кнопка «🗄 Архив логов»: логи прошлых запусков документом ─────────
    # Архив (logs/archive.log) собирает logging_setup при каждом старте:
    # логи прошлых сессий склеиваются подряд, хранятся последние 7.
    # Отправка — как у «Текущего лога»: мимо гигиены панелей и без кнопок.
    if data == "adm_logs_archive":
        log_path, raw = _read_archive_log()
        if not raw:
            await query.answer(
                "🗄 Архив пуст: логов прошлых запусков ещё нет.\n"
                "Он соберётся сам при следующем запуске бота.",
                show_alert=True
            )
            return
        await query.answer()
        logger.info("🔧 Админ %s скачал архив логов прошлых запусков", user_id)
        fname = os.path.basename(log_path)
        size_kb = max(1, round(len(raw) / 1024))
        sessions = _count_archive_sessions(raw)
        # Число сессий считаем по заголовкам в самом файле, а не по потолку
        # ARCHIVE_SESSIONS_TO_KEEP: архив бывает и короче потолка.
        sessions_part = f" · сессий: {sessions}" if sessions else ""
        sent_msg = await context.bot.send_document(
            chat_id=chat_id,
            document=raw,
            filename=fname,
            caption=(
                f"🗄 <b>Архив логов прошлых запусков</b>\n"
                f"<code>{html.escape(fname)}</code> · {size_kb} КБ{sessions_part}\n"
                f"<i>Сообщение исчезнет через минуту — успейте открыть или сохранить.</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
        if sent_msg:
            schedule_delete(context.bot, chat_id, sent_msg.message_id, _LOG_FILE_TTL)
        return

    # ── Кнопка «💾 Копия базы»: снять копию прямо сейчас ─────────────────
    # Та же копия, что уходит владельцу каждую ночь (jobs.nightly_backup),
    # только по требованию: перед правкой настроек, перед выкаткой, «просто
    # чтобы была свежая». Ночной метки НЕ трогает — ручная копия не отменяет
    # ночную и не приближает её.
    # ⚠️ Файл идёт МИМО гигиены панелей и БЕЗ таймера самоудаления, в отличие
    # от логов: это последняя копия базы, она обязана остаться в переписке.
    if data == "adm_backup":
        # Отвечаем ДО работы: у Telegram ~15 секунд на ответ callback, а тут
        # диск — снимок базы и сжатие.
        await query.answer("⏳ Делаю копию базы…")
        from services import backup
        loop = asyncio.get_running_loop()
        try:
            path, size = await loop.run_in_executor(None, backup.make_backup)
        except Exception as e:
            logger.error("⚠️ Копия базы по кнопке не сделана: %s", e)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Не удалось сделать копию базы: <code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        logger.info("🔧 Админ %s сделал копию базы вручную", user_id)
        with open(path, "rb") as f:
            blob = f.read()
        fname = os.path.basename(path)
        kept = len(backup.list_backups())
        await context.bot.send_document(
            chat_id=chat_id,
            document=blob,
            filename=fname,
            caption=(
                f"💾 <b>Копия базы бота</b>\n"
                f"<code>{html.escape(fname)}</code> · {backup.human_size(size)}\n"
                f"<i>Сохрани у себя: промпты, настройки, счета и квоты, личные "
                f"дела, звания и журналы — этого нет на GitHub. "
                f"На сервере хранится копий: {kept}.</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
        # Статьи базы знаний — вторым файлом (2026-08-12), как и ночью.
        # Под своим try: копия базы уже у владельца, и сорвавшийся архив
        # статей не должен превратить удачное нажатие в сообщение об ошибке.
        try:
            kb_path, kb_size, kb_ok, kb_wait = await loop.run_in_executor(None, backup.make_kb_backup)
            with open(kb_path, "rb") as f:
                kb_blob = f.read()
            kb_name = os.path.basename(kb_path)
            await context.bot.send_document(
                chat_id=chat_id,
                document=kb_blob,
                filename=kb_name,
                caption=backup.kb_caption(kb_name, kb_size, kb_ok, kb_wait),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error("⚠️ Архив статей базы знаний по кнопке не сделан: %s", e)
        return

    # ── Панель базы знаний: карточка статьи и действия ──────────────────
    if data.startswith("kb_"):
        await _handle_kb_callback(query, context, data, chat_id, user_id)
        return

    # ── Тумблер самообновления ──────────────────────────────────────────
    # Выключенный тумблер не мешает обновляться вручную (кнопка перезапуска
    # и .bat) — он гасит только автоматический цикл.
    if data == "adm_autoupdate":
        from config import AUTO_UPDATE_ENABLED_DEFAULT
        from handlers.admin.panel_main import build_adm_keyboard

        now_on = get_setting("auto_update_enabled", AUTO_UPDATE_ENABLED_DEFAULT) == "1"
        set_setting("auto_update_enabled", "0" if now_on else "1")
        logger.info("🔧 Админ %s %s самообновление", user_id, "выключил" if now_on else "включил")
        _audit(user_id, "autoupdate", None, "выключено" if now_on else "включено")
        await query.answer(
            "⬇️ Самообновление выключено — правки с GitHub бот сам забирать не будет."
            if now_on else
            "⬇️ Самообновление включено — раз в 5 минут бот проверяет GitHub.",
            show_alert=True
        )
        try:
            await query.edit_message_reply_markup(reply_markup=build_adm_keyboard(user_id))
        except Exception as e:
            logger.warning("⚠️ Не удалось перерисовать панель после тумблера самообновления: %s", e)
        return


    # ── Кнопка перезапуска бота ─────────────────────────────────────────
    if data == "system_restart":
        logger.info("🔧 Админ %s запросил перезапуск бота", user_id)
        # Помечаем, что остановка — это ПЕРЕЗАПУСК (кнопкой). По этому флагу:
        # хук остановки (main.post_stop) НЕ шлёт «остановлен вручную», а
        # main.main() после полной остановки запускает новую копию бота.
        context.application.bot_data["shutdown_reason"] = "restart"
        # Плашка-уведомление: тот же текст, что и в сообщении (без жирного/курсива —
        # всплывающие плашки Telegram не поддерживают форматирование).
        # Отвечаем СРАЗУ: у Telegram около 15 секунд на ответ кнопке, а
        # обновление кода может занять дольше.
        await query.answer(
            "🔄 Выполняется перезапуск бота...\n\n"
            "Бот перезапустится автоматически через несколько секунд.",
            show_alert=True
        )

        # ── Обновление кода с GitHub (2026-07-27) ───────────────────────
        # На СЕРВЕРЕ кнопка сначала забирает свежий код, и только потом
        # перезапускается — иначе она поднимала бы бота на том же старом коде
        # (ровно на это Максим и напоролся 27.07). Дома проверка can_update()
        # возвращает False, и всё работает как раньше.
        # Что бы ни случилось с обновлением, ПЕРЕЗАПУСК ВСЁ РАВНО ПРОИСХОДИТ:
        # человек нажал «перезапустить», и это его просьба, а не следствие
        # удачного обновления.
        from services import deploy

        update_line = ""
        if deploy.can_update():
            try:
                await query.edit_message_text(
                    "🔄 <b>Проверяю, есть ли новый код...</b>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
            try:
                update_line = deploy.describe(await deploy.update()) + "\n\n"
            except Exception as e:
                logger.warning("⚠️ Обновление сорвалось: %s", e)
                update_line = "⚠️ Обновить код не удалось, перезапускаюсь на прежнем.\n\n"

        try:
            await query.edit_message_text(
                f"{update_line}"
                "🔄 <b>Выполняется перезапуск бота...</b>\n\n"
                "<i>Бот перезапустится автоматически через несколько секунд.</i>",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение при перезапуске: %s", e)

        # Просим библиотеку корректно остановить бота: run_polling в main.py
        # завершается штатно (отрабатывают post_stop и post_shutdown, фоновые
        # задачи закрываются), после чего main.py запускает новую копию бота
        # в этом же окне консоли. Новый процесс сам сообщит админам о запуске.
        context.application.stop_running()
        return

    # ── Подтверждение и отмена сброса ПЯТИ промптов ────────────────────
    # Десять кнопок, одна логика: стереть ключ в settings → попап →
    # переписать сообщение. Тексты и ключи — в таблице `_PROMPT_RESETS`
    # (panel_prompts.py), там же исполнитель `handle_prompt_reset`.
    # ⚠️ СПИСОК ЛИТЕРАЛОВ НИЖЕ НЕ ЗАМЕНЯТЬ ВЫЧИСЛЯЕМЫМ (`_PROMPT_RESET_CALLBACKS`
    # или приставкой): preflight.py читает роутер разбором ast и видит только
    # `data == "…"`, `data in (…)` и `data.startswith("…")` — за вычисляемым
    # ключом проверка «кнопки ↔ роутер» объявит все десять необработанными.
    # Отвечает на callback сам handle_prompt_reset, здесь `query.answer` НЕТ:
    # второй ответ Telegram отбивает ошибкой, и кнопка выглядит зависшей.
    if data in ("prompt_reset_confirm", "prompt_reset_cancel",
                "news_prompt_reset_confirm", "news_prompt_reset_cancel",
                "rag_prompt_reset_confirm", "rag_prompt_reset_cancel",
                "author_prompt_reset_confirm", "author_prompt_reset_cancel",
                "proactive_prompt_reset_confirm", "proactive_prompt_reset_cancel"):
        await handle_prompt_reset(query, user_id, data)
        return

    if data == "toggle_admin_prompt":
        current_val = get_setting(f"admin_no_prompt_{user_id}", "0")
        new_val = "0" if current_val == "1" else "1"
        set_setting(f"admin_no_prompt_{user_id}", new_val)
        status_text = "выключен (ИИ общается без промпта)" if new_val == "1" else "включен (личность C4_Max активна)"
        logger.info("🔧 Админ: персональный промпт %s", "выключен" if new_val == "1" else "включён")
        await query.answer(f"Персональный промпт {status_text}!", show_alert=True)
        try:
            _, prompt_markup = _build_prompt_panel_text_and_keyboard(user_id)
            await query.edit_message_reply_markup(reply_markup=prompt_markup)
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить клавиатуру переключения промпта: %s", e)
        return

    if data == "toggle_ai_replies":
        current_val = get_setting("ai_replies_enabled", "1")
        new_val = "0" if current_val == "1" else "1"
        set_setting("ai_replies_enabled", new_val)
        status_text = "выключены (бот молчит в личке админа)" if new_val == "0" else "включены"
        logger.info("🔧 Админ: ответы ИИ %s", "выключены" if new_val == "0" else "включены")
        await query.answer(f"Ответы ИИ {status_text}!", show_alert=True)
        try:
            _, prompt_markup = _build_prompt_panel_text_and_keyboard(user_id)
            await query.edit_message_reply_markup(reply_markup=prompt_markup)
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить клавиатуру тумблера ответов ИИ: %s", e)
        return

    if data == "toggle_thoughts":
        # Тумблер «🧠 МЫСЛИ ПОД КАПОТОМ» (2026-08-03, просьба Максима):
        # прячет и открывает свёрнутую цитату с рассуждениями модели.
        # Настройка ОБЩАЯ — действует в личке, в группах и в режиме
        # «Сам в разговор»; применяется в utils_format.build_text_and_entities.
        # ⚠️ Модели при этом продолжают думать: тумблер про ПОКАЗ, а не про
        # расход токенов — обещать здесь экономию нельзя.
        from utils_format import THOUGHTS_SETTING_KEY, thoughts_enabled
        cur_on = thoughts_enabled()
        new_val = "0" if cur_on else "1"
        set_setting(THOUGHTS_SETTING_KEY, new_val)
        state = "показываются" if new_val == "1" else "скрыты"
        logger.info("🔧 Админ %s: мысли под капотом %s (для всех)", user_id, state)
        _audit(user_id, "thoughts", 0, f"мысли под капотом {state}")
        await query.answer(
            f"🧠 Мысли под капотом {state}."
            + ("" if new_val == "1" else " Модели всё равно думают — прячется только цитата."),
            show_alert=True,
        )
        try:
            # Кнопка живёт в панели «📡 Настройки API» (переехала туда
            # 2026-08-04) — перерисовываем ЕЁ клавиатуру, а не панели промптов.
            await query.edit_message_reply_markup(reply_markup=_build_api_keyboard(user_id))
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить клавиатуру тумблера мыслей: %s", e)
        return

    if data == "web:link":
        # Одноразовая ссылка на веб-админку для НАСТОЯЩЕГО браузера
        # (30.08.2026, этап 0). Соседняя кнопка «🌐 Админка» открывает тот же
        # сайт внутри Telegram и ссылки не требует — там Telegram сам говорит
        # странице, кто пришёл.
        #
        # ⚠️ ССЫЛКА — ЭТО КЛЮЧ ОТ АДМИНКИ. Уходит в личку нажавшему и никуда
        # больше; пересылать её нельзя. Срок и подпись — web/auth.py.
        #
        # ⚠️ СРОК БЕРЁТСЯ ИЗ ПЕРЕМЕННОЙ, А НЕ ЧИСЛОМ, и сразу в трёх местах:
        # в надписи сообщения, в сроке самоудаления и в самой подписи ссылки.
        # Зашитая «5 минут» пережила бы смену срока и начала бы врать молча —
        # ровно те же грабли, что с именем модели в логе.
        from web.auth import LOGIN_LINK_TTL_SEC, make_login_url
        link = make_login_url(user_id)
        if not link:
            await query.answer("❌ Адрес сайта не настроен (WEB_PUBLIC_URL)",
                               show_alert=True)
            return
        logger.info("🌐 Админ %s запросил ссылку входа в веб-админку", user_id)
        await query.answer("Ссылка отправлена в личку")
        # Отдельным сообщением, а не всплывашкой: из всплывашки ссылку не
        # скопировать и не нажать.
        minutes = max(1, LOGIN_LINK_TTL_SEC // 60)
        sent_link = await context.bot.send_message(
            chat_id=user_id,
            text=("🌐 <b>Вход в админку</b>\n\n"
                  f'<a href="{link}">Открыть в браузере</a>\n\n'
                  f"<i>Ссылка работает {minutes} минут и только для вас. "
                  "Не пересылайте её.</i>"),
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        # Самоудаление РОВНО ПО СРОКУ ЖИЗНИ ССЫЛКИ (просьба Максима
        # 30.08.2026): дальше в переписке висел бы мёртвый ключ от админки.
        # Гигиеной панелей это сообщение убирать нельзя — она снесла бы его
        # первой же открытой панелью, не дав перейти по ссылке.
        # ⚠️ Удаление живёт в памяти процесса (utils.schedule_delete):
        # перезапустится бот в эти минуты — сообщение останется висеть.
        # Ссылка к тому моменту всё равно уже не работает.
        if sent_link:
            schedule_delete(context.bot, user_id, sent_link.message_id,
                            LOGIN_LINK_TTL_SEC)
        return

    if data.startswith("think:"):
        # Кнопки глубины раздумий в панели «📡 Настройки API» (29.08.2026):
        # «<значок провайдера> <имя> <фаза луны> <глубина>», например
        # «🐋 DeepSeek 🌔 Высокая». Нажатие листает положения ПО КРУГУ:
        # с последнего возвращает на первое — кнопки «назад» для этого не нужно.
        #
        # ⚠️ Кнопка у каждого провайдера СВОЯ, потому что провайдеры умеют
        # разное (у MiMo вообще только «думает/не думает») — см.
        # config.THINKING_LEVELS, там же живые замеры.
        #
        # ⚠️ Настройка ОБЩАЯ и действует на все ОТВЕТЫ бота: личка, группы,
        # режим «Сам в разговор», голосовые и видео. Два исключения, оба
        # намеренные: РАЗБОР вложений (у него свои зашитые уровни,
        # services/gemini.py) и СБОРКА вопросов викторины (services/quiz_bank.py
        # шлёт thinking_override=True — вопросы собираются на полную независимо
        # от кнопки, решение Максима 05.08.2026).
        provider = data.split(":", 1)[1]
        levels = THINKING_LEVELS.get(provider)
        if not levels:
            await query.answer("❌ Неизвестный провайдер!", show_alert=True)
            return
        codes = [code for code, _ in levels]
        from services.gemini import thinking_level
        cur = thinking_level(provider)
        # Текущего кода может не оказаться в списке (правка настройки руками) —
        # тогда `index` бросил бы ValueError. thinking_level такое уже чинит,
        # но защёлка дешевле, чем упавшая кнопка у Максима в руках.
        pos = codes.index(cur) if cur in codes else 0
        new_code = codes[(pos + 1) % len(codes)]
        new_label = dict(levels)[new_code]
        set_setting(THINKING_SETTING_PREFIX + provider, new_code)
        meta = PROVIDERS.get(provider, {})
        title = meta.get("title", provider)
        logger.info("🔧 Админ %s: глубина раздумий %s → %s", user_id, title, new_label)
        _audit(user_id, "thinking", 0, f"глубина {title}: {new_label}")
        # Всплывашка повторяет надпись кнопки, включая фазу луны: человек
        # видит подтверждение в том же виде, в каком оно осталось на кнопке.
        phases = THINKING_PHASES.get(len(codes))
        new_pos = codes.index(new_code)
        phase = phases[new_pos] if phases else ("🌑" if new_pos == 0 else "🌕")
        await query.answer(f"{meta.get('icon', '')} {title} {phase} {new_label}")
        try:
            await query.edit_message_reply_markup(reply_markup=_build_api_keyboard(user_id))
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить клавиатуру глубины раздумий: %s", e)
        return

    if data.startswith("set_model:"):
        new_model = data.split(":", 1)[1]
        if new_model not in AVAILABLE_MODELS:
            await query.answer("❌ Неизвестная модель!", show_alert=True)
            return
        # Нажали кнопку уже активной модели: клавиатура получится точь-в-точь
        # прежней, а Telegram на такую перерисовку отвечает ошибкой
        # «Message is not modified» (шум в логе). Просто говорим об этом.
        if get_setting("active_model", GEMINI_MODEL) == new_model:
            await query.answer(f"Модель {AVAILABLE_MODELS[new_model]['name']} уже выбрана", show_alert=False)
            return
        set_setting("active_model", new_model)
        model_name_display = AVAILABLE_MODELS[new_model]['name']
        logger.info("🔧 Админ переключил модель: %s", model_name_display)
        await query.answer(f"Модель изменена на {model_name_display}", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=_build_api_keyboard(user_id))
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить клавиатуру выбора модели: %s", e)
        return

    if data.startswith("set_image_model:"):
        new_model = data.split(":", 1)[1]
        if new_model not in AVAILABLE_IMAGE_MODELS:
            await query.answer("❌ Неизвестная модель картинок!", show_alert=True)
            return
        # Та же защита от «Message is not modified», что и у текстовых моделей
        # (наступили на этом 2026-07-19 именно на кнопке картинок).
        if get_setting("active_image_model", "gemini-3.1-flash-image") == new_model:
            await query.answer(f"Модель картинок {AVAILABLE_IMAGE_MODELS[new_model]['name']} уже выбрана",
                               show_alert=False)
            return
        set_setting("active_image_model", new_model)
        model_name_display = AVAILABLE_IMAGE_MODELS[new_model]['name']
        logger.info("🔧 Админ переключил модель картинок: %s", model_name_display)
        await query.answer(f"Модель картинок изменена на {model_name_display}", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=_build_api_keyboard(user_id))
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить клавиатуру выбора модели картинок: %s", e)
        return

    # ── Экран «💰 Счета и квоты» (остатки на счетах и квоты Qwen) ─────────
    # Префикс bal:<действие> — обработчик в panel_main.py (экран живёт внутри
    # панели «📡 Настройки API», рядом со счётчиками, которые он правит).
    if data.startswith("bal:"):
        await _handle_balance_callback(query, context, data, chat_id, user_id)
        return

    # ── 📊 Недельный дайджест группы (экран в панели статистики) ─────────
    # Префикс dig:<действие> — обработчик в panel_digest.py.
    if data.startswith("dig:"):
        await _handle_digest_callback(query, context, data, chat_id, user_id)
        return

    # ── 🎮 Викторина (панель /quizadm) ──────────────────────────────────
    # Префикс quiz:<действие> — обработчик в panel_quiz.py: сборка вопросов по
    # статьям базы знаний, разбор черновиков, одобрение и удаление.
    # ⚠️ НЕ путать с публичной кнопкой `quiz_start` (запуск игры), которая
    # разбирается в самом верху роутера, ДО гейта прав: она для всех, а эти —
    # владельческие (в _CALLBACK_RULES приставки нет = запрет по умолчанию).
    if data.startswith("quiz:"):
        await _handle_quiz_callback(query, context, data, chat_id, user_id)
        return

    # ── ⬇️ Обновления (экран в панели статистики) ────────────────────────
    # Префикс upd:<действие> — обработчик в panel_updates.py. Сами обновления
    # в роутер НЕ приходят: их кнопки ссылочные (url=…), Telegram открывает
    # страницу GitHub без участия бота — сюда попадает только листание.
    if data.startswith("upd:"):
        await _handle_updates_callback(query, context, data, chat_id, user_id)
        return

    # ── Панель модерации (/mod) ─────────────────────────────────────────
    # Префикс mod:<секция>:<действие> — задел под будущие ветки
    # (mod:mute:..., mod:ban:...). Сейчас реализована секция antispam.
    if data.startswith("mod:"):
        await _handle_mod_callback(update, context, query, user_id, data)
        return

    # ── Раздел «👥 Пользователи» (список и карточка участника) ───────────
    # Префикс usr:<секция>:<данные> — обработчик в panel_users.py.
    if data.startswith("usr:"):
        await _handle_users_callback(query, context, data, chat_id, user_id)
        return

    # ── Режим «Сам в разговор» (панель промптов) ─────────────────────────
    # Префикс proactive:<действие> — тумблер и регуляторы ➖/➕ проактивного
    # участия в разговоре групп (обработчик в panel_prompts.py).
    if data.startswith("proactive:"):
        await _handle_proactive_callback(query, user_id, data)
        return

    # ── ВРЕМЕННЫЙ дубликат «🧹 Очистить РАЗГОВОРЫ» в главной панели ───────
    # (2026-07-26, просьба Максима на время тестов). Действие то же самое, что
    # у кнопки в панели промптов, — зовём тот же обработчик, но с флагом
    # from_adm: он вернёт клавиатуру /adm, а не панели промптов.
    # Убрать вместе с кнопкой, когда тесты закончатся.
    # ⚠️ БЕЗ ПОДТВЕРЖДЕНИЯ (2026-08-10, просьба Максима «вылазит ещё одна
    # кнопка подтверждения — её нужно убрать»): жмём — и бот сразу забывает
    # разговоры. Действие обратимое, поэтому спрашивать не о чем: архив НЕ
    # удаляется, в settings лишь ставится черта времени, старше которой
    # стенограмма не читается. Та же кнопка в панели промптов подтверждение
    # СОХРАНЯЕТ — там её жмут реже и не глядя.
    # Ветки adm:wipe_yes / adm:wipe_no убраны вместе с подтверждением; если
    # у кого-то висит старое сообщение с теми кнопками, нажатие просто
    # ничего не сделает.
    if data == "adm:wipe":
        await _handle_proactive_wipe(query, user_id, True, from_adm=True)
        return
