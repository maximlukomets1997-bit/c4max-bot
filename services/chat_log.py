# ─────────────────────────────────────────────
#  services/chat_log.py — 💬 дословный лог разговора в режиме «Сам в разговор»
#  (2026-08-16, просьба Максима)
#
#  Пишет то, чего в обычном логе нет и не будет: ЦЕЛИКОМ текст, уходящий
#  модели, и ЦЕЛИКОМ её ответ — вместе с мыслями, со словом «ПРОПУСК» и с
#  разбором фото, голосовых и видео.
#
#  ⚠️ ПОЧЕМУ ОТДЕЛЬНАЯ ПАПКА, А НЕ ОБЩИЙ ЛОГ. Такая запись уже существовала
#  строкой «🧪 что уходит модели» (заведена 10.08.2026, убрана 11.08 по
#  решению Максима): она печатала весь запрос в общий лог на КАЖДОЕ сообщение
#  группы — десятки строк на реплику, чужая переписка вперемешку с работой
#  бота, распухший архив. Здесь тот же текст живёт своей жизнью, и общий лог
#  остаётся читаемым. Возвращать это в общий лог нельзя.
#
#  ⚠️ ЗАПИСЬ СЧИТАЕТСЯ ПО ОЧИСТКАМ РАЗГОВОРОВ — не по дням и не по запускам
#  бота (решение Максима 2026-08-16). Кнопка «🧹 Очистить РАЗГОВОРЫ» подводит
#  черту под памятью бота; с той же черты начинается файл. Открыл запись —
#  видишь ровно ту переписку, которую бот помнил. Перезапуск бота запись НЕ
#  обрывает, в отличие от обычного лога: файл продолжается тот же.
#
#  В папке logs/chat не бывает больше двух файлов:
#    «chat ДД.ММ ЧЧ-ММ.log» — текущая запись (в имени — момент её начала);
#    «chat archive.log»     — 7 последних записей подряд, под заголовками.
#
#  ⚠️ ПЕРЕКЛАДЫВАНИЕ В АРХИВ ЗДЕСЬ СВОЁ, хотя logging_setup.py делает почти то
#  же самое. Это осознанно: тот код работает на КАЖДОМ старте бота, и лезть в
#  него ради нового соседа опаснее, чем написать соседу свои тридцать строк.
#  Похоже — да, но у записей свой счёт (очистки, а не запуски) и свой формат
#  заголовка.
#
#  Модуль ТИХИЙ: любая ошибка записи глушится и наружу не выходит. Потерять
#  реплику в группе из-за лога нельзя.
# ─────────────────────────────────────────────

import logging
import os
import threading
from datetime import datetime

from logging_setup import LOG_DIR

logger = logging.getLogger(__name__)

_ICON = "💬"

# Папка внутри logs — так все логи лежат в одном месте, и .gitignore, который
# целиком закрывает logs/, накрывает её без единой правки.
DIR = os.path.join(LOG_DIR, "chat")

ARCHIVE_FILE = "chat archive.log"

# Сколько последних записей хранит архив. Считаются ОЧИСТКИ РАЗГОВОРОВ: восьмая
# срезает самую старую с начала файла.
SESSIONS_TO_KEEP = 7

# Начало строки-заголовка ОДНОЙ проверки. По нему же проверки СЧИТАЮТСЯ для
# экрана логов, поэтому оно обязано быть уникальным: подзаголовки внутри записи
# начинаются с «── », разделитель проверок — сплошная линия из «═», заголовок
# записи в архиве — с «════════ РАЗГОВОР ». Ни один из них с часов не начинается.
_CHECK_MARK = "🕐 "

# Заголовок, которым в архиве одна запись отделяется от другой. По нему же они
# считаются при обрезке — формат менять только здесь, иначе старые куски архива
# перестанут распознаваться и обрезка их не тронет.
_SESSION_MARK_PREFIX = "════════ РАЗГОВОР "
_SESSION_MARK_SUFFIX = " ════════"

# Запись идёт из рабочих потоков (модель зовут через run_in_executor), поэтому
# без замка две проверки могли бы перемешать строки в одном файле.
_lock = threading.Lock()

_TRIGGER_KIND_RU = {"photo": "фото", "voice": "голосовое", "video": "видео"}


# ─── пути ───────────────────────────────────────────────────────────

def archive_path() -> str:
    return os.path.join(DIR, ARCHIVE_FILE)


def current_path() -> str | None:
    """Файл текущей записи или None, если после очистки ещё не писали."""
    try:
        names = [n for n in os.listdir(DIR)
                 if n.endswith(".log") and n != ARCHIVE_FILE]
    except OSError:
        return None
    if not names:
        return None
    # Файл всегда один. Если их почему-то стало больше (сорвалась подводка
    # черты, положили руками) — берём самый свежий, а не первый попавшийся.
    paths = [os.path.join(DIR, n) for n in names]
    try:
        return max(paths, key=os.path.getmtime)
    except OSError:
        return paths[-1]


def started_label(path: str | None = None) -> str:
    """«16.08 в 19:05» — когда начата запись. Момент взят из имени файла."""
    path = path or current_path()
    if not path:
        return ""
    name = os.path.basename(path)[:-len(".log")]
    name = name[len("chat "):] if name.startswith("chat ") else name
    day, _, clock = name.partition(" ")
    return f"{day} в {clock.replace('-', ':')}" if clock else name


# ─── запись ─────────────────────────────────────────────────────────

def _write(text: str) -> None:
    """Дописать кусок в текущую запись, заведя её при необходимости. Тихая."""
    try:
        with _lock:
            os.makedirs(DIR, exist_ok=True)
            path = current_path() or os.path.join(
                DIR, datetime.now().strftime("chat %d.%m %H-%M") + ".log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
    except Exception as e:
        logger.debug("%s Не удалось записать лог разговора: %s", _ICON, e)


def note_check(chat_id: int, trigger_kind: str, user_id: int | None,
               trigger_text: str) -> None:
    """Шапка новой проверки — всё остальное пишется под ней."""
    kind = _TRIGGER_KIND_RU.get(trigger_kind, "текст")
    # Повод в одну строку и коротко: полный текст всё равно уйдёт ниже,
    # в стенограмме запроса.
    snippet = " ".join((trigger_text or "").split())[:120] or "—"
    _write("\n" + "═" * 60 + "\n"
           + f"{_CHECK_MARK}{datetime.now().strftime('%H:%M:%S')} · чат {chat_id} · "
             f"{kind} от {user_id}: {snippet}\n")


def note_media(kind: str, size_bytes: int, text: str) -> None:
    """Разбор вложения: что модель разглядела в фото, голосовом или видео.

    ⚠️ Имени модели здесь НЕТ намеренно: разбор идёт по цепочке подстраховки,
    и какая именно модель ответила, наружу не возвращается. Зашитая строка
    пережила бы смену цепочки и начала бы врать молча.
    """
    kb = max(1, round((size_bytes or 0) / 1024))
    size = f", {kb} КБ" if size_bytes else ""
    _write(f"── РАЗБОР ({kind}{size}) ──\n{(text or '').strip() or '(пусто)'}\n")


def note_request(model: str, prompt: str, task: str) -> None:
    """Всё, что уходит модели: системная часть целиком и задание."""
    _write(f"── УХОДИТ МОДЕЛИ ({model}, {len(prompt)} симв.) ──\n{prompt}\n"
           f"── ЗАДАНИЕ ──\n{task}\n")


def note_answer(model: str, seconds: float, answer: str) -> None:
    """
    Ответ модели: сама реплика или слово «ПРОПУСК».

    ⚠️ РАЗМЫШЛЕНИЯ СРЕЗАЮТСЯ (решение Максима 2026-08-16). Блок <thought>
    бывает длиннее самой реплики в разы, а читают запись ради того, ЧТО бот
    сказал и почему промолчал. Срез — общим `strip_thoughts`, а не своим
    выражением: в проекте это единственное правильное место, где знают формат
    мыслей, и второе такое разойдётся с ним при первой же смене модели.
    """
    from utils_format import strip_thoughts
    took = f", {seconds:.1f} с" if seconds else ""
    _write(f"── ВЕРНУЛА МОДЕЛЬ ({model}{took}) ──\n"
           f"{strip_thoughts(answer) or '(пусто — весь ответ был размышлением)'}\n")


def note_outcome(text: str) -> None:
    """Чем кончилась проверка — реплика, тишина, ошибка."""
    _write(f"── ИТОГ: {text} ──\n")


# ─── черта по кнопке «Очистить РАЗГОВОРЫ» ───────────────────────────

def close_session() -> bool:
    """
    Подводит черту: текущая запись уезжает в конец архива под заголовком, сам
    файл удаляется, архив обрезается до SESSIONS_TO_KEEP последних записей.
    Следующая проверка заведёт новый файл. Возвращает True, если было что
    переложить. Тихая: очистка разговоров не должна падать из-за лога.
    """
    path = current_path()
    if not path:
        return False
    name = os.path.basename(path)[:-len(".log")]
    name = name[len("chat "):] if name.startswith("chat ") else name
    try:
        with _lock:
            with open(path, "r", encoding="utf-8", errors="replace") as src, \
                    open(archive_path(), "a", encoding="utf-8") as dst:
                dst.write("\n" + _SESSION_MARK_PREFIX + name + _SESSION_MARK_SUFFIX + "\n")
                for line in src:
                    dst.write(line)
            os.remove(path)
    except OSError as e:
        logger.warning("⚠️ %s Не удалось переложить запись разговора в архив: %s", _ICON, e)
        return False
    cut = _trim_archive()
    logger.info("%s Запись разговора «%s» убрана в архив%s", _ICON, name,
                f" (старых записей срезано: {cut})" if cut else "")
    return True


def _trim_archive() -> int:
    """Оставляет в архиве SESSIONS_TO_KEEP последних записей, отрезая начало.

    Файл читается ПОСТРОЧНО, а не целиком в память: он растёт вместе с
    разговорами. Возвращает число убранных записей; ошибки глушатся.
    """
    path = archive_path()
    tmp = path + ".tmp"
    try:
        starts = []
        with open(path, "r", encoding="utf-8", errors="replace") as src:
            for number, line in enumerate(src):
                if line.startswith(_SESSION_MARK_PREFIX):
                    starts.append(number)

        extra = len(starts) - SESSIONS_TO_KEEP
        if extra <= 0:
            return 0

        first_line = starts[extra]   # отсюда начинается самая старая из тех, что оставляем
        with open(path, "r", encoding="utf-8", errors="replace") as src, \
                open(tmp, "w", encoding="utf-8") as dst:
            for number, line in enumerate(src):
                if number >= first_line:
                    dst.write(line)
        os.replace(tmp, path)
        return extra
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0


# ─── цифры для экрана логов ─────────────────────────────────────────

def _count_lines_with(path: str, prefix: str) -> int:
    """Сколько строк файла начинается с приставки. Файл читается построчно."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.startswith(prefix))
    except OSError:
        return 0


def _size(path: str | None) -> int:
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def stats() -> dict:
    """
    Сводка для экрана логов: текущая запись (когда начата, сколько проверок,
    сколько весит) и архив (сколько записей, сколько весит).
    """
    current = current_path()
    archive = archive_path()
    return {
        "path": current,
        "name": os.path.basename(current) if current else "",
        "started": started_label(current),
        "checks": _count_lines_with(current, _CHECK_MARK) if current else 0,
        "size": _size(current),
        "archive_path": archive,
        "archive_size": _size(archive) if os.path.exists(archive) else 0,
        "archive_sessions": (_count_lines_with(archive, _SESSION_MARK_PREFIX)
                             if os.path.exists(archive) else 0),
    }
