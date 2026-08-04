# ─────────────────────────────────────────────
#  services/tech_card.py — 📊 справочник техники ПО БАЗЕ ЗНАНИЙ, БЕЗ НЕЙРОСЕТИ
#  (2026-08-04)
#
#  Зачем: в knowledge/approved лежат десятки эталонных статей, у каждой блок
#  «## Характеристики (ТТХ)» с готовой таблицей. Достать оттуда ТТХ можно было
#  только вопросом к модели — это секунды ожидания, токены и риск, что модель
#  перескажет цифры своими словами. Здесь ответ отдаётся ИЗ ФАЙЛА: мгновенно,
#  бесплатно и слово в слово так, как написал владелец.
#
#  Контракт:
#  • Модуль НЕ ходит в сеть сам. Единственная сетевая ступень поиска —
#    `rag.test_search`, и её зовёт вызывающий (handlers/tech.py) в отдельном
#    потоке. Всё остальное — чтение файлов.
#  • Модуль «тихий» в том же смысле, что knowledge_store: нечитаемая статья
#    не роняет поиск, а просто выпадает из выдачи.
#  • Разметку Telegram собираем ТОЛЬКО в HTML (`parse_mode`), без entities:
#    смешивать их нельзя, а карточка — панельный текст со своей клавиатурой.
#
#  ⚠️ ТУМБЛЕР «📖 База знаний» (settings rag_enabled) СПРАВОЧНИКА НЕ КАСАЕТСЯ.
#  Он про подмешивание статей в промпт модели, а тут человек явно попросил
#  справку — ровно так же устроена диагностика «🔍 Проверить поиск». Не
#  «чинить» добавлением проверки: команда молча замолчит при выключенном
#  тумблере, и понять почему будет неоткуда.
# ─────────────────────────────────────────────

import difflib
import html
import logging
import os
import re

from services.knowledge_store import ARTICLE_KINDS, list_articles

logger = logging.getLogger(__name__)

_ICON = "📊"

# Сколько кандидатов предлагаем, когда точного совпадения нет.
CANDIDATES = 3

# Потолок текста карточки. У Telegram 4096; запас — на случай, если в статье
# окажется таблица ТТХ длиннее обычной (обрежем список, а не потеряем ответ).
_TEXT_LIMIT = 3800

# Разделитель панелей — 27 знаков «─» (U+2500), общий стандарт проекта.
SEPARATOR = "─" * 27


# ─── разбор статьи ──────────────────────────────────────────────────

def _inline_html(text: str) -> str:
    """
    Строка markdown статьи → HTML Telegram.

    Порядок важен: СНАЧАЛА экранируем (иначе «<» из текста статьи порвёт
    разметку и сообщение не отправится вовсе), и только потом расставляем
    теги. Жирное разбираем до курсивного — иначе `**` съест одиночная звёздочка.
    """
    # quote=False: Telegram требует экранировать только «<», «>» и «&».
    # С кавычками текст статьи превратился бы в «&quot;Дрозд&quot;».
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)\*(?!\*)", r"<i>\1</i>", out)
    # Экранирование самого markdown («C4\_Max») в Telegram не нужно.
    out = re.sub(r"\\([_*\[\]()#+\-.!])", r"\1", out)
    return out


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") and line.strip().endswith("|")


def _table_cells(line: str) -> list:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _body_to_html(body: str) -> str:
    """
    Тело раздела статьи → HTML.

    Таблицы Telegram не рисует вовсе, поэтому строка таблицы превращается
    в «• <b>Название:</b> значение» — это и есть основная работа функции.
    Строка-разделитель («|-|-|») и шапка («Характеристика | Значение»)
    выбрасываются: в списке из двух колонок они не значат ничего.
    """
    lines = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            # Пустые строки схлопываем: в статье они разделяют абзацы, а в
            # сообщении двойные отступы съедают экран телефона.
            if lines and lines[-1] != "":
                lines.append("")
            continue

        if _is_table_row(line):
            cells = _table_cells(line)
            if all(re.fullmatch(r":?-{1,}:?", c) for c in cells if c):
                continue  # разделитель шапки
            if len(cells) >= 2:
                name = _inline_html(cells[0]).strip()
                value = _inline_html(" · ".join(cells[1:])).strip()
                # Шапка таблицы — единственная строка без выделения в названии
                if name.lower() in ("характеристика", "параметр") :
                    continue
                if not re.search(r"</?b>", name):
                    name = f"<b>{name}</b>"
                lines.append(f"• {name}: {value}")
            else:
                lines.append(_inline_html(cells[0]))
            continue

        stripped = line.lstrip()
        if re.fullmatch(r"([-*_])\1{2,}", stripped):
            # Горизонтальная черта markdown → общий разделитель панелей.
            lines.append(SEPARATOR)
            continue
        if stripped.startswith(("* ", "- ")):
            lines.append("• " + _inline_html(stripped[2:]))
        else:
            lines.append(_inline_html(line))
    out = "\n".join(lines).strip()
    # Черта в самом конце раздела ничего не отделяет — только съедает экран.
    while out.endswith(SEPARATOR):
        out = out[:-len(SEPARATOR)].strip()
    return out


def _split_sections(text: str) -> tuple[str, list]:
    """
    Статья → (описание до первого «## », список разделов [(заголовок, тело)]).

    Шапка YAML (`---` … `---`, есть у статей об игровых механиках) и строка
    «# Название» в описание не попадают: заголовок мы и так знаем, а шапка —
    служебная.
    """
    body = text
    if body.lstrip().startswith("---"):
        parts = body.lstrip().split("---", 2)
        if len(parts) == 3:
            body = parts[2]

    chunks = re.split(r"^##\s+", body, flags=re.M)
    head = chunks[0]
    head = re.sub(r"^#\s+.*$", "", head, count=1, flags=re.M).strip()

    sections = []
    for chunk in chunks[1:]:
        title, _, rest = chunk.partition("\n")
        sections.append((title.strip(), rest.strip()))
    return head, sections


# Как называется кнопка раздела. Ищем ПОДСТРОКУ в заголовке раздела, а не
# точное совпадение: заголовки в статьях слегка разные («Вооружение» и
# «Вооружение и стрельба», «Уязвимые места и защита»), а правило одно.
# Не нашлось ничего — кнопка соберётся из самого заголовка (значок 📄).
_SECTION_BUTTONS = (
    ("характеристик", "📊", "ТТХ"),
    ("сильные",       "⚔️", "Плюсы и минусы"),
    ("уязвим",        "🎯", "Куда пробивать"),
    ("вооружен",      "🔫", "Вооружение"),
    ("живуч",         "🛡", "Живучесть"),
    ("тактик",        "🧠", "Тактика"),
    ("видеообзор",    "🎬", "Видеообзор"),
)


def section_label(title: str) -> str:
    """Короткая надпись кнопки для раздела статьи."""
    low = title.lower()
    for needle, icon, label in _SECTION_BUTTONS:
        if needle in low:
            return f"{icon} {label}"
    return "📄 " + " ".join(title.split()[:2])[:16]


def is_specs(title: str) -> bool:
    """Это раздел с таблицей ТТХ (он показывается сразу, своей кнопки не имеет)."""
    return "характеристик" in title.lower()


# ─── поиск статьи ───────────────────────────────────────────────────

# Кириллические буквы, неотличимые НА ВИД от латинских. Названия техники люди
# набирают вперемешку, а в самой базе рядом лежат «Т-80УМ2» (кириллица) и
# «Type 90» (латиница). Без этого свода запрос «t-80» не находил ни одного
# Т-80: в подсказки попадал F-80C-10 — самолёт вместо трёх танков.
# Свод применяется К ОБЕИМ сторонам сравнения, поэтому ничего не ломает:
# «Type 90» и «тайп 90» просто сходятся в одном ключе.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ѕ": "s",
})


def _norm(text: str) -> str:
    """
    Ключ для сравнения названий: «Т-80У-Е1» и «т80уе1» обязаны совпасть.
    Выкидываем всё, кроме букв и цифр, приравниваем «ё» к «е» и складываем
    кириллические двойники с латиницей (см. _HOMOGLYPHS) — «t-80ум2» и
    «Т-80УМ2» обязаны быть одним и тем же запросом.
    """
    text = (text or "").lower().replace("ё", "е").translate(_HOMOGLYPHS)
    return re.sub(r"[^0-9a-zа-я]+", "", text)


_NICK_RE = re.compile(r"игровые\s+прозвища:\s*([^)]+)", re.I)

# Разобранный список статей (список словарей) и отпечаток папки, по которому
# понятно, что его пора собрать заново. Пересборка читает по 2 КБ из каждой
# статьи — на каждую команду это лишнее, а раз в изменение папки в самый раз.
_cache: list = []
_cache_stamp = None


def _folder_stamp():
    """
    Дешёвый отпечаток папки одобренных статей: (число файлов, max mtime).
    Путь берём у knowledge_store — второй сборки пути не заводим, иначе после
    переезда папки справочник молча начнёт смотреть не туда.
    """
    from services.knowledge_store import _dir_for
    try:
        newest, count = 0.0, 0
        with os.scandir(_dir_for("approved")) as it:
            for entry in it:
                if entry.name.lower().endswith(".md"):
                    count += 1
                    newest = max(newest, entry.stat().st_mtime)
        return count, newest
    except Exception:
        return None


def index() -> list:
    """
    Список одобренных статей с ключами поиска. Каждый элемент:
    {"fname", "title", "path", "kind", "keys": {…нормализованные названия…}}.

    Ключи — само название, имя файла и ИГРОВЫЕ ПРОЗВИЩА из первой строки
    описания («игровые прозвища: уе1, т80уе1»): люди спрашивают именно так,
    и без прозвищ справочник отвечал бы «не нашёл» на половину запросов.
    """
    global _cache, _cache_stamp
    stamp = _folder_stamp()
    if _cache and stamp is not None and stamp == _cache_stamp:
        return _cache

    items = []
    for art in list_articles():
        if art["folder"] != "approved":
            continue
        keys = {_norm(art["title"]), _norm(os.path.splitext(art["fname"])[0])}
        nicks = []
        try:
            with open(art["path"], "r", encoding="utf-8") as f:
                head = f.read(2000)
            m = _NICK_RE.search(head)
            if m:
                nicks = [n.strip() for n in m.group(1).split(",") if n.strip()]
        except Exception as e:
            logger.debug("%s Не удалось прочитать статью %s: %s", _ICON, art["fname"], e)
        keys.update(_norm(n) for n in nicks)
        keys.discard("")
        items.append({**art, "keys": keys, "nicks": nicks})

    _cache, _cache_stamp = items, stamp
    return items


def find_local(query: str) -> tuple:
    """
    Поиск ПО НАЗВАНИЯМ, без сети и без нейросети. Возвращает
    (статья или None, список кандидатов).

    Три ступени, от строгой к мягкой: точное совпадение ключа → название
    начинается с запроса или содержит его → похожие названия (difflib).
    Ровно один кандидат на второй ступени считается попаданием: «леопард»
    обязан открывать Leopard 2A5, а не спрашивать «вы имели в виду».
    """
    q = _norm(query)
    if not q:
        return None, []
    items = index()

    for art in items:
        if q in art["keys"]:
            return art, []

    partial = [a for a in items if any(q in k for k in a["keys"])]
    if len(partial) == 1:
        return partial[0], []
    if partial:
        return None, partial[:CANDIDATES]

    # Ничего похожего по подстроке — ищем на опечатки.
    flat = {}
    for art in items:
        for k in art["keys"]:
            flat.setdefault(k, art)
    close = difflib.get_close_matches(q, list(flat), n=CANDIDATES, cutoff=0.62)
    seen, out = set(), []
    for k in close:
        art = flat[k]
        if art["fname"] not in seen:
            seen.add(art["fname"])
            out.append(art)
    return None, out


def suggest(query: str, limit: int = 5) -> list:
    """
    Список подходящих статей — для инлайн-режима и кнопок «возможно, ты имел
    в виду». Порядок: точное совпадение, потом вхождение подстроки, потом
    похожие по написанию. Пустой запрос — начало общего списка (он уже
    отсортирован по типам техники в knowledge_store).
    """
    items = index()
    q = _norm(query)
    if not q:
        return items[:limit]

    out, seen = [], set()

    def add(art):
        if art["fname"] not in seen:
            seen.add(art["fname"])
            out.append(art)

    for art in items:
        if q in art["keys"]:
            add(art)
    for art in items:
        if any(q in k for k in art["keys"]):
            add(art)
    if len(out) < limit:
        flat = {}
        for art in items:
            for k in art["keys"]:
                flat.setdefault(k, art)
        for k in difflib.get_close_matches(q, list(flat), n=limit, cutoff=0.62):
            add(flat[k])
    return out[:limit]


def by_title(title: str):
    """Статья по точному названию — так результат поиска RAG превращается в статью."""
    key = _norm(title)
    for art in index():
        if key in art["keys"]:
            return art
    return None


def token(fname: str) -> str:
    """
    Короткий ключ статьи для callback_data (у Telegram на неё 64 байта, а имена
    файлов туда не влезают).

    ⚠️ ВЫЧИСЛЯЕТСЯ ИЗ ИМЕНИ ФАЙЛА, а не выдаётся из словаря в памяти, — этим
    справочник отличается от панели /rag (`kb_file_map` в bot_data). Там после
    перезапуска бота карта пуста и старые кнопки «протухают»; здесь тот же
    ключ считается заново, и кнопки под старой карточкой работают всегда.
    """
    import hashlib
    return hashlib.md5(fname.encode("utf-8")).hexdigest()[:10]


def by_token(tok: str):
    """Статья по короткому ключу из callback_data (см. token)."""
    for art in index():
        if token(art["fname"]) == tok:
            return art
    return None


# ─── сборка сообщений ───────────────────────────────────────────────

def load(article: dict) -> dict:
    """Читает статью с диска: {"title", "head", "sections": [(заголовок, тело)]}."""
    with open(article["path"], "r", encoding="utf-8") as f:
        text = f.read()
    head, sections = _split_sections(text)
    return {"title": article["title"], "head": head, "sections": sections}


def kind_icon(kind: str) -> str:
    """Значок типа техники (🚜 / ✈️ / 🛥️ / ⚓ / 📘) — из общего ARTICLE_KINDS."""
    return ARTICLE_KINDS.get(kind, ARTICLE_KINDS["other"])["icon"]


def _cut(text: str, limit: int = _TEXT_LIMIT) -> str:
    """Обрезает по границе строки, честно помечая обрыв."""
    if len(text) <= limit:
        return text
    keep = text[:limit].rsplit("\n", 1)[0]
    return keep + "\n\n<i>…дальше не поместилось — жми «Вся статья файлом».</i>"


def render_card(article: dict, data: dict) -> str:
    """
    Главный экран: значок типа, название, описание из статьи и таблица ТТХ
    строками. Ничего своего не сочиняем — весь текст взят из статьи.
    """
    icon = kind_icon(article["kind"])
    parts = [f"{icon} <b>{html.escape(data['title'])}</b>"]
    if data["head"]:
        parts.append(_body_to_html(data["head"]))

    specs = next((body for title, body in data["sections"] if is_specs(title)), "")
    if specs:
        # Разделитель приклеиваем к самому списку ТТХ, а не отдельной частью:
        # иначе между чертой и первой строкой повисает пустая строка.
        parts.append(SEPARATOR + "\n" + _body_to_html(specs))
    return _cut("\n\n".join(p for p in parts if p))


def render_section(article: dict, data: dict, index_: int) -> str:
    """Экран одного раздела статьи."""
    title, body = data["sections"][index_]
    icon = kind_icon(article["kind"])
    return _cut(
        f"{icon} <b>{html.escape(data['title'])}</b>\n"
        f"<b>{html.escape(title)}</b>\n"
        f"{SEPARATOR}\n"
        + _body_to_html(body)
    )


def render_candidates(query: str, candidates: list) -> str:
    """Текст ответа, когда точного совпадения не нашлось."""
    if candidates:
        return (f"🔍 По запросу «<b>{html.escape(query)}</b>» точного совпадения нет.\n"
                f"Возможно, ты имел в виду:")
    return (f"🔍 По запросу «<b>{html.escape(query)}</b>» в базе знаний ничего нет.\n"
            f"<i>Всего статей: {len(index())}. Попробуй другое название — "
            f"понимаю и прозвища, и латиницу, и кириллицу.</i>")
