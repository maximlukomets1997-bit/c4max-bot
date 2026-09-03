# ───────────────────────────────────────────────
#  database/journals.py — ЧЕТЫРЕ журнала бота (02.09.2026).
#
#  Шаг 5 разреза history.py. Четыре независимые таблицы, устроенные почти
#  одинаково — «когда, кто, что» плюс своя чистка по сроку:
#
#    staff_log      — действия ПЕРСОНАЛА: кто из владельцев и модераторов
#                     что нажал. Экран «📋 Журнал персонала», только владельцу
#    knowledge_log  — действия с базой знаний RAG (панель /rag)
#    proactive_log  — проверки режима «Сам в разговор»: чем кончилась каждая,
#                     сколько думала модель, какой длины вышла реплика
#    join_log       — вступления в группы (приветствие новичков, капча)
#
#  ⚠️ ЖУРНАЛ НАКАЗАНИЙ СЮДА НЕ ВХОДИТ — он в database/moderation.py, и это
#  не придирка: к нему привязаны УЛИКИ (тексты чужих сообщений), у него своя
#  чистка, которая обязана убирать улики вместе с записями, и свои читатели.
#  Четыре журнала здесь — простые: строка записалась, строка удалилась.
#
#  ⚠️ У КАЖДОГО СВОЙ СРОК ХРАНЕНИЯ, и все четыре чистки зовёт один суточный
#  цикл jobs/cleanup.py, каждую под своим `try`. Сроки заданы не здесь, а в
#  config.py (STAFF_LOG_DAYS, KB_LOG_DAYS, PROACTIVE_LOG_DAYS, JOIN_LOG_DAYS) —
#  чтобы менялись в одном месте.
#
#  ⚠️ ВРЕМЯ — time.time() (unix, дробное), как в moderation_log. Не путать с
#  архивом групп и вызовами API: там время строкой UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС».
#  Оба вида в базе живут одновременно, и сравнивать их между собой нельзя.
#
#  Кто это читает:
#    handlers/admin/panel_users.py — экран журнала персонала
#    handlers/admin/panel_rag.py   — журнал базы знаний
#    handlers/admin/panel_prompts.py — цифры участия в разговоре
#    services/greeter.py           — пишет вступления
#    web/pages.py, web/actions.py  — страница /journal и очистки
#    jobs/cleanup.py               — все четыре чистки по сроку
#
#  ⚠️ ДВА ЖУРНАЛА СОБРАНЫ ИЗ РАЗНЫХ МЕСТ history.py: вступления лежали
#  отдельно от прочих трёх, за полсотни строк, через чужие функции. Код не
#  менялся ни на символ.
# ───────────────────────────────────────────────

import logging

from ._core import _lock, _get_connection

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Журнал действий персонала (экран «📋 Журнал персонала», только владельцу)
# ─────────────────────────────────────────────

def log_staff_action(actor_id: int, actor_name: str, action: str,
                     target_id: int = 0, details: str = "") -> None:
    """
    Записывает осознанное действие персонала: модерацию, смену настроек,
    выдачу прав. В отличие от moderation_log сюда НЕ пишет автоматика —
    только то, что сделал человек своими руками.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO staff_log (ts, actor_id, actor_name, action, target_id, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_time.time(), actor_id, actor_name or str(actor_id), action, target_id, details or ""),
        )
        conn.commit()


def get_recent_staff_actions(limit: int = 20) -> list:
    """Последние действия персонала (новые первыми)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT id, ts, actor_id, actor_name, action, target_id, details "
            "FROM staff_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_staff_actions(days: int = 7) -> int:
    """Сколько действий персонала за последние `days` дней."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT COUNT(*) AS n FROM staff_log WHERE ts >= ?", (cutoff,)).fetchone()
    return row["n"] if row else 0


def delete_old_staff_log(days: int = 30) -> int:
    """Удаляет записи журнала персонала старше `days` дней (чистит cleanup_loop)."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM staff_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount or 0
        conn.commit()
    if deleted:
        logger.info("🧹 Журнал персонала: удалено %d записей старше %d дней", deleted, days)
    return deleted


def clear_staff_log() -> int:
    """Полная очистка журнала персонала (кнопка «🧹 Очистить журнал»)."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM staff_log")
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted


# ─────────────────────────────────────────────
#  Журнал действий с базой знаний RAG (панель /rag)
# ─────────────────────────────────────────────

def add_kb_action(action: str, article: str, user_id: int = 0) -> None:
    """
    Записывает действие с базой знаний: «одобрена», «удалена из базы»,
    «удалена из ожидания», «заменена», «добавлена», «пересборка».
    article — человекочитаемое название статьи (не имя файла).
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO knowledge_log (ts, action, article, user_id) VALUES (?, ?, ?, ?)",
            (_time.time(), action, (article or "")[:200], user_id),
        )
        conn.commit()


def get_recent_kb_actions(limit: int = 5) -> list:
    """Последние действия с базой знаний (новые первыми)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT id, ts, action, article, user_id FROM knowledge_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_old_kb_log(days: int = 30) -> int:
    """Удаляет записи журнала базы знаний старше `days` дней. Возвращает число удалённых."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM knowledge_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
    if deleted:
        logger.info("🚀 Очистка журнала базы знаний: удалено %d записей старше %d дней", deleted, days)
    return deleted


def clear_kb_log() -> int:
    """Полная очистка журнала базы знаний (кнопка «🧹 Очистить журнал»). Возвращает число удалённых."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM knowledge_log")
        deleted = cur.rowcount
        conn.commit()
    return deleted


# ───────────────────────────────────────────────
#  Журнал проактивных проверок (2026-07-31)
# ─────────────────────────────────────────────

def log_proactive_check(chat_id: int, outcome: str, model: str = "",
                        seconds: float = 0.0, reply_len: int = 0,
                        trigger_kind: str = "text") -> None:
    """
    Записывает ОДНУ проактивную проверку — то есть один запрос к модели
    в режиме «Сам в разговор» и его исход.

    Исходы (`outcome`), их ровно пять и они соответствуют веткам
    services/proactive.py::_run_proactive — новый исход добавлять И ТАМ, И
    в подписи `_OUTCOME_TITLES` панели, иначе он покажется голым кодом:
      reply      — бот вступил в разговор;
      reply_mute — вступил и заодно сам выдал мут («руки»);
      silent     — модель решила промолчать (вернула «ПРОПУСК»);
      empty      — ответ состоял из одной пометки мута, говорить было нечего;
      error      — запрос сорвался (сеть, отказ всех моделей цепочки).

    ⚠️ Зовётся ТОЛЬКО из фоновой задачи проверки — не с горячего пути
    архиватора групп: там на каждое сообщение и походов в базу быть не должно.
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO proactive_log (chat_id, outcome, model, seconds, reply_len, trigger_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, outcome, model, round(float(seconds), 2), int(reply_len), trigger_kind),
        )
        conn.commit()


def proactive_stats(start_utc: str, end_utc: str | None = None) -> dict:
    """
    Итоги проактивных проверок за промежуток. Границы — строки UTC
    «ГГГГ-ММ-ДД ЧЧ:ММ:СС», как у остальных счётчиков по времени; правая
    граница строго меньше (как в суточном отчёте), пустая — «по сейчас».

    Возвращает словарь: сколько проверок всего, сколько каких исходов,
    среднее время ответа модели и разбивку по виду триггера.
    """
    where = "ts >= ?"
    params = [start_utc]
    if end_utc:
        where += " AND ts < ?"
        params.append(end_utc)

    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            f"SELECT outcome, COUNT(*) AS n, AVG(seconds) AS avg_sec "
            f"FROM proactive_log WHERE {where} GROUP BY outcome", params,
        ).fetchall()
        kinds = conn.execute(
            f"SELECT trigger_kind, COUNT(*) AS n FROM proactive_log "
            f"WHERE {where} GROUP BY trigger_kind", params,
        ).fetchall()

    out = {"checks": 0, "reply": 0, "reply_mute": 0, "silent": 0, "empty": 0,
           "error": 0, "avg_sec": 0.0, "by_trigger": {}}
    total_sec = 0.0
    for r in rows:
        n = r["n"] or 0
        out["checks"] += n
        out[r["outcome"]] = out.get(r["outcome"], 0) + n
        total_sec += (r["avg_sec"] or 0.0) * n
    if out["checks"]:
        out["avg_sec"] = total_sec / out["checks"]
    out["by_trigger"] = {r["trigger_kind"] or "text": r["n"] for r in kinds}
    return out


def proactive_by_chat(start_utc: str, limit: int = 10) -> list:
    """Проверки и вступления по чатам за промежуток, самые бойкие сверху."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT chat_id, COUNT(*) AS checks, "
            "       SUM(CASE WHEN outcome IN ('reply', 'reply_mute') THEN 1 ELSE 0 END) AS replies "
            "FROM proactive_log WHERE ts >= ? "
            "GROUP BY chat_id ORDER BY checks DESC LIMIT ?",
            (start_utc, limit),
        ).fetchall()
    return [(r["chat_id"], r["checks"] or 0, r["replies"] or 0) for r in rows]


def proactive_by_day(days: int = 7) -> list:
    """
    Проверки и вступления по дням — чтобы видеть, что изменилось после правки
    порога или промпта. Дни считаются ПО МЕСТНОМУ времени сервера (`localtime`),
    а он живёт в Europe/Kyiv — то есть по тем же суткам, что и отчёты.
    ⚠️ На машине с другим часовым поясом границы дней уедут; для показа это
    терпимо, но считать по ним деньги нельзя.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT date(ts, 'localtime') AS day, COUNT(*) AS checks, "
            "       SUM(CASE WHEN outcome IN ('reply', 'reply_mute') THEN 1 ELSE 0 END) AS replies "
            "FROM proactive_log WHERE ts >= datetime('now', ?) "
            "GROUP BY day ORDER BY day DESC",
            (f"-{int(days)} days",),
        ).fetchall()
    return [(r["day"], r["checks"] or 0, r["replies"] or 0) for r in rows]


def delete_old_proactive_log(days: int = 30) -> int:
    """Чистка журнала проверок (суточный цикл). Возвращает число удалённых."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "DELETE FROM proactive_log WHERE ts < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        deleted = cur.rowcount or 0
        conn.commit()
    if deleted:
        logger.info("🤖 Очистка журнала проактивных проверок: удалено %d записей старше %d дней",
                    deleted, days)
    return deleted


# ─── 👋 Журнал вступлений в группы (приветствие новичков) ───────────

def log_join(chat_id: int, user_id: int, name: str, outcome: str) -> None:
    """
    Пишет строку в журнал вступлений (services/greeter.py).

    outcome: 'join' — человек вошёл в группу, 'ok' — прошёл проверку
    «я не бот», 'timeout' — не нажал за отведённый срок, 'kick' — не нажал
    и был выгнан (кикать не прошедших — отдельный тумблер панели /mod).
    На одного новичка приходится ДВЕ строки: приход и исход, — иначе
    «пришло» и «прошли» пришлось бы считать по разным источникам.

    Время — time.time(), как в moderation_log: обе таблицы читает одна и та
    же панель, и разнобой форматов пришлось бы разбирать в ней.
    """
    import time as _time
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO join_log (ts, chat_id, user_id, name, outcome) VALUES (?, ?, ?, ?, ?)",
            (_time.time(), chat_id, user_id, name or str(user_id), outcome),
        )
        conn.commit()


def get_join_counts(days: int = 7) -> dict:
    """
    Сколько человек пришло и чем кончилась их проверка за последние `days`
    дней. Ключи: joins, ok, timeouts, kicks (неизвестные исходы игнорируются).
    """
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM join_log WHERE ts >= ? GROUP BY outcome",
            (cutoff,),
        ).fetchall()
    counts = {"joins": 0, "ok": 0, "timeouts": 0, "kicks": 0}
    keys = {"join": "joins", "ok": "ok", "timeout": "timeouts", "kick": "kicks"}
    for r in rows:
        key = keys.get(r["outcome"])
        if key:
            counts[key] = r["n"]
    return counts


def delete_old_join_log(days: int = 30) -> int:
    """Чистка журнала вступлений (суточный цикл). Возвращает число удалённых."""
    import time as _time
    cutoff = _time.time() - days * 86400
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM join_log WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount or 0
        conn.commit()
    if deleted:
        logger.info("👋 Очистка журнала вступлений: удалено %d записей старше %d дней",
                    deleted, days)
    return deleted
