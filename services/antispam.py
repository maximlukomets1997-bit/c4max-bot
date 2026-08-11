# ─────────────────────────────────────────────
#  services/antispam.py — антифлуд для групп (MVP)
#
#  Контракт:
#  • Единственная точка входа — check_and_mute(bot, chat_id, user, ...),
#    её зовёт handlers.messages.collect_group_message на КАЖДОЕ
#    сообщение группы (handler group=1). Функция обязана быть
#    «тихой»: любое исключение глушится, бот не должен падать из-за
#    модерации.
#  • Счётчик частоты живёт В ПАМЯТИ процесса (без БД на каждое
#    сообщение). При перезапуске бота сбрасывается — это ОК для
#    антифлуда (окно всего несколько секунд).
#  • Пороги читаются из settings (ключи antispam_*), дефолты — из config.
#    Настройки глобальные (одни на все группы), см. контракт проекта.
#  • Прогрессивных наказаний здесь НЕТ — это следующий шаг (потребует
#    таблицу нарушений + reset_db.USER_TABLES). MVP всегда даёт ровно
#    временный мут одной длительности.
# ─────────────────────────────────────────────

import time
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus

from config import (
    ANTISPAM_ENABLED_DEFAULT,
    ANTISPAM_MSG_COUNT,
    ANTISPAM_WINDOW_SEC,
    ANTISPAM_MUTE_SEC,
    LINKFILTER_ENABLED_DEFAULT,
    LINKFILTER_WHITELIST,
    LINKFILTER_MUTE_COUNT,
    LINKFILTER_WINDOW_SEC,
    TRUST_MIN_DAYS,
    TRUST_MIN_MESSAGES,
    TRUST_FORGIVE_DAYS,
    ADMIN_IDS,
)
from utils import schedule_delete
from database.history import (
    get_setting,
    log_moderation_action,
    save_mute_evidence,
    get_moderation_counts,
    get_recent_moderation_actions,
    get_mute_evidence as _hist_get_mute_evidence,
    get_dossier,
    dossier_add_mute,
    dossier_add_linkdel,
)

logger = logging.getLogger(__name__)

# user_id -> deque[(monotonic_ts, chat_id, message_id, text, has_photo)] последних
# сообщений (в памяти процесса). Глобальный по user_id: для антифлуда важна частота
# сообщений одного человека. chat_id/message_id хранятся, чтобы при муте можно было
# удалить весь спам-всплеск; text/has_photo — чтобы сохранить улики (тексты удаляемых
# сообщений) в журнал модерации перед удалением их из чата.
_timestamps: dict[int, deque] = defaultdict(deque)

# (chat_id, user_id) -> момент времени, до которого мут уже выдан.
# Защищает от повторных restrict-вызовов, пока мут ещё действует
# (сообщения могли прийти пачкой до того, как Telegram применил ограничение).
_muted_until: dict[tuple[int, int], float] = {}

# ─── Лог модерации (персистентный, в БД) ────────────────────────────
# Журнал мутов/размутов и тексты удалённых сообщений теперь хранятся в БД
# (таблицы moderation_log / mute_evidence) — чтобы статистика за 7 дней и
# улики переживали перезапуск бота. Детектор частоты выше остаётся чисто
# in-memory (ему персистентность не нужна: окно всего несколько секунд).

# Сколько дней статистика и улики показываются/хранятся (см. jobs.cleanup_loop).
MOD_STATS_DAYS = 7


def get_mute_stats(days: int = MOD_STATS_DAYS) -> dict:
    """Счётчики мутов/размутов за последние `days` дней (из БД)."""
    try:
        return get_moderation_counts(days)
    except Exception as e:
        logger.debug("🛡 Не удалось прочитать счётчики модерации: %s", e)
        return {"mutes": 0, "unmutes": 0, "linkdels": 0}


def get_recent_actions(limit: int = 5) -> list:
    """Последние действия модерации (новые первыми) из БД.

    Каждый элемент — dict с ключами id, ts, action, chat_id, user_id, name.
    """
    try:
        return get_recent_moderation_actions(limit)
    except Exception as e:
        logger.debug("🛡 Не удалось прочитать журнал модерации: %s", e)
        return []


def get_evidence(log_id: int) -> list:
    """Тексты удалённых сообщений конкретного мута (из БД)."""
    try:
        return _hist_get_mute_evidence(log_id)
    except Exception as e:
        logger.debug("🛡 Не удалось прочитать улики мута %s: %s", log_id, e)
        return []


# ─── чтение настроек ────────────────────────────────────────────────

def is_enabled() -> bool:
    """Глобальный тумблер антиспама (settings: antispam_enabled)."""
    return get_setting("antispam_enabled", ANTISPAM_ENABLED_DEFAULT) == "1"


def _int_setting(key: str, default: int) -> int:
    """settings хранит строки — безопасно приводим к int с фолбэком на дефолт."""
    try:
        return int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_thresholds() -> tuple[int, int, int]:
    """(msg_count, window_sec, mute_sec) — ОБЩИЕ пороги из settings (панель /mod)."""
    return (
        _int_setting("antispam_msg_count", ANTISPAM_MSG_COUNT),
        _int_setting("antispam_window_sec", ANTISPAM_WINDOW_SEC),
        _int_setting("antispam_mute_sec", ANTISPAM_MUTE_SEC),
    )


def get_thresholds_for(user_id: int) -> tuple[int, int, int]:
    """
    Пороги для КОНКРЕТНОГО человека: персональные из карточки пользователя, где
    заданы, иначе общие. Персональные лежат в памяти (services/user_settings.py),
    похода в БД здесь нет — функция зовётся на каждое сообщение группы.
    """
    from services.user_settings import thresholds_for
    return thresholds_for(user_id, get_thresholds())


# ─── детектор частоты (in-memory) ───────────────────────────────────

def _count_messages(dq) -> int:
    """
    Сколько ОТПРАВЛЕНИЙ в окне: альбом фотографий считается за одно.

    Telegram шлёт альбом (media group) как несколько отдельных сообщений с
    общим media_group_id — без этой поправки альбом из 5 фото мгновенно
    выбирал порог «5 сообщений» и человек получал мут ни за что (наступили
    2026-07-19). Части одного альбома схлопываются в единицу, РАЗНЫЕ альбомы
    считаются по отдельности — залп альбомами по-прежнему ловится.

    ЕДИНЫЙ источник правды о размере всплеска. Единственный читатель —
    проверка порога в _register_and_check (бонус «проверенным» удалён
    2026-07-20 вместе с TRUST_EXTRA_MESSAGES).
    """
    singles = 0
    albums = set()
    for rec in dq:
        album_id = rec[5] if len(rec) > 5 else ""
        if album_id:
            albums.add(album_id)
        else:
            singles += 1
    return singles + len(albums)


def _register_and_check(user_id: int, chat_id: int, message_id: int,
                        msg_count: int, window_sec: int,
                        text: str = "", has_photo: bool = False,
                        media_group_id: str = "") -> bool:
    """
    Регистрирует сообщение пользователя (с chat_id/message_id и текстом) и
    возвращает True, если за последние window_sec секунд набралось >= msg_count
    ОТПРАВЛЕНИЙ (альбом фото = одно, см. _count_messages). Чисто арифметика по
    таймстампам в памяти — без БД и без I/O.
    text/has_photo хранятся, чтобы при муте сохранить улики до удаления;
    media_group_id — чтобы не считать альбом за флуд. Порядок элементов записи
    менять НЕЛЬЗЯ: _burst_message_ids/_burst_records читают их по номеру.
    """
    now = time.monotonic()
    dq = _timestamps[user_id]
    dq.append((now, chat_id, message_id, text or "", bool(has_photo), media_group_id or ""))

    # Выкидываем всё, что вышло за окно (сравниваем по таймстампу — элемент [0]).
    cutoff = now - window_sec
    while dq and dq[0][0] < cutoff:
        dq.popleft()

    return _count_messages(dq) >= msg_count


def _burst_message_ids(user_id: int, chat_id: int) -> list[int]:
    """message_id сообщений пользователя в данном чате, что сейчас в окне (для чистки спама)."""
    dq = _timestamps.get(user_id)
    if not dq:
        return []
    return [rec[2] for rec in dq if rec[1] == chat_id and rec[2]]


def _burst_records(user_id: int, chat_id: int) -> list[dict]:
    """Записи сообщений всплеска (для улик): message_id, text, has_photo — в порядке прихода."""
    dq = _timestamps.get(user_id)
    if not dq:
        return []
    out = []
    for rec in dq:
        # rec = (monotonic_ts, chat_id, message_id, text, has_photo)
        if rec[1] == chat_id:
            out.append({"message_id": rec[2], "text": rec[3], "has_photo": rec[4]})
    return out


async def _delete_messages(bot, chat_id: int, message_ids) -> None:
    """Удаляет сообщения по id, тихо игнорируя ошибки (нет прав / уже удалено)."""
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as e:
            logger.debug("🛡 Не удалось удалить сообщение %s в чате %s: %s", mid, chat_id, e)


def _reset_user(user_id: int) -> None:
    """Сбрасывает счётчик после выдачи мута, чтобы не мутить повторно за то же окно."""
    _timestamps.pop(user_id, None)


# ─── проверка прав (кого нельзя мутить) ─────────────────────────────

async def _is_protected(bot, chat_id: int, user_id: int) -> bool:
    """
    True, если пользователя НЕ нужно трогать:
    • ПЕРСОНАЛ бота — владельцы и модераторы (services/roles.py);
    • сам бот;
    • админы/создатель Telegram-группы (их Telegram и так не даст замутить).
    """
    from services.roles import is_staff
    if is_staff(user_id):
        return True
    if bot.id == user_id:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return True
    except Exception as e:
        # Не смогли проверить статус — НЕ мутим (безопасный дефолт против ложных срабатываний).
        logger.debug("🛡 Не удалось проверить статус пользователя %s в чате %s: %s", user_id, chat_id, e)
        return True
    return False


# ─── статус «проверенный» (личное дело, бонус к порогу) ─────────────

def trust_info(user_id: int) -> dict:
    """
    Сводка личного дела для статуса «проверенный» и блока «Служба» в /rank.
    ЕДИНСТВЕННОЕ место с правилом доверия: стаж ≥ TRUST_MIN_DAYS И сообщений
    ≥ TRUST_MIN_MESSAGES И (мутов не было ИЛИ последний старше
    TRUST_FORGIVE_DAYS — «прощение»). Всегда возвращает словарь (при пустом
    деле — нули), ошибки глушатся.

    Ручное управление статусом удалено 2026-07-20 (решение Максима) —
    проверенный присваивается только автоматически.
    """
    out = {"days": 0, "msgs": 0, "mutes": 0, "links": 0, "trusted": False,
           "need_days": TRUST_MIN_DAYS, "need_msgs": TRUST_MIN_MESSAGES,
           "forgive_left": 0}
    try:
        d = get_dossier(user_id)
        if d:
            now = time.time()
            out["days"] = int((now - (d["first_seen"] or now)) // 86400)
            out["msgs"] = d["msg_count"] or 0
            out["mutes"] = d["mute_count"] or 0
            out["links"] = d["link_count"] or 0
            if out["mutes"] and d["last_mute_ts"]:
                left = TRUST_FORGIVE_DAYS - int((now - d["last_mute_ts"]) // 86400)
                out["forgive_left"] = max(0, left)
            out["trusted"] = (out["days"] >= TRUST_MIN_DAYS
                              and out["msgs"] >= TRUST_MIN_MESSAGES
                              and out["forgive_left"] == 0)
    except Exception as e:
        logger.debug("🛡 Не удалось прочитать личное дело %s: %s", user_id, e)
    return out


def is_trusted(user_id: int) -> bool:
    """True — участник «проверенный» (см. trust_info)."""
    return trust_info(user_id)["trusted"]


# ─── выдача мута ────────────────────────────────────────────────────

async def _apply_mute(bot, chat_id: int, user_id: int, mute_sec: int) -> bool:
    """
    Временный мут через restrict_chat_member + until_date.
    Возвращает True при успехе. Гасит исключения (нет прав, юзер вышел и т.п.).
    """
    until = datetime.now(timezone.utc) + timedelta(seconds=mute_sec)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        return True
    except Exception as e:
        # Самая частая причина — бот не админ или нет права ограничивать участников.
        logger.warning("⚠️ Не удалось замутить пользователя %s в чате %s: %s", user_id, chat_id, e)
        return False


# ─── единая точка входа ─────────────────────────────────────────────

async def check_and_mute(bot, chat_id: int, user, user_text: str = "",
                         message_id: int | None = None,
                         has_photo: bool = False,
                         media_group_id: str = "") -> bool:
    """
    Главный вход. Зовётся из collect_group_message на каждое сообщение группы.

    Возвращает True, если пользователь был замучен (на случай, если
    вызывающий код захочет залогировать/отреагировать). Любая ошибка
    внутри проглатывается — модерация не должна ронять бота.
    """
    try:
        if not is_enabled():
            return False

        user_id = user.id

        # Персональный иммунитет (карточка пользователя): человека не мутит
        # автоматика вообще. Выходим ДО регистрации сообщения — считать всплеск
        # для того, кого всё равно нельзя замутить, незачем.
        from services.user_settings import is_immune
        if is_immune(user_id):
            return False

        msg_count, window_sec, mute_sec = get_thresholds_for(user_id)

        # Уже под мутом в этом чате — не мутим повторно, но добиваем «протёкшее»
        # сообщение (могло прийти за доли секунды до применения restrict Telegram'ом).
        key = (chat_id, user_id)
        now = time.monotonic()
        active_until = _muted_until.get(key)
        if active_until and active_until > now:
            if message_id:
                await _delete_messages(bot, chat_id, [message_id])
            return False

        if not _register_and_check(user_id, chat_id, message_id or 0,
                                   msg_count, window_sec, user_text, has_photo,
                                   media_group_id):
            return False

        # Порог превышен — проверяем, не защищён ли пользователь.
        if await _is_protected(bot, chat_id, user_id):
            _reset_user(user_id)
            return False

        muted = await _apply_mute(bot, chat_id, user_id, mute_sec)
        if not muted:
            return False

        # Перед сбросом счётчика забираем весь спам-всплеск этого пользователя
        # в этом чате: id (для удаления) и тексты (для улик в журнале).
        burst = _burst_records(user_id, chat_id)
        burst_ids = [r["message_id"] for r in burst if r["message_id"]]

        _muted_until[key] = now + mute_sec
        _reset_user(user_id)

        name = user.first_name or (f"@{user.username}" if user.username else str(user_id))

        # Журнал модерации + улики (тексты удаляемых сообщений) — ДО удаления из чата.
        try:
            log_id = log_moderation_action("mute", chat_id, user_id, name)
            if burst:
                save_mute_evidence(log_id, burst)
        except Exception as e:
            logger.debug("🛡 Не удалось записать журнал/улики мута: %s", e)

        # Личное дело: +1 мут (лишает статуса «проверенный» на TRUST_FORGIVE_DAYS)
        try:
            dossier_add_mute(user_id)
        except Exception as e:
            logger.debug("🛡 Не удалось записать мут в личное дело: %s", e)

        # Чистим спам (всегда включено). Работает только если бот — админ с правом удалять.
        await _delete_messages(bot, chat_id, burst_ids)

        # Уведомление в чат с кнопкой «Размутить» (для админов) + .txt-лог.
        minutes = max(1, mute_sec // 60)
        unmute_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 Размутить", callback_data=f"mod:unmute:{chat_id}:{user_id}")
        ]])
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"🛡 {name} получает мут на {minutes} мин. за флуд.",
                reply_markup=unmute_kb,
            )
        except Exception:
            pass
        logger.info("🛡 Антиспам: %s получил мут на %d мин (%d сек) — порог %d сообщений за %d сек (чат %s)",
                    name, minutes, mute_sec, msg_count, window_sec, chat_id)

        # Модераторам в личку — с кнопкой размута (чтобы разобраться быстро).
        await _notify_moderators_mute(bot, chat_id, user_id, name, minutes,
                                      f"флуд ({msg_count} сообщ. за {window_sec} сек)")
        return True

    except Exception as e:
        logger.debug("🛡 Ошибка в check_and_mute: %s", e)
        return False


# ─── снятие мута ────────────────────────────────────────────────────

async def unmute(bot, chat_id: int, user_id: int, name: str | None = None,
                 admin_name: str | None = None, journal: bool = True) -> bool:
    """
    Снимает мут: восстанавливает пользователю ДЕФОЛТНЫЕ права чата
    (а не «всё разрешено» — чтобы не выдать больше, чем у обычного участника).
    Чистит внутреннее состояние, чтобы повторный флуд ловился заново.
    admin_name — кто снял мут (пишется в журнал, показывается в панели /mod).
    Возвращает True при успехе. Гасит исключения.

    journal=False (2026-08-04) — не писать строку в журнал модерации. Нужно
    ровно одному месту: приветствию новичков (services/greeter.py), где права
    возвращаются не по решению админа, а по нажатию кнопки «Я не бот».
    Такие строки замусорили бы «Последние действия» панели /mod и завысили
    счётчик размутов. Само восстановление прав живёт ЗДЕСЬ и только здесь:
    второго места, знающего, что такое «права обычного участника», быть не
    должно — они разъедутся при первой же правке.
    """
    try:
        # Берём дефолтные права группы; если недоступны — безопасный разрешающий набор.
        perms = None
        try:
            chat = await bot.get_chat(chat_id)
            perms = getattr(chat, "permissions", None)
        except Exception:
            perms = None
        if perms is None:
            perms = ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=perms)
    except Exception as e:
        logger.warning("⚠️ Не удалось размутить пользователя %s в чате %s: %s", user_id, chat_id, e)
        return False

    _muted_until.pop((chat_id, user_id), None)
    _reset_user(user_id)
    if journal:
        try:
            log_moderation_action("unmute", chat_id, user_id, name or str(user_id), admin_name)
        except Exception as e:
            logger.debug("🛡 Не удалось записать размут в журнал: %s", e)
    return True


# ─── уведомление модераторов об автоматическом муте ─────────────────

async def _notify_moderators_mute(bot, chat_id: int, user_id: int, name: str,
                                  minutes: int, reason: str) -> None:
    """
    Сообщает МОДЕРАТОРАМ в личку, что автоматика кого-то замутила, и даёт
    кнопку «🔓 Размутить» — чтобы несправедливый мут можно было снять, не
    заходя в группу.

    Шлём модераторам с правом «🛡DDoS-Guard» (остальным кнопка всё равно не
    нажмётся) И ВЛАДЕЛЬЦАМ — 2026-08-11, просьба Максима: «за флуд бот пишет в
    чате, а в ЛС не присылает». До этого владельцам не слали намеренно («есть
    панель /mod с журналом»), но на практике уведомление в личке нужно не ради
    журнала, а ради КНОПКИ: снять несправедливый мут может тот, кто первым его
    увидел, не заходя ни в группу, ни в панель. Ровно тем же порядком 11.08
    заведено уведомление о муте, который выдаёт модель (notify_owners_ai_mute).
    Тихая: недоступность одного получателя не мешает остальным и не роняет
    модерацию (вся antispam.py обязана быть «тихой»).
    """
    try:
        from services.roles import list_moderators, can
        # dict.fromkeys — порядок сохраняем, дубли убираем: владелец вполне
        # может оказаться и в списке модераторов.
        recipients = list(dict.fromkeys(
            [uid for uid in list_moderators() if can(uid, "mod")] + list(ADMIN_IDS)
        ))
        if not recipients:
            return

        # Название группы — из списка известных чатов (без запроса к Telegram).
        title = str(chat_id)
        try:
            from database.history import get_known_chats
            for c in get_known_chats():
                if c["chat_id"] == chat_id:
                    title = c.get("title") or str(chat_id)
                    break
        except Exception:
            pass

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 Размутить", callback_data=f"mod:unmute:{chat_id}:{user_id}")
        ]])
        text = (f"🛡 Антиспам: {name} получил мут на {minutes} мин.\n"
                f"Группа: {title}\nПричина: {reason}")
        for uid in recipients:
            try:
                await bot.send_message(chat_id=uid, text=text, reply_markup=kb)
            except Exception as e:
                logger.debug("🛡 Не удалось уведомить модератора %s о муте: %s", uid, e)
    except Exception as e:
        logger.debug("🛡 Ошибка уведомления модераторов о муте: %s", e)


# ─── ручные действия админа (карточка пользователя, /users) ─────────
# Отличаются от автоматики тем, что решение принял человек: проверку
# «кого нельзя трогать» (_is_protected) здесь НЕ применяем — админ группы
# вполне может быть целью. Оставлен единственный жёсткий запрет: свои же
# админы бота и сам бот. Всё остальное разрешает или запрещает Telegram,
# его отказ возвращается вызывающему тексту ошибки.

def _manual_guard(bot, user_id: int, actor_id: int | None = None) -> str:
    """
    Причина отказа в ручном действии или пустая строка, если можно.
    Иерархия (services/roles.can_act_on): владелец может ко всем, модератор —
    только к обычным участникам (ни к владельцу, ни к другому модератору,
    ни к самому себе). actor_id=None — вызов не от человека, действует старое
    правило «персонал бота не трогаем».
    """
    from services.roles import can_act_on, is_staff, is_owner
    if getattr(bot, "id", None) == user_id:
        return "Это сам бот."
    if actor_id is None:
        return "Это администратор бота — трогать его нельзя." if is_staff(user_id) else ""
    if not can_act_on(actor_id, user_id):
        if actor_id == user_id:
            return "Нельзя применять меры к самому себе."
        return ("Это владелец бота — трогать его нельзя." if is_owner(user_id)
                else "Это модератор бота — его может трогать только владелец.")
    return ""


async def mute_user(bot, chat_id: int, user_id: int, seconds: int,
                    name: str | None = None, admin_name: str | None = None,
                    actor_id: int | None = None, action: str = "mute_adm") -> str:
    """
    Ручной мут из карточки пользователя. Возвращает пустую строку при успехе
    или текст ошибки. Пишет в журнал модерации действием «mute_adm» — чтобы в
    /mod было видно, что это решение админа, а не срабатывание антифлуда.

    action (2026-07-20) — тип записи в журнале. Кроме «mute_adm» бывает
    «mute_ai»: мут, который бот решил выдать сам в режиме «Сам в разговор».
    Новый тип ОБЯЗАН быть добавлен в словарь значков панели /mod, иначе журнал
    покажет его чужим значком и начнёт врать.
    """
    deny = _manual_guard(bot, user_id, actor_id)
    if deny:
        return deny
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
    except Exception as e:
        logger.warning("⚠️ Ручной мут не удался (%s в чате %s): %s", user_id, chat_id, e)
        return str(e)

    _muted_until[(chat_id, user_id)] = time.monotonic() + seconds
    try:
        log_moderation_action(action, chat_id, user_id, name or str(user_id), admin_name)
        dossier_add_mute(user_id)
    except Exception as e:
        logger.debug("🛡 Не удалось записать ручной мут: %s", e)
    logger.info("🛡 Админ %s замутил %s на %d сек (чат %s)", admin_name, name or user_id, seconds, chat_id)
    return ""


async def notify_owners_ai_mute(bot, chat_id: int, user_id: int, name: str,
                                seconds: int, quote: str) -> None:
    """
    Сообщает ВЛАДЕЛЬЦАМ в личку, что бот сам выдал мут в режиме «Сам в разговор»
    (2026-07-20), и даёт кнопку «🔓 Размутить».

    ⚠️ 2026-08-11: ШЛЁМ И МОДЕРАТОРАМ с правом «🛡DDoS-Guard» (просьба
    Максима — «кнопка разблокировать у админа и модераторов не появилась»).
    До этого уведомление уходило ТОЛЬКО владельцам: считалось, что за решением
    модели должен следить хозяин бота. На деле мут снимает тот, кто первым
    увидел, а модераторы про него попросту не знали — снять несправедливый мут
    было некому, пока владелец не заглянет в личку.

    В сообщение кладём ЦИТАТУ сообщения, за которое прилетел мут: без неё
    невозможно понять, справедливо ли решение, а журнал улик для этого типа
    записей не ведётся.
    Тихая: сбой уведомления не должен отменять уже выданный мут.
    """
    try:
        from config import ADMIN_IDS
        from services.roles import list_moderators, can
        # Владельцы + модераторы с правом «mod» (кнопка у остальных не нажмётся).
        # dict.fromkeys — порядок сохраняем, дубли убираем: владелец может
        # оказаться и в списке модераторов.
        recipients = list(dict.fromkeys(
            list(ADMIN_IDS) + [uid for uid in list_moderators() if can(uid, "mod")]
        ))
        if not recipients:
            return

        title = str(chat_id)
        try:
            from database.history import get_known_chats
            for c in get_known_chats():
                if c["chat_id"] == chat_id:
                    title = c.get("title") or str(chat_id)
                    break
        except Exception:
            pass

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 Размутить", callback_data=f"mod:unmute:{chat_id}:{user_id}")
        ]])
        snippet = (quote or "").strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        text = (f"🗣 Бот сам выдал мут: {name} на {seconds // 60} мин {seconds % 60} сек.\n"
                f"Группа: {title}\n"
                f"Сообщение: {snippet or '(без текста)'}")
        for uid in recipients:
            try:
                await bot.send_message(chat_id=uid, text=text, reply_markup=kb)
            except Exception as e:
                logger.debug("🛡 Не удалось уведомить %s о муте от бота: %s", uid, e)
    except Exception as e:
        logger.debug("🛡 Ошибка уведомления о муте от бота: %s", e)


async def kick_user(bot, chat_id: int, user_id: int,
                    name: str | None = None, admin_name: str | None = None,
                    actor_id: int | None = None) -> str:
    """
    Кик: выгнать из группы с возможностью вернуться. В Telegram это бан со
    снятием сразу же (без снятия человек не сможет войти снова).
    Возвращает пустую строку при успехе или текст ошибки.
    """
    deny = _manual_guard(bot, user_id, actor_id)
    if deny:
        return deny
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except Exception as e:
        logger.warning("⚠️ Кик не удался (%s из чата %s): %s", user_id, chat_id, e)
        return str(e)
    try:
        log_moderation_action("kick", chat_id, user_id, name or str(user_id), admin_name)
    except Exception as e:
        logger.debug("🛡 Не удалось записать кик в журнал: %s", e)
    logger.info("🛡 Админ %s выгнал %s из чата %s", admin_name, name or user_id, chat_id)
    return ""


async def ban_user(bot, chat_id: int, user_id: int,
                   name: str | None = None, admin_name: str | None = None,
                   actor_id: int | None = None) -> str:
    """Бан: выгнать без права вернуться. Снимается кнопкой «Разбанить»."""
    deny = _manual_guard(bot, user_id, actor_id)
    if deny:
        return deny
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        logger.warning("⚠️ Бан не удался (%s в чате %s): %s", user_id, chat_id, e)
        return str(e)
    try:
        log_moderation_action("ban", chat_id, user_id, name or str(user_id), admin_name)
    except Exception as e:
        logger.debug("🛡 Не удалось записать бан в журнал: %s", e)
    logger.info("🛡 Админ %s забанил %s в чате %s", admin_name, name or user_id, chat_id)
    return ""


async def unban_user(bot, chat_id: int, user_id: int,
                     name: str | None = None, admin_name: str | None = None,
                     actor_id: int | None = None) -> str:
    """Снятие бана: человек снова может войти в группу (сам, по ссылке)."""
    deny = _manual_guard(bot, user_id, actor_id)
    if deny:
        return deny
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
    except Exception as e:
        logger.warning("⚠️ Разбан не удался (%s в чате %s): %s", user_id, chat_id, e)
        return str(e)
    try:
        log_moderation_action("unban", chat_id, user_id, name or str(user_id), admin_name)
    except Exception as e:
        logger.debug("🛡 Не удалось записать разбан в журнал: %s", e)
    logger.info("🛡 Админ %s снял бан с %s в чате %s", admin_name, name or user_id, chat_id)
    return ""


# ─── фильтр ссылок ──────────────────────────────────────────────────
# Удаляет сообщения со ссылками на чужие сайты/группы (тумблер «🔗 Фильтр
# ссылок» в /mod, settings 'linkfilter_enabled'). Белый список доменов —
# LINKFILTER_WHITELIST (config.py). За LINKFILTER_MUTE_COUNT удалённых ссылок
# за окно LINKFILTER_WINDOW_SEC — временный мут (длительность общая с
# антиспамом, ключ antispam_mute_sec). Счётчик повторов живёт в памяти и
# сбрасывается при рестарте — как детектор флуда, это нормально.

# (chat_id, user_id) -> deque[monotonic_ts] удалений ссылок (мут за повторы)
_link_strikes: dict[tuple[int, int], deque] = defaultdict(deque)


def is_linkfilter_enabled() -> bool:
    """Тумблер фильтра ссылок (settings: linkfilter_enabled, панель /mod)."""
    return get_setting("linkfilter_enabled", LINKFILTER_ENABLED_DEFAULT) == "1"


def _extract_links(message) -> list[str]:
    """Все ссылки сообщения: явные (url) и спрятанные под текст (text_link),
    из текста И из подписи к фото. Смещения entity Telegram считает в UTF-16,
    поэтому только parse_entities (он режет правильно) — никакой самодельной
    нарезки по offset."""
    links = []
    try:
        if message.text:
            for ent, val in message.parse_entities(types=["url", "text_link"]).items():
                links.append(ent.url if ent.type == "text_link" else val)
        if message.caption:
            for ent, val in message.parse_caption_entities(types=["url", "text_link"]).items():
                links.append(ent.url if ent.type == "text_link" else val)
    except Exception as e:
        logger.debug("🛡 Не удалось разобрать ссылки сообщения: %s", e)
    return links


def _is_whitelisted(url: str) -> bool:
    """True, если домен ссылки из белого списка (сам домен или его поддомен)."""
    host = url.lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0]
    host = host.split("@")[-1].split(":", 1)[0]  # убрать логин@ и :порт
    return any(host == d or host.endswith("." + d) for d in LINKFILTER_WHITELIST)


async def check_and_delete_links(bot, chat_id: int, user, message) -> bool:
    """
    Фильтр ссылок. Зовётся из collect_group_message на каждое сообщение группы
    (после антиспама). Возвращает True, если сообщение удалено. «Тихая», как
    check_and_mute: любая ошибка глушится — модерация не должна ронять бота.
    """
    try:
        if not is_linkfilter_enabled():
            return False

        user_id = user.id
        # Персональное разрешение из карточки пользователя: этому человеку
        # ссылки можно. Проверяем ДО разбора сообщения — дешевле.
        from services.user_settings import links_allowed
        if links_allowed(user_id):
            return False

        if not any(not _is_whitelisted(u) for u in _extract_links(message)):
            return False
        # Защищённых (бот-админы, админы/владелец группы, сам бот) не трогаем
        if await _is_protected(bot, chat_id, user_id):
            return False

        try:
            await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
        except Exception as e:
            # Нет прав или сообщение уже удалено (например, добито антиспамом)
            logger.debug("🛡 Фильтр ссылок: не удалось удалить сообщение %s в чате %s: %s",
                         message.message_id, chat_id, e)
            return False

        name = user.first_name or (f"@{user.username}" if user.username else str(user_id))

        # Журнал + улика (текст удалённого) — тем же механизмом, что у мутов
        try:
            log_id = log_moderation_action("linkdel", chat_id, user_id, name)
            save_mute_evidence(log_id, [{"text": message.text or message.caption or "",
                                         "has_photo": bool(message.photo)}])
        except Exception as e:
            logger.debug("🛡 Не удалось записать удаление ссылки в журнал: %s", e)

        # Личное дело: +1 удалённая ссылка
        try:
            dossier_add_linkdel(user_id)
        except Exception as e:
            logger.debug("🛡 Не удалось записать ссылку в личное дело: %s", e)

        logger.info("🛡 Фильтр ссылок: удалено сообщение со ссылкой от %s (чат %s)", name, chat_id)

        # Предупреждение в чат — самоудаляется через 15 сек, чтобы не мусорить
        try:
            sent = await bot.send_message(chat_id=chat_id,
                                          text=f"🔗 {name}, ссылки в этом чате запрещены.")
            if sent:
                schedule_delete(bot, chat_id, sent.message_id, 15)
        except Exception:
            pass

        # Мут за повторы: LINKFILTER_MUTE_COUNT удалений за LINKFILTER_WINDOW_SEC
        now = time.monotonic()
        dq = _link_strikes[(chat_id, user_id)]
        dq.append(now)
        while dq and dq[0] < now - LINKFILTER_WINDOW_SEC:
            dq.popleft()
        # Персональный иммунитет действует и здесь: ссылки такому человеку
        # всё равно удаляем, но мут за повторы не выдаём.
        from services.user_settings import is_immune
        if len(dq) >= LINKFILTER_MUTE_COUNT and not is_immune(user_id):
            _, _, mute_sec = get_thresholds_for(user_id)
            if await _apply_mute(bot, chat_id, user_id, mute_sec):
                _link_strikes.pop((chat_id, user_id), None)
                _muted_until[(chat_id, user_id)] = now + mute_sec
                try:
                    log_moderation_action("mute", chat_id, user_id, name)
                except Exception as e:
                    logger.debug("🛡 Не удалось записать мут за ссылки в журнал: %s", e)
                # Личное дело: мут за ссылки тоже лишает статуса «проверенный»
                try:
                    dossier_add_mute(user_id)
                except Exception as e:
                    logger.debug("🛡 Не удалось записать мут за ссылки в личное дело: %s", e)
                minutes = max(1, mute_sec // 60)
                unmute_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔓 Размутить", callback_data=f"mod:unmute:{chat_id}:{user_id}")
                ]])
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"🛡 {name} получает мут на {minutes} мин. за повторные ссылки.",
                        reply_markup=unmute_kb,
                    )
                except Exception:
                    pass
                logger.info("🛡 Фильтр ссылок: %s получил мут на %d мин за повторные ссылки (чат %s)",
                            name, minutes, chat_id)
                await _notify_moderators_mute(bot, chat_id, user_id, name, minutes,
                                              f"{LINKFILTER_MUTE_COUNT} ссылки за час")
        return True

    except Exception as e:
        logger.debug("🛡 Ошибка в check_and_delete_links: %s", e)
        return False
