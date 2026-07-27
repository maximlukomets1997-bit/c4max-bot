# ───────────────────────────────────────────────
#  logging_setup.py — единая настройка логирования бота
#
#  Один «пульт»: setup_logging() вызывается первым делом в main.py.
#  Пишет всё (уровень DEBUG) одновременно в консоль и в отдельный файл
#  сессии logs/«день.месяц час-минута».log (например 04.07 18-54.log).
#  Каждый запуск/перезапуск создаёт НОВЫЙ файл
#  (двоеточие в имени заменено на дефис — Windows его в именах запрещает).
#
#  В папке logs всегда лежат РОВНО ДВА файла:
#    «день.месяц час-минута».log — лог текущей сессии, пишется в реальном времени;
#    archive.log                 — логи всех прошлых сессий, склеенные подряд.
#  Перенос делает archive_old_logs(): логи прошлых запусков дописываются в конец
#  архива (каждый под заголовком-разделителем) и удаляются, а сам архив
#  обрезается до ARCHIVE_SESSIONS_TO_KEEP последних сессий.
#  Зовётся она из main.py ПОСЛЕ остановки старой копии бота — пока старый
#  процесс жив, он дописывает свой файл, и последние строки в архив бы не попали.
#  Болтливые сторонние библиотеки приглушены, чтобы опрос Telegram не
#  заливал лог служебным шумом.
#
#  ЕДИНЫЙ СТИЛЬ ЗАПИСЕЙ (легенда значков — закреплена, не расползаться):
#    Каждая строка начинается со значка своего раздела:
#      🚀 запуск/остановка бота, база данных, индексация/загрузка RAG
#      👤 входящие сообщения пользователей (текст, фото, голос — и их пропуск)
#      ♊️ запросы и ответы моделей Gemini и Gemma
#      🐪 запросы и ответы моделей Qwen
#      🐋 запросы и ответы моделей DeepSeek
#         (значок ставится по ТОЙ модели, что в строке: ушёл фолбэк на запасную —
#          значок сменится, это видно сразу. Список значков — PROVIDER_ICONS
#          в config.py, ЕДИНЫЙ с админ-панелью «📡 Настройки API».
#          🤖 — запасной значок: модели нет в AVAILABLE_MODELS)
#      📚 поиск по базе знаний RAG (эмбеддинг запроса, отбор статей, вставка в промпт)
#      📰 новости (найдена, сводка, рассылка, подписки)
#      👥 архив сообщений групп
#      🛡 модерация и антиспам (включая панель /mod)
#      🎨 генерация картинок
#      🎮 викторины и звания
#      🔧 действия админа в панелях (/stats, промпты, выбор модели)
#      🤖 проактивное участие в разговоре групп («Сам в разговор»)
#         (с 2026-07-21, раньше 🗣; он же — запасной значок неизвестной модели)
#      ⌨️ входящие команды — ВСЕ: публичные, админские, неизвестные
#         (центральный регистратор log_incoming_command, group=-1)
#    На уровнях WARNING/ERROR значок раздела заменяется на ⚠️, а текст
#    строится по форме «⚠️ Не удалось <что>: <причина>».
#    Пользователь подписывается «Имя (@ник)» или «(пользователь <id>)»,
#    чат — «чат <id>». Без точек и многоточий в конце строки.
#    Рутинные циклы (проверка новостей каждые 10 минут) НЕ логируются —
#    в лог попадают только события и ошибки.
# ───────────────────────────────────────────────

import os
import sys
import logging
from datetime import datetime

LOG_DIR = "logs"

# Общий архив: сюда при каждом запуске дописываются логи ПРОШЛЫХ сессий.
ARCHIVE_FILE = "archive.log"

# Сколько последних сессий хранить в архиве. Более старые куски отрезаются
# от НАЧАЛА файла при запуске бота — иначе архив рос бы бесконечно.
ARCHIVE_SESSIONS_TO_KEEP = 7

# Строка-заголовок, которой в архиве одна сессия отделяется от другой.
# По ней же сессии СЧИТАЮТСЯ при обрезке — формат менять только здесь,
# иначе старые куски архива перестанут распознаваться и обрезка их не тронет.
_SESSION_MARK_PREFIX = "════════ СЕССИЯ "
_SESSION_MARK_SUFFIX = " ════════"

# Путь к файлу лога ТЕКУЩЕЙ сессии. Заполняется в setup_logging() при старте;
# кнопка «📜 Логи бота» в админ-панели берёт файл отсюда, а не угадывает по дате.
CURRENT_LOG_FILE = None

# время | УРОВЕНЬ | сообщение
# «Адрес» строки (модуль:функция:строка) и пустая колонка убраны. Эмодзи-метка и
# детали идут прямо в тексте сообщения через разделитель «|».
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# Библиотеки, которые на DEBUG заливают лог служебным шумом
_NOISY_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "telegram": logging.WARNING,
    "urllib3": logging.WARNING,
    "asyncio": logging.INFO,
    # Каждый SQL-запрос логируется на DEBUG (см. database.history._LoggingConnection).
    # Поднимаем уровень до INFO, чтобы эти технические строки не сыпались в консоль
    # и файл; осмысленные сообщения базы (уровень INFO) при этом остаются.
    "database.history": logging.INFO,
}


def _session_mark(name):
    """Строка-заголовок сессии в архиве: «════ СЕССИЯ 20.07 04-51 ════»."""
    return _SESSION_MARK_PREFIX + name + _SESSION_MARK_SUFFIX


def _archive_path():
    return os.path.join(LOG_DIR, ARCHIVE_FILE)


def _old_log_files():
    """Логи ПРОШЛЫХ запусков: все *.log в папке, кроме архива и файла текущей
    сессии. Возвращает пути, отсортированные от старых к новым (по времени
    изменения — в имени файла нет года, на него полагаться нельзя)."""
    current = ""
    if CURRENT_LOG_FILE:
        current = os.path.normcase(os.path.abspath(CURRENT_LOG_FILE))

    entries = []
    for name in os.listdir(LOG_DIR):
        if not name.endswith(".log") or name == ARCHIVE_FILE:
            continue
        path = os.path.join(LOG_DIR, name)
        if os.path.normcase(os.path.abspath(path)) == current:
            continue
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            pass
    entries.sort()
    return [path for _, path in entries]


def _trim_archive():
    """Оставляет в архиве только ARCHIVE_SESSIONS_TO_KEEP последних сессий,
    отрезая начало файла. Архив может вырасти, поэтому он читается ПОСТРОЧНО,
    а не целиком в память: первый проход ищет строки-заголовки сессий, второй
    переписывает нужный хвост во временный файл, который затем одним движением
    подменяет старый. Возвращает число убранных сессий; ошибки глушатся."""
    path = _archive_path()
    tmp = path + ".tmp"
    try:
        starts = []
        with open(path, "r", encoding="utf-8", errors="replace") as src:
            for number, line in enumerate(src):
                if line.startswith(_SESSION_MARK_PREFIX):
                    starts.append(number)

        extra = len(starts) - ARCHIVE_SESSIONS_TO_KEEP
        if extra <= 0:
            return 0

        first_line = starts[extra]   # с этой строки начинается самая старая из тех, что оставляем
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


def archive_old_logs():
    """Дописывает логи прошлых запусков в конец logs/archive.log и удаляет их,
    после чего обрезает архив до ARCHIVE_SESSIONS_TO_KEEP последних сессий.
    В папке logs остаются два файла: текущий лог и архив.

    Вызывается из main.py ПОСЛЕ остановки старой копии бота (не из setup_logging):
    пока старый процесс жив, он дописывает свой файл, и последние его строки
    в архив бы не попали. Любые ошибки глушатся — перенос логов не должен
    мешать запуску бота."""
    log = logging.getLogger(__name__)
    moved = 0
    try:
        files = _old_log_files()
    except OSError as e:
        log.warning("⚠️ Не удалось прочитать папку логов: %s", e)
        return 0

    for path in files:
        name = os.path.basename(path)[:-len(".log")]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as src, \
                    open(_archive_path(), "a", encoding="utf-8") as dst:
                dst.write("\n" + _session_mark(name) + "\n")
                for line in src:
                    dst.write(line)
        except OSError as e:
            log.warning("⚠️ Не удалось перенести лог %s в архив: %s", name, e)
            continue
        try:
            os.remove(path)
        except OSError as e:
            # Файл занят (обычно — живой процесс). Перенос уже сделан, поэтому при
            # следующем запуске эта сессия попадёт в архив второй раз — предупреждаем.
            log.warning("⚠️ Не удалось удалить лог %s после переноса в архив: %s", name, e)
        moved += 1

    if moved:
        log.debug("🚀 Логов прошлых сессий перенесено в архив: %d", moved)
        cut = _trim_archive()
        if cut:
            log.debug(
                "🚀 Из архива убрано старых сессий: %d (хранятся последние %d)",
                cut, ARCHIVE_SESSIONS_TO_KEEP
            )
    return moved


def setup_logging():
    """Настраивает корневой логгер: DEBUG → консоль + отдельный файл сессии
    logs/«день.месяц час-минута».log (новый при каждом запуске; логи прошлых
    запусков переносит в архив archive_old_logs(), её зовёт main.py)."""
    os.makedirs(LOG_DIR, exist_ok=True)

    # Имя файла — момент запуска/перезапуска: «04.07 18-54.log».
    # Двоеточие в имени файла Windows запрещает, поэтому «час-минута» через дефис.
    log_file = os.path.join(LOG_DIR, datetime.now().strftime("%d.%m %H-%M") + ".log")

    # Запоминаем путь для кнопки «📜 Логи бота» в админ-панели
    global CURRENT_LOG_FILE
    CURRENT_LOG_FILE = log_file

    # На Windows консоль может быть не в UTF-8 — кириллица иначе ломается.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()   # на случай повторной настройки — без дублей
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    for name, level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(level)

    log = logging.getLogger(__name__)
    log.debug("🚀 Запись логов началась: консоль и файл %s", log_file)
