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
import tarfile
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

# Имена архивов статей базы знаний: knowledge-2026-08-12_00-00.tar.gz. Лежат
# в той же папке и ротируются так же, но собираются ОТДЕЛЬНЫМ файлом, а не
# вместе с базой: base — это sqlite, статьи — папка текстов, один gzip их
# не вместит. Префикс другой — значит ротация базы их не видит и не тронет.
_KB_PREFIX = "knowledge-"
_KB_SUFFIX = ".tar.gz"

# Промежуточный несжатый снимок. Живёт секунды: снимок → сжатие → удаление.
_TMP_NAME = "history-snapshot.tmp"

# Метка «за какой день копия уже сделана» (дата по Киеву, «ГГГГ-ММ-ДД»).
# Лежит в БД, а не в памяти: перезапуски у нас частые (кнопка и
# самообновление раз в 5 минут), и с памятью бот слал бы копию после
# каждого из них. Та же причина, по которой в базе живут метка месячного
# сброса статистики (stats_reset_month) и недельная копилка отчёта
# (weekly_report_acc): отметка «уже сделано» обязана пережить перезапуск.
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


def _rotate(prefix: str = _PREFIX, suffix: str = _SUFFIX) -> int:
    """
    Оставляет последние BACKUP_KEEP копий, остальные удаляет. Возвращает
    число удалённых. Сортировка по ИМЕНИ (дата в начале имени) — время файла
    не спрашиваем: при переносе папки оно меняется, а имя нет.

    Префикс и суффикс — параметры, потому что в папке живут ДВА ряда копий:
    база (history-….db.gz) и статьи базы знаний (knowledge-….tar.gz). Каждый
    ряд считается отдельно, иначе семь файлов на двоих означали бы, что новый
    архив статей вытесняет позавчерашнюю копию базы.
    """
    folder = backup_dir()
    try:
        names = sorted(
            n for n in os.listdir(folder)
            if n.startswith(prefix) and n.endswith(suffix)
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
            logger.warning("⚠️ Не удалось удалить старую копию %s: %s", name, e)
    if removed:
        logger.info("💾 Ротация копий (%s): удалено %d, оставлено %d", prefix, removed, BACKUP_KEEP)
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
#  Статьи базы знаний (2026-08-12)
#
#  Зачем отдельно от базы. Тексты статей — единственное ценное, чего нет ни
#  в базе, ни на GitHub после того, как папка knowledge перестала уезжать
#  туда вместе с кодом. Весят они копейки (все 87 статей — около 90 КБ в
#  сжатом виде), а восстановить их неоткуда: пишет их бот на сервере.
#
#  ⚠️ УКАЗАТЕЛЬ (knowledge_base_vectors.json) В АРХИВ НЕ КЛАДЁМ. Он в десятки
#  раз тяжелее всех статей вместе взятых и при этом пересчитывается ботом из
#  них же — ровно по той причине, по которой он не ездит и на GitHub.
# ─────────────────────────────────────────────

def make_kb_backup(moment: datetime | None = None) -> tuple[str, int, int, int]:
    """
    Собирает архив статей базы знаний и возвращает
    (путь к архиву, его размер, статей в базе, статей на одобрении).

    Работа с диском — звать ТОЛЬКО из отдельного потока (run_in_executor),
    как и make_backup.
    """
    # Пути берём у knowledge_store, а не собираем заново: он единственный
    # хозяин этих папок, и второй сборкой пути мы бы однажды заархивировали
    # не то, что бот на самом деле читает (то же правило, что в tech_card).
    from services.knowledge_store import _dir_for

    folder = backup_dir()
    os.makedirs(folder, exist_ok=True)

    moment = moment or datetime.now()
    final_path = os.path.join(folder, f"{_KB_PREFIX}{moment:%Y-%m-%d_%H-%M}{_KB_SUFFIX}")

    counts = {"approved": 0, "pending": 0}
    with tarfile.open(final_path, "w:gz", compresslevel=6) as tar:
        for kind in ("approved", "pending"):
            src = _dir_for(kind)
            if not os.path.isdir(src):
                continue
            for fname in sorted(os.listdir(src)):
                # Только сами статьи: указатель базы знаний (.json) и любой
                # случайный файл рядом в архив не попадают.
                if not fname.lower().endswith(".md"):
                    continue
                tar.add(os.path.join(src, fname), arcname=f"{kind}/{fname}")
                counts[kind] += 1

    size = os.path.getsize(final_path)
    logger.info("📚 Архив статей базы знаний готов: %s (%s, в базе %d, на одобрении %d)",
                os.path.basename(final_path), human_size(size),
                counts["approved"], counts["pending"])
    _rotate(_KB_PREFIX, _KB_SUFFIX)
    return final_path, size, counts["approved"], counts["pending"]


def kb_caption(fname: str, size: int, approved: int, pending: int) -> str:
    """
    Подпись к архиву статей — ОДНА на оба места отправки (ночной цикл и
    кнопка «💾 Копия базы»). Две разные подписи об одном и том же файле
    неизбежно разъехались бы.
    """
    return (
        f"📚 <b>Статьи базы знаний</b>\n"
        f"<code>{fname}</code> · {human_size(size)}\n"
        f"В базе: {approved} · Ждут одобрения: {pending}\n"
        f"<i>Тексты статей, из которых бот строит справки по технике. "
        f"Живут только на сервере — сохрани вместе с копией базы.</i>"
    )


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
