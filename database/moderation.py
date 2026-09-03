# ───────────────────────────────────────────────
#  database/moderation.py — журнал наказаний и улики (02.09.2026).
#
#  Шаг 4 разреза history.py. Две таблицы:
#    moderation_log — кого, за что и когда наказали (мут, размут, кик, бан,
#                     удалённая ссылка); показывает панель /mod и сайт
#    mute_evidence  — ТЕКСТЫ удалённых при муте сообщений, привязанные к
#                     записи журнала
#
#  ⚠️ ЗДЕСЬ ЛЕЖИТ ЧУЖАЯ ПЕРЕПИСКА. Улики — это дословные сообщения живых
#  людей, удалённые ботом. С 01.09.2026 их показывает и страница /journal на
#  сайте, поэтому цена утечки входа выросла. Добавляя сюда что-либо, спроси:
#  «что будет, если это однажды увидит чужой».
#
#  ⚠️ УЛИКИ УМИРАЮТ ВМЕСТЕ С ЖУРНАЛОМ, и обе чистки это делают явно: без
#  своей записи улика превращается в мусор, до которого уже ничем не
#  добраться. Заведёшь третий способ стирать журнал — не забудь про улики.
#
#  Кто это читает:
#    services/antispam.py         — пишет записи при муте, кике, бане, ссылке
#    handlers/admin/panel_mod.py  — панель /mod, экран улик, размут
#    handlers/admin/panel_users.py — карточка участника
#    web/pages.py, web/actions.py — страница /journal и её очистка
#    jobs/cleanup.py              — суточная чистка старых записей
#
#  ⚠️ ТРИ ФУНКЦИИ СОБРАНЫ СЮДА ИЗ РАЗНЫХ КОНЦОВ history.py: шесть основных
#  лежали вместе, а обе чистки — за пятьсот и за восемьсот строк от них,
#  среди чужих журналов. Код не менялся ни на символ.
#
#  ⚠️ get_moderation_counts ЧИТАЕТ selftest — ИСХОДНЫМ ТЕКСТОМ, а не вызовом
#  (группа «страница журналов»): он вынимает из тела функции список видов
#  записей и требует, чтобы сводка «за N дней» считала ровно те же виды, что
#  показывает журнал. Переименуешь функцию или перепишешь её возвраты —
#  проверка это заметит. Так и задумано: 20.07.2026 завели новый вид мута,
#  забыли вписать в счётчик, и панель молча занижала цифру.
# ───────────────────────────────────────────────

import logging

from ._core import _lock, _get_connection

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
#  Модерация: журнал действий + улики (для панели /mod)
#  Персистентная замена прежним in-memory счётчикам антиспама.
#  Статистика считается за скользящее окно в N дней; улики — тексты
#  удалённых при муте сообщений. Медиа не хранятся (только has_photo).
# ───────────────────────────────────────────────

def log_moderation_action(action: str, chat_id: int, user_id: int, name: str | None = None,
                          admin_name: str | None = None) -> int:
    """
    Записывает действие модерации ('mute'/'unmute'/'linkdel') в журнал. Возвращает id строки.
    admin_name — кто выполнил (передаётся при размуте кнопкой или /unmute; у автоматики None).
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "INSERT INTO moderation_log (ts, action, chat_id, user_id, name, admin_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_time.time(), action, chat_id, user_id, name or str(user_id), admin_name),
        )
        log_id = cur.lastrowid
        conn.commit()
    return log_id


def save_mute_evidence(log_id: int, messages: list) -> None:
    """
    Сохраняет тексты удалённых сообщений для мута (log_id).
    messages — список dict с ключами text (str) и has_photo (bool).
    Текст обрезается до 1000 символов на сообщение (защита от гигантских вставок).
    """
    if not log_id or not messages:
        return
    with _lock:
        conn = _get_connection()
        for i, m in enumerate(messages):
            conn.execute(
                "INSERT INTO mute_evidence (log_id, ord, text, has_photo) VALUES (?, ?, ?, ?)",
                (log_id, i, (m.get("text") or "")[:1000], 1 if m.get("has_photo") else 0),
            )
        conn.commit()


def get_moderation_counts(days: int = 7) -> dict:
    """Сколько мутов/размутов было за последние `days` дней (по журналу)."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT action, COUNT(*) AS n FROM moderation_log WHERE ts >= ? GROUP BY action",
            (cutoff,),
        ).fetchall()
    counts = {"mutes": 0, "unmutes": 0, "linkdels": 0, "kicks": 0, "bans": 0}
    for r in rows:
        if r["action"] in ("mute", "mute_adm", "mute_ai"):
            # mute — автоматика (антифлуд/ссылки), mute_adm — ручной мут админа
            # из карточки пользователя, mute_ai — мут, который бот выдал сам
            # в режиме «Сам в разговор». В сводке /mod считаем вместе.
            # ⚠️ НОВЫЙ ТИП МУТА ДОБАВЛЯТЬ И СЮДА: иначе он будет виден в списке
            # последних действий, но пропадёт из счётчика «За 7 дней» —
            # панель начнёт занижать цифру (наступили на этом 2026-07-20).
            counts["mutes"] += r["n"]
        elif r["action"] in ("unmute", "unban"):
            counts["unmutes"] += r["n"]
        elif r["action"] == "linkdel":
            # Удаления ссылок фильтром (services/antispam.py::check_and_delete_links)
            counts["linkdels"] = r["n"]
        elif r["action"] == "kick":
            counts["kicks"] = r["n"]
        elif r["action"] == "ban":
            counts["bans"] = r["n"]
    return counts


def get_recent_moderation_actions(limit: int = 5) -> list:
    """Последние действия модерации (новые первыми) из журнала."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT id, ts, action, chat_id, user_id, name, admin_name FROM moderation_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_moderation_entry(log_id: int) -> dict | None:
    """Одна строка журнала по id (для экрана улик)."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT id, ts, action, chat_id, user_id, name, admin_name FROM moderation_log WHERE id = ?",
            (log_id,),
        ).fetchone()
    return dict(row) if row else None


def get_mute_evidence(log_id: int) -> list:
    """Тексты удалённых сообщений конкретного мута (по порядку)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT ord, text, has_photo FROM mute_evidence WHERE log_id = ? ORDER BY ord ASC",
            (log_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_old_moderation_log(days: int = 7) -> int:
    """
    Удаляет из журнала модерации записи старше `days` дней вместе с их уликами.
    Возвращает число удалённых строк журнала.
    """
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM moderation_log WHERE ts < ?", (cutoff,)
        ).fetchall()]
        if old_ids:
            qmarks = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM mute_evidence WHERE log_id IN ({qmarks})", old_ids)
            conn.execute(f"DELETE FROM moderation_log WHERE id IN ({qmarks})", old_ids)
        conn.commit()
    deleted = len(old_ids)
    if deleted:
        logger.info("🛡 Очистка журнала модерации: удалено %d записей старше %d дней", deleted, days)
    return deleted


def clear_moderation_log() -> int:
    """
    Полная очистка журнала модерации (кнопка «🧹 Очистить журнал» в /mod).
    Улики стираются вместе с журналом: они привязаны к его записям, и без
    журнала превратились бы в мусор, до которого уже ничем не добраться.
    Возвращает число удалённых записей журнала.
    """
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM mute_evidence")
        cur = conn.execute("DELETE FROM moderation_log")
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted
