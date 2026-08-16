# ─────────────────────────────────────────────
#  services/group_digest.py — 📊 недельный дайджест жизни группы (2026-08-04)
#
#  Цифры по расходам владелец получает каждую ночь, а по самой ГРУППЕ не было
#  никаких — хотя данные копятся давно: архив сообщений, журнал модерации,
#  викторина, новости, журнал проактивных проверок.
#
#  ⚠️ РЕШЕНИЕ МАКСИМА (2026-08-04): дайджест уходит ВЛАДЕЛЬЦУ В ЛИЧКУ, а не
#  в группу. В группу его отправляет человек — кнопкой «📤 Отправить в группу»
#  под текстом. Автоматической отправки в чат НЕТ и заводить её без отдельной
#  просьбы нельзя: сначала он хочет посмотреть на живых данных, что получается.
#
#  Контракт:
#  • Модуль считает и рисует. Отправляет — jobs/reports.py (по понедельникам)
#    и панель статистики (кнопка «показать сейчас»).
#  • Никакой сети и никаких моделей: только выборки из базы.
#  • Тихим НЕ является: сорвавшийся подсчёт лучше увидеть в логе, чем
#    получить дайджест с молча пропавшим разделом.
# ─────────────────────────────────────────────

import html
import json
import logging
from collections import Counter
from datetime import timedelta

from database import history as hist

logger = logging.getLogger(__name__)

_ICON = "📊"

# Ключи settings: неделя последней отправки и снимок викторины для разницы.
WEEK_MARK_KEY = "group_digest_week"
QUIZ_SNAPSHOT_KEY = "group_digest_quiz"
ENABLED_KEY = "group_digest_enabled"
ENABLED_DEFAULT = "1"

# Во сколько по Киеву уходит понедельничный дайджест. НЕ в полночь (когда
# считаются расходы): дайджест читают люди, а в 00:00 его никто не увидит.
SEND_HOUR = 10

_DOW_RU = ("понедельник", "вторник", "среда", "четверг",
           "пятница", "суббота", "воскресенье")
_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")

# Сколько людей показываем в топе активности.
_TOP_LIMIT = 5

# Черта между разделами дайджеста. Одна на весь текст: разъедутся по длине —
# сообщение выглядит кривым, а заметно это только глазами.
_SEP = "───────────────────────────"

# ⚠️ Считается ТОЛЬКО то, что бот записал: архив групп чистится через 10 дней
# (jobs/cleanup.py), так что неделя влезает в него с запасом в три дня, но
# время, когда бот не работал, в цифрах просто отсутствует — «мало сообщений»
# после долгого простоя означает не тишину в чате, а молчание бота.


# ─── тумблер и метка недели ─────────────────────────────────────────

def is_enabled() -> bool:
    """Тумблер понедельничной отправки (settings: group_digest_enabled)."""
    return hist.get_setting(ENABLED_KEY, ENABLED_DEFAULT) == "1"


def _kyiv_now():
    """Сейчас по Киеву — тем же расчётом, что у отчётов о расходах.

    ⚠️ Второго расчёта киевского времени в проекте заводить нельзя: на
    переводе часов они разъедутся, и дайджест уедет на час от отчётов.
    """
    from services.daily_report import kyiv_now
    return kyiv_now()


def week_key(moment=None) -> str:
    """Метка недели «2026-W32» — по ней видно, слали ли дайджест за эту неделю."""
    moment = moment or _kyiv_now()
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def due_now() -> bool:
    """
    Пора ли слать понедельничный дайджест: включён тумблер, по Киеву
    понедельник, час не раньше SEND_HOUR, и за эту неделю ещё не слали.

    Проспанный понедельник дайджест НЕ досылает — в отличие от отчёта о
    расходах: тот про деньги, и пропуск суток в нём означал бы дыру в
    истории, а дайджест — чтение к утру понедельника, и во вторник он уже
    не то же самое. Захочешь досылать — убери проверку дня недели.
    """
    if not is_enabled():
        return False
    now = _kyiv_now()
    if now.weekday() != 0 or now.hour < SEND_HOUR:
        return False
    return hist.get_setting(WEEK_MARK_KEY, "") != week_key(now)


def note_sent() -> None:
    """Помечает неделю отправленной (ставится ТОЛЬКО после удачной доставки)."""
    hist.set_setting(WEEK_MARK_KEY, week_key())


# ─── границы периода ────────────────────────────────────────────────

def _utc_str(moment) -> str:
    """Момент → строка UTC «ГГГГ-ММ-ДД ЧЧ:ММ:СС» (формат created_at в базе)."""
    from datetime import timezone
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _period_label(start, end) -> str:
    """«28 июля — 3 августа 2026» человеческим языком."""
    if start.month == end.month:
        return f"{start.day}–{end.day} {_MONTHS_RU[end.month - 1]} {end.year}"
    return (f"{start.day} {_MONTHS_RU[start.month - 1]} — "
            f"{end.day} {_MONTHS_RU[end.month - 1]} {end.year}")


# ─── викторина: разница со снимком ──────────────────────────────────

def _quiz_week(save: bool) -> tuple[int, str, int]:
    """
    (верных ответов за неделю, лучший игрок, его верных ответов).

    ⚠️ В quiz_stats НЕТ времени — только накопительные счётчики. Поэтому
    неделя считается разницей с прошлым снимком, а снимок обновляется ТОЛЬКО
    при настоящей понедельничной отправке (save=True): кнопка «показать
    сейчас» не должна сдвигать точку отсчёта, иначе нажавший её человек
    обнулил бы себе следующий дайджест.
    Первый запуск (снимка нет) — за неделю считаем всё, что накопилось: это
    честнее нуля и случается ровно один раз.
    """
    now = hist.get_all_quiz_stats()
    try:
        old = json.loads(hist.get_setting(QUIZ_SNAPSHOT_KEY, "") or "{}")
    except (TypeError, ValueError):
        old = {}

    total, best_name, best_n = 0, "", 0
    for user_id, (name, correct) in now.items():
        gained = correct - int(old.get(str(user_id), 0))
        if gained <= 0:
            continue
        total += gained
        if gained > best_n:
            best_name, best_n = name or str(user_id), gained

    if save:
        hist.set_setting(QUIZ_SNAPSHOT_KEY,
                         json.dumps({str(k): v[1] for k, v in now.items()}))
    return total, best_name, best_n


# ─── сбор цифр ──────────────────────────────────────────────────────

def collect(chat_id: int, days: int = 7, save_quiz: bool = False,
            bot_id: int = 0) -> dict:
    """
    Все цифры дайджеста одним словарём. Сеть не трогает.

    `bot_id` — номер самого бота: его строки не идут ни в «Писали», ни в
    «Самые активные». Не передали (0) — бот попадёт в оба места.
    """
    end = _kyiv_now()
    start = end - timedelta(days=days)
    # ⚠️ Верхнюю границу НЕ передаём: период всегда кончается «сейчас», а все
    # выборки сравнивают строки времени с точностью до секунды — записи ТОЙ ЖЕ
    # секунды, в которую собирается дайджест, при `created_at < end` тихо
    # выпадали бы из счёта (поймали на подсчёте новостей).
    rows = hist.get_group_messages_between(chat_id, _utc_str(start))

    authors = Counter()
    names: dict[int, str] = {}
    by_dow, by_hour = Counter(), Counter()
    photos = voices = videos = 0
    from datetime import datetime, timezone
    tz = end.tzinfo

    for r in rows:
        uid = r["user_id"]
        authors[uid] += 1
        name = r.get("first_name") or (f"@{r['username']}" if r.get("username") else "")
        if name:
            names[uid] = name
        photos += 1 if r.get("has_photo") else 0
        voices += 1 if r.get("has_voice") else 0
        videos += 1 if r.get("has_video") else 0
        try:
            moment = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
            moment = moment.replace(tzinfo=timezone.utc).astimezone(tz)
            by_dow[moment.weekday()] += 1
            by_hour[moment.hour] += 1
        except (TypeError, ValueError):
            continue

    # ⚠️ Бот вычёркивается ЗДЕСЬ, до отбора пятёрки (решение Максима,
    # 2026-08-16): свои реплики он кладёт в тот же архив, что и люди
    # (services/proactive.py, jobs/news.py, handlers/messages.py — так модель
    # видит в стенограмме собственные ответы), и по числу сообщений уверенно
    # занимал первое место. Вычеркнуть ПОСЛЕ most_common нельзя — в списке
    # осталось бы четыре человека вместо пяти.
    # Строка «💬 Сообщений» считается по всем записям и бота включает: это
    # оборот чата, а не соревнование.
    authors.pop(bot_id, None)

    quiz_total, quiz_best, quiz_best_n = _quiz_week(save_quiz)

    out = {
        "start": start, "end": end, "days": days,
        "messages": len(rows),
        "people": len(authors),
        "photos": photos, "voices": voices, "videos": videos,
        "top": [(names.get(uid, str(uid)), n) for uid, n in authors.most_common(_TOP_LIMIT)],
        "dow": by_dow.most_common(1)[0] if by_dow else None,
        "hour": by_hour.most_common(1)[0] if by_hour else None,
        "quiz_total": quiz_total, "quiz_best": quiz_best, "quiz_best_n": quiz_best_n,
        "news": hist.count_sent_news_between(_utc_str(start)),
    }

    # Модерация и проактивное участие — счётчики бота, не чата: в журналах
    # есть chat_id, но людям в дайджесте важнее общая картина. Подписи ниже
    # об этом честно говорят.
    try:
        out["mod"] = hist.get_moderation_counts(days)
    except Exception as e:
        logger.warning("⚠️ %s Не удалось прочитать журнал модерации: %s", _ICON, e)
        out["mod"] = {}
    try:
        out["joins"] = hist.get_join_counts(days)
    except Exception as e:
        logger.warning("⚠️ %s Не удалось прочитать журнал вступлений: %s", _ICON, e)
        out["joins"] = {}
    try:
        out["proactive"] = hist.proactive_stats(_utc_str(start))
    except Exception as e:
        logger.warning("⚠️ %s Не удалось прочитать журнал проактивных проверок: %s", _ICON, e)
        out["proactive"] = {}
    return out


# ─── отрисовка ──────────────────────────────────────────────────────

def render(data: dict, chat_title: str = "") -> str:
    """Готовый HTML-текст дайджеста. Тот же и для лички, и для группы."""
    title = f" · {html.escape(chat_title)}" if chat_title else ""
    lines = [
        f"{_ICON} <b>ИТОГИ НЕДЕЛИ</b>{title}",
        f"<i>{_period_label(data['start'], data['end'])}</i>",
        _SEP,
    ]

    if not data["messages"]:
        lines.append("😴 За неделю в группе не написали ни одного сообщения.")
        return "\n".join(lines)

    lines.append(f"💬 Сообщений: <b>{data['messages']}</b>")
    lines.append(f"👥 Писали: <b>{data['people']}</b> чел.")
    if data["dow"]:
        lines.append(f"🔥 Самый живой день: <b>{_DOW_RU[data['dow'][0]]}</b> ({data['dow'][1]})")
    if data["hour"]:
        hour = data["hour"][0]
        lines.append(f"🕐 Час пик: <b>{hour:02d}:00–{(hour + 1) % 24:02d}:00</b> ({data['hour'][1]})")
    media = []
    if data["photos"]:
        media.append(f"🖼 {data['photos']}")
    if data["voices"]:
        media.append(f"🎤 {data['voices']}")
    if data["videos"]:
        media.append(f"🎬 {data['videos']}")
    if media:
        lines.append("📎 Медиа: " + " · ".join(media))

    # Пьедестал отбит чертой сверху и снизу (решение Максима, 2026-08-16).
    # Дальше блоки отделяются пустой строкой ТОЛЬКО если черты перед ними ещё
    # нет: иначе после пьедестала получилась бы черта плюс пустая строка.
    if data["top"]:
        lines.append(_SEP)
        lines.append("🏆 <b>САМЫЕ АКТИВНЫЕ</b>")
        medals = ("🥇", "🥈", "🥉", "4.", "5.")
        for i, (name, count) in enumerate(data["top"]):
            lines.append(f"{medals[i] if i < len(medals) else f'{i + 1}.'} "
                         f"{html.escape(name[:24])} — <b>{count}</b>")
        lines.append(_SEP)

    if data["quiz_total"]:
        if lines[-1] != _SEP:
            lines.append("")
        lines.append("🎮 <b>ВИКТОРИНА</b>")
        lines.append(f"Верных ответов: <b>{data['quiz_total']}</b>")
        if data["quiz_best"]:
            lines.append(f"Лучший: {html.escape(data['quiz_best'][:24])} — "
                         f"<b>{data['quiz_best_n']}</b>")

    if lines[-1] != _SEP:
        lines.append("")
    if data["news"]:
        lines.append(f"📰 Новостей с wtmobile: <b>{data['news']}</b>")
    proactive = data.get("proactive") or {}
    replies = proactive.get("reply", 0) + proactive.get("reply_mute", 0)
    if replies:
        lines.append(f"🗣 Сам вступал в разговор: <b>{replies}</b> раз")
    joins = data.get("joins") or {}
    if joins.get("joins"):
        lines.append(f"👋 Новичков пришло: <b>{joins['joins']}</b>"
                     + (f" · прошли проверку: <b>{joins.get('ok', 0)}</b>" if joins.get("ok") else ""))
    mod = data.get("mod") or {}
    if mod.get("mutes") or mod.get("linkdels"):
        lines.append(f"🛡 DDoS-Guard: мутов <b>{mod.get('mutes', 0)}</b> · "
                     f"ссылок удалено <b>{mod.get('linkdels', 0)}</b>")

    # Черты подряд не ставим: без викторины и без нижних строк подвал упёрся
    # бы прямо в черту под пьедесталом.
    if lines[-1] != _SEP:
        lines.append(_SEP)
    lines.append("<i>Спросить о технике — /ttx · своё звание — /rank</i>")
    return "\n".join(lines)


def build(chat_id: int, chat_title: str = "", days: int = 7,
          save_quiz: bool = False, bot_id: int = 0) -> str:
    """Собрать и нарисовать дайджест одной строкой (то, что зовут снаружи)."""
    return render(collect(chat_id, days, save_quiz, bot_id), chat_title)
