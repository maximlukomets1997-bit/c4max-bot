# ─────────────────────────────────────────────
#  services/update_log.py — ⬇️ ИСТОРИЯ ОБНОВЛЕНИЙ БОТА (2026-08-04)
#
#  Отвечает на вопрос «что и когда в боте менялось» — и даёт на каждое
#  изменение ССЫЛКУ на GitHub, чтобы можно было посмотреть сам разбор правки.
#
#  Откуда берутся данные: из ЖУРНАЛА GIT той самой папки, из которой бот
#  сейчас работает (`git log --first-parent`). Не из базы и не из сети:
#  • база знает только о самообновлениях, случившихся при живом боте, —
#    правки, приехавшие до её заведения или при остановленном боте, в ней
#    не появились бы вовсе;
#  • сеть (GitHub API) потребовала бы ключа, а на молчащем GitHub панель
#    перестала бы открываться. Журнал git лежит на диске рядом и всегда
#    совпадает с тем кодом, который прямо сейчас выполняется.
#
#  Контракт:
#  • Модуль СЧИТАЕТ и СОБИРАЕТ, ничего не отправляет — показ в
#    handlers/admin/panel_updates.py.
#  • Модуль «тихий», как deploy.py: наружу не бросает никогда. Нет git,
#    нет папки .git, git ответил мусором — вернётся пустой список, и панель
#    честно скажет «истории нет».
#  • В сеть НЕ ходит. `git log` читает только местный журнал.
# ─────────────────────────────────────────────

import logging
import os
import re
import subprocess
import time
from datetime import datetime

from config import GITHUB_REPO_URL

logger = logging.getLogger(__name__)

_ICON = "⬇️"

# Папка проекта = на уровень выше этого файла (как в services/deploy.py:
# у службы systemd рабочая папка чужая, считать путь от неё нельзя).
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Сколько записей журнала берём за раз. 200 — с большим запасом: панель
# показывает по странице, а счётчикам «за неделю / за месяц» больше и не надо.
_LOG_LIMIT = 200

# Журнал git меняется только при обновлении бота, то есть в лучшем случае раз
# в 10 минут (jobs/update.py). Держим разобранный ответ в памяти, чтобы
# листание страниц панели не дёргало git на каждое нажатие.
_CACHE_TTL = 60
_cache: dict = {"ts": 0.0, "items": []}

# Разделители полей и записей в выводе git. Берём управляющие символы, а не
# запятые с табуляциями: и заголовок, и тело правки пишет человек, и любой
# «обычный» разделитель однажды встретится внутри текста.
_FS = "\x1f"   # между полями одной записи
_RS = "\x1e"   # между записями

# Заголовок слияния, который GitHub ставит кнопкой «Merge pull request»:
# сам номер РП там, а человеческое название правки — первой строкой тела.
_MERGE_RE = re.compile(r"^Merge pull request #(\d+)\b")
# Второй вид — «сплющенное» слияние (Squash and merge): номер РП в конце
# заголовка, отдельного тела нет.
_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")


def _git(args: list[str], timeout: int = 10) -> str | None:
    """Запуск git в папке проекта. None — не получилось (это НЕ беда)."""
    try:
        proc = subprocess.run(
            ["git", "-C", PROJECT_DIR] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        logger.debug("%s git не найден — истории обновлений не будет", _ICON)
        return None
    except Exception as e:
        logger.debug("%s git %s не запустился: %s", _ICON, args, e)
        return None
    if proc.returncode != 0:
        logger.debug("%s git %s ответил кодом %s: %s",
                     _ICON, args, proc.returncode, (proc.stderr or "")[:200])
        return None
    return proc.stdout or ""


def available() -> bool:
    """Есть ли здесь вообще журнал git (в архиве кода без .git его нет)."""
    return os.path.isdir(os.path.join(PROJECT_DIR, ".git"))


# Версия на каждый коммит: спрашивается у git по требованию и запоминается
# НАВСЕГДА. Коммит — вещь неизменная, поэтому устареть эта память не может,
# а страниц человек листает единицы.
_versions: dict[str, str] = {}


def version_of(sha: str) -> str:
    """
    Номер версии бота НА МОМЕНТ этого обновления — содержимое файла VERSION
    в том коммите («3.85»; пусто, если файла там нет или git недоступен).

    ⚠️ ЗНАЙ СЛЕПОЕ ПЯТНО САМОГО НОМЕРА: его поднимает `Отправить изменения на
    GitHub.bat` при отправке с компьютера Максима, а правки, приехавшие через
    pull request из сессии Claude, номер не двигают, если его не подняли руками
    в том же коммите. Поэтому подряд идущие обновления в списке МОГУТ показывать
    одну и ту же версию — это не сбой панели, а честная картина: код изменился,
    а номер остался прежним. Отличает их метка сборки (`.build`), она меняется
    всегда.

    Спрашивается только для показанной страницы (8 записей), а не для всего
    журнала: 200 запусков git ради экрана из восьми строк ни к чему.
    """
    if not sha:
        return ""
    if sha in _versions:
        return _versions[sha]
    out = _git(["show", f"{sha}:VERSION"], timeout=5)
    version = (out or "").strip().splitlines()
    _versions[sha] = version[0].strip() if version else ""
    return _versions[sha]


def _parse_record(raw: str) -> dict | None:
    """Одна запись журнала → готовая строка для панели."""
    parts = raw.split(_FS)
    if len(parts) < 4:
        return None
    sha, ts_raw, subject, body = parts[0].strip(), parts[1].strip(), parts[2], parts[3]
    if not sha:
        return None
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        ts = 0

    pr = 0
    title = subject.strip()
    merge = _MERGE_RE.match(title)
    if merge:
        pr = int(merge.group(1))
        # У слияния GitHub заголовок служебный («Merge pull request #37
        # from …/claude-…»), а название правки лежит первой строкой тела.
        # Без этой подмены в панели был бы список одинаковых строк с
        # именами веток — то есть ничего.
        first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        if first_line:
            title = first_line
    else:
        squash = _SQUASH_RE.search(title)
        if squash:
            pr = int(squash.group(1))
            title = _SQUASH_RE.sub("", title).strip()

    return {
        "sha": sha,
        "short": sha[:7],
        "ts": ts,
        "title": title or sha[:7],
        "pr": pr,
        # Ссылка ведёт НА РП, если правка приехала слиянием (там видно и
        # обсуждение, и весь разбор целиком), иначе — на сам коммит.
        "url": f"{GITHUB_REPO_URL}/pull/{pr}" if pr else f"{GITHUB_REPO_URL}/commit/{sha}",
    }


def _collect() -> list[dict]:
    """Свежий разбор журнала git (без кэша)."""
    if not available():
        return []
    out = _git([
        "log", "--first-parent", f"-n{_LOG_LIMIT}",
        f"--format=%H{_FS}%ct{_FS}%s{_FS}%b{_RS}",
    ])
    if not out:
        return []
    items = []
    for chunk in out.split(_RS):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        item = _parse_record(chunk)
        if item:
            items.append(item)
    return items


def recent(limit: int = _LOG_LIMIT) -> list[dict]:
    """
    Последние обновления, новые сверху. Берём ТОЛЬКО первую линию истории
    (`--first-parent`): она отвечает на вопрос «что приехало в бота», а без
    неё в список полезли бы все промежуточные коммиты внутри каждой правки.

    Ответ кэшируется на _CACHE_TTL секунд — панель листается кнопками, и
    дёргать git на каждое нажатие незачем.
    """
    now = time.monotonic()
    if not _cache["items"] or now - _cache["ts"] > _CACHE_TTL:
        try:
            _cache["items"] = _collect()
        except Exception as e:  # модуль «тихий»: панель важнее истории
            logger.debug("%s Не удалось собрать историю обновлений: %s", _ICON, e)
            _cache["items"] = []
        _cache["ts"] = now
    return _cache["items"][:limit]


def stats() -> dict:
    """
    Сводка для шапки панели: сколько обновлений за сутки, неделю и месяц,
    сколько всего в журнале и когда было последнее (unix-время, 0 — нет).
    """
    items = recent()
    now = time.time()
    day = week = month = 0
    for it in items:
        age = now - it["ts"]
        if it["ts"] <= 0:
            continue
        if age <= 86400:
            day += 1
        if age <= 7 * 86400:
            week += 1
        if age <= 30 * 86400:
            month += 1
    return {
        "day": day,
        "week": week,
        "month": month,
        "total": len(items),
        "last_ts": items[0]["ts"] if items else 0,
    }


# ─── подписи времени ────────────────────────────────────────────────

def fmt_time(ts: float) -> str:
    """Время обновления по Киеву — «04.08.2026 14:20»."""
    if not ts:
        return "—"
    from services.daily_report import _kyiv_tz
    return datetime.fromtimestamp(ts, _kyiv_tz()).strftime("%d.%m.%Y %H:%M")


def fmt_day(ts: float) -> str:
    """Короткая дата для надписи на кнопке — «04.08»."""
    if not ts:
        return "—"
    from services.daily_report import _kyiv_tz
    return datetime.fromtimestamp(ts, _kyiv_tz()).strftime("%d.%m")


def fmt_ago(ts: float) -> str:
    """«сколько прошло» человеческими словами: 40 мин / 3 ч / 5 дн назад."""
    if not ts:
        return ""
    seconds = max(0, int(time.time() - ts))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"
