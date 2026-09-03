# ───────────────────────────────────────────────
#  database/quiz.py — викторина: банк вопросов и счёт игроков (02.09.2026).
#
#  Последний, одиннадцатый шаг разреза history.py. После него сам history.py
#  перестал быть файлом с кодом и стал ОГЛАВЛЕНИЕМ пакета.
#
#  Три таблицы:
#    quiz_bank   — сам банк: вопрос, варианты, верный ответ, разбор, статья,
#                  из которой он собран, и статус «в игре / черновик»
#    quiz_failed — статьи, на которых сборка вопросов сорвалась (чтобы не
#                  долбиться в них снова и показывать список в панели)
#    quiz_stats  — счёт игроков: сколько верных ответов, сколько попыток;
#                  из него же считается ЗВАНИЕ (config.QUIZ_RANKS)
#
#  ⚠️ ВАРИАНТЫ ОТВЕТА ХРАНЯТСЯ СТРОКОЙ JSON — sqlite списков не умеет. Разбор
#  и сборка спрятаны здесь (`_row_to_question`, `add_quiz_question`), наружу
#  всегда уходит готовый список. Разложить `json.loads` по вызывающим — верный
#  способ однажды забыть его в новом месте и получить строку вместо вариантов.
#
#  ⚠️ ЦИФРЫ СЧЁТА НАКОПИТЕЛЬНЫЕ — времени в quiz_stats нет вовсе. «Сколько
#  ответили за неделю» считается разницей с недельным снимком, который хранит
#  сам дайджест (services/group_digest.py).
#
#  ⚠️ ЗВАНИЯ СЧИТАЮТСЯ ЗДЕСЬ, а лестница задана в config.QUIZ_RANKS —
#  единственном её источнике. Проверка `check_quiz_ranks` в selftest требует
#  лестницу без дыр (107 проверок), а `check_seed_diff` сверяет банк с файлом
#  вопросов. Тема покрыта хорошо: 18 прямых вызовов из selftest.
#
#  Кто это читает:
#    handlers/quiz.py        — сама игра, звания, /rank
#    services/quiz_bank.py   — сборка вопросов моделью по статьям базы знаний
#    handlers/admin/panel_quiz.py, web/* — панель викторины и страница сайта
#    services/group_digest.py — счёт за неделю для дайджеста
# ───────────────────────────────────────────────

import json
import time
import logging

from config import QUIZ_RANKS

from ._core import _lock, _get_connection

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


def add_quiz_attempt(user_id: int, username: str, is_correct: bool):
    """Обновляет статистику ответов пользователя в викторине."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO quiz_stats (user_id, username, correct_answers, total_attempts)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(user_id)
               DO UPDATE SET 
                   username = excluded.username,
                   correct_answers = correct_answers + ?,
                   total_attempts = total_attempts + 1""",
            (user_id, username, 1 if is_correct else 0, 1 if is_correct else 0),
        )
        conn.commit()


def get_user_stats(user_id: int) -> dict:
    """Возвращает статистику пользователя и детали его текущего звания."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            "SELECT username, correct_answers, total_attempts FROM quiz_stats WHERE user_id=?",
            (user_id,),
        ).fetchone()
    
    if not row:
        # Новичок без единой попытки: первое звание из общего списка
        # (config.QUIZ_RANKS) — не дублируем его текст здесь, иначе при правке
        # списка новичок и все остальные получат РАЗНЫЕ звания.
        first = QUIZ_RANKS[0]
        return {
            "username": "Боец",
            "correct_answers": 0,
            "total_attempts": 0,
            "success_rate": 0.0,
            "rank": first["name"],
            "rank_icon": first["icon"],
            "rank_desc": first["desc"],
            "next_rank_needed": QUIZ_RANKS[1]["min"] if len(QUIZ_RANKS) > 1 else -1,
            "rank_min": first["min"],
        }
    
    correct = row["correct_answers"]
    attempts = row["total_attempts"]
    rate = (correct / attempts * 100.0) if attempts > 0 else 0.0

    # Расчет звания. Список званий — ЕДИНЫЙ источник в config.QUIZ_RANKS
    # (его же читают кнопки выбора почётного звания в карточке /users);
    # раньше он был зашит прямо здесь.
    ranks = QUIZ_RANKS

    current_rank = ranks[0]
    next_rank_needed = ranks[1]["min"] if len(ranks) > 1 else -1
    for idx, r in enumerate(ranks):
        if r["min"] <= correct <= r["max"]:
            current_rank = r
            if idx + 1 < len(ranks):
                next_rank_needed = ranks[idx + 1]["min"]
            else:
                next_rank_needed = -1 # Максимальное звание
            break
            
    return {
        "username": row["username"] or "Боец",
        "correct_answers": correct,
        "total_attempts": attempts,
        "success_rate": round(rate, 1),
        "rank": current_rank["name"],
        "rank_icon": current_rank["icon"],
        "rank_desc": current_rank["desc"],
        "next_rank_needed": next_rank_needed,
        "rank_min": current_rank["min"]
    }


# ───────────────────────────────────────────────
#  Банк вопросов викторины (таблица quiz_bank)
# ───────────────────────────────────────────────
#
# Вопросы собирает по статьям базы знаний services/quiz_bank.py, показывает и
# одобряет панель /quizadm, а берёт в игру handlers/quiz.py. Сама эта половина
# знает только про строки таблицы: ни про модель, ни про Telegram.
#
# ⚠️ options хранится СТРОКОЙ JSON — sqlite списков не умеет. Разбор и сборка
# спрятаны здесь (_row_to_question / add_quiz_question), наружу всегда уходит
# готовый список: разложить json.loads по вызывающим — верный способ однажды
# забыть его в новом месте и получить строку вместо вариантов ответа.

def _row_to_question(row) -> dict | None:
    """Строка таблицы → словарь вопроса. Битый JSON вариантов = вопроса нет."""
    if row is None:
        return None
    try:
        options = json.loads(row["options"])
    except (TypeError, ValueError):
        logger.warning("⚠️ Вопрос %s: не разобрать варианты ответа", row["id"])
        return None
    if not isinstance(options, list) or len(options) < 2:
        return None
    return {
        "id": row["id"],
        "article": row["article"],
        "question": row["question"],
        "options": options,
        "correct_idx": row["correct_idx"],
        "explanation": row["explanation"] or "",
        "approved": bool(row["approved"]),
        "asked_count": row["asked_count"],
    }


def add_quiz_question(article: str, question: str, options: list,
                      correct_idx: int, explanation: str) -> int | None:
    """
    Кладёт собранный вопрос в банк ЧЕРНОВИКОМ (approved=0).

    Возвращает id новой записи или None, если такой вопрос уже есть: сборку
    можно жать сколько угодно раз, повторы в базу не полезут (сверяем по паре
    «статья + текст вопроса» — модель на одной статье часто повторяется).
    """
    with _lock:
        conn = _get_connection()
        exists = conn.execute(
            "SELECT 1 FROM quiz_bank WHERE article=? AND question=?",
            (article, question),
        ).fetchone()
        if exists:
            return None
        cur = conn.execute(
            """INSERT INTO quiz_bank
                   (article, question, options, correct_idx, explanation, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (article, question, json.dumps(options, ensure_ascii=False),
             int(correct_idx), explanation, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def get_random_quiz_question() -> dict | None:
    """
    Случайный ОДОБРЕННЫЙ вопрос для викторины — из тех, что задавали реже
    всего. None означает «банк пуст»: вопросов нет вовсе или ни один ещё не
    одобрен. Решение, что сказать людям, принимает вызывающий код.
    """
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            """SELECT * FROM quiz_bank WHERE approved=1
               ORDER BY asked_count ASC, RANDOM() LIMIT 1"""
        ).fetchone()
    return _row_to_question(row)


def note_quiz_question_asked(qid: int) -> None:
    """Отмечает, что вопрос ушёл в чат: он опустится в конец очереди выбора."""
    with _lock:
        conn = _get_connection()
        conn.execute("UPDATE quiz_bank SET asked_count = asked_count + 1 WHERE id=?", (qid,))
        conn.commit()


def get_quiz_bank_counts() -> dict:
    """Сколько вопросов в игре, сколько черновиков и сколько статей покрыто."""
    with _lock:
        conn = _get_connection()
        row = conn.execute(
            """SELECT SUM(approved=1) AS approved,
                      SUM(approved=0) AS drafts,
                      COUNT(DISTINCT article) AS articles
               FROM quiz_bank"""
        ).fetchone()
    return {
        "approved": row["approved"] or 0,
        "drafts": row["drafts"] or 0,
        "articles": row["articles"] or 0,
    }


def get_quiz_articles_covered() -> set:
    """
    Имена файлов статей, по которым вопросы уже собирали (в любом виде —
    и одобренные, и черновики, и отклонённые остатки). Нужно кнопке сборки,
    чтобы догонять ТОЛЬКО новые статьи, а не платить за базу заново.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute("SELECT DISTINCT article FROM quiz_bank").fetchall()
    return {r["article"] for r in rows}


def list_quiz_questions(approved: bool, limit: int = 50, offset: int = 0) -> list[dict]:
    """Вопросы банка для листания в панели: черновики (0) или игровые (1)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            """SELECT * FROM quiz_bank WHERE approved=?
               ORDER BY id ASC LIMIT ? OFFSET ?""",
            (1 if approved else 0, limit, offset),
        ).fetchall()
    return [q for q in (_row_to_question(r) for r in rows) if q]


def list_all_quiz_questions() -> list[dict]:
    """
    ВЕСЬ банк целиком — и черновики, и игровые (2026-09-01).

    Нужен сверке эталонного файла с банком (services/quiz_bank.py::seed_diff):
    она смотрит не «сколько их», а «совпадает ли содержимое каждого», и
    листать банк страницами для этого бессмысленно.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute("SELECT * FROM quiz_bank ORDER BY id ASC").fetchall()
    return [q for q in (_row_to_question(r) for r in rows) if q]


def get_quiz_question(qid: int) -> dict | None:
    """Один вопрос по номеру (карточка в панели)."""
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT * FROM quiz_bank WHERE id=?", (qid,)).fetchone()
    return _row_to_question(row)


def update_quiz_question_body(qid: int, options: list, correct_idx: int,
                              explanation: str) -> None:
    """
    Переписывает у вопроса ВАРИАНТЫ, ВЕРНЫЙ ОТВЕТ и РАЗБОР (2026-09-01).

    ⚠️ ЕДИНСТВЕННЫЙ способ поправить содержимое вопроса в банке, и заведён он
    ровно под одну задачу: догнать базу до эталонного файла, когда в файле
    поправили разбор или варианты (services/quiz_bank.py::seed_apply). Руками
    вопросы нигде не редактируются — ни в боте, ни на сайте, — поэтому автор
    у этих текстов один, файл, и перезаписывать нечью работу здесь нечем.

    ⚠️ НЕ ТРОГАЕТ `article`, `question`, `approved` и `asked_count`. Первые
    два — ключ, по которому вопрос и нашли; `approved` — решение Максима
    («в игре» после правки разбора остаётся в игре); `asked_count` — очередь
    выбора, обнулить её значит задать один и тот же вопрос дважды подряд.
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            """UPDATE quiz_bank SET options=?, correct_idx=?, explanation=?
               WHERE id=?""",
            (json.dumps(options, ensure_ascii=False), int(correct_idx),
             explanation, qid),
        )
        conn.commit()


def set_quiz_question_approved(qid: int, approved: bool) -> None:
    """Отправляет вопрос в игру (True) или возвращает в черновики (False)."""
    with _lock:
        conn = _get_connection()
        conn.execute("UPDATE quiz_bank SET approved=? WHERE id=?",
                     (1 if approved else 0, qid))
        conn.commit()


def delete_quiz_question(qid: int) -> None:
    """Убирает вопрос из банка совсем."""
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM quiz_bank WHERE id=?", (qid,))
        conn.commit()


def note_quiz_failure(article: str, reason: str) -> None:
    """
    Отмечает, что по статье собрать вопросы не вышло. Повторная неудача не
    заводит вторую строку, а растит счётчик попыток и обновляет причину:
    список неудач — это очередь на повтор, а не журнал (для журнала есть лог).
    """
    with _lock:
        conn = _get_connection()
        conn.execute(
            """INSERT INTO quiz_failed (article, ts, reason, attempts)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(article)
               DO UPDATE SET ts = excluded.ts,
                             reason = excluded.reason,
                             attempts = attempts + 1""",
            (article, time.time(), reason),
        )
        conn.commit()


def clear_quiz_failure(article: str) -> None:
    """Снимает статью с учёта неудач (по ней наконец собрались вопросы)."""
    with _lock:
        conn = _get_connection()
        conn.execute("DELETE FROM quiz_failed WHERE article=?", (article,))
        conn.commit()


def list_quiz_failures(limit: int = 50) -> list[dict]:
    """Статьи, по которым сборка не далась: свежие сверху (для экрана панели)."""
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT * FROM quiz_failed ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"article": r["article"], "ts": r["ts"], "reason": r["reason"] or "",
             "attempts": r["attempts"]} for r in rows]


def count_quiz_failures() -> int:
    """Сколько статей ждёт повторной попытки (число на кнопке панели)."""
    with _lock:
        conn = _get_connection()
        row = conn.execute("SELECT COUNT(*) AS n FROM quiz_failed").fetchone()
    return row["n"] or 0


def clear_quiz_failures() -> int:
    """Забыть весь список неудач разом (кнопка «🗑 Забыть список»)."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM quiz_failed")
        conn.commit()
        return cur.rowcount or 0


def delete_quiz_drafts() -> int:
    """Стирает ВСЕ черновики разом (кнопка «очистить черновики»). Игровые не трогает."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM quiz_bank WHERE approved=0")
        conn.commit()
        return cur.rowcount or 0


def reset_all_quiz_stats() -> int:
    """
    Обнуляет статистику викторины У ВСЕХ: сколько кто ответил верно и сколько
    было попыток (кнопка «🧹 Обнулить статистику игроков», 2026-08-05, решение
    Максима после пересборки лестницы званий). Возвращает, у скольких человек
    стёрты счётчики.

    ⚠️ ВМЕСТЕ СО СЧЁТЧИКАМИ СБРАСЫВАЕТСЯ НЕДЕЛЬНЫЙ СНИМОК ДАЙДЖЕСТА
    (settings `group_digest_quiz`). Он хранит «сколько было верных ответов у
    каждого на прошлой неделе», а неделя считается разницей с ним. Обнулим
    счётчики, оставив снимок, — разница станет отрицательной, и дайджест
    будет писать «за неделю 0 ответов» до тех пор, пока люди не переиграют
    старые цифры. Тихая поломка на несколько недель, поэтому снимок здесь же.

    ⚠️ ПОЧЁТНЫЕ ЗВАНИЯ НЕ ТРОГАЕМ: они живут в user_settings и присвоены
    человеком вручную — их обнуление никто не просил.
    """
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM quiz_stats")
        conn.execute("DELETE FROM settings WHERE key='group_digest_quiz'")
        conn.commit()
        return cur.rowcount or 0


def delete_all_quiz_questions() -> int:
    """
    Стирает ВЕСЬ банк вопросов — и черновики, и те, что уже в игре
    (кнопка «🗑 Стереть ВСЕ вопросы», 2026-08-05, решение Максима).

    ⚠️ Заодно чистится очередь неудачных статей: после полной очистки банка
    пуст и он, и разговор о том, по каким статьям «не вышло», начинается
    заново — иначе в панели висело бы «⚠️ Не разобрались: N» про вопросы,
    которых больше нет.
    """
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM quiz_bank")
        conn.execute("DELETE FROM quiz_failed")
        conn.commit()
        return cur.rowcount or 0


def get_all_quiz_stats() -> dict:
    """
    {user_id: (имя, верных ответов)} по всей таблице викторины.

    ⚠️ Цифры НАКОПИТЕЛЬНЫЕ — в quiz_stats нет времени вовсе. «Сколько
    ответили за неделю» считается разницей с недельным снимком, который
    хранит сам дайджест (см. services/group_digest.py). Заводить время в
    quiz_stats ради этого не стали: снимок дешевле и уже есть такой же приём
    у недельного отчёта о расходах.
    """
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT user_id, username, correct_answers FROM quiz_stats"
        ).fetchall()
    return {r["user_id"]: (r["username"] or "", r["correct_answers"] or 0) for r in rows}
