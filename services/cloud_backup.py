# ───────────────────────────────────────────────
#  cloud_backup.py — копия базы в Google Drive (2026-08-10)
#
#  Что делает: забирает готовый файл копии (его делает services/backup.py) и
#  кладёт его в облако через rclone — стороннюю программу, которая умеет
#  разговаривать с Google Drive. Ночной цикл зовёт это вместо отправки файла
#  в Телеграм, кнопка «💾 Копия базы» — вдобавок к отправке.
#
#  ⚠️ САМ ФАЙЛ ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ. Снимок и сжатие — services/backup.py,
#  снимок базы — database/history.py. Здесь только отправка наружу. Разделение
#  то же, что между backup.py и history.py, и по той же причине: сделать копию
#  и увезти копию — разные беды и разные отказы.
#
#  ⚠️ Модуль НЕ «тихий», в отличие от deploy/antispam: ошибки летят наружу.
#  Копия, о которой доложили «уехала в облако», а её там нет, хуже честного
#  «увезти не смог»: разбираться будут в тот единственный день, когда она
#  понадобится, и разбираться будет уже поздно. Единственное исключение —
#  configured() и count(): это вопросы «а есть ли облако» и «сколько там
#  лежит», на них честный ответ «не знаю» никого не подводит.
#
#  ⚠️ Почему внешняя программа, а не библиотека Google для Python: у Google
#  доступ выдаётся не паролем, а разовым «рукопожатием» через браузер, после
#  которого выдаётся долгоживущий ключ. rclone умеет и рукопожатие, и
#  обновление протухшего ключа сам; своя реализация этого — недели возни
#  и чужие ключи в нашем коде. Настройка разовая, руками, вне бота:
#  ключ лежит в /home/c4bot/.config/rclone/rclone.conf и в GitHub не ездит.
#
#  На компьютере Максима rclone нет — configured() вернёт False, и всё
#  хозяйство прозрачно откатится к старому поведению (файл в личку).
#  Отдельного кода «мы под Windows» для этого не нужно.
# ─────────────────────────────────────────────

import os
import shutil
import logging
import subprocess

from config import RCLONE_BIN, RCLONE_REMOTE, RCLONE_TIMEOUT_SEC

logger = logging.getLogger(__name__)


def _exe() -> str | None:
    """Полный путь к rclone или None, если программы на машине нет."""
    return shutil.which(RCLONE_BIN)


def _remote_name() -> str:
    """
    Имя «места назначения» без папки: из «gdrive:C4Max-Backups» — «gdrive».
    Именно в таком виде его знает сам rclone (список мест — listremotes).
    """
    return RCLONE_REMOTE.split(":", 1)[0]


def _run(args: list[str], timeout: int = RCLONE_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    """
    Запуск rclone. Отдельной функцией — чтобы у всех вызовов был одинаковый
    таймаут и одинаковый разбор вывода, и чтобы флаги повторов стояли в одном
    месте. Повторы важны: сеть у облака чужая, и разовый обрыв связи не повод
    объявлять ночь без копии.
    """
    return subprocess.run(
        [_exe() or RCLONE_BIN, *args,
         "--retries", "3", "--low-level-retries", "5", "--timeout", "60s"],
        capture_output=True, text=True, timeout=timeout,
    )


def configured() -> bool:
    """
    Готово ли облако: программа на месте И место назначения в ней настроено.

    Тихая: любая заминка означает «облака нет», и вызывающий спокойно уходит
    на старое поведение. Тревожить владельца тут нечем — пока рукопожатия с
    Google не было, это не поломка, а ожидаемое состояние.
    """
    if not _exe():
        return False
    try:
        proc = _run(["listremotes"], timeout=20)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    return f"{_remote_name()}:" in (proc.stdout or "").split()


def upload(path: str) -> None:
    """
    Кладёт файл копии в облако. Молчит при успехе, БРОСАЕТ исключение при
    неудаче — чтобы вызывающий не смог случайно доложить об успехе.

    После загрузки СПРАШИВАЕТ У ОБЛАКА размер положенного файла и сверяет со
    своим. Лишний вызов сети, но без него «успехом» считался бы нулевой код
    возврата, а нам нужно знать не «команда отработала», а «файл там лежит и
    он целый». Ровно в этом смысл всей затеи.

    Работа сетевая и небыстрая (секунды) — звать ТОЛЬКО из отдельного потока
    (run_in_executor), как и снятие копии, а не прямо из обработчика.
    """
    if not _exe():
        raise RuntimeError("rclone на сервере не установлен")

    name = os.path.basename(path)
    size_local = os.path.getsize(path)

    proc = _run(["copy", path, RCLONE_REMOTE])
    if proc.returncode != 0:
        # Вывод rclone бывает многострочным и болтливым; в сообщение берём
        # последнюю строку — там суть, а полный текст уходит в журнал.
        err = (proc.stderr or proc.stdout or "").strip()
        logger.error("⚠️ ☁️ rclone не увёз копию базы в облако: %s", err)
        raise RuntimeError(err.splitlines()[-1] if err else f"rclone вернул код {proc.returncode}")

    size_cloud = _size_in_cloud(name)
    if size_cloud is None:
        raise RuntimeError("после загрузки файла в облаке не видно")
    if size_cloud != size_local:
        raise RuntimeError(f"в облаке файл другого размера: {size_cloud} вместо {size_local}")

    logger.info("☁️ Копия базы уехала в облако: %s (%s)", name, RCLONE_REMOTE)


def _size_in_cloud(name: str) -> int | None:
    """Размер файла в облаке по имени или None, если его там нет."""
    try:
        proc = _run(["lsf", RCLONE_REMOTE, "--include", name, "--format", "sp"], timeout=60)
    except Exception as e:
        logger.warning("⚠️ ☁️ Не удалось спросить облако о файле %s: %s", name, e)
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        size, _, fname = line.partition(";")
        if fname.strip() == name and size.strip().isdigit():
            return int(size)
    return None


def count() -> int | None:
    """
    Сколько копий лежит в облаке. None — «спросить не удалось»: это только
    справка для сообщения, из-за неё ни копия, ни доклад о ней не срываются.
    """
    if not _exe():
        return None
    try:
        proc = _run(["lsf", RCLONE_REMOTE, "--include", "history-*.db.gz"], timeout=60)
    except Exception as e:
        logger.warning("⚠️ ☁️ Не удалось пересчитать копии в облаке: %s", e)
        return None
    if proc.returncode != 0:
        return None
    return len([n for n in (proc.stdout or "").splitlines() if n.strip()])


def folder_human() -> str:
    """Папка в облаке человеческими словами — для сообщений: «C4Max-Backups»."""
    _, _, folder = RCLONE_REMOTE.partition(":")
    return folder.strip("/") or "корень диска"
