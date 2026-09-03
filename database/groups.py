# ───────────────────────────────────────────────
#  database/groups.py — архив сообщений групп (02.09.2026).
#
#  Шаг 7 разреза history.py. Через этот файл проходит КАЖДОЕ сообщение в
#  группе — это самая горячая тема из всех перенесённых.
#
#  Две таблицы:
#    group_messages — стенограмма групп; живёт 10 дней, чистит jobs/cleanup.py
#    known_chats    — где бот вообще состоит (карточка участника берёт отсюда
#                     список групп, в которых можно выдать мут, кик, бан)
#
#  Что отсюда читают:
#    services/gemini.py       — последние N сообщений как контекст режима
#                               «Сам в разговор»
#    services/group_digest.py — неделя сообщений как сырьё недельного дайджеста
#    handlers/messages.py     — пишет каждое входящее и запоминает группу
#    handlers/admin/*         — счётчик архива, список групп в карточке
#
#  ⚠️ ВРЕМЯ ЗДЕСЬ — СТРОКА UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС» (CURRENT_TIMESTAMP), а не
#  time.time(), как в журналах. Строковое сравнение таких дат совпадает с
#  хронологическим — на этом держатся выборки «за период». Исключение —
#  known_chats.last_seen: там как раз time.time(). Не путать.
#
#  ⚠️ set_proactive_reset_mark ПИШЕТ НАСТРОЙКУ, а не архив: это отметка «с
#  какого момента считать архив» для счётчика в /stats. Она здесь, потому что
#  предмет у неё — архив групп, а не настройки сами по себе.
#
#  ⚠️ ЭТА ТЕМА НЕ ПОКРЫТА selftest НИ ОДНИМ ВЫЗОВОМ (проверено поиском по всем
#  восьми именам — ноль совпадений). Правка здесь страховки не имеет и
#  проверяется только руками: контекст режима «Сам в разговор», недельный
#  дайджест, список групп в карточке участника.
# ───────────────────────────────────────────────

import logging

from ._core import _lock, _get_connection
from .settings import get_setting, set_setting

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
#  Сбор сообщений группы (Stage 3 — контекстный бот)
# ─────────────────────────────────────────────

def save_group_message(
    chat_id: int,
    user_id: int,
    username: str,
    first_name: str,
    text: str,
    has_photo: bool = False,
    has_voice: bool = False,
    has_video: bool = False,
):
    """Сохраняет сообщение группы в архив для будущего анализа контекста."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO group_messages (chat_id, user_id, username, first_name, text, has_photo, has_voice, has_video)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, user_id, username or "", first_name or "", text or "",
             1 if has_photo else 0, 1 if has_voice else 0, 1 if has_video else 0),
        )
        conn.commit()


def update_last_group_message_text(chat_id: int, user_id: int, text: str) -> None:
    """
    Дописывает текст ПОСЛЕДНЕЙ записи пользователя в архиве группы
    (2026-08-10, просьба Максима «в стенограмме модель должна всегда видеть
    дословную расшифровку, а не [голосовое]»).

    Зачем: у медиа-сообщений архивный text пуст — Telegram присылает файл, а не
    слова. Расшифровка голосового и разбор картинки существовали только в
    памяти одной проверки и в стенограмме подставлялись ТОЛЬКО последнему
    сообщению; всё, что уехало вглубь истории, снова становилось «[голосовое]».
    Теперь разбор оседает в архиве и виден модели столько, сколько живёт сама
    запись (10 дней, чистка в jobs/cleanup.py).

    ⚠️ Обновляет запись ПО ПОСЛЕДНЕМУ id этого пользователя в этом чате, а не
    по id сообщения Telegram: его в архиве нет вовсе (таблица заведена без
    него). Промахнуться почти невозможно — запись создаётся в
    collect_group_message на пару строк выше вызова проактивной проверки, — но
    если человек успел прислать второе сообщение раньше, чем модель разобрала
    первое, разбор ляжет на новую запись. Цена ошибки мала (одна строка
    стенограммы), цена лечения — колонка message_id и миграция таблицы.
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            """UPDATE group_messages SET text = ?
               WHERE id = (SELECT id FROM group_messages
                           WHERE chat_id = ? AND user_id = ?
                           ORDER BY id DESC LIMIT 1)""",
            (text or "", chat_id, user_id),
        )
        conn.commit()


def set_proactive_reset_mark() -> str:
    """
    Подводит «черту» под стенограммой групп для режима «Сам в разговор»
    (кнопка «🧹Очистить РАЗГОВОРЫ» в панели промптов): бот перестаёт видеть
    всё, что было сказано ДО этого момента, во ВСЕХ чатах и у всех людей
    сразу — включая владельцев и модераторов.

    НИЧЕГО НЕ УДАЛЯЕТ. Архив group_messages остаётся цел, поэтому не страдают
    ни счётчик «архив группы за 10 дней» в /stats, ни запасной источник имён
    для списка /users. Старое уйдёт само обычной суточной чисткой (10 дней).
    Решение обратимо: сотри ключ proactive_reset_mark — стенограмма вернётся.

    Время берём у самой SQLite (CURRENT_TIMESTAMP, UTC) — тем же способом,
    каким заполняется created_at: иначе форматы могли бы разъехаться и
    сравнение строк перестало бы совпадать с хронологическим.
    Возвращает поставленную метку.
    """
    with _lock:
        conn = _get_connection()
        mark = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    # set_setting берёт _lock сам — только ПОСЛЕ выхода из блока выше
    # (обычный threading.Lock не реентерабельный, вложение = взаимоблокировка).
    set_setting("proactive_reset_mark", mark)
    return mark


def get_recent_group_messages(chat_id: int, limit: int = 25) -> list[dict]:
    """
    Последние `limit` сообщений конкретного чата из архива групп,
    в порядке от старых к новым — стенограмма беседы для проактивного
    участия в разговоре (services/proactive.py, gemini.ask_group_proactive).

    Уважает «черту» proactive_reset_mark (см. set_proactive_reset_mark):
    сказанное до неё в стенограмму не попадает, хотя физически остаётся
    в архиве.
    """
    # get_setting берёт _lock сам — читаем ДО входа в блок ниже.
    mark = get_setting("proactive_reset_mark", "")

    sql = ("SELECT user_id, username, first_name, text, has_photo, has_voice, has_video "
           "FROM group_messages WHERE chat_id=?")
    params: list = [chat_id]
    if mark:
        sql += " AND created_at > ?"
        params.append(mark)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with _lock:
        conn = _get_connection()
        rows = conn.execute(sql, params).fetchall()
    rows = list(reversed(rows))
    return [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "first_name": r["first_name"],
            "text": r["text"],
            "has_photo": bool(r["has_photo"]),
            "has_voice": bool(r["has_voice"]),
            "has_video": bool(r["has_video"]),
        }
        for r in rows
    ]


# ───────────────────────────────────────────────
#  Очистка архива групповых сообщений (retention)
# ───────────────────────────────────────────────

def delete_old_group_messages(days: int = 10) -> int:
    """
    Удаляет из group_messages записи старше `days` дней.

    created_at хранится как CURRENT_TIMESTAMP (UTC), и datetime('now') тоже UTC —
    сравнение корректное. Возвращает число удалённых строк.
    """
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "DELETE FROM group_messages WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        deleted = cur.rowcount
        conn.commit()
    if deleted:
        logger.info("🚀 Очистка архива групп: удалено %d записей старше %d дней", deleted, days)
    return deleted


# ─────────────────────────────────────────────
#  Известные группы (карточка пользователя: где выдавать мут/кик/бан)
# ─────────────────────────────────────────────

def remember_chat(chat_id: int, title: str = "") -> None:
    """
    Запоминает группу, где работает бот (зовётся из collect_group_message).
    Название обновляется на каждое сообщение — переименование группы
    подхватывается само. Пустое название не затирает сохранённое.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO known_chats (chat_id, title, last_seen)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   title     = COALESCE(NULLIF(excluded.title, ''), known_chats.title),
                   last_seen = excluded.last_seen""",
            (chat_id, title or "", _time.time()),
        )
        conn.commit()


def get_known_chats() -> list[dict]:
    """Группы, где работает бот (новые по активности сверху)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT chat_id, title, last_seen FROM known_chats "
            "ORDER BY last_seen DESC NULLS LAST, chat_id"
        ).fetchall()
    return [dict(r) for r in rows]


# ─── 📊 Выборки для недельного дайджеста группы ─────────────────────

def get_group_messages_between(chat_id: int, start_utc: str, end_utc: str | None = None) -> list[dict]:
    """
    Сообщения группы за промежуток — сырьё для недельного дайджеста
    (services/group_digest.py). Границы — строки UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС»,
    как у остальных выборок по времени; правая строго меньше.

    Отдаёт СЫРЫЕ строки, а не готовые счётчики, намеренно: дайджест считает
    и топ авторов, и распределение по дням недели и часам, и долю медиа —
    пять отдельных запросов к базе ради одного сообщения раз в неделю не
    нужны, а неделя архива это тысячи строк, не миллионы (архив групп и так
    чистится через 10 дней).

    ⚠️ Время в базе — UTC, а дайджест показывает киевские дни и часы:
    переводит их вызывающий (там же, где живёт зона), здесь только выборка.
    """
    where = "chat_id = ? AND created_at >= ?"
    params = [chat_id, start_utc]
    if end_utc:
        where += " AND created_at < ?"
        params.append(end_utc)
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            f"SELECT user_id, username, first_name, text, has_photo, has_voice, "
            f"has_video, created_at FROM group_messages WHERE {where} ORDER BY id",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
