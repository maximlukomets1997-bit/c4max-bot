# ─────────────────────────────────────────────
#  services/greeter.py — 👋 приветствие новичков и заслон от спам-ботов
#  (2026-08-04)
#
#  Зачем: до этого бот узнавал о человеке только из его ПЕРВОГО СООБЩЕНИЯ.
#  Спам-аккаунт успевал войти и запостить, а фильтр ссылок срабатывал уже
#  по факту. Здесь заслон стоит РАНЬШЕ первого сообщения: вошедшему
#  ограничивается право писать, и оно возвращается только после нажатия
#  кнопки «Я не бот». Спам-боты кнопок не жмут.
#
#  Контракт:
#  • Единственная точка входа — on_chat_member(update, context), её зовёт
#    ChatMemberHandler (handlers/__init__.py, group=0). Кнопку разбирает
#    handle_join_callback, её зовёт роутер ДО гейта прав: жмёт её обычный
#    участник группы, а не персонал.
#  • Модуль «ТИХИЙ», как antispam и proactive: любое исключение глушится.
#    Сорвавшееся приветствие не повод ронять обработку событий чата.
#  • Настройки общие на все группы (settings, кнопки в панели /mod):
#    greet_enabled / greet_captcha / greet_timeout_sec / greet_kick.
#    Семантика ключей ПРЯМАЯ — «1» значит «работает».
#
#  ⚠️ ТРИ ВЕЩИ, БЕЗ КОТОРЫХ ЭТО МОЛЧА НЕ РАБОТАЕТ:
#  1. Telegram присылает события chat_member, ТОЛЬКО если их явно
#     запросили в allowed_updates (main.py) — по умолчанию их НЕТ.
#     Забыть это = обработчик не сработает ни разу, и в логе будет тишина.
#  2. Бот обязан быть АДМИНОМ группы: иначе Telegram не пришлёт события
#     вовсе, а без права «Ограничивать участников» нечем и ограничивать
#     (об этом бот один раз пишет владельцу в личку — _warn_owners).
#  3. Ожидание нажатия живёт в ПАМЯТИ процесса: перезапуск бота в эту
#     минуту отменяет кик не прошедшего. Не страшно — ограничение выдано
#     с until_date, и Telegram снимет его сам; человек просто останется
#     в группе молчащим до конца срока. Возвращать права нечем и не надо.
# ─────────────────────────────────────────────

import asyncio
import html
import logging

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ParseMode
from datetime import datetime, timedelta, timezone

from config import (
    GREET_ENABLED_DEFAULT,
    GREET_CAPTCHA_DEFAULT,
    GREET_KICK_DEFAULT,
    GREET_TIMEOUT_SEC,
    GREET_TTL_SEC,
    GREET_TEXT,
    GREET_CAPTCHA_TEXT,
)
from database.history import get_setting, log_join, remember_chat
from utils import schedule_delete

logger = logging.getLogger(__name__)

# Значок этого раздела в логах (как 🛡 у антиспама и 🤖 у проактивного режима).
_ICON = "👋"

# (chat_id, user_id) -> message_id приветствия, на которое ждём нажатия.
# В ПАМЯТИ процесса: см. оговорку №3 в шапке файла.
_pending: dict[tuple[int, int], int] = {}

# Кому уже пожаловались на нехватку прав — чтобы не писать владельцу об
# одной и той же группе на каждого новичка. Тоже в памяти: после
# перезапуска бот напомнит один раз, и это скорее полезно.
_warned_chats: set[int] = set()


# ─── чтение настроек ────────────────────────────────────────────────

def is_enabled() -> bool:
    """Общий тумблер приветствия (settings: greet_enabled)."""
    return get_setting("greet_enabled", GREET_ENABLED_DEFAULT) == "1"


def captcha_enabled() -> bool:
    """Требовать ли нажатие кнопки «Я не бот» (settings: greet_captcha)."""
    return get_setting("greet_captcha", GREET_CAPTCHA_DEFAULT) == "1"


def kick_enabled() -> bool:
    """Кикать ли тех, кто не нажал кнопку (settings: greet_kick)."""
    return get_setting("greet_kick", GREET_KICK_DEFAULT) == "1"


def timeout_sec() -> int:
    """Сколько ждём нажатия кнопки (settings: greet_timeout_sec)."""
    try:
        return int(get_setting("greet_timeout_sec", str(GREET_TIMEOUT_SEC)))
    except (TypeError, ValueError):
        return GREET_TIMEOUT_SEC


# ─── разбор события «участник чата сменил статус» ───────────────────

# Статусы, при которых человек СОСТОИТ в группе. RESTRICTED сюда попадает
# только с is_member=True: замученный участник группы всё ещё в ней, а вот
# «ограниченный, но вышедший» — уже нет.
_IN_CHAT = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER)


def _is_in(member) -> bool:
    """Состоит ли человек в группе по этой записи о членстве."""
    if member.status in _IN_CHAT:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


def _joined(upd) -> bool:
    """
    Событие означает ВСТУПЛЕНИЕ (не было в группе → есть).

    Именно так, а не «новый статус = участник»: наш собственный мут меняет
    статус на «ограничен», и без сравнения со старым состоянием бот
    здоровался бы с человеком каждый раз, когда сам его замутил.
    """
    return _is_in(upd.new_chat_member) and not _is_in(upd.old_chat_member)


def _left(upd) -> bool:
    """Событие означает УХОД из группы (был → нет)."""
    return _is_in(upd.old_chat_member) and not _is_in(upd.new_chat_member)


# ─── единая точка входа ─────────────────────────────────────────────

async def on_chat_member(update, context) -> None:
    """
    Вход обработчика ChatMemberHandler. Всё содержимое — в _handle ниже,
    здесь только «глушилка»: сбой приветствия не должен ронять обработку.
    """
    try:
        await _handle(update, context)
    except Exception as e:
        logger.warning("%s Приветствие новичка сорвалось: %s", _ICON, e)


async def _handle(update, context) -> None:
    upd = update.chat_member
    if upd is None:
        return
    chat = upd.chat
    if chat.type not in ("group", "supergroup"):
        return
    member = upd.new_chat_member.user

    # Человек вышел (сам или его выгнали) — снимаем ожидание, чтобы отложенная
    # проверка не пыталась кикнуть того, кого уже нет, и убираем приветствие.
    if _left(upd):
        await _forget(context.bot, chat.id, member.id)
        return

    if not _joined(upd):
        return

    # Новая группа попадает в список известных: карточка /users берёт оттуда,
    # где человека мутить и кикать. Раньше группа записывалась только с первым
    # сообщением в ней — теперь ещё и с первым вступлением.
    try:
        remember_chat(chat.id, chat.title or "")
    except Exception as e:
        logger.debug("%s Не удалось запомнить чат %s: %s", _ICON, chat.id, e)

    if not is_enabled():
        return
    # Ботов не встречаем: их добавляет админ осознанно, и ограничивать их
    # правами группы бессмысленно.
    if member.is_bot:
        return

    # Персонал бота, сам бот и админы группы проходят без проверки. Правило
    # ровно то же, что у антиспама, и живёт оно в ОДНОМ месте — второго
    # списка «кого не трогаем» заводить нельзя, они разъедутся.
    # ⚠️ Не смогли выяснить статус — функция возвращает True, и мы просто
    # здороваться не станем: ограничивать того, чей статус неизвестен, хуже,
    # чем промолчать.
    from services.antispam import _is_protected
    if await _is_protected(context.bot, chat.id, member.id):
        return

    name = (member.first_name
            or (f"@{member.username}" if member.username else str(member.id)))
    try:
        log_join(chat.id, member.id, name, "join")
    except Exception as e:
        logger.debug("%s Не удалось записать вступление в журнал: %s", _ICON, e)

    seconds = timeout_sec()
    captcha = captcha_enabled()
    if captcha:
        # Ограничиваем ДО отправки приветствия: между «поздоровался» и
        # «ограничил» спам-бот успел бы написать.
        # until_date — страховка от собственного перезапуска: даже если бот
        # умрёт, не дождавшись нажатия, Telegram вернёт права сам.
        if not await _restrict(context.bot, chat.id, member.id, seconds):
            await _warn_owners(context.bot, chat)
            captcha = False  # прав нет — остаётся просто поздороваться

    text = _welcome_text(name, member.id, captcha, seconds, context.bot)
    markup = None
    if captcha:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(
            "🎖 Я не бот — пропустите",
            callback_data=f"join:ok:{chat.id}:{member.id}",
        )]])

    # ⚠️ БЕЗ register_and_clean_bot_message: гигиена панелей удаляет предыдущее
    # сообщение бота в этом чате, а в группе это была бы новость или ответ
    # человеку. Приветствие живёт своим сроком (см. ниже).
    sent = await context.bot.send_message(
        chat_id=chat.id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
    )

    if not captcha:
        # Без проверки приветствие — обычное служебное сообщение: висит
        # недолго и убирается само, чтобы не засорять группу.
        if sent:
            schedule_delete(context.bot, chat.id, sent.message_id, GREET_TTL_SEC)
        logger.info("%s Поздоровался с новичком %s в чате %s", _ICON, name, chat.id)
        return

    _pending[(chat.id, member.id)] = sent.message_id if sent else 0
    logger.info("%s Новичок %s в чате %s ограничен до проверки «я не бот» (%d сек)",
                _ICON, name, chat.id, seconds)
    # Отложенной ЗАДАЧЕЙ, а не sleep в обработчике: пока мы ждём, бот обязан
    # обслуживать остальных (тот же приём, что у следующего вопроса викторины).
    context.application.create_task(
        _wait_for_answer(context.bot, chat.id, member.id, name, seconds)
    )


# ─── ограничение прав и его снятие ──────────────────────────────────

async def _restrict(bot, chat_id: int, user_id: int, seconds: int) -> bool:
    """
    Запрещает новичку писать на `seconds` секунд. True при успехе.
    Самая частая причина отказа — бот не админ или без права ограничивать.
    """
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        return True
    except Exception as e:
        logger.warning("⚠️ %s Не удалось ограничить новичка %s в чате %s: %s",
                       _ICON, user_id, chat_id, e)
        return False


async def _warn_owners(bot, chat) -> None:
    """
    Один раз на группу сообщает ВЛАДЕЛЬЦАМ, что заслон не работает без права
    ограничивать участников. Без этого сообщения включённый тумблер выглядел
    бы рабочим, а приветствие приходило бы без всякой проверки.
    """
    if chat.id in _warned_chats:
        return
    _warned_chats.add(chat.id)
    title = html.escape(chat.title or str(chat.id))
    text = (f"⚠️ <b>Приветствие новичков не работает</b> в «{title}»:\n"
            f"у меня нет права <b>«Ограничивать участников»</b>.\n"
            f"Выдай его в настройках группы — и заслон включится сам.\n"
            f"<i>Пока что новички просто получают приветствие без проверки.</i>")
    try:
        from handlers.admin.common import _notify_owners
        await _notify_owners(bot, text)
    except Exception as e:
        logger.debug("%s Не удалось предупредить владельцев о правах: %s", _ICON, e)


async def _forget(bot, chat_id: int, user_id: int) -> None:
    """
    Снимает ожидание нажатия и убирает приветствие: человек либо прошёл
    проверку, либо вышел из группы. Возвращает True, если ожидание было.
    """
    msg_id = _pending.pop((chat_id, user_id), None)
    if msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug("%s Не удалось убрать приветствие: %s", _ICON, e)


# ─── ожидание ответа и наказание молчунов ───────────────────────────

async def _wait_for_answer(bot, chat_id: int, user_id: int, name: str, seconds: int) -> None:
    """
    Ждёт `seconds` и смотрит, нажал ли новичок кнопку. Нажал — записи в
    _pending уже нет, и делать нечего. Не нажал — приветствие убирается, а
    сам он либо остаётся молчать до конца ограничения, либо выгоняется
    (тумблер «👢 Кикать не прошедших»).

    Кик, а не бан: это заслон от спам-ботов, а живой человек, отвлёкшийся на
    пять минут, обязан иметь возможность вернуться и пройти проверку заново.
    """
    try:
        await asyncio.sleep(seconds)
        msg_id = _pending.pop((chat_id, user_id), None)
        if msg_id is None:
            return  # прошёл проверку или вышел сам — оба случая уже разобраны

        if msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as e:
                logger.debug("%s Не удалось убрать приветствие: %s", _ICON, e)

        if not kick_enabled():
            log_join(chat_id, user_id, name, "timeout")
            logger.info("%s Новичок %s не прошёл проверку в чате %s — оставлен молчать",
                        _ICON, name, chat_id)
            return

        # Кик идёт через общий механизм модерации: он умеет «бан + снятие»
        # (иначе в Telegram человек не сможет вернуться) и пишет запись в
        # журнал модерации — выгнать человека из группы бот не должен молча.
        from services.antispam import kick_user
        err = await kick_user(bot, chat_id, user_id, name,
                              admin_name="👋 приветствие (не прошёл проверку)")
        if err:
            log_join(chat_id, user_id, name, "timeout")
            logger.warning("⚠️ %s Не удалось выгнать не прошедшего проверку %s: %s",
                           _ICON, name, err)
            return
        log_join(chat_id, user_id, name, "kick")
        logger.info("%s Новичок %s выгнан из чата %s — не прошёл проверку «я не бот»",
                    _ICON, name, chat_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("⚠️ %s Проверка новичка сорвалась: %s", _ICON, e)


# ─── кнопка «Я не бот» ──────────────────────────────────────────────

async def handle_join_callback(query, context, data: str) -> None:
    """
    Ветка join:ok:<chat_id>:<user_id> — кнопка приветствия.

    ⚠️ Зовётся из роутера ДО гейта прав: это кнопка для ОБЫЧНОГО участника
    группы (как quiz_start), а не для персонала. Нажать её может только тот,
    кому она адресована, — иначе любой прохожий пропускал бы спам-ботов.
    """
    parts = data.split(":")
    try:
        chat_id = int(parts[2])
        user_id = int(parts[3])
    except (IndexError, ValueError):
        await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
        return

    if query.from_user.id != user_id:
        await query.answer("Это приветствие адресовано не тебе 🙂", show_alert=True)
        return

    name = query.from_user.first_name or str(user_id)
    # Возвращаем ДЕФОЛТНЫЕ права группы тем же кодом, что снимает муты, —
    # второго места, знающего, «что значит обычный участник», быть не должно.
    # journal=False: это не размут по решению админа, и в журнале модерации
    # такие строки только мешали бы (счётчик размутов начал бы врать).
    from services.antispam import unmute
    ok = await unmute(query.get_bot(), chat_id, user_id, name, journal=False)
    if not ok:
        await query.answer("❌ Не получилось вернуть права — позови администратора.",
                           show_alert=True)
        return

    _pending.pop((chat_id, user_id), None)
    try:
        log_join(chat_id, user_id, name, "ok")
    except Exception as e:
        logger.debug("%s Не удалось записать проверку в журнал: %s", _ICON, e)
    logger.info("%s Новичок %s прошёл проверку «я не бот» в чате %s", _ICON, name, chat_id)

    await query.answer("✅ Добро пожаловать!")
    # Само приветствие с правилами уже прочитано — заменяем его короткой
    # строкой и убираем через полминуты, как остальные служебные сообщения.
    try:
        await query.edit_message_text(
            f"✅ <b>{html.escape(name)}</b>, добро пожаловать! Права выданы.\n"
            f"<i>Своё звание и статистика — /rank.</i>",
            parse_mode=ParseMode.HTML,
        )
        msg = query.message
        if msg:
            schedule_delete(query.get_bot(), msg.chat_id, msg.message_id, 30)
    except Exception as e:
        logger.debug("%s Не удалось обновить приветствие: %s", _ICON, e)


# ─── текст приветствия ──────────────────────────────────────────────

def _welcome_text(name: str, user_id: int, captcha: bool, seconds: int, bot) -> str:
    """
    Собирает текст приветствия из шаблонов config.GREET_TEXT (+ хвост
    GREET_CAPTCHA_TEXT при включённой проверке).

    ⚠️ Имя новичка — ЧУЖОЙ ТЕКСТ: экранируем, иначе «<» в имени порвёт
    HTML-разметку и приветствие не отправится вовсе.
    ⚠️ Имя подаётся ССЫЛКОЙ на профиль через tg://user?id=…, а не через
    @юзернейм: юзернейма у человека может не быть вовсе, а номер есть всегда.
    """
    link = f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
    text = GREET_TEXT.format(
        name=link,
        bot=bot.username or "",
    )
    if captcha:
        text += "\n" + GREET_CAPTCHA_TEXT.format(minutes=max(1, seconds // 60))
    return text
