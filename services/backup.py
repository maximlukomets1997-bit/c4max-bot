# ───────────────────────────────────────────────
#  backup.py — ночная копия базы (2026-07-31)
#
#  Что делает: снимает согласованный снимок history.db, сжимает его и кладёт
#  в папку копий, оставляя последние BACKUP_KEEP штук. Файл отдают дальше —
#  ночной цикл шлёт его владельцу в личку, кнопка «💾 Копия базы» в /adm
#  делает то же самое по требованию.
#
#  ⚠️ САМ SQLITE ЭТОТ МОДУЛЬ НЕ ТРОГАЕТ. Снимок делает database/history.py
#  (единственный модуль бота, которому разрешено работать с базой) — здесь
#  только файлы: сжатие, имена, ротация. Полезешь сюда с sqlite3 напрямую —
#  получишь копию мимо общего замка, то есть в разгар чужой записи.
#
#  ⚠️ Модуль НЕ «тихий», в отличие от antispam/proactive/deploy: ошибки летят
#  наружу. Копия, о которой доложили «готово», а её нет, хуже, чем честное
#  «сделать не смог»: разбираться будут в тот единственный день, когда она
#  понадобится, и разбираться будет уже поздно.
# ─────────────────────────────────────────────

import os
import gzip
import shutil
import logging
from datetime import datetime

from config import BACKUP_DIR, BACKUP_KEEP
from database import history as hist

logger = logging.getLogger(__name__)

# Имена копий: history-2026-07-31_00-00.db.gz. Дата в начале и с ведущими
# нулями — тогда обычная сортировка по имени совпадает с хронологической,
# и ротации не нужно спрашивать у файловой системы время создания (оно
# врёт при переносе файлов).
_PREFIX = "history-"
_SUFFIX = ".db.gz"

# Промежуточный несжатый снимок. Живёт секунды: снимок → сжатие → удаление.
_TMP_NAME = "history-snapshot.tmp"

# Метка «за какой день копия уже сделана» (дата по Киеву, «ГГГГ-ММ-ДД»).
# Лежит в БД, а не в памяти: перезапуски у нас частые (кнопка и
# самообновление раз в 10 минут), и с памятью бот слал бы копию после
# каждого из них. Та же причина, по которой в базе живёт состояние тревоги.
_DONE_KEY = "backup_last_date"


def backup_dir() -> str:
    """
    Папка копий — рядом с файлами бота. Путь считается ОТ ЭТОГО ФАЙЛА, а не
    от рабочей папки: у службы systemd на сервере рабочая папка чужая, и по
    относительному пути копии разъехались бы по случайным местам (те же
    грабли, что с чтением VERSION и с .env у сторожа).
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, BACKUP_DIR)


def human_size(size: int) -> str:
    """Размер файла человеческими словами: «312 КБ», «4.2 МБ»."""
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} КБ"
    return f"{size / (1024 * 1024):.1f} МБ"


def _rotate() -> int:
    """
    Оставляет последние BACKUP_KEEP копий, остальные удаляет. Возвращает
    число удалённых. Сортировка по ИМЕНИ (дата в начале имени) — время файла
    не спрашиваем: при переносе папки оно меняется, а имя нет.
    """
    folder = backup_dir()
    try:
        names = sorted(
            n for n in os.listdir(folder)
            if n.startswith(_PREFIX) and n.endswith(_SUFFIX)
        )
    except OSError:
        return 0

    removed = 0
    for name in names[:-BACKUP_KEEP] if BACKUP_KEEP > 0 else names:
        try:
            os.remove(os.path.join(folder, name))
            removed += 1
        except OSError as e:
            # Не смогли удалить старую копию — это не повод считать неудачной
            # свежую: она уже на диске и это главное.
            logger.warning("⚠️ Не удалось удалить старую копию базы %s: %s", name, e)
    if removed:
        logger.info("💾 Ротация копий базы: удалено %d, оставлено %d", removed, BACKUP_KEEP)
    return removed


def make_backup(moment: datetime | None = None) -> tuple[str, int]:
    """
    Делает копию базы и возвращает (путь к сжатому файлу, его размер).

    Порядок: снимок средствами SQLite → сжатие → удаление промежуточного
    файла → ротация. Сжатие обычным gzip: база — текст и числа, ужимается
    в разы, а распаковать её сможет любая система без бота (даже проводник
    Windows), что для последней копии важнее любой экономии места.

    Работа с диском тяжёлая по меркам бота (десятки миллисекунд) — звать
    ТОЛЬКО из отдельного потока (run_in_executor), а не прямо из обработчика.
    """
    folder = backup_dir()
    os.makedirs(folder, exist_ok=True)

    moment = moment or datetime.now()
    raw_path = os.path.join(folder, _TMP_NAME)
    final_path = os.path.join(folder, f"{_PREFIX}{moment:%Y-%m-%d_%H-%M}{_SUFFIX}")

    try:
        plain_size = hist.backup_to(raw_path)
        with open(raw_path, "rb") as src, gzip.open(final_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
    finally:
        # Промежуточный файл убираем в любом случае: это полная копия базы,
        # и оставлять её несжатой в папке — лишние мегабайты на пустом месте.
        try:
            os.remove(raw_path)
        except OSError:
            pass

    size = os.path.getsize(final_path)
    logger.info("💾 Копия базы готова: %s (%s, из %s)",
                os.path.basename(final_path), human_size(size), human_size(plain_size))
    _rotate()
    return final_path, size


def list_backups() -> list[tuple[str, int]]:
    """Копии на сервере от старых к свежим: (имя файла, размер). Для отчётов."""
    folder = backup_dir()
    try:
        names = sorted(
            n for n in os.listdir(folder)
            if n.startswith(_PREFIX) and n.endswith(_SUFFIX)
        )
    except OSError:
        return []
    out = []
    for name in names:
        try:
            out.append((name, os.path.getsize(os.path.join(folder, name))))
        except OSError:
            continue
    return out


# ───────────────────────────────────────────────
#  «За какой день копия уже сделана»
# ─────────────────────────────────────────────

def _today_kyiv() -> str:
    """
    Сегодняшняя дата по Киеву строкой «ГГГГ-ММ-ДД». Часовой пояс берём у
    services/daily_report.py — там он уже настроен и выверен; заводить второй
    расчёт киевского времени нельзя, они разъедутся на переводе часов.
    Импорт внутри функции — чтобы модули не зависели друг от друга при загрузке.
    """
    from services.daily_report import kyiv_now
    return kyiv_now().strftime("%Y-%m-%d")


def due_today() -> bool:
    """
    Пора ли делать ночную копию: за сегодняшний (по Киеву) день её ещё не было.

    Это и есть защита от повторов при перезапусках, и она же «догоняет»
    пропущенную ночь: бот был выключен в полночь — копия уйдёт при первом
    запуске, а не потеряется. Устройство то же, что у суточного отчёта.
    """
    try:
        return hist.get_setting(_DONE_KEY, "") != _today_kyiv()
    except Exception as e:
        # Не смогли прочитать метку — лучше сделать лишнюю копию, чем ни одной.
        logger.warning("⚠️ Не удалось прочитать метку последней копии базы: %s", e)
        return True


def note_done() -> None:
    """Помечает, что за сегодня копия сделана. Звать ПОСЛЕ удачной отправки."""
    hist.set_setting(_DONE_KEY, _today_kyiv())


def last_done() -> str:
    """Дата последней ночной копии («ГГГГ-ММ-ДД») или пустая строка."""
    try:
        return hist.get_setting(_DONE_KEY, "") or ""
    except Exception:
        return ""
