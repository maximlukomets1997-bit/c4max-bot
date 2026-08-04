import html
import logging
from telegram import (Update, LinkPreviewOptions, InlineKeyboardButton,
                      InlineKeyboardMarkup)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import MAX_CONTEXT_MESSAGES, IMAGE_DAILY_LIMIT
from database.history import clear_history
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete
from utils_format import reply_md

logger = logging.getLogger(__name__)

# Разделитель панелей — 27 знаков «─» (U+2500), общий стандарт проекта.
_SEP = "─" * 27


async def _send_public_panel(bot, chat_id: int, text: str, markup=None):
    """
    Отправка публичного экрана (/start, /help) с общим запасным путём.

    До 2026-08-04 у /start и /help было по своей копии этой отправки — две
    почти дословно одинаковые лестницы if/else на 25 строк каждая. Теперь она
    одна: разметка не собралась — уходим без неё, но человек всё равно получает
    ответ, а не тишину.
    """
    try:
        sent = await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as e:
        logger.warning("⚠️ Не удалось отправить экран с разметкой: %s — отправляю без неё", e)
        import re as _re
        sent = await bot.send_message(
            chat_id=chat_id, text=_re.sub(r"<[^>]+>", "", text), reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    if sent:
        await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    return sent


# ─────────────────────────────────────────────
#  🏠 Главное меню (/start) — экран с кнопками
#
#  Зачем кнопки: раньше /start и /help были стеной текста со списком команд,
#  которые человек должен был перепечатывать руками. Половина возможностей
#  бота при этом оставалась невидимой — справочник /ttx и викторина видны
#  только тому, кто сам догадался их поискать.
#
#  ⚠️ Кнопки ПУБЛИЧНЫЕ: их разбирает роутер ДО гейта прав, как quiz_start.
# ─────────────────────────────────────────────

def _menu_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура главного экрана. Надпись кнопки новостей показывает ТЕКУЩЕЕ
    состояние подписки ЭТОГО чата (стандарт тумблеров проекта — метка через
    общий _onoff, а не «Подписаться»/«Отписаться»).
    """
    from database.history import is_chat_subscribed
    from handlers.admin.common import _onoff
    news = is_chat_subscribed(chat_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 ТТХ техники", callback_data="menu:ttx"),
            InlineKeyboardButton("🎮 Викторина", callback_data="quiz_start"),
        ],
        [
            InlineKeyboardButton("🎖 Моё звание", callback_data="menu:rank"),
            InlineKeyboardButton(f"📰 Новости: {_onoff(news)}", callback_data="menu:news"),
        ],
        [InlineKeyboardButton("📖 Все команды", callback_data="menu:help")],
    ])


def _menu_text(bot_username: str, first_name: str = "") -> str:
    """Текст главного экрана. Одинаков в личке и в группе — он короткий."""
    hello = f"👋 <b>Привет, {html.escape(first_name)}!</b>" if first_name else "👋 <b>Привет!</b>"
    return (
        f"{hello}\n\n"
        f"Я <b>C4_Max</b> — бот по War Thunder Mobile.\n"
        f"Отвечу на вопрос, покажу ТТХ, погоняю в викторине.\n"
        f"{_SEP}\n"
        f"💬 <b>В личке</b> пиши что угодно — можно голосовым, фото или видео, "
        f"я всё пойму.\n"
        f"🗣 <b>В группе</b> — упомяни @{bot_username} или ответь на моё сообщение.\n"
        f"{_SEP}\n"
        f"<i>Жми кнопки — команды набирать не обязательно.</i>"
    )


def _help_text(bot_username: str) -> str:
    """Полный справочник — то, что не влезло на главный экран."""
    return (
        f"📖 <b>ВСЕ КОМАНДЫ</b>\n"
        f"{_SEP}\n"
        f"<b>📊 /ttx</b> &lt;название&gt; — ТТХ техники прямо из базы знаний, "
        f"мгновенно и без нейросети.\n"
        f"   <code>/ttx ариете</code> · <code>/ttx Leopard 2A5</code> · "
        f"<code>/ttx т80уе1</code>\n"
        f"   Понимаю названия, игровые прозвища, латиницу и кириллицу.\n\n"
        f"<b>🎖 /rank</b> — звание, статистика викторин, остаток картинок "
        f"и служба в гарнизоне.\n\n"
        f"<b>🎨 /imagine</b> &lt;описание&gt; — картинка по описанию "
        f"(до {IMAGE_DAILY_LIMIT} в сутки).\n"
        f"   <code>/imagine горящий Абрамс в пустыне</code>\n\n"
        f"<b>🗑 /clear</b> — забыть наш разговор и начать с чистого листа.\n\n"
        f"<b>📰 /subscribe</b> · <b>/unsubscribe</b> — рассылка новостей "
        f"wtmobile.com. Проще кнопкой «📰 Новости» на главном экране.\n"
        f"{_SEP}\n"
        f"<b>💬 Как со мной говорить</b>\n"
        f"• В личке — просто пиши. Понимаю текст, голосовые, фото и видео.\n"
        f"• В группе — упомяни @{bot_username} или ответь на моё сообщение.\n"
        f"• Помню последние <b>{MAX_CONTEXT_MESSAGES}</b> сообщений разговора.\n"
        f"• Могу подсказать по технике где угодно: набери в любом чате "
        f"<code>@{bot_username} ариете</code>."
    )


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Главное меню", callback_data="menu:back")
    ]])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — главный экран с кнопками. Работает и в личке, и в группе."""
    await delete_user_message_safe(update.message)
    # logger.info("ℹ️ Команда /start (user %s)", update.effective_user.id)  # скрыто по просьбе
    chat_id = update.effective_chat.id
    user = update.effective_user
    await _send_public_panel(
        context.bot, chat_id,
        _menu_text(context.bot.username, user.first_name if user else ""),
        _menu_keyboard(chat_id),
    )



async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_user_message_safe(update.message)
    # logger.info("ℹ️ Команда /help (user %s)", update.effective_user.id)  # скрыто по просьбе
    chat_id = update.effective_chat.id
    await _send_public_panel(
        context.bot, chat_id, _help_text(context.bot.username), _back_keyboard()
    )


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


async def handle_menu_callback(query, context, data: str) -> None:
    """
    Кнопки главного экрана: menu:ttx | menu:rank | menu:news | menu:help | menu:back.

    ⚠️ Зовётся из роутера ДО гейта прав — это кнопки для ВСЕХ, как quiz_start.
    Кнопка «🎮 Викторина» своей ветки здесь не имеет намеренно: она шлёт
    существующий `quiz_start`, второго входа в викторину заводить незачем.
    """
    what = data.split(":", 1)[1] if ":" in data else ""
    bot = query.get_bot()
    chat_id = query.message.chat_id if query.message else None
    if chat_id is None:
        await query.answer()
        return

    if what == "news":
        await _toggle_news(query, chat_id)
        return

    if what == "rank":
        # Личное дело уходит НОВЫМ сообщением (у него своя клавиатура и своя
        # длина), а гигиена панелей уберёт за ним главный экран.
        await query.answer()
        from handlers.quiz import send_rank_panel
        await send_rank_panel(bot, chat_id, query.from_user)
        return

    await query.answer()
    if what == "ttx":
        from handlers.tech import ttx_hint_text
        text, markup = ttx_hint_text(bot), _back_keyboard()
    elif what == "help":
        text, markup = _help_text(bot.username), _back_keyboard()
    else:  # back — возврат на главный экран
        text = _menu_text(bot.username, query.from_user.first_name or "")
        markup = _menu_keyboard(chat_id)

    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except Exception as e:
        # Самое частое — нажали кнопку экрана, который уже открыт: Telegram
        # отвечает «Message is not modified». Ругаться незачем.
        logger.debug("🏠 Не удалось перерисовать главный экран: %s", e)


async def _toggle_news(query, chat_id: int) -> None:
    """
    Тумблер «📰 Новости» — подписка ЭТОГО чата на рассылку.

    ⚠️ В ГРУППЕ жать может только персонал. Подписка групповая: одним касанием
    любой участник отписал бы от новостей весь чат, и никто бы не понял, куда
    они делись. В личке ограничения нет — это свой чат человека.
    (Команды /subscribe и /unsubscribe остались как были и такой проверки не
    имеют — менять их поведение Максим не просил.)
    """
    from database.history import is_chat_subscribed, subscribe_chat, unsubscribe_chat
    is_group = query.message.chat.type in ("group", "supergroup") if query.message else False
    if is_group:
        from services.roles import has_any_perm
        if not has_any_perm(query.from_user.id):
            await query.answer("📰 Подписку группы меняют администраторы.", show_alert=True)
            return

    if is_chat_subscribed(chat_id):
        unsubscribe_chat(chat_id)
        logger.info("📰 Чат %s: отписка от новостей (кнопкой)", chat_id)
        await query.answer("📰 Новости выключены.")
    else:
        subscribe_chat(chat_id)
        logger.info("📰 Чат %s: подписка на новости (кнопкой)", chat_id)
        await query.answer("📰 Новости включены — пришлю, как только выйдут.")

    # Меняется только надпись кнопки, текст экрана прежний — перерисовываем
    # ОДНУ клавиатуру: это дешевле и не мигает сообщением.
    try:
        await query.edit_message_reply_markup(reply_markup=_menu_keyboard(chat_id))
    except Exception as e:
        logger.debug("📰 Не удалось обновить кнопку новостей: %s", e)


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
        # Заодно снимаем ожидание числа для экрана «💰 Счета и квоты»: человек
        # набрал команду — значит, вводить остаток или квоту он передумал.
        # Единственное место, где это ловится для ВСЕХ команд сразу (кнопки
        # гасят ожидание в router.py). Ни на что больше не влияет.
        context.user_data.pop("balance_edit", None)
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

