import logging
from telegram import Update, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import MAX_CONTEXT_MESSAGES
from database.history import clear_history
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete
from utils_format import reply_md

logger = logging.getLogger(__name__)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    # logger.info("ℹ️ Команда /start (user %s)", update.effective_user.id)  # скрыто по просьбе

    text = (
        f"<b>💬 Как общаться:</b>\n"
        f"• <b>В группах</b> — упомяните меня @{context.bot.username} или сделайте Reply на моё сообщение.\n"
        f"• <b>В личных сообщениях</b> — пишите любые вопросы напрямую.\n\n"
        f"<b>🛠 Доступные команды:</b>\n"
        f"/help — справочник команд и описание работы.\n"
        f"/imagine [описание] — сгенерировать изображение с помощью ИИ.\n"
        f"/rank — просмотреть свою статистику ответов в викторинах.\n"
        f"/subscribe — подписаться на автоматическую рассылку новостей.\n"
        f"/unsubscribe — отключить рассылку новостей.\n"
        f"/clear — очистить контекст текущего диалога.\n"
    )
    chat_id = update.effective_chat.id
    sent_msg = None
    try:
        if update.message:
            sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
                text, 
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id, 
                text=text, 
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
    except Exception as e:
        logger.warning("⚠️ Не удалось отправить /start с разметкой: %s — отправляю без разметки", e)
        clean_text = text.replace("*", "").replace("`", "")
        if update.message:
            sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
                clean_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id, 
                text=clean_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)



async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    # logger.info("ℹ️ Команда /help (user %s)", update.effective_user.id)  # скрыто по просьбе
    text = (
        "📖 <b>Справочник команд ИИ-ассистента</b>\n\n"
        "<b>Личные сообщения:</b>\n"
        "Пишите любые вопросы напрямую — я постараюсь ответить на них максимально развернуто.\n\n"
        "<b>Общие группы:</b>\n"
        f"• Упомяните меня @{context.bot.username} в тексте сообщения\n"
        "• Или сделайте ответ (Reply) на любое моё сообщение\n\n"
        "<b>Доступные команды:</b>\n"
        "/start — запустить бота / показать приветствие\n"
        "/imagine — сгенерировать картинку по описанию (например, <code>/imagine красивый закат</code>)\n"
        "/rank — посмотреть свой ранг и количество очков в викторине\n"
        "/clear — полностью очистить историю диалога (память контекста)\n"
        "/subscribe — подписаться на новостную рассылку\n"
        "/unsubscribe — отписаться от новостной рассылки\n\n"
        f"Контекст диалога: последние <b>{MAX_CONTEXT_MESSAGES}</b> сообщений (скользящее окно)."
    )
    chat_id = update.effective_chat.id
    sent_msg = None
    try:
        if update.message:
            sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
                text, 
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id, 
                text=text, 
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
    except Exception as e:
        logger.warning("⚠️ Не удалось отправить /help с разметкой: %s — отправляю без разметки", e)
        clean_text = text.replace("*", "").replace("`", "")
        if update.message:
            sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=
                clean_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        else:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id, 
                text=clean_text,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    clear_history(user_id)
    sent_msg = None
    text = "🗑️ <b>Окно контекста нашего диалога очищено!</b>\nНачинаем общение с чистого листа."
    if update.message:
        sent_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode=ParseMode.HTML)
    else:
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
    if sent_msg:
        schedule_delete(context.bot, chat_id, sent_msg.message_id, 7)

    # Авто-вызов /adm после очистки (только в личке и для персонала)
    if update.effective_chat.type == "private":
        from services.roles import has_any_perm
        if has_any_perm(user_id):
            from handlers.admin import send_adm_panel
            await send_adm_panel(context.bot, chat_id, user_id)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    chat_id = update.effective_chat.id
    from database.history import subscribe_chat
    
    is_new = subscribe_chat(chat_id)
    # logger.info("📰 Чат %s: подписка на новости (%s)", chat_id, "оформлена" if is_new else "уже была активна")  # скрыто по просьбе
    if is_new:
        text = (
            "**📰 Подписка оформлена!**\n"
            "Этот чат успешно подключен к автоматической рассылке новостей и обновлений!\n\n"
            "/unsubscribe - отменить подписку."
        )
    else:
        text = (
            "**📰 Подписка уже активна!**\n"
            "Этот чат уже получает рассылку новостей и обновлений!\n\n"
            "/unsubscribe - отменить подписку."
        )
        
    sent_msg = await reply_md(context.bot, chat_id, text)
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    chat_id = update.effective_chat.id
    from database.history import unsubscribe_chat
    
    is_removed = unsubscribe_chat(chat_id)
    logger.info("📰 Чат %s: отписка от новостей (%s)", chat_id, "выполнена" if is_removed else "не был подписан")
    if is_removed:
        text = (
            "**⚠️ Подписка отменена!**\n"
            "Этот чат отписан от всех рассылок."
        )
    else:
        text = (
            "**⚠️ Подписка не найдена!**\n"
            "Этот чат не был подписан на рассылки."
        )
        
    sent_msg = await reply_md(context.bot, chat_id, text)
    if sent_msg:
        await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)


async def log_incoming_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Центральный регистратор команд (handler group=-1 в handlers/__init__.py):
    пишет в лог КАЖДУЮ входящую команду — публичные, админские, неизвестные,
    в том числе попытки не-админов дёрнуть админские — раньше основных
    обработчиков. Ни на что не влияет: команда дальше обрабатывается как
    обычно. Единственная точка, где включается/выключается лог команд.
    Обязан НИКОГДА не бросать исключения (как collect_group_message).
    """
    try:
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or not message.text:
            return
        # Длинные команды (/prompt_set с текстом и т.п.) обрезаем — сам текст
        # промпта в логе не нужен, факт вызова важнее.
        cmd_text = message.text if len(message.text) <= 100 else message.text[:100] + "…"
        if user is not None:
            uname = f"@{user.username}" if user.username else "без ника"
            who = f"{user.full_name} ({uname})"
        else:
            who = "(отправитель неизвестен)"
        if chat is not None and chat.type == "private":
            where = "личка"
        elif chat is not None:
            where = f"чат {chat.id}"
        else:
            where = "чат неизвестен"
        logger.info("⌨️ Команда: %s | от %s | %s", cmd_text, who, where)
    except Exception as e:
        logger.debug("⌨️ Не удалось записать команду в лог: %s", e)


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Неизвестная команда: МОЛЧА удаляем ошибочное сообщение, ничего
    не отвечая в чат.

    В группе удаление сработает только если у бота есть право удалять
    сообщения; в личке бот может удалить входящее сообщение. Ошибки
    удаления глушатся внутри delete_user_message_safe.
    """
    if not update.message:
        return

    await delete_user_message_safe(update.message)



# ───────────────────────────────────────────────
#  Запуск
# ───────────────────────────────────────────────

