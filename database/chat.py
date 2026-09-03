# ───────────────────────────────────────────────
#  database/chat.py — переписка с ботом и гигиена его сообщений (02.09.2026).
#
#  Шаг 9 разреза history.py. Две вещи, обе про сообщения:
#
#    messages / user_context — САМА ПЕРЕПИСКА: что человек спросил, что бот
#        ответил, какой моделью и во сколько токенов это обошлось. Отсюда
#        берётся контекст КАЖДОГО ответа модели
#    bot_sent_messages — гигиена панелей: какие сообщения бот прислал сам,
#        чтобы стереть прошлую панель, когда рисует новую
#
#  ⚠️ КОНТЕКСТ БЕРЁТСЯ ПО ЧЕЛОВЕКУ, А НЕ ПО ЧАТУ. get_history собирает
#  последние MAX_CONTEXT_MESSAGES сообщений участника ПО ВСЕМ чатам сразу:
#  человек продолжает в личке разговор, начатый в группе, и наоборот.
#  Из-за этого же `/clear` стирает переписку человека целиком, а не в одном
#  чате.
#
#  ⚠️ MAX_CONTEXT_MESSAGES СТОИТ ЗНАЧЕНИЕМ ПО УМОЛЧАНИЮ В СИГНАТУРЕ
#  get_history — поэтому импортируется в шапке, а не внутри функции, как
#  почти всё остальное в этом пакете. Иначе значение бралось бы на момент
#  вызова, а не на момент объявления.
#
#  ⚠️ ЭТО САМЫЙ ГОРЯЧИЙ ПУТЬ БОТА: get_history зовётся на каждый ответ
#  модели (services/gemini.py, четыре места), add_messages — после каждого.
#
#  Покрытие: прямых вызовов из selftest нет, но группа «потолки ожидания»
#  зовёт настоящие ask_gemini_*, а те по дороге читают историю и пишут в
#  неё, — то есть страховка КОСВЕННАЯ. Проверено нарочной поломкой.
# ───────────────────────────────────────────────

import logging

from config import MAX_CONTEXT_MESSAGES

from ._core import _lock, _get_connection
from .settings import get_setting

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
#  Чтение истории
# ───────────────────────────────────────────────

def get_history(user_id: int, limit: int | None = MAX_CONTEXT_MESSAGES) -> list[dict]:
    """
    Возвращает объединённое контекстное окно пользователя (личка + все группы)
    в порядке от старых к новым.

    Контекст привязан к user_id, а не к конкретному чату, поэтому диалоги
    одного человека в личных сообщениях и в группах образуют единое окно.

    :param limit: вернуть только последние `limit` сообщений (скользящее окно).
                  По умолчанию MAX_CONTEXT_MESSAGES. None — вернуть всё.
    """
    with _lock:
        conn = _get_connection()
        if limit is not None:
            # Берём последние `limit` строк, затем разворачиваем в хронологию
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_history_length(user_id: int) -> int:
    """Возвращает размер объединённого контекстного окна пользователя."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def get_user_usage(user_id: int) -> dict:
    """
    Возвращает накопленный расход на пользователя (по всем чатам: личка + группы):
      - total_tokens:   суммарно токенов (вход+выход) за всё время
      - total_requests: сколько запросов сделано
    """
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT total_tokens, total_requests FROM user_token_usage WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"total_tokens": 0, "total_requests": 0}
    return {
        "total_tokens": row["total_tokens"] or 0,
        "total_requests": row["total_requests"] or 0,
    }


# ───────────────────────────────────────────────
#  Запись и обновление
# ───────────────────────────────────────────────

def add_messages(
    chat_id: int,
    user_id: int,
    user_text: str,
    answer: str,
    prompt_tokens: int = 0,
    model_name: str = None,
    total_tokens: int = 0,
):
    """
    Сохраняет пару (вопрос пользователя + ответ бота) и поддерживает
    единое скользящее окно контекста.

    Контекст объединён по user_id: после вставки в окне остаются только
    последние MAX_CONTEXT_MESSAGES сообщений этого пользователя (по всем чатам —
    личка и группы), всё что старше — физически удаляется. Реальный chat_id
    сохраняется в строках для архива и логов, но на размер окна не влияет.

    Накопленный расход токенов и число запросов учитываются на user_id.
    Вызывается только после успешного ответа модели.

    :param prompt_tokens: токены входа последнего запроса (для логов; на окно не влияет)
    :param total_tokens:  вход+выход за этот запрос — прибавляется к накопленной сумме
    """
    if model_name is None:
        try:
            from config import GEMINI_MODEL
            model_name = get_setting("active_model", GEMINI_MODEL)
        except Exception:
            model_name = "unknown"

    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, model_name) VALUES (?,?,?,?,?)",
            (chat_id, user_id, "user", user_text, None),
        )
        conn.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, model_name) VALUES (?,?,?,?,?)",
            (chat_id, user_id, "assistant", answer, model_name),
        )

        # Скользящее окно: оставляем только последние MAX_CONTEXT_MESSAGES строк
        # этого пользователя (по всем чатам), остальное удаляем.
        conn.execute(
            "DELETE FROM messages WHERE user_id=? AND id NOT IN ("
            "SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_CONTEXT_MESSAGES),
        )

        # Накопительный учёт токенов и запросов на пользователя
        conn.execute(
            """INSERT INTO user_token_usage (user_id, total_tokens, total_requests)
               VALUES (?, ?, 1)
               ON CONFLICT(user_id)
               DO UPDATE SET
                   total_tokens   = total_tokens + excluded.total_tokens,
                   total_requests = total_requests + 1""",
            (user_id, total_tokens),
        )
        conn.commit()


def add_bot_message(chat_id: int, user_id: int, text: str) -> None:
    """
    Кладёт в личную память ОДНО сообщение бота — без вопроса человека рядом
    (2026-08-20, решение Максима). Нужно рассылке новостей: бот пишет человеку
    сам, апдейтом это не приходит, и без записи он через минуту не помнил бы,
    что именно отправил.

    Окно то же и подрезается так же, как в add_messages: последние
    MAX_CONTEXT_MESSAGES сообщений этого пользователя по всем чатам. Значит
    новость живёт как любое другое сообщение и уезжает из памяти сама — ради
    этого всё и затевалось (прежняя «вечная» память о последней новости
    удалена в тот же день).

    ⚠️ Два сообщения бота подряд в истории — нормальная пара: проверено живым
    запросом ко всем четырём провайдерам (Gemini, DeepSeek, Qwen, Xiaomi),
    все ответили. Служебной строки «от лица человека» рядом не нужно.

    Учёт токенов НЕ трогает: запроса к модели тут не было.
    """
    if not text:
        return
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, model_name) VALUES (?,?,?,?,?)",
            (chat_id, user_id, "assistant", text, None),
        )
        conn.execute(
            "DELETE FROM messages WHERE user_id=? AND id NOT IN ("
            "SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_CONTEXT_MESSAGES),
        )
        conn.commit()


def clear_history(user_id: int):
    """Полностью удаляет объединённое контекстное окно пользователя (команда /clear).

    Удаляет все сообщения этого user_id из всех чатов (личка + группы).
    Накопленную статистику токенов НЕ обнуляет.
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            "DELETE FROM messages WHERE user_id=?",
            (user_id,),
        )
        conn.commit()


def register_bot_message(chat_id: int, message_id: int):
    """Регистрирует отправленное ботом сообщение в БД."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO bot_sent_messages (chat_id, message_id) VALUES (?, ?)",
            (chat_id, message_id),
        )
        conn.commit()


def get_old_bot_messages(chat_id: int, keep_count: int = 3) -> list[int]:
    """Возвращает список ID сообщений бота, которые нужно удалить."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT message_id FROM bot_sent_messages "
            "WHERE chat_id=? ORDER BY id DESC",
            (chat_id,),
        ).fetchall()
    
    msg_ids = [r["message_id"] for r in rows]
    if len(msg_ids) <= keep_count:
        return []
    return msg_ids[keep_count:]


def remove_bot_message(chat_id: int, message_id: int):
    """Удаляет запись об отправленном сообщении из БД."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "DELETE FROM bot_sent_messages WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )
        conn.commit()
