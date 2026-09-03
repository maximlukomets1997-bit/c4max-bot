# ───────────────────────────────────────────────
#  database/news.py — подписки на новости и учёт разосланного (02.09.2026).
#
#  Шаг 2 разреза history.py (0 — фундамент, 1 — устройство базы). Первая
#  «обычная» тема: с остальными она не связана вовсе, зовёт только замок и
#  соединение.
#
#  Две таблицы, и путать их не надо:
#    news_subscriptions — КУДА слать (чаты, подписанные кнопкой «📰 Новости»)
#    sent_news          — ЧТО уже слали (адреса статей, чтобы не повторяться)
#
#  Кто это читает:
#    handlers/commands.py    — кнопка-тумблер «📰 Новости» на экране /start
#    jobs/news.py            — сам цикл рассылки, раз в 10 минут
#    services/group_digest.py — счёт новостей за неделю для дайджеста
#  Все трое зовут через `from database.history import …`, и так и остаётся:
#  history.py берёт эти имена обратно и отдаёт наружу.
#
#  ⚠️ count_sent_news_between ЖИЛА В ДРУГОМ КОНЦЕ ФАЙЛА — среди журналов, за
#  полторы тысячи строк от остальных новостей. Здесь она встала на своё место.
#  Сам код не менялся ни на символ.
# ───────────────────────────────────────────────

import sqlite3

from ._core import _lock, _get_connection

def subscribe_chat(chat_id: int) -> bool:
    """Подписывает чат на новости. Возвращает True если подписка оформлена впервые."""
    with _lock:
        conn = _get_connection()
        try:
            conn.execute("INSERT INTO news_subscriptions (chat_id) VALUES (?)", (chat_id,))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Чат уже подписан (chat_id — первичный ключ). Транзакцию откатываем:
            # соединение общее, недописанная транзакция висела бы в нём.
            conn.rollback()
            return False


def unsubscribe_chat(chat_id: int) -> bool:
    """Отписывает чат. Возвращает True если подписка была активна."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM news_subscriptions WHERE chat_id=?", (chat_id,))
        rows = cur.rowcount
        conn.commit()
    return rows > 0


def is_chat_subscribed(chat_id: int) -> bool:
    """
    Подписан ли чат на новости. Нужен КНОПКЕ-ТУМБЛЕРУ «📰 Новости» экрана
    /start: она обязана показывать текущее состояние, а не действие
    (общий стандарт тумблеров проекта, см. _onoff).
    """
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT 1 FROM news_subscriptions WHERE chat_id=? LIMIT 1", (chat_id,)
        ).fetchone()
    return row is not None


def get_subscribed_chats() -> list[int]:
    """Возвращает список ID всех подписанных чатов."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute("SELECT chat_id FROM news_subscriptions").fetchall()
    return [r["chat_id"] for r in rows]


def is_news_already_sent(url: str) -> bool:
    """Проверяет, была ли новость уже отправлена."""
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT 1 FROM sent_news WHERE url=?", (url,)).fetchone()
    return row is not None


def mark_news_as_sent(url: str):
    """Помечает новость как отправленную."""
    with _lock:
        conn = _get_connection()
        conn.execute("INSERT OR IGNORE INTO sent_news (url) VALUES (?)", (url,))
        conn.commit()


def count_sent_news_between(start_utc: str, end_utc: str | None = None) -> int:
    """Сколько новостей разослано за промежуток (таблица sent_news)."""
    where = "created_at >= ?"
    params = [start_utc]
    if end_utc:
        where += " AND created_at < ?"
        params.append(end_utc)
    with _lock:
        conn = _get_connection()
        row = conn.execute(f"SELECT COUNT(*) AS n FROM sent_news WHERE {where}", params).fetchone()
    return row["n"] if row else 0
