# ───────────────────────────────────────────────
#  knowledge_store.py — файловое хранилище статей базы знаний RAG
#
#  Идея (утверждена владельцем, июль 2026): новости не попадают в базу знаний
#  автоматически — сначала админ одобряет их в панели, чтобы база не
#  засорялась устаревающими акциями/событиями.
#
#  Этап 1 (готов): каждая разосланная новость сохраняется в папку ожидания
#      KNOWLEDGE_PENDING_DIR — файл .md с шапкой-метаданными (title/tag/url/
#      date) и ИСХОДНЫМ текстом статьи из парсера. Именно исходным, а не
#      сводкой модели: база знаний хранит факты (ТТХ, способы получения),
#      а не художественный пересказ.
#  Этап 2 (готов): одобренные статьи лежат в KNOWLEDGE_APPROVED_DIR,
#      векторизуются поштучно; RAG читает базу из папки файлов (services/rag.py).
#  Этап 3 (готов): управление файлами из админ-панели /adm → «База знаний»:
#      list_articles / read_article / approve_article / delete_article /
#      replace_article. Папка = статус: pending — ждёт, approved — в базе.
# ───────────────────────────────────────────────

import os
import re
import logging
from datetime import date

from config import KNOWLEDGE_PENDING_DIR, KNOWLEDGE_APPROVED_DIR

logger = logging.getLogger(__name__)

# Разрешённые «папки-статусы» для операций панели. Все функции панели
# принимают folder строкой и проверяют её по этому словарю — защита от
# кривых путей в callback-данных.
_FOLDERS = {
    "pending": KNOWLEDGE_PENDING_DIR,
    "approved": KNOWLEDGE_APPROVED_DIR,
}


def _dir_for(folder: str) -> str:
    if folder not in _FOLDERS:
        raise ValueError(f"Неизвестная папка базы знаний: {folder}")
    return _FOLDERS[folder]


def _safe_fname(fname: str) -> str:
    """Отсекает попытки выйти из папки: берём только имя файла, только .md."""
    fname = os.path.basename(fname)
    if not fname.lower().endswith(".md"):
        raise ValueError(f"Недопустимое имя файла базы знаний: {fname}")
    return fname


def _slug_from_url(url: str) -> str:
    """Имя файла из URL новости: последний сегмент пути, только безопасные символы."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)
    return slug or "news"


def save_pending_news(title: str, tag: str, url: str, text: str, description: str = "") -> str:
    """
    Сохраняет разосланную новость в папку ожидания базы знаний.

    Возвращает путь сохранённого файла. Если полный текст статьи получить
    не удалось (парсер вернул пусто), сохраняется хотя бы заголовок с кратким
    описанием — админ увидит это в панели и решит, что делать с файлом.
    """
    os.makedirs(KNOWLEDGE_PENDING_DIR, exist_ok=True)

    body = text.strip() if text and text.strip() else f"# {title}\n\n{description}".strip()
    today = date.today().isoformat()
    file_name = f"{today}_{_slug_from_url(url)}.md"
    file_path = os.path.join(KNOWLEDGE_PENDING_DIR, file_name)

    content = (
        "---\n"
        f"title: {title}\n"
        f"tag: {tag}\n"
        f"url: {url}\n"
        f"date: {today}\n"
        "---\n\n"
        f"{body}\n"
    )
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("📚 Новость сохранена в папку ожидания базы знаний: %s", file_path)
    return file_path


# ───────────────────────────────────────────────
#  Операции для админ-панели «База знаний»
# ───────────────────────────────────────────────

def read_title(file_path: str) -> str:
    """Название статьи: из шапки (title:), иначе из первого «# …», иначе имя файла."""
    return _read_meta(file_path)[0]


# ───────────────────────────────────────────────
#  Тип статьи: наземная техника / самолёт / корабль / подлодка
#
#  Нужен панели /rag: значок на кнопке и порядок сортировки списка.
#  Определяется по ПЕРВОЙ СТРОКЕ ОПИСАНИЯ статьи — той, где есть слова
#  «War Thunder Mobile» (её пишет методика эталонных статей, навык
#  rag-etalon): «… — итальянский основной боевой танк XIII ранга в War
#  Thunder Mobile. …». Только по ней, а не по всему тексту: в статье про
#  корабль легко встретить слово «самолёт», и наоборот — тип бы «поплыл».
#  Нет такой строки (статьи об игровых механиках, сырые новости) → "guide".
#
#  ВАЖЕН ПОРЯДОК проверок: «истребитель танков» — это наземная машина,
#  а не самолёт, поэтому наземные обороты проверяются раньше авиационных.
# ───────────────────────────────────────────────

# Значок и порядок сортировки для каждого типа (единственное место, где
# они заданы: панель /rag берёт их отсюда). order — очередь в списке:
# сначала наземная техника, потом авиация, корабли, подлодки, механики.
ARTICLE_KINDS = {
    "ground": {"icon": "🚜", "order": 0, "name": "наземная техника"},
    "air":    {"icon": "✈️", "order": 1, "name": "авиация"},
    "ship":   {"icon": "🛥️", "order": 2, "name": "корабли"},
    "sub":    {"icon": "⚓", "order": 3, "name": "подводные лодки"},
    "guide":  {"icon": "📘", "order": 4, "name": "игровые механики"},
    "other":  {"icon": "📄", "order": 5, "name": "без типа"},
}

# Проверяются СВЕРХУ ВНИЗ, первое совпадение выигрывает.
_KIND_PATTERNS = (
    ("sub",    r"подводн\w*\s+лодк|субмарин|подлодк"),
    ("ship",   r"линкор|линейн\w*\s+крейсер|крейсер|эсминц|эсминец|фрегат|корвет|"
               r"дредноут|авианос|канонерк|торпедн\w*\s+катер|корабл"),
    # Наземные обороты со словом «истребитель» — ДО авиации (см. выше)
    ("ground", r"истребител\w*\s+танков|поддержки\s+танков"),
    ("air",    r"истребител|штурмовик|бомбардировщик|перехватчик|самол[её]т|"
               r"вертол[её]т|биплан|гидросамол[её]т"),
    ("ground", r"\bтанк|\bобт\b|\bбмп\b|\bбмпт\b|\bбтр\b|\bзсу\b|\bсау\b|"
               r"самоходк|бронемашин|бронетранспорт|зенитн\w*\s+установк"),
)


def detect_kind(descriptor: str) -> str:
    """
    Тип статьи по строке описания: "ground" | "air" | "ship" | "sub" | "guide".
    Пустая строка (описания нет) → "guide"; описание есть, но тип не опознан
    → "other" (значок 📄 в панели — сигнал, что в первой строке статьи забыли
    указать тип техники).
    """
    text = (descriptor or "").lower()
    if not text.strip():
        return "guide"
    for kind, pattern in _KIND_PATTERNS:
        if re.search(pattern, text):
            return kind
    return "other"


def _read_meta(file_path: str) -> tuple[str, str]:
    """
    Заголовок и тип статьи ОДНИМ чтением файла (панель открывает десятки
    статей подряд — второй проход по тем же файлам был бы лишней работой).
    Возвращает (title, kind).
    """
    title = os.path.splitext(os.path.basename(file_path))[0]
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            head = f.read(2000)
    except Exception as e:
        logger.warning("⚠️ Не удалось прочитать статью %s: %s", file_path, e)
        return title, "other"

    m = re.search(r"^title:\s*(.+)$", head, re.M)
    if m:
        title = m.group(1).strip()
    else:
        m = re.search(r"^#\s+(.+)$", head, re.M)
        if m:
            title = m.group(1).strip()

    # Строка описания — та, где упомянута игра (см. пояснение выше)
    descriptor = ""
    for line in head.splitlines():
        if "war thunder mobile" in line.lower():
            descriptor = line
            break
    return title, detect_kind(descriptor)


def list_articles() -> list[dict]:
    """
    Список всех статей базы знаний для панели.

    Порядок: сначала ожидающие одобрения (новые сверху — это очередь, её
    разбирают по свежести), затем одобренные, сгруппированные по типу
    техники (решение владельца 2026-07-18): наземная → авиация → корабли →
    подлодки → игровые механики, внутри группы по алфавиту.

    Каждый элемент: {"folder": "pending"|"approved", "fname", "title",
    "path", "kind"} — kind см. ARTICLE_KINDS (значок и порядок сортировки).
    """
    result = []
    for folder, newest_first in (("pending", True), ("approved", False)):
        directory = _dir_for(folder)
        os.makedirs(directory, exist_ok=True)
        names = sorted(
            (f for f in os.listdir(directory) if f.lower().endswith(".md")),
            reverse=newest_first,
        )
        items = []
        for fname in names:
            path = os.path.join(directory, fname)
            title, kind = _read_meta(path)
            items.append({
                "folder": folder,
                "fname": fname,
                "title": title,
                "path": path,
                "kind": kind,
            })
        # Очередь на одобрение оставляем как есть (новые сверху);
        # одобренные раскладываем по типам техники.
        if folder == "approved":
            items.sort(key=lambda a: (
                ARTICLE_KINDS.get(a["kind"], ARTICLE_KINDS["other"])["order"],
                a["title"].lower(),
            ))
        result.extend(items)
    return result


def read_article(folder: str, fname: str) -> tuple[str, str]:
    """Возвращает (путь, содержимое) статьи."""
    path = os.path.join(_dir_for(folder), _safe_fname(fname))
    with open(path, "r", encoding="utf-8") as f:
        return path, f.read()


def approve_article(fname: str) -> str:
    """
    Одобряет статью: переносит файл из папки ожидания в папку одобренных.
    Возвращает новое имя файла (при совпадении имён добавляется суффикс).
    Векторизацию файла выполняет вызывающий код (services.rag.sync_knowledge_base).
    """
    fname = _safe_fname(fname)
    src = os.path.join(KNOWLEDGE_PENDING_DIR, fname)
    dst_name = fname
    dst = os.path.join(KNOWLEDGE_APPROVED_DIR, dst_name)
    os.makedirs(KNOWLEDGE_APPROVED_DIR, exist_ok=True)
    counter = 2
    while os.path.exists(dst):
        base, ext = os.path.splitext(fname)
        dst_name = f"{base}_{counter}{ext}"
        dst = os.path.join(KNOWLEDGE_APPROVED_DIR, dst_name)
        counter += 1
    os.replace(src, dst)
    logger.info("📚 Статья одобрена: %s → %s", fname, dst)
    return dst_name


def delete_article(folder: str, fname: str) -> None:
    """Удаляет файл статьи. Из индекса RAG её уберёт следующий sync."""
    path = os.path.join(_dir_for(folder), _safe_fname(fname))
    os.remove(path)
    logger.info("📚 Статья удалена (%s): %s", folder, fname)


def add_article(file_name: str, text: str) -> str:
    """
    Добавляет новую статью сразу в папку одобренных (кнопка «➕ Добавить RAG»:
    файл загружает сам админ, поэтому этап ожидания не нужен). Имя берётся из
    присланного файла (безопасные символы, расширение приводится к .md,
    при совпадении имён добавляется суффикс). Возвращает итоговое имя файла.
    Векторизацию выполняет вызывающий код (services.rag.sync_knowledge_base).
    """
    base = os.path.basename(file_name or "article")
    base = re.sub(r'[\\/:*?"<>|]', "", base).strip()
    root = os.path.splitext(base)[0].strip() or "article"

    os.makedirs(KNOWLEDGE_APPROVED_DIR, exist_ok=True)
    fname = root + ".md"
    path = os.path.join(KNOWLEDGE_APPROVED_DIR, fname)
    counter = 2
    while os.path.exists(path):
        fname = f"{root}_{counter}.md"
        path = os.path.join(KNOWLEDGE_APPROVED_DIR, fname)
        counter += 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    logger.info("📚 Новая статья добавлена в базу знаний: %s", fname)
    return fname


def replace_article(folder: str, fname: str, new_text: str) -> None:
    """Полностью заменяет содержимое статьи (кнопка «Заменить» в панели)."""
    path = os.path.join(_dir_for(folder), _safe_fname(fname))
    if not os.path.exists(path):
        raise FileNotFoundError(f"Файл статьи не найден: {path}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text.strip() + "\n")
    logger.info("📚 Содержимое статьи заменено (%s): %s", folder, fname)
