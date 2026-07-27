# ───────────────────────────────────────────────
#  handlers/admin/router.py — handle_callback_query — ЕДИНСТВЕННЫЙ роутер ВСЕХ callback-кнопок бота.
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import logging
import os

import logging_setup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, ADMIN_IDS, GEMINI_MODEL
from database.history import set_setting, get_setting, append_prompt_addition, get_active_system_prompt, get_bot_stats, get_news_system_prompt, get_rag_instruction
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete


logger = logging.getLogger(__name__)
from .common import _adm_back_row, _audit, _build_log_text, _read_current_log
from .panel_main import (_build_api_keyboard, _handle_balance_callback, build_adm_keyboard,
                         send_adm_panel, send_api_panel,
                         send_daily_report_panel, send_weekly_report_panel,
                         send_stats_panel)
from .panel_mod import _handle_mod_callback, send_mod_panel
from .panel_prompts import (_build_prompt_panel_text_and_keyboard, _handle_proactive_callback,
                            _handle_proactive_wipe, send_prompts_panel, send_prompt_files)
from .panel_rag import _end_kb_test, _handle_kb_callback, send_rag_panel
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

    # ── Кнопка «📜 Логи бота»: текст с максимумом последних строк лога ────
    if data == "adm_logs":
        # Переход из панели отменяет ожидание файлов и режим проверки поиска
        context.user_data.pop("kb_add_mode", None)
        context.user_data.pop("kb_replace_target", None)
        await _end_kb_test(context.bot, chat_id, context)
        log_path, raw = _read_current_log()
        if not raw:
            await query.answer("⚠️ Файл лога текущей сессии пуст или не найден", show_alert=True)
            return
        await query.answer()
        logger.info("🔧 Админ %s запросил логи текущей сессии", user_id)
        fname = os.path.basename(log_path)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💾 Скачать файл", callback_data="adm_logs_file")],
            _adm_back_row(),
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

    # ── Кнопка «💾 Скачать файл»: полный файл лога документом ─────────────
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
                f"<code>{html.escape(fname)}</code> · {size_kb} КБ"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([_adm_back_row()]),
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
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
            "⬇️ Самообновление включено — раз в 10 минут бот проверяет GitHub.",
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

    if data == "prompt_reset_confirm":
        set_setting("custom_system_prompt", "")
        set_setting("prompt_additions", "")
        logger.info("🔧 Админ %s сбросил системный промпт к заводским настройкам", user_id)
        await query.answer("✅ Промпт сброшен к заводским настройкам!", show_alert=True)
        try:
            await query.edit_message_text(
                "✅ <b>Системный промпт сброшен!</b>\n\n"
                "Кастомный промпт и все дополнения удалены.\n"
                "Бот снова использует заводской промпт из <code>config.py</code>.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение сброса промпта: %s", e)
        return

    if data == "prompt_reset_cancel":
        await query.answer("Отменено.", show_alert=False)
        try:
            await query.edit_message_text(
                "🔄 <b>Сброс промпта отменён.</b>\n\nТекущий промпт остался без изменений.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение отмены сброса: %s", e)
        return

    if data == "news_prompt_reset_confirm":
        set_setting("news_system_prompt", "")
        logger.info("🔧 Админ %s удалил промпт новостей", user_id)
        await query.answer("✅ Промпт новостей удалён!", show_alert=True)
        try:
            await query.edit_message_text(
                "✅ <b>Промпт новостей удалён!</b>\n\n"
                "Новости теперь форматируются без системного промпта.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение сброса промпта новостей: %s", e)
        return

    if data == "news_prompt_reset_cancel":
        await query.answer("Отменено.", show_alert=False)
        try:
            await query.edit_message_text(
                "🔄 <b>Сброс промпта новостей отменён.</b>\n\nТекущий промпт остался без изменений.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение отмены сброса промпта новостей: %s", e)
        return

    if data == "rag_prompt_reset_confirm":
        # Удаляем свою инструкцию — get_rag_instruction вернётся к заводской.
        set_setting("rag_instruction", "")
        logger.info("🔧 Админ %s вернул заводскую RAG-инструкцию", user_id)
        await query.answer("✅ Заводская RAG-инструкция возвращена!", show_alert=True)
        try:
            await query.edit_message_text(
                "✅ <b>Заводская RAG-инструкция возвращена.</b>\n\n"
                "Своя удалена — модель снова получает стандартную «шапку» перед статьями.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение сброса RAG-инструкции: %s", e)
        return

    if data == "rag_prompt_reset_cancel":
        await query.answer("Отменено.", show_alert=False)
        try:
            await query.edit_message_text(
                "🔄 <b>Возврат заводской RAG-инструкции отменён.</b>\n\nТвоя инструкция осталась без изменений.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение отмены сброса RAG-инструкции: %s", e)
        return

    if data == "proactive_prompt_reset_confirm":
        # Удаляем свою инструкцию — get_proactive_instruction вернётся к заводской.
        set_setting("proactive_instruction", "")
        logger.info("🔧 Админ %s вернул заводскую инструкцию участия в разговоре", user_id)
        await query.answer("✅ Заводская инструкция участия возвращена!", show_alert=True)
        try:
            await query.edit_message_text(
                "✅ <b>Заводская инструкция участия в разговоре возвращена.</b>\n\n"
                "Своя удалена — режим «Сам в разговор» снова работает по стандартным правилам.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение сброса инструкции участия: %s", e)
        return

    if data == "proactive_prompt_reset_cancel":
        await query.answer("Отменено.", show_alert=False)
        try:
            await query.edit_message_text(
                "🔄 <b>Возврат заводской инструкции участия отменён.</b>\n\nТвоя инструкция осталась без изменений.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning("⚠️ Не удалось обновить сообщение отмены сброса инструкции участия: %s", e)
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
    if data in ("adm:wipe", "adm:wipe_yes", "adm:wipe_no"):
        if data == "adm:wipe_no":
            await query.answer("Отменено.")
            try:
                await query.edit_message_reply_markup(reply_markup=build_adm_keyboard(user_id))
            except Exception as e:
                logger.debug("🔧 Не удалось вернуть клавиатуру /adm после отмены: %s", e)
            return
        await _handle_proactive_wipe(query, user_id, data == "adm:wipe_yes", from_adm=True)
        return
