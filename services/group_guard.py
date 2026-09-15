# ─────────────────────────────────────────────
#  services/group_guard.py — 🚪 заслон от чужих групп (2026-09-15)
#
#  Зачем: до этого бот работал в ЛЮБОЙ группе, куда его добавили. 12.09.2026
#  его без ведома Максима добавили в чужую группу «Ветеэм» — и в тот же вечер
#  бот ответил там 99 раз, сам влезал в разговор, потом слал туда вопрос дня
#  и включил её в понедельничный дайджест. Максим узнал 15.09.
#
#  Как устроено:
#  • СПИСОК СВОИХ ГРУПП — таблица known_chats. Новая строка появляется в ней
#    только отсюда: бота добавил ВЛАДЕЛЕЦ или владелец нажал «✅ Остаться».
#    Архиватор, приветствие и карточка участника по-прежнему зовут
#    remember_chat, но сообщения чужих групп до них больше не доходят.
#  • gate — сам заслон. Стоит в handlers/__init__.py в группе −3, раньше всех
#    обработчиков. Обновление из группы, которой нет в списке, дальше не идёт:
#    ни ответов, ни архива, ни антиспама, ни «Сам в разговор», ни приветствия.
#  • on_my_chat_member — событие «сменился статус САМОГО бота в чате».
#    Добавил не владелец — владельцу уходит вопрос с двумя кнопками; бота
#    удалили — группа уходит из списка, вопрос дня туда больше не рвётся.
#  • handle_group_callback — кнопки grp:stay:<чат> и grp:leave:<чат>. Роутер
#    зовёт её ПОСЛЕ гейта прав: `grp:` в таблице прав — только владелец.
#
#  ⚠️ СБОЙ ЗАСЛОНА ПРОПУСКАЕТ, А НЕ ЗАПИРАЕТ. Не ответила база — обновление
#  идёт дальше, как до появления этого файла. Замолчать во всех своих группах
#  из-за сбоя проверки хуже, чем один раз ответить в чужой.
#
#  ⚠️ ОБРАБОТЧИК ЗАСЛОНА ОБЯЗАН БЫТЬ БЛОКИРУЮЩИМ (без block=False). В
#  неблокирующем ApplicationHandlerStop не действует, и заслон молча
#  превратится в дырку — выглядеть это будет как обычная работа бота.
#
#  ⚠️ О КАКИХ ГРУППАХ УЖЕ СПРОСИЛИ — в settings (PENDING_KEY), а не в памяти:
#  самообновление перезапускает бота по нескольку раз в день, и вопрос из
#  памяти приходил бы после каждого перезапуска заново.
# ─────────────────────────────────────────────

import html
import json
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationHandlerStop

from database.history import (forget_chat, get_setting, is_known_chat,
                              remember_chat, set_setting)
from utils import schedule_delete

logger = logging.getLogger(__name__)

# Значок этого раздела в логах (как 👋 у приветствия и 🛡 у антиспама).
_ICON = "🚪"

# Группы, о которых владельца уже спросили:
# {chat_id строкой: {"title": название, "by": кто добавил («Имя (@ник)» или
#  пусто), "asked": time.time() последнего вопроса,
#  "msgs": [[id владельца, номер сообщения с вопросом], …]}}.
PENDING_KEY = "group_guard_pending"

# Чужая группа ждёт решения, а в ней продолжают писать — напоминать владельцу
# не чаще этого. Молчит — не напоминаем вовсе.
REASK_SEC = 24 * 3600

_GROUP_TYPES = ("group", "supergroup")

# Сколько живут в личке владельца сообщения, по которым решать уже нечего:
# итоги кнопок («Вышел», «Остаюсь», «Меня уже нет»), снятые при переезде
# группы вопросы и «Меня удалили из группы» (15.09.2026, просьба Максима).
# Вопросы, ЖДУЩИЕ решения, не удаляются никогда — с ними пропали бы кнопки.
# ⚠️ Таймер живёт в памяти процесса (utils.schedule_delete): перезапуск бота
# в эти минуты оставит сообщение висеть. Telegram к тому же не даёт боту
# удалить сообщение старше 48 часов.
DONE_TTL_SEC = 5 * 60


# ─── заслон: пускать ли обновление дальше ───────────────────────────

async def gate(update, context) -> None:
    """
    Своя группа, личка, обновление без чата (опросы, инлайн) — молча
    пропускает. Чужая группа — при необходимости спрашивает владельца и
    обрывает обработку: ApplicationHandlerStop не пускает обновление ни в
    одну следующую группу обработчиков.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in _GROUP_TYPES:
        return
    message = update.effective_message
    try:
        if await _carry_migration(context.bot, chat, message):
            return
        if is_known_chat(chat.id):
            return
    except Exception as e:
        logger.warning("⚠️ %s Не удалось проверить группу %s — пропускаю как свою: %s",
                       _ICON, chat.id, e)
        return

    logger.debug("%s Обновление из чужой группы %s не обработано", _ICON, chat.id)
    # Служебные «X добавил бота» и «бот покинул группу» — про САМОГО бота, и
    # вопроса они не порождают. Первое приходит рядом с событием
    # my_chat_member (порядок Telegram не обещает), и вопрос с именем
    # добавившего задаёт обработчик события. Второе приходит, когда бот уже
    # вышел: без этой проверки кнопка «🚪 Выйти» тут же рождала новый вопрос
    # о только что покинутой группе (живой тест 15.09.2026).
    joined = getattr(message, "new_chat_members", None) or ()
    left = getattr(message, "left_chat_member", None)
    about_me = (any(u.id == context.bot.id for u in joined)
                or (left is not None and left.id == context.bot.id))
    if not about_me:
        await _remind_owner(context.bot, chat)
    raise ApplicationHandlerStop


async def _carry_migration(bot, chat, message) -> bool:
    """
    Обычная группа, ставшая супергруппой, получает НОВЫЙ номер. Telegram
    шлёт служебные сообщения в старую («переехала в…») и в новую («переехала
    из…») группу, а ещё — событие «бота добавили» в новый номер. Порядок всех
    трёх не обещан (живой тест 15.09.2026: группа переехала через 14 секунд
    после добавления бота, и владелец получил два вопроса об одной группе).
    Какое бы ни пришло первым:

    • группа была СВОЕЙ — своим становится новый номер; если про новый номер
      владельца уже успели спросить, вопрос снимается («это ваша группа»);
    • группа ЖДАЛА РЕШЕНИЯ — ожидание переезжает на новый номер, а кнопки уже
      отправленного вопроса переключаются на него; если про новый номер уже
      ушёл второй вопрос, первый снимается («вопрос о ней ниже»).

    True — это сообщение о переезде: его пропускаем, спрашивать владельца о
    нём не о чем (номер со служебного сообщения подделать нельзя — его ставит
    сам Telegram).
    """
    to_id = getattr(message, "migrate_to_chat_id", None)
    from_id = getattr(message, "migrate_from_chat_id", None)
    if to_id:
        old_id, new_id = chat.id, to_id
    elif from_id:
        old_id, new_id = from_id, chat.id
    else:
        return False
    shown = html.escape(chat.title or str(new_id))
    pending = _pending()
    old_rec = pending.get(str(old_id))
    new_rec = pending.get(str(new_id))
    if is_known_chat(old_id):
        remember_chat(new_id, chat.title or "")
        forget_group(old_id)
        logger.info("%s Группа «%s» стала супергруппой (%s → %s) — осталась своей",
                    _ICON, chat.title or new_id, old_id, new_id)
        if new_rec:
            await _retire(bot, new_rec, f"✅ «{shown}» — ваша группа: она стала "
                                        f"супергруппой, работаю в ней как раньше.")
            _drop_pending(new_id)
    elif old_rec:
        if new_rec:
            await _retire(bot, old_rec, f"{_ICON} Группа «{shown}» стала "
                                        f"супергруппой — вопрос о ней ниже.")
            _drop_pending(old_id)
        else:
            _move_pending(old_id, new_id)
            await _retarget(bot, old_rec, new_id)
        logger.info("%s Группа «%s», ждущая решения, стала супергруппой (%s → %s)",
                    _ICON, chat.title or new_id, old_id, new_id)
    return True


async def _retarget(bot, rec: dict, new_id: int) -> None:
    """Кнопки уже отправленных вопросов — на новый номер группы."""
    for owner_id, message_id in rec.get("msgs") or ():
        try:
            await bot.edit_message_reply_markup(chat_id=owner_id, message_id=message_id,
                                                reply_markup=request_keyboard(new_id))
        except Exception as e:
            logger.debug("%s Не удалось переключить кнопки вопроса %s: %s",
                         _ICON, message_id, e)


async def _retire(bot, rec: dict, text: str) -> None:
    """
    Снимает уже отправленные вопросы: новый текст вместо старого, без кнопок.
    Решать по снятому вопросу нечего — через DONE_TTL_SEC он исчезает.
    """
    for owner_id, message_id in rec.get("msgs") or ():
        try:
            await bot.edit_message_text(chat_id=owner_id, message_id=message_id,
                                        text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug("%s Не удалось снять вопрос %s: %s", _ICON, message_id, e)
        schedule_delete(bot, owner_id, message_id, DONE_TTL_SEC)


async def _remind_owner(bot, chat) -> None:
    """Чужая группа подала голос: спросить владельца, если давно не спрашивали."""
    try:
        asked = _pending().get(str(chat.id), {}).get("asked", 0)
        if time.time() - float(asked or 0) < REASK_SEC:
            return
        # Спрашиваем только о группе, где бот правда состоит: хвосты вроде
        # служебных сообщений приходят и из чата, который бот уже покинул.
        if await _bot_state(bot, chat.id) is not True:
            return
        await ask_owner(bot, chat)
    except Exception as e:
        logger.warning("⚠️ %s Не удалось спросить владельца о группе %s: %s",
                       _ICON, chat.id, e)


# ─── статус самого бота: добавили, удалили ──────────────────────────

async def on_my_chat_member(update, context) -> None:
    """
    Вход обработчика ChatMemberHandler(MY_CHAT_MEMBER). Всё содержимое — в
    _handle_my_status, здесь только глушилка: сбой разбора не повод ронять
    обработку событий.
    """
    try:
        await _handle_my_status(update, context)
    except Exception as e:
        logger.warning("⚠️ %s Не удалось разобрать смену моего статуса в чате: %s",
                       _ICON, e)


async def _handle_my_status(update, context) -> None:
    upd = update.my_chat_member
    if upd is None or upd.chat.type not in _GROUP_TYPES:
        return
    # «Состоит ли в группе» решает ОДНА функция на весь бот — та же, по
    # которой приветствие отличает вступление от мута. Второе правило
    # разъехалось бы с первым.
    from services.greeter import _is_in
    was_in = _is_in(upd.old_chat_member)
    now_in = _is_in(upd.new_chat_member)
    if now_in and not was_in:
        await _on_added(context.bot, upd.chat, upd.from_user)
    elif was_in and not now_in:
        await _on_removed(context.bot, upd.chat, upd.from_user)


async def _on_added(bot, chat, actor) -> None:
    from services import roles

    title = chat.title or str(chat.id)
    if actor is not None and roles.is_owner(actor.id):
        remember_chat(chat.id, chat.title or "")
        _drop_pending(chat.id)
        logger.info("%s Владелец добавил меня в группу «%s» (%s) — работаю",
                    _ICON, title, chat.id)
        return
    if is_known_chat(chat.id):
        # Группа уже своя: она переехала на новый номер (сообщение о переезде
        # пришло раньше этого события) или событие об удалении когда-то
        # потерялось. Владелец её одобрял — спрашивать не о чем.
        logger.info("%s Группа «%s» (%s) уже своя — работаю", _ICON, title, chat.id)
        return
    asked = _pending().get(str(chat.id), {}).get("asked", 0)
    if time.time() - float(asked or 0) < REASK_SEC:
        # Ожидание решения переехало сюда вместе с группой (см. _carry_migration):
        # вопрос уже у владельца, и его кнопки смотрят на этот номер.
        logger.info("%s О группе «%s» (%s) уже спросил — жду решения", _ICON, title, chat.id)
        return
    await ask_owner(bot, chat, added_by=actor)


async def _on_removed(bot, chat, actor) -> None:
    from services import roles

    title = chat.title or str(chat.id)
    was_own = forget_group(chat.id)
    logger.info("%s Меня убрали из группы «%s» (%s), убрал: %s",
                _ICON, title, chat.id, _who(actor) if actor is not None else "—")
    # Владельцу пишем, только если группа была СВОЕЙ и убрал не он сам и не
    # сам бот: кнопка «🚪 Выйти» тоже порождает это событие.
    if not was_own or actor is None or roles.is_owner(actor.id) or actor.id == bot.id:
        return
    import config
    text = (f"👋 <b>Меня удалили из группы «{html.escape(title)}»</b>\n"
            f"Удалил: {html.escape(_who(actor))}\n"
            f"Вопрос дня и дайджест туда больше не идут.")
    # Своя рассылка, а не handlers.admin.common._notify_owners: тому не нужен
    # номер отправленного сообщения, а здесь он нужен — через DONE_TTL_SEC
    # сообщение убирается, как и прочие итоги этого раздела.
    for owner_id in config.ADMIN_IDS:
        try:
            sent = await bot.send_message(chat_id=owner_id, text=text,
                                          parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.debug("%s Не удалось сообщить владельцу %s об удалении: %s",
                         _ICON, owner_id, e)
            continue
        if getattr(sent, "message_id", None):
            schedule_delete(bot, owner_id, sent.message_id, DONE_TTL_SEC)


def forget_group(chat_id: int) -> bool:
    """
    Группа перестаёт быть своей: уходит из списка, из памяти вопроса дня и из
    ждущих решения. True — была в списке своих.
    """
    was_own = forget_chat(chat_id)
    try:
        _drop_pending(chat_id)
    except Exception as e:
        logger.debug("%s Не удалось убрать группу %s из ждущих решения: %s",
                     _ICON, chat_id, e)
    try:
        # Иначе запись о последнем опросе висела бы в settings вечно: вопрос
        # дня перебирает только свои группы и чужую запись не тронет никогда.
        from services import quiz_daily
        quiz_daily.forget(chat_id)
    except Exception as e:
        logger.debug("%s Не удалось забыть вопрос дня группы %s: %s", _ICON, chat_id, e)
    return was_own


# ─── вопрос владельцу ───────────────────────────────────────────────

def request_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """
    Две кнопки под вопросом. ⚠️ Приставка `grp:` стоит В НАЧАЛЕ callback_data:
    с переменной впереди preflight кнопку не увидел бы.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Остаться", callback_data=f"grp:stay:{chat_id}"),
        InlineKeyboardButton("🚪 Выйти", callback_data=f"grp:leave:{chat_id}"),
    ]])


async def ask_owner(bot, chat, added_by=None) -> int:
    """
    Шлёт владельцам вопрос «остаться или выйти» и запоминает, что спросили.
    added_by — кто добавил бота; None — о группе узнали по сообщению в ней.
    Если о группе уже спрашивали, это НАПОМИНАНИЕ: кто добавил, берётся из
    прошлого вопроса, а не теряется. Возвращает, скольким владельцам ушло.
    """
    import config

    title = chat.title or str(chat.id)
    before = _pending().get(str(chat.id)) or {}
    reminder = added_by is None and bool(before)
    by = _who(added_by) if added_by is not None else before.get("by", "")
    text = await _request_text(bot, chat, by, reminder)
    markup = request_keyboard(chat.id)
    sent = 0
    msgs = []
    for owner_id in config.ADMIN_IDS:
        try:
            # БЕЗ register_and_clean_bot_message — как дайджест: это вопрос,
            # а не панель, и следующая открытая панель не должна его стереть.
            question = await bot.send_message(chat_id=owner_id, text=text,
                                              parse_mode=ParseMode.HTML, reply_markup=markup)
            sent += 1
            if getattr(question, "message_id", None):
                # Номер сообщения нужен при переезде группы: переключить
                # кнопки на новый номер группы или снять вопрос.
                msgs.append([owner_id, question.message_id])
        except Exception as e:
            logger.warning("⚠️ %s Не удалось спросить владельца %s о группе «%s»: %s",
                           _ICON, owner_id, title, e)
    # Отметка ставится, даже если не ушло никому: иначе каждое сообщение
    # чужой группы порождало бы новую попытку. Через REASK_SEC спросим снова.
    _mark_asked(chat.id, title, by, msgs)
    logger.info("%s %s владельца, оставаться ли в группе «%s» (%s)%s",
                _ICON, "Снова спросил" if reminder else "Спросил", title, chat.id,
                f", добавил {by}" if by else "")
    return sent


async def _request_text(bot, chat, by: str, reminder: bool) -> str:
    """by — «Имя (@ник)» добавившего, сырой текст; пусто — неизвестно."""
    title = html.escape(chat.title or str(chat.id))
    if reminder:
        head = "Напоминаю: жду решения по группе"
    elif by:
        head = "Меня добавили в чужую группу"
    else:
        head = "Я состою в группе, которую вы не одобряли"
    people = await _people_count(bot, chat.id)
    lines = [f"{_ICON} <b>{head}</b>",
             f"«{title}»" + (f" · {people}" if people else "")]
    owner = await _group_owner(bot, chat.id)
    if owner is not None:
        lines.append(f"Владелец: {html.escape(_who(owner))}")
    if by:
        lines.append(f"Добавил: {html.escape(by)}")
    elif not reminder:
        lines.append("<i>Кто добавил — не знаю: узнал о группе по сообщению в ней.</i>")
    lines += ["", "Пока вы не решили, я там молчу и ничего не трачу."]
    return "\n".join(lines)


async def _people_count(bot, chat_id: int) -> str:
    """«4 человека» — участники БЕЗ самого бота: Telegram считает и его."""
    try:
        n = await bot.get_chat_member_count(chat_id) - 1
    except Exception as e:
        logger.debug("%s Не удалось узнать число участников %s: %s", _ICON, chat_id, e)
        return ""
    if n < 1:
        return ""
    return f"{n} {_plural(n, 'человек', 'человека', 'человек')}"


async def _group_owner(bot, chat_id: int):
    """Создатель группы или None: скрыт, анонимен, Telegram не ответил."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.debug("%s Не удалось узнать админов группы %s: %s", _ICON, chat_id, e)
        return None
    for member in admins:
        if member.status == ChatMemberStatus.OWNER:
            return member.user
    return None


def _who(user) -> str:
    """«Имя Фамилия (@ник)» — СЫРОЙ текст: перед вставкой в HTML экранировать."""
    parts = (getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or "")
    name = " ".join(p for p in parts if p) or str(getattr(user, "id", ""))
    username = getattr(user, "username", None)
    return f"{name} (@{username})" if username else name


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское число: 1 человек, 2 человека, 5 человек, 11 человек, 22 человека."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


# ─── память «о какой группе уже спросили» ───────────────────────────

def _pending() -> dict:
    """Испорченное значение (руками правили settings) — считаем, что пусто."""
    try:
        data = json.loads(get_setting(PENDING_KEY, "") or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _mark_asked(chat_id: int, title: str, by: str = "", msgs=None) -> None:
    data = _pending()
    # Номера прежних вопросов (напоминаний) не выбрасываем: при переезде
    # группы кнопки переключаются у ВСЕХ её вопросов, а не только у последнего.
    kept = (data.get(str(chat_id)) or {}).get("msgs") or []
    data[str(chat_id)] = {"title": title, "by": by, "asked": time.time(),
                          "msgs": (kept + list(msgs or []))[-10:]}
    set_setting(PENDING_KEY, json.dumps(data, ensure_ascii=False))


def _drop_pending(chat_id: int) -> None:
    data = _pending()
    if data.pop(str(chat_id), None) is not None:
        set_setting(PENDING_KEY, json.dumps(data, ensure_ascii=False))


def _move_pending(old_id: int, new_id: int) -> None:
    """Ожидание решения переезжает на новый номер группы вместе со всем, что в нём было."""
    data = _pending()
    rec = data.pop(str(old_id), None)
    if rec is not None:
        data[str(new_id)] = rec
        set_setting(PENDING_KEY, json.dumps(data, ensure_ascii=False))


# ─── кнопки «✅ Остаться» и «🚪 Выйти» ──────────────────────────────

async def handle_group_callback(query, context, data: str, user_id: int) -> None:
    """
    Ветки grp:stay:<чат> и grp:leave:<чат>. Зовётся роутером ПОСЛЕ гейта
    прав — жмёт только владелец.
    """
    from handlers.admin.common import _audit

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        target = int(parts[2])
    except (IndexError, ValueError):
        await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
        return
    bot = context.bot
    title = _pending().get(str(target), {}).get("title") or str(target)
    shown = html.escape(title)

    if action == "stay":
        state = await _bot_state(bot, target)
        if state is None:
            await query.answer("❌ Telegram не ответил — нажмите ещё раз чуть позже.",
                               show_alert=True)
            return
        if not state:
            # Бота удалили раньше, чем владелец решил, — оставаться негде.
            forget_group(target)
            await query.answer()
            await _finish(bot, query, f"{_ICON} Меня уже нет в «{shown}» — оставаться негде.")
            return
        remember_chat(target, "" if title == str(target) else title)
        _drop_pending(target)
        _audit(user_id, "group", 0, f"остался в группе «{title}»")
        logger.info("%s Владелец %s оставил меня в группе «%s» (%s)",
                    _ICON, user_id, title, target)
        await query.answer("✅ Остаюсь")
        await _finish(bot, query, f"✅ Остаюсь в «{shown}» — работаю там, как в ваших группах.")
        return

    if action == "leave":
        try:
            await bot.leave_chat(target)
            result = f"{_ICON} Вышел из «{shown}»."
        except (Forbidden, BadRequest) as e:
            # Бота там уже нет — выходить неоткуда, но список почистить надо.
            logger.info("%s Выходить из группы %s не пришлось: %s", _ICON, target, e)
            result = f"{_ICON} Меня уже нет в «{shown}»."
        except Exception as e:
            logger.warning("⚠️ %s Не удалось выйти из группы %s: %s", _ICON, target, e)
            await query.answer("❌ Не получилось выйти — нажмите ещё раз чуть позже.",
                               show_alert=True)
            return
        forget_group(target)
        _audit(user_id, "group", 0, f"вышел из группы «{title}»")
        logger.info("%s Владелец %s вывел меня из группы «%s» (%s)",
                    _ICON, user_id, title, target)
        await query.answer()
        await _finish(bot, query, result)
        return

    await query.answer()


async def _bot_state(bot, chat_id: int):
    """True — бот в группе, False — его там нет, None — Telegram не ответил."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except (Forbidden, BadRequest):
        return False
    except Exception as e:
        logger.warning("⚠️ %s Не удалось узнать мой статус в группе %s: %s",
                       _ICON, chat_id, e)
        return None
    from services.greeter import _is_in
    return _is_in(member)


async def _finish(bot, query, text: str) -> None:
    """
    Меняет вопрос на итог, убирает кнопки и ставит итог на удаление через
    DONE_TTL_SEC. Двойное нажатие даёт ошибку Telegram «Message is not
    modified» — она здесь глушится (второе удаление того же сообщения тоже
    тихо ничего не сделает).
    """
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug("%s Не удалось обновить вопрос о группе: %s", _ICON, e)
    msg = query.message
    if msg is not None:
        schedule_delete(bot, msg.chat_id, msg.message_id, DONE_TTL_SEC)
