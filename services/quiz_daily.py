# ─────────────────────────────────────────────
#  services/quiz_daily.py — 🕛 вопрос дня: одна викторина в сутки в 12:00
#  (2026-08-20, просьба Максима)
#
#  Что делает: раз в сутки в полдень по Киеву бот сам отправляет в группы один
#  вопрос из банка, а вчерашний опрос удаляет. Здесь — только расписание,
#  тумблер и память о разосланных опросах; сама отправка живёт в
#  jobs/reports.py, показ вопроса — в handlers/quiz.py.
#
#  ⚠️ ЗАЩИТЫ ОТ ПОВТОРОВ ЗДЕСЬ НЕТ И НЕ НУЖНО: вопрос выбирает
#  database.history.get_random_quiz_question — «из тех, что задавали реже
#  всего», а после отправки счётчик поднимается. При 219 одобренных вопросах
#  и одном вопросе в сутки первый повтор случится через семь месяцев.
#  Не заводить здесь второй механизм отбора: разъедутся.
#
#  ⚠️ ПОЧЕМУ ОПРОСЫ ХРАНЯТСЯ В БАЗЕ. Бот держит «живые» опросы в памяти
#  процесса (handlers/quiz.ACTIVE_QUIZZES), и после перезапуска ответы на
#  забытый опрос молча не засчитываются. Вопрос по кнопке живёт минуту, и это
#  не беда; вопрос ДНЯ висит сутки, а бот за сутки перезапускается сколько
#  угодно раз (каждое обновление кода). Поэтому запись о нём кладётся в
#  settings и поднимается обратно при старте — иначе счётчик ответов и звания
#  теряли бы половину игроков в дни правок.
#
#  ⚠️ ЗАПИСЬ ХРАНИТ И НОМЕР СООБЩЕНИЯ — им же удаляется вчерашний опрос
#  (просьба Максима: «чтобы старый вопрос удалялся, когда приходит новый»).
#  То есть одна запись работает на два дела сразу.
# ─────────────────────────────────────────────

import json
import logging

from database import history as hist

logger = logging.getLogger(__name__)

_ICON = "🎮"

# Ключи settings. Метка дня — «2026-08-20»: по ней видно, слали ли сегодня.
ENABLED_KEY = "quiz_auto_enabled"
DAY_KEY = "quiz_auto_day"
ACTIVE_KEY = "quiz_auto_active"

# По умолчанию ВЫКЛЮЧЕНО — как «Сам в разговор» и понедельничный дайджест:
# новый механизм, который сам пишет в чат, не должен включаться молча.
ENABLED_DEFAULT = "0"


def is_enabled() -> bool:
    """Тумблер «🕛 Вопрос дня» (панель викторины)."""
    return hist.get_setting(ENABLED_KEY, ENABLED_DEFAULT) == "1"


def set_enabled(on: bool) -> None:
    hist.set_setting(ENABLED_KEY, "1" if on else "0")


def _kyiv_now():
    """Сейчас по Киеву — тем же расчётом, что у отчётов и дайджеста.

    ⚠️ Второго расчёта киевского времени в проекте заводить нельзя: на
    переводе часов они разъедутся, и вопрос уедет на час от расписания.
    """
    from services.daily_report import kyiv_now
    return kyiv_now()


def day_key(moment=None) -> str:
    """Метка суток «2026-08-20» — по ней видно, отправляли ли сегодня."""
    return (moment or _kyiv_now()).strftime("%Y-%m-%d")


def due_now() -> bool:
    """
    Пора ли слать вопрос дня: включён тумблер, по Киеву уже наступил
    QUIZ_AUTO_HOUR и за сегодня ещё не слали.

    ⚠️ Проспанный полдень ДОСЫЛАЕТСЯ в тот же день (в отличие от
    понедельничного дайджеста, который во вторник уже не нужен): вопрос,
    пришедший в 15:00 вместо 12:00, остаётся вопросом дня. А вот назавтра он
    не досылается — метка суток к тому времени сменится, и уйдёт свежий.
    """
    if not is_enabled():
        return False
    from config import QUIZ_AUTO_HOUR
    now = _kyiv_now()
    if now.hour < QUIZ_AUTO_HOUR:
        return False
    return hist.get_setting(DAY_KEY, "") != day_key(now)


def note_sent() -> None:
    """Помечает сутки отправленными. Ставится ТОЛЬКО после удачной отправки —
    иначе сорвавшаяся рассылка молча съела бы вопрос дня."""
    hist.set_setting(DAY_KEY, day_key())


# ─── память о разосланных опросах ───────────────────────────────────

def active() -> dict:
    """
    Что сейчас висит в чатах: {chat_id (строкой): запись}. Запись — словарь
    с полями message_id, poll_id, correct_idx, question, options, explanation.
    Испорченное значение (руками правили settings) — считаем, что пусто:
    вопрос дня не тот механизм, ради которого стоит ронять бота.
    """
    try:
        data = json.loads(hist.get_setting(ACTIVE_KEY, "") or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def remember(chat_id: int, record: dict) -> None:
    """Запоминает опрос, отправленный в этот чат (вытесняя вчерашний)."""
    data = active()
    data[str(chat_id)] = record
    try:
        hist.set_setting(ACTIVE_KEY, json.dumps(data, ensure_ascii=False))
    except Exception as e:
        logger.warning("⚠️ %s Не удалось запомнить вопрос дня для чата %s: %s",
                       _ICON, chat_id, e)


def forget(chat_id: int) -> None:
    """Убирает запись о чате (опрос удалён или чат недоступен)."""
    data = active()
    if data.pop(str(chat_id), None) is None:
        return
    try:
        hist.set_setting(ACTIVE_KEY, json.dumps(data, ensure_ascii=False))
    except Exception as e:
        logger.warning("⚠️ %s Не удалось забыть вопрос дня чата %s: %s", _ICON, chat_id, e)


def restore() -> int:
    """
    Поднимает записи обратно в память бота при старте (main.py) — чтобы ответы
    на вчерашний опрос считались и после перезапуска. Возвращает число
    восстановленных опросов. Тихая: сбой не должен мешать запуску.
    """
    try:
        from handlers.quiz import ACTIVE_QUIZZES
        restored = 0
        for chat_id, rec in active().items():
            poll_id = rec.get("poll_id")
            if not poll_id:
                continue
            ACTIVE_QUIZZES[poll_id] = {
                "correct_idx": rec.get("correct_idx", 0),
                "chat_id": int(chat_id),
                "triggered_next": False,
                "question": rec.get("question", ""),
                "options": rec.get("options", []),
                "explanation": rec.get("explanation", ""),
                # ⚠️ Метка обязательна и здесь: без неё после перезапуска бота
                # ответ на восстановленный вопрос дня запустил бы цепочку
                # «следующий вопрос» — правило смотрит именно на неё.
                "auto": True,
            }
            restored += 1
        if restored:
            logger.info("%s Вопрос дня восстановлен после перезапуска: опросов %d",
                        _ICON, restored)
        return restored
    except Exception as e:
        logger.warning("⚠️ %s Не удалось восстановить вопрос дня: %s", _ICON, e)
        return 0


def next_run_label() -> str:
    """«сегодня в 12:00» / «завтра в 12:00» — для строки состояния в панели."""
    from config import QUIZ_AUTO_HOUR
    now = _kyiv_now()
    sent_today = hist.get_setting(DAY_KEY, "") == day_key(now)
    today_left = now.hour < QUIZ_AUTO_HOUR and not sent_today
    return f"{'сегодня' if today_left else 'завтра'} в {QUIZ_AUTO_HOUR:02d}:00"
