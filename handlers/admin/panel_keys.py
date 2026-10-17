# ─────────────────────────────────────────────
#  handlers/admin/panel_keys.py — ⌨️ нижние кнопки владельца (2026-10-17)
# ─────────────────────────────────────────────
#  Панель кнопок ПОД ПОЛЕМ ВВОДА в личке владельца (просьба Максима — «как в
#  другом боте»). Кнопки ведут в те же разделы, что /adm, плюс две очистки
#  оттуда же. Модераторам, другим людям и в группах панели нет.
#
#  Как устроено:
#   • send_owner_keys — шлёт короткое сообщение с панелью и сразу его удаляет:
#     живая проба 17.10.2026 показала, что кнопки остаются и без сообщения, с
#     которым пришли. Так в чате ничего не копится. Зовут /start и /adm
#     владельца и автопанель при запуске бота (main.py) — пропадёт панель
#     однажды сама, любая из трёх её вернёт.
#   • handle_owner_key — нажатие нижней кнопки приходит ОБЫЧНЫМ текстом. Его
#     зовёт handlers/messages.py ДО модели и до склейки: текст совпал с
#     кнопкой и пишет владелец в личке — сообщение удаляется (как команда),
#     открывается раздел. Тот же текст от кого угодно другого — обычный вопрос.
#   • Выключатель «⌨️ НИЖНИЕ КНОПКИ» в /adm (settings 'owner_keys_enabled',
#     по умолчанию включён). Выключен — панель убирается, нажатия больше не
#     перехватываются.
#
#  ⚠️ «🌐 Сайт» — НЕ кнопка-мини-приложение (проба 17.10.2026): открытый с
#  нижней кнопки сайт не получает от Telegram, кто его открыл, и пишет «Вход
#  только через Telegram». Поэтому кнопка присылает те же две, что вверху /adm
#  (panel_main._web_row): там вход работает.
#  ⚠️ Раздел открывается ТЕМИ ЖЕ функциями, что кнопки /adm (router.py), и
#  гасит те же режимы ожидания, что любая команда: второго набора правил
#  «что открыть» в проекте быть не должно.
# ─────────────────────────────────────────────

import logging

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode

from database.history import get_setting
from utils import delete_user_message_safe, register_and_clean_bot_message, schedule_delete

logger = logging.getLogger(__name__)

SETTING_KEY = "owner_keys_enabled"

# Надпись кнопки → код действия. Порядок — раскладка: по две кнопки в ряд
# (утверждена Максимом на пробе 17.10.2026). Надпись и есть то, что Telegram
# пришлёт текстом при нажатии, — менять её значит менять и распознавание.
KEYS = (
    ("🎛 Админ-панель", "adm"),
    ("💬 Статистика", "stats"),
    ("⚙️ PROMPTы", "prompts"),
    ("📡 Настройки API", "api"),
    ("🛠 DDoS-Guard", "mod"),
    ("👥 Пользователи", "users"),
    ("📚 База знаний", "rag"),
    ("🎮 Викторина", "quiz"),
    ("📜 Логи бота", "logs"),
    ("🌐 Сайт", "site"),
    ("🧹 Очистить РАЗГОВОРЫ", "wipe"),
    ("🗑 Очистить мой диалог", "clear"),
)
_BY_LABEL = {label: code for label, code in KEYS}

# Сколько живут короткие ответы очисток — как у /clear.
_NOTICE_TTL_SEC = 7


def keys_enabled() -> bool:
    """Выключатель «⌨️ НИЖНИЕ КНОПКИ» (по умолчанию включён). Сбой чтения — выключен."""
    try:
        return get_setting(SETTING_KEY, "1") == "1"
    except Exception:
        return False


def owner_keyboard() -> ReplyKeyboardMarkup:
    """Сама панель: по две кнопки в ряд, компактная, не сворачивается сама."""
    rows = [[KeyboardButton(label) for label, _ in KEYS[i:i + 2]]
            for i in range(0, len(KEYS), 2)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _is_owner_private(chat_id: int, user_id: int | None) -> bool:
    """Личка владельца: в личке номер чата совпадает с номером человека."""
    from services.roles import is_owner
    return bool(user_id) and chat_id == user_id and is_owner(user_id)


async def _flash(bot, chat_id: int, text: str, markup) -> None:
    """
    Поставить или убрать панель: сообщение с ней и сразу удалить. Без звука —
    иначе каждая /adm будила бы телефон уведомлением о пустом месте.
    """
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup,
                                 disable_notification=True)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    except Exception as e:
        logger.debug("⌨️ Не удалось убрать сообщение с нижними кнопками: %s", e)


async def send_owner_keys(bot, chat_id: int, user_id: int | None = None) -> None:
    """
    Поставить нижние кнопки в личке владельца. Не владелец, не личка или
    кнопки выключены — ничего не делает. Тихая: панель — удобство, из-за неё
    не должна падать ни /adm, ни запуск бота.
    """
    uid = chat_id if user_id is None else user_id
    if not _is_owner_private(chat_id, uid) or not keys_enabled():
        return
    try:
        await _flash(bot, chat_id, "⌨️ Панель управления — кнопки внизу, под полем ввода.",
                     owner_keyboard())
    except Exception as e:
        logger.warning("⚠️ Не удалось поставить нижние кнопки владельцу %s: %s", chat_id, e)


async def remove_owner_keys(bot, chat_id: int) -> None:
    """Убрать нижние кнопки (выключатель погасили). Тихая."""
    try:
        await _flash(bot, chat_id, "⌨️ Нижние кнопки убраны.", ReplyKeyboardRemove())
    except Exception as e:
        logger.warning("⚠️ Не удалось убрать нижние кнопки у %s: %s", chat_id, e)


async def _notice(bot, chat_id: int, text: str) -> None:
    """Короткий ответ очистки — сам исчезает, как ответ /clear."""
    msg = await bot.send_message(chat_id=chat_id, text=text)
    if msg:
        schedule_delete(bot, chat_id, msg.message_id, _NOTICE_TTL_SEC)


async def _run(code: str, context, chat_id: int, user_id: int) -> None:
    """
    Открыть раздел по коду кнопки. Функции — те же, что у кнопок /adm
    (router.py); берутся в момент нажатия: модули панелей тянут этот файл
    обратно через оглавление пакета.
    """
    bot = context.bot
    if code == "adm":
        from .panel_main import send_adm_panel
        await send_adm_panel(bot, chat_id, user_id)
    elif code == "stats":
        from .panel_main import send_stats_panel
        await send_stats_panel(bot, chat_id, user_id)
    elif code == "prompts":
        from .panel_prompts import send_prompts_panel
        await send_prompts_panel(bot, chat_id, user_id)
    elif code == "api":
        from .panel_main import send_api_panel
        await send_api_panel(bot, chat_id, user_id)
    elif code == "mod":
        from .panel_mod import send_mod_panel
        await send_mod_panel(bot, chat_id, user_id)
    elif code == "users":
        from .panel_users import send_users_panel
        await send_users_panel(bot, chat_id, user_id)
    elif code == "rag":
        from .panel_rag import send_rag_panel
        await send_rag_panel(bot, chat_id, context)
    elif code == "quiz":
        from .panel_quiz import send_quiz_panel
        await send_quiz_panel(bot, chat_id, context)
    elif code == "logs":
        from .common import send_logs_menu
        await send_logs_menu(bot, chat_id)
    elif code == "site":
        from telegram import InlineKeyboardMarkup
        from .panel_main import _web_row
        rows = _web_row()
        if not rows:
            await _notice(bot, chat_id, "🌐 Сайт не поднят или не настроен его адрес.")
            return
        sent = await bot.send_message(
            chat_id=chat_id, parse_mode=ParseMode.HTML,
            text="🌐 <b>Веб-админка</b>\n<i>Внутри Telegram — «🌐 Админка», "
                 "в настоящем браузере — ссылка на пять минут.</i>",
            reply_markup=InlineKeyboardMarkup(rows))
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)
    elif code == "wipe":
        from .panel_prompts import wipe_conversations
        wipe_conversations(user_id)
        await _notice(bot, chat_id, "🧹 Готово: бот забыл разговоры во всех группах.")
    elif code == "clear":
        from database.history import clear_history
        clear_history(user_id)
        # Та же строка лога, что у кнопки «🗑 Очистить мой диалог» в /adm:
        # по ней Максиму считают, как часто он чистит диалог.
        logger.info("🔧 История диалога очищена кнопкой (пользователь %s)", user_id)
        await _notice(bot, chat_id, "✅ История диалога очищена!")


async def handle_owner_key(update, context, text: str) -> bool:
    """
    Нажатие нижней кнопки владельца. Возвращает True, если это была она —
    тогда сообщение уже обработано и модели не уходит.

    Перехватывает ТОЛЬКО: личку, владельца, включённые кнопки и текст ровно
    как на кнопке. Как любая команда, гасит ожидание числа (экраны счетов и
    счёта викторины) и режимы панели базы знаний.
    """
    code = _BY_LABEL.get((text or "").strip())
    if not code:
        return False
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or not _is_owner_private(chat.id, user.id):
        return False
    if not keys_enabled():
        return False
    await delete_user_message_safe(update.message)
    for key in ("balance_edit", "quiz_edit", "kb_add_mode", "kb_replace_target"):
        context.user_data.pop(key, None)
    from .panel_rag import _end_kb_test
    await _end_kb_test(context.bot, chat.id, context)
    logger.info("⌨️ Нижняя кнопка: %s | владелец %s", text.strip(), user.id)
    try:
        await _run(code, context, chat.id, user.id)
    except Exception as e:
        logger.warning("⚠️ Нижняя кнопка «%s» не сработала: %s", text.strip(), e)
    return True
