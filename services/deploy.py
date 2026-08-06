# ─────────────────────────────────────────────────────────────
#  services/deploy.py — обновление кода с GitHub (2026-07-27)
# ─────────────────────────────────────────────────────────────
# Тонкая обёртка над deploy.sh: вся логика обновления живёт ТАМ, здесь только
# запуск и разбор ответа. Сделано так намеренно — тот же сценарий зовёт
# «Отправить изменения на GitHub.bat» с компьютера Максима, и две разные
# реализации одного и того же неизбежно разъехались бы.
#
# Модуль «тихий»: наружу исключений не бросает никогда. Обновление — не та
# вещь, из-за которой бот вправе упасть.

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

# ── Когда в чатах последний раз кто-то писал ──────────────────────────────
# Нужно самообновлению: перезапуск обрывает разговор, и тот, кто ждал ответа,
# его не получит. Поэтому обновляемся только в тишине.
# Метку ставит отдельный лёгкий обработчик (handlers/__init__.py, группа -2),
# который видит ВСЕ входящие сообщения и больше ничего не делает.
# В памяти, а не в базе: цена — одно присваивание на сообщение, а после
# перезапуска «тишина» и так начинается заново.
_last_activity = 0.0


def note_activity() -> None:
    """Запомнить, что в чатах только что кто-то написал."""
    global _last_activity
    _last_activity = time.monotonic()


def quiet_for() -> float:
    """Сколько секунд в чатах тихо. Сразу после запуска — очень много."""
    if not _last_activity:
        return float("inf")
    return time.monotonic() - _last_activity

# Папка проекта = на уровень выше этого файла.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(PROJECT_DIR, "deploy.sh")

# ⚠️ Сценарий запускается из КОПИИ: обновление заменяет и сам deploy.sh, а bash
# дочитывает файл с диска по ходу выполнения — подмена на лету оборвала бы его
# на середине.
# ⚠️ И копия лежит В ДОМАШНЕЙ ПАПКЕ, а не в /tmp: там защита Ubuntu не даёт
# переписать файл, созданный другим пользователем, а этот же сценарий копирует
# себе обновление с компьютера Максима (оно работает от root). Общий путь в
# /tmp они бы отбирали друг у друга — обновления молча перестали бы идти.
SCRIPT_COPY = os.path.join(os.path.expanduser("~"), ".c4max-deploy-run.sh")

# Потолок ожидания. Обычное обновление — секунды; долго бывает только когда
# изменился requirements.txt и ставятся библиотеки.
TIMEOUT_SEC = 300

# ⚠️ ЗАМОК НА ДВА ВХОДА. Обновление зовут ДВОЕ: автоматический цикл раз в пять
# минут (jobs/update.py) и кнопка «🔄 ПЕРЕЗАПУСК» в панели. Оба готовят себе
# сценарий в ОДИН И ТОТ ЖЕ файл SCRIPT_COPY — и нажатие кнопки посреди работы
# цикла переписало бы файл прямо под уже работающим bash (тот дочитывает
# сценарий с диска по ходу выполнения). flock внутри самого deploy.sh от этого
# не спасает: он срабатывает уже ПОСЛЕ копирования.
# Оба входа живут в одном цикле событий, поэтому хватает asyncio-замка.
_update_lock = asyncio.Lock()


def can_update() -> bool:
    """
    Можно ли здесь обновляться из GitHub.

    ⚠️ На домашнем компьютере Максима — НЕЛЬЗЯ, и это главное назначение
    проверки: там лежат ещё не отправленные правки, и скачивание с GitHub
    дралось бы с его же работой. Обновляется только сервер.
    """
    if sys.platform == "win32":
        return False
    return os.path.isdir(os.path.join(PROJECT_DIR, ".git")) and os.path.isfile(SCRIPT)


def _run_blocking() -> dict:
    """Синхронный запуск сценария. Зовётся из отдельного потока — см. update()."""
    try:
        shutil.copyfile(SCRIPT, SCRIPT_COPY)
    except Exception as e:
        logger.warning("⚠️ Обновление: не удалось подготовить сценарий: %s", e)
        return {"STATUS": "FAIL", "MSG": "Не удалось подготовить сценарий обновления."}

    try:
        proc = subprocess.run(
            ["bash", SCRIPT_COPY],
            capture_output=True, text=True, timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logger.warning("⚠️ Обновление: сценарий не уложился в %s сек", TIMEOUT_SEC)
        return {"STATUS": "FAIL", "MSG": "Обновление затянулось дольше положенного."}
    except Exception as e:
        logger.warning("⚠️ Обновление: сценарий не запустился: %s", e)
        return {"STATUS": "FAIL", "MSG": "Не удалось запустить обновление."}

    # Сценарий печатает строки КЛЮЧ=значение. Всё, что на них не похоже,
    # пропускаем: это может быть болтовня git в поток вывода.
    result = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            if key.isupper():
                result[key] = value.strip()

    if not result:
        logger.warning("⚠️ Обновление: непонятный ответ сценария: %s", (proc.stderr or "")[:200])
        return {"STATUS": "FAIL", "MSG": "Обновление ответило непонятным образом."}

    return result


async def update(quiet_nochange: bool = False) -> dict:
    """
    Обновить код с GitHub. Возвращает разобранный ответ сценария:
      STATUS  — UPDATED | NOCHANGE | ROLLBACK | NETFAIL | FAIL | BUSY
      VERSION — версия, на которой бот остался
      MSG     — готовая человеческая строка
    Бота НЕ перезапускает: это дело того, кто позвал.

    quiet_nochange=True — не писать в лог исход NOCHANGE («нового кода нет»).
    Его ставит автоматический цикл самообновления: он спрашивает GitHub каждые
    5 минут, и одна и та же строка 288 раз в сутки забивала лог, пряча в нём
    настоящие события (решение Максима 2026-07-27). Все ПРОЧИЕ исходы пишутся
    всегда — молчать о неудаче нельзя.
    """
    # Обновление уже идёт (второй вход) — НЕ ждём его в очереди: ожидание до
    # TIMEOUT_SEC пережгло бы 15-секундный предел ответа на нажатие кнопки.
    # Отвечаем тем же исходом BUSY, что печатает сам deploy.sh при занятом
    # flock, — для позвавшего разницы нет.
    if _update_lock.locked():
        res = {"STATUS": "BUSY", "MSG": "Обновление уже идёт, подожди несколько секунд."}
    else:
        async with _update_lock:
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, _run_blocking)
    if not (quiet_nochange and res.get("STATUS") == "NOCHANGE"):
        logger.info("⬇️ Обновление: %s — %s", res.get("STATUS"), res.get("MSG"))
    if res.get("ERR"):
        logger.warning("⚠️ Обновление, текст ошибки: %s", res["ERR"])
    return res


def describe(res: dict) -> str:
    """Готовая строка для показа человеку (HTML). Значки — по исходу."""
    status = res.get("STATUS", "FAIL")
    msg = res.get("MSG", "Обновление не удалось.")
    icon = {
        "UPDATED": "⬇️",
        "NOCHANGE": "✅",
        "ROLLBACK": "⚠️",
        "NETFAIL": "🌐",
        "BUSY": "⏳",
    }.get(status, "⚠️")
    return f"{icon} {msg}"
