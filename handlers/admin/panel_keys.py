# ─────────────────────────────────────────────
#  handlers/admin/panel_keys.py — ⌨️ нижние кнопки владельца (2026-10-17)
# ─────────────────────────────────────────────
#  Панель кнопок ПОД ПОЛЕМ ВВОДА в личке владельца (просьба Максима — «как в
#  другом боте»). Кнопки ведут в те же разделы, что /adm, плюс две очистки
#  и перезапуск оттуда же (перезапуск — с 18.10.2026).
#  Модераторам, другим людям и в группах панели нет.
#
#  Как устроено:
#   • send_owner_keys — шлёт короткое сообщение с панелью и ОСТАВЛЯЕТ его, а
#     предыдущее такое сообщение удаляет: в чате всегда одно. Зовут /start
#     владельца, автопанель при запуске бота (main.py) и включение выключателем.
#     ⚠️ СООБЩЕНИЕ С ПАНЕЛЬЮ УДАЛЯТЬ НЕЛЬЗЯ (v5.71 → v5.72, 18.10.2026). В v5.71
#     бот удалял его в ту же секунду — по пробе 17.10, где кнопки пережили
#     удаление, сделанное через несколько минут. При мгновенном удалении
#     Telegram панель не сохраняет: у Максима она пропала сразу. Проба 18.10:
#     сообщение лежит — панель переживает и выход из чата, и перезапуск
#     Telegram; новое пришло, старое удалено — панель на месте.
#     ⚠️ Поэтому /adm панель больше НЕ присылает: сообщение не удаляется, и
#     прыгало бы под панель на каждое /adm. Пропала (сообщение удалили
#     руками) — её вернёт /start.
#   • handle_owner_key — нажатие нижней кнопки приходит ОБЫЧНЫМ текстом. Его
#     зовёт handlers/messages.py ДО модели и до склейки: текст совпал с
#     кнопкой и пишет владелец в личке — сообщение удаляется (как команда),
#     открывается раздел. Тот же текст от кого угодно другого — обычный вопрос.
#   • Выключатель «⌨️ НИЖНИЕ КНОПКИ» в /adm (settings 'owner_keys_enabled',
#     по умолчанию включён). Выключен — сообщение с панелью удаляется, и
#     панель уходит вместе с ним; нажатия больше не перехватываются.
#   • Номер сообщения с панелью — в settings 'owner_keys_msg_<номер владельца>':
#     по нему удаляется предыдущее. ⚠️ Telegram не даёт ботам удалять сообщения
#     старше двух суток — тогда старое остаётся, и в чате их два.
#   • Подменю «📡 Настройки API» (18.10.2026, пилот) — API_ROWS: та же панель,
#     другие кнопки; сообщение с ним — тот же один в чате. Подробности — у API_ROWS.
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

from database.history import get_setting, set_setting
from utils import delete_user_message_safe, register_and_clean_bot_message, schedule_delete

logger = logging.getLogger(__name__)

SETTING_KEY = "owner_keys_enabled"
# Номер сообщения, с которым стоит панель: owner_keys_msg_<номер владельца>.
MSG_KEY_PREFIX = "owner_keys_msg_"
CARRIER_TEXT = "⌨️ Панель управления — кнопки внизу, под полем ввода."

# Надпись кнопки → код действия. Порядок — раскладка: по две кнопки в ряд
# (утверждена Максимом на пробе 17.10.2026), нечётная последняя — во всю
# ширину. Надпись и есть то, что Telegram
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
    # Перезапуск (18.10.2026, просьба Максима) — КОПИЯ кнопки /adm, там она
    # остаётся: выключишь нижнюю панель — без неё бот не останется. Сначала
    # спрашивает: нижняя панель всегда под пальцем, а в /adm та же кнопка
    # перезапускает сразу. Рядом до вечера 18.10 стоял тумблер «⬇️ ОБНОВЛЕНИЕ»;
    # самообновление теперь включено всегда (jobs/update.py) — не возвращать.
    ("🔄 ПЕРЕЗАПУСК", "restart"),
)

# Подменю «📡 Настройки API» (2026-10-18, пилот по решению Максима): нижняя
# «📡 Настройки API» присылает панель раздела, как раньше, а нижняя панель на
# время становится переключателями раздела. Ряды — раскладка; «⬅️ Назад»
# возвращает главную. Переключатели — ТЕ ЖЕ шаги, что кнопки под панелью
# (panel_main.flip_api_toggle / step_batch), а под самой панелью их тогда нет
# (panel_main.api_toggles_below). /start, перезапуск бота и выключатель всегда
# ставят главную: какое подменю открыто, бот не помнит и помнить не обязан.
API_ROWS = (
    (("🧠 МЫСЛИ ПОД КАПОТОМ", "thoughts"),),
    (("✍️ ОТВЕТ НА ГЛАЗАХ", "live_answer"),),
    (("🧠 МЫСЛИ В ЧЕРНОВИКЕ", "live_thoughts"),),
    (("➖ склейка", "batch_dec"), ("🧩 СКЛЕЙКА", "batch_info"), ("➕ склейка", "batch_inc")),
    (("⬅️ Назад", "back"),),
)
API_CARRIER_TEXT = "📡 Настройки API — переключатели внизу. «⬅️ Назад» — к разделам."
API_KEYS = tuple(key for row in API_ROWS for key in row)

_BY_LABEL = {label: code for label, code in KEYS + API_KEYS}
# У этих кнопок к надписи дописано состояние («…: 🟢ВКЛ», «…: 3 с») — узнаются
# по началу надписи.
_STATEFUL = ("thoughts", "live_answer", "live_thoughts", "batch_info")

# Сколько живут короткие ответы очисток — как у /clear.
_NOTICE_TTL_SEC = 7
# Пояснение склейки длиннее — живёт дольше.
_INFO_TTL_SEC = 20


def keys_enabled() -> bool:
    """Выключатель «⌨️ НИЖНИЕ КНОПКИ» (по умолчанию включён). Сбой чтения — выключен."""
    try:
        return get_setting(SETTING_KEY, "1") == "1"
    except Exception:
        return False


def _shown(label: str, code: str) -> str:
    """
    Надпись на кнопке с состоянием, как под панелью API: у переключателей
    надпись целиком берётся у кнопки под панелью (panel_main.api_toggle_text)
    — разойтись им нельзя.
    """
    if code in _STATEFUL:
        from .panel_main import api_toggle_text
        return api_toggle_text(code)
    return label


def _code_of(text: str) -> str | None:
    """
    Какая кнопка нажата. Кнопки с состоянием узнаются при ЛЮБОМ состоянии в
    надписи: панель с прежней надписью могла остаться на экране (а
    переключатель — поменяться на сайте), и её нажатие обязано сработать, а
    не уйти модели вопросом.
    """
    text = (text or "").strip()
    code = _BY_LABEL.get(text)
    if code:
        return code
    for label, key in KEYS + API_KEYS:
        if key in _STATEFUL and text.startswith(label + ":"):
            return key
    return None


def owner_keyboard(menu: str = "main") -> ReplyKeyboardMarkup:
    """
    Сама панель, компактная, не сворачивается сама. main — разделы по две
    кнопки в ряд; api — подменю «📡 Настройки API» по своим рядам.
    """
    if menu == "api":
        layout = API_ROWS
    else:
        layout = [KEYS[i:i + 2] for i in range(0, len(KEYS), 2)]
    rows = [[KeyboardButton(_shown(label, code)) for label, code in row] for row in layout]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _is_owner_private(chat_id: int, user_id: int | None) -> bool:
    """Личка владельца: в личке номер чата совпадает с номером человека."""
    from services.roles import is_owner
    return bool(user_id) and chat_id == user_id and is_owner(user_id)


def _stored_msg(chat_id: int) -> int:
    """Номер сообщения, с которым сейчас стоит панель (0 — не знаем такого)."""
    try:
        return int(get_setting(f"{MSG_KEY_PREFIX}{chat_id}", "0") or 0)
    except (TypeError, ValueError):
        return 0


async def _delete(bot, chat_id: int, message_id: int) -> bool:
    """Удалить сообщение; False — не вышло (например, оно старше двух суток)."""
    if not message_id:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.debug("⌨️ Не удалось удалить сообщение %s с нижними кнопками: %s", message_id, e)
        return False


async def _post(bot, chat_id: int, text: str, markup) -> None:
    """
    Прислать сообщение, с которым стоит панель (или её снятие), запомнить его и
    убрать предыдущее — уже ПОСЛЕ нового: панель живёт по последнему сообщению.
    Без звука — служебное сообщение будить телефон не должно.
    """
    previous = _stored_msg(chat_id)
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup,
                                 disable_notification=True)
    set_setting(f"{MSG_KEY_PREFIX}{chat_id}", str(msg.message_id))
    if previous and previous != msg.message_id:
        await _delete(bot, chat_id, previous)


async def send_owner_keys(bot, chat_id: int, user_id: int | None = None,
                          menu: str = "main") -> None:
    """
    Поставить нижние кнопки в личке владельца: главную панель (main) или
    подменю «📡 Настройки API» (api). Не владелец, не личка или кнопки
    выключены — ничего не делает. Тихая: панель — удобство, из-за неё не
    должны падать ни /start, ни запуск бота.
    """
    uid = chat_id if user_id is None else user_id
    if not _is_owner_private(chat_id, uid) or not keys_enabled():
        return
    text = API_CARRIER_TEXT if menu == "api" else CARRIER_TEXT
    try:
        await _post(bot, chat_id, text, owner_keyboard(menu))
    except Exception as e:
        logger.warning("⚠️ Не удалось поставить нижние кнопки владельцу %s: %s", chat_id, e)


async def remove_owner_keys(bot, chat_id: int) -> None:
    """
    Убрать нижние кнопки (выключатель погасили): удалить сообщение, с которым
    они стоят, — панель уйдёт вместе с ним. Не удалилось (старше двух суток)
    или номера нет — прислать снятие панели отдельным сообщением; оно
    остаётся в чате и уберётся при следующем включении. Тихая.
    """
    try:
        if await _delete(bot, chat_id, _stored_msg(chat_id)):
            set_setting(f"{MSG_KEY_PREFIX}{chat_id}", "0")
            return
        await _post(bot, chat_id, "⌨️ Нижние кнопки убраны.", ReplyKeyboardRemove())
    except Exception as e:
        logger.warning("⚠️ Не удалось убрать нижние кнопки у %s: %s", chat_id, e)


async def _notice(bot, chat_id: int, text: str, ttl: int = _NOTICE_TTL_SEC) -> None:
    """Короткий ответ очистки или переключателя — сам исчезает, как ответ /clear."""
    msg = await bot.send_message(chat_id=chat_id, text=text)
    if msg:
        schedule_delete(bot, chat_id, msg.message_id, ttl)


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
        # Панель раздела — как раньше, а внизу — его переключатели.
        from .panel_main import send_api_panel
        await send_api_panel(bot, chat_id, user_id)
        await send_owner_keys(bot, chat_id, user_id, menu="api")
    elif code in ("thoughts", "live_answer", "live_thoughts"):
        from .panel_main import flip_api_toggle
        await _notice(bot, chat_id, flip_api_toggle(code, user_id))
        await send_owner_keys(bot, chat_id, user_id, menu="api")
    elif code in ("batch_dec", "batch_inc"):
        from .panel_main import step_batch
        said = step_batch(user_id, 1 if code == "batch_inc" else -1)
        if said is None:
            # Упёрлись в предел — надписи те же, панель заново не нужна.
            await _notice(bot, chat_id, "Это крайнее значение.")
            return
        await _notice(bot, chat_id, said)
        await send_owner_keys(bot, chat_id, user_id, menu="api")
    elif code == "batch_info":
        from .panel_main import BATCH_INFO_TEXT
        await _notice(bot, chat_id, BATCH_INFO_TEXT, _INFO_TTL_SEC)
    elif code == "back":
        await send_owner_keys(bot, chat_id, user_id)
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
    elif code == "restart":
        # Сначала вопрос. «✅ Да» — та же ветка system_restart, что у кнопки
        # /adm (router.py): заберёт свежий код и перезапустит бота.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        sent = await bot.send_message(
            chat_id=chat_id, parse_mode=ParseMode.HTML,
            text="🔄 <b>Перезапустить бота?</b>\n<i>На сервере он сначала заберёт "
                 "свежий код с GitHub.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, перезапустить", callback_data="system_restart"),
                InlineKeyboardButton("Отмена", callback_data="keys:restart_no"),
            ]]))
        if sent:
            await register_and_clean_bot_message(bot, chat_id, sent.message_id)


async def handle_keys_callback(query, data: str) -> None:
    """
    Кнопки под сообщениями нижней панели (router.py, ветка «keys:»). Сейчас
    одна — «Отмена» у вопроса о перезапуске: вопрос убирается.
    """
    if data == "keys:restart_no":
        await query.answer("Отменено.")
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug("⌨️ Не удалось убрать вопрос о перезапуске: %s", e)
        return
    await query.answer("Кнопка устарела.", show_alert=True)


async def handle_owner_key(update, context, text: str) -> bool:
    """
    Нажатие нижней кнопки владельца. Возвращает True, если это была она —
    тогда сообщение уже обработано и модели не уходит.

    Перехватывает ТОЛЬКО: личку, владельца, включённые кнопки и текст ровно
    как на кнопке. Как любая команда, гасит ожидание числа (экраны счетов и
    счёта викторины) и режимы панели базы знаний.
    """
    code = _code_of(text)
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
