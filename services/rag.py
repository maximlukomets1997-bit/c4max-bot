# ─────────────────────────────────────────────
#  rag.py — модуль RAG базы знаний
#
#  ИСТОЧНИК БАЗЫ (с июля 2026): папка KNOWLEDGE_APPROVED_DIR с файлами .md —
#  по одному файлу на статью. Файл может иметь шапку-метаданные (--- title/
#  tag/url/date ---, её пишет services/knowledge_store.py), заголовок «# …»
#  и секции «## …». Старый единый knowledge_base.md разрезан на файлы и
#  переименован в knowledge_base.md.bak (не используется).
#
#  ЧАНКИ: текст до первой секции «## » — отдельный чанк статьи; каждая
#  секция «## » — свой чанк. В текст для эмбеддинга всегда включается
#  название статьи («Статья: …»), чтобы секции с одинаковыми заголовками
#  («Способы получения» и т.п.) из разных статей не путались между собой.
#
#  ИНДЕКС: knowledge_base_vectors.json, формат 2 — по файлам, с mtime.
#  Синхронизация ПОШТУЧНАЯ: пересчитываются только новые/изменённые файлы,
#  записи удалённых файлов выкидываются. Благодаря этому sync_knowledge_base()
#  можно дёргать на лету (например, из админ-панели после одобрения статьи) —
#  это быстро и не требует перезапуска бота. Старый формат индекса (список)
#  распознаётся и приводит к полной пересборке.
# ─────────────────────────────────────────────

import os
import json
import math
import logging
import requests
# Соединения к моделям и сайтам переиспользуются (2026-07-27): «рукопожатие»
# TLS с сервером стоит 130–200 мс и раньше платилось на КАЖДЫЙ запрос.
# Подробности и запрет на повторы — в services/http.py.
from services.http import session as _http
import re
import time

from config import (
    RAG_ENABLED,
    RAG_EMBEDDING_MODEL,
    RAG_INDEX_FILE,
    RAG_TOP_K,
    RAG_MIN_SIMILARITY,
    RAG_CHUNK_MODE,
    RAG_LEX_BOOST,
    RAG_PEAK_MARGIN,
    RAG_STRONG_SIM,
    RAG_ICON,
    GEMINI_API_KEY,
    KNOWLEDGE_APPROVED_DIR,
)

logger = logging.getLogger(__name__)

# Загруженный в оперативную память индекс: список словарей с "title", "content", "vector"
_KNOWLEDGE_INDEX = []
# Сколько файлов-статей в загруженном индексе (для статистики панели /rag)
_KNOWLEDGE_FILES = 0
# Основы слов, встречающиеся в БОЛЬШИНСТВЕ чанков («танк», «ранг» и т.п.) —
# в лексическом бонусе не участвуют: они не различают статьи, а только
# раздувают баллы всем подряд. Пересчитывается при каждой загрузке индекса.
_COMMON_STEMS = set()


def cosine_similarity(v1: list, v2: list) -> float:
    """Вычисляет косинусное сходство двух векторов на чистом Python."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot_product / (norm_a * norm_b)


def _norm(vector: list) -> float:
    """Длина вектора. Считается ОДИН раз и запоминается — см. _load_into_memory."""
    return math.sqrt(sum(a * a for a in vector))


def _cosine_fast(v1: list, norm1: float, v2: list, norm2: float) -> float:
    """
    То же косинусное сходство, но длины векторов ПРИХОДЯТ ГОТОВЫМИ
    (2026-07-27, ускорение).

    Раньше поиск звал cosine_similarity, и та на каждую статью пересчитывала
    длину и вектора статьи, и вектора запроса — хотя длина статьи не меняется
    никогда, а длина запроса одна на весь поиск. Выходило втрое больше работы:
    замерено на 80 статьях по 3072 числа — 30.5 мс против 12.5 мс.

    Сам результат не меняется (та же формула), поэтому порог, «запас над фоном»
    и прочая калибровка поиска остаются верными — перемерять не надо.
    """
    if not v1 or not v2 or norm1 == 0.0 or norm2 == 0.0 or len(v1) != len(v2):
        return 0.0
    return sum(a * b for a, b in zip(v1, v2)) / (norm1 * norm2)


# Паузы (сек) перед повторами, когда Google отвечает 429 «слишком много
# запросов» — минутный лимит бесплатного тарифа. Только для индексации статей.
_INDEX_RETRY_DELAYS = (10, 30, 60)
# Пауза между статьями при массовой индексации — чтобы реже упираться в лимит.
# Наступили 2026-07-18: пересборка 56 статей подряд получила 429 после 36-й,
# индекс остался неполным (36 из 56). Полсекунды → секунда — решение Максима
# 2026-07-19, секунда → ТРИ — его же решение 2026-08-04: предупреждений о
# лимите Google в логе всё равно оставалось много (надёжность важнее скорости,
# пересборка идёт в фоне). Цена известна и принята: полная пересборка ~80
# статей занимает около 4 минут вместо полутора, а СТАРТОВАЯ синхронизация
# в main.py блокирующая — после массовой правки статей бот поднимется на
# столько же позже (по 3 сек за каждую изменённую статью).
_INDEX_PAUSE_SEC = 3.0


class RagQuotaError(Exception):
    """Google отвечает 429 даже после всех пауз — квота исчерпана не «на минуту»."""


def get_embedding(text: str, is_query: bool = False) -> list:
    """
    Запрашивает числовой вектор (эмбеддинг) для текста через нативный
    эндпоинт Gemini embedContent.

    task_type важен для качества поиска: запросы пользователя кодируются как
    RETRIEVAL_QUERY, а статьи базы знаний — как RETRIEVAL_DOCUMENT. Это
    оптимизирует геометрию векторов под асимметричный поиск (короткий запрос
    против длинного документа). Без этого разделения качество RAG падает.

    Ответ 429 «слишком много запросов»: при ИНДЕКСАЦИИ статей ждём и повторяем
    (паузы _INDEX_RETRY_DELAYS); если не отпустило и после последней паузы —
    бросаем RagQuotaError, чтобы синхронизация не мучила API впустую. Запросы
    ПОЛЬЗОВАТЕЛЕЙ (is_query=True) не ждут никогда: лучше ответ без базы знаний
    сейчас, чем с базой через минуту.
    """
    model = RAG_EMBEDDING_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
    payload = {
        "model": f"models/{model}",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
    }
    attempts = 1 if is_query else 1 + len(_INDEX_RETRY_DELAYS)
    for attempt in range(attempts):
        try:
            response = _http().post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            embedding = data.get("embedding", {}).get("values", [])
            if embedding:
                return embedding
            logger.error("⚠️ Ответ Gemini API не содержит embedding.values: %s", str(data)[:300])
            return []
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429 and not is_query:
                if attempt < attempts - 1:
                    delay = _INDEX_RETRY_DELAYS[attempt]
                    logger.warning("⚠️ Google просит притормозить (429) — пауза %d сек, потом повтор", delay)
                    time.sleep(delay)
                    continue
                raise RagQuotaError() from e
            logger.error("⚠️ Не удалось получить эмбеддинг от Gemini API: %s", e)
            return []
        except Exception as e:
            logger.error("⚠️ Не удалось получить эмбеддинг от Gemini API: %s", e)
            return []
    return []


# ─────────────────────────────────────────────
#  Разбор файлов статей и построение индекса
# ─────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def parse_article_file(file_path: str) -> list:
    """
    Разбирает один .md файл статьи на чанки для индексации.

    Понимает: шапку-метаданные (--- … ---), заголовок статьи («# …»),
    секции второго уровня («## …»). Текст до первой секции — отдельный
    чанк; каждая секция — свой чанк. Название статьи включается в текст
    каждого чанка для эмбеддинга.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Шапка-метаданные: берём из неё title, остальное для поиска не нужно
    title = ""
    m = _FRONTMATTER_RE.match(content)
    if m:
        tm = re.search(r"^title:\s*(.+)$", m.group(1), re.M)
        if tm:
            title = tm.group(1).strip()
        content = content[m.end():]
    content = content.strip()

    # Заголовок статьи «# …» первой строкой
    if content.startswith("# "):
        first_nl = content.find("\n")
        h1 = content[2:first_nl].strip() if first_nl != -1 else content[2:].strip()
        title = title or h1
        content = content[first_nl + 1:].strip() if first_nl != -1 else ""

    if not title:
        title = os.path.splitext(os.path.basename(file_path))[0]

    # Режим «1 файл = 1 чанк» (дефолт с 2026-07-05): статья уходит модели
    # целиком, включая заголовки «## » внутри текста. Статьи маленькие —
    # цельный вектор не размывается, а модель всегда видит полную картину.
    if RAG_CHUNK_MODE == "file":
        body = content.strip()
        if not body:
            return []
        return [{"title": title, "content": body, "full_text": f"Тема: {title}\n{body}"}]

    chunks = []

    def _add_chunk(section_title: str, body: str):
        body = body.strip()
        if not body:
            return
        # Название статьи всегда в тексте для эмбеддинга — секции с одинаковыми
        # заголовками из разных статей не должны путаться между собой.
        if section_title and section_title != title:
            display = f"{title} — {section_title}"
            full_text = f"Статья: {title}\nРаздел: {section_title}\n{body}"
        else:
            display = title
            full_text = f"Тема: {title}\n{body}"
        chunks.append({"title": display, "content": body, "full_text": full_text})

    sections = ("\n" + content).split("\n## ")
    _add_chunk("", sections[0])  # текст статьи до первой секции «## »
    for section in sections[1:]:
        lines = section.split("\n")
        _add_chunk(lines[0].strip(), "\n".join(lines[1:]))

    return chunks


def _load_index_file() -> dict:
    """
    Читает индекс с диска. Возвращает словарь {имя_файла: {mtime, chunks}}.
    Старый формат (список, до июля 2026) не совместим — возвращается пустой
    словарь, что приводит к полной пересборке.
    """
    if not os.path.exists(RAG_INDEX_FILE):
        return {}
    try:
        with open(RAG_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("format") == 2:
            # Индекс, собранный в другом режиме нарезки, не совместим —
            # пересобираем полностью (метку пишет _save_index_file)
            if data.get("chunk_mode", "sections") != RAG_CHUNK_MODE:
                logger.info("🚀 Режим нарезки изменился (%s → %s) — будет полная пересборка",
                            data.get("chunk_mode", "sections"), RAG_CHUNK_MODE)
                return {}
            return data.get("files", {})
        logger.info("🚀 Обнаружен индекс старого формата — будет полная пересборка")
        return {}
    except Exception as e:
        logger.error("⚠️ Не удалось прочитать векторный индекс: %s", e)
        return {}


def _save_index_file(files_index: dict) -> None:
    try:
        with open(RAG_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({"format": 2, "chunk_mode": RAG_CHUNK_MODE, "files": files_index}, f, ensure_ascii=False)
        logger.info("🚀 Векторный индекс сохранён: %s (файлов: %d)", RAG_INDEX_FILE, len(files_index))
    except Exception as e:
        logger.error("⚠️ Не удалось сохранить векторный индекс: %s", e)


# ─────────────────────────────────────────────
#  Лексический канал гибридного поиска: буквальное совпадение слов запроса
#  с текстом статьи. Прозвища («умка», «хамер») написаны в статьях —
#  совпадение слова даёт железный сигнал, не зависящий от капризов
#  модели эмбеддингов (введено 2026-07-05 после калибровки).
# ─────────────────────────────────────────────

_WORD_RE = re.compile(r"[а-яёa-z0-9][а-яёa-z0-9-]*")
_STRIP_ENDINGS = "аеёиоуыэюяйь"  # русские окончания: «умку/умка» → «умк»

# Слова-пустышки в запросах: не несут смысла, но разбавляют лексический бонус
# («у КАКОГО танка ЕСТЬ спайки» — бонус должен считаться от «танк»+«спайк»).
_QUERY_STOPWORDS = (
    "есть", "какой", "какая", "какое", "какие", "какого", "какому", "каком",
    "такой", "такая", "такое", "чего", "кого", "этого", "того",
    "можно", "нужно", "надо", "почему", "зачем", "сколько", "когда", "где",
)


def _stem(word: str) -> str:
    """Грубая основа слова: срезаем до двух гласных/ь/й с конца (не короче 3 букв)."""
    for _ in range(2):
        if len(word) > 3 and word[-1] in _STRIP_ENDINGS:
            word = word[:-1]
    return word


def _text_stems(text: str) -> set:
    """Множество основ слов текста (слова от 4 букв — короткие слишком шумные)."""
    return {_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) >= 4}


_QUERY_STOPSTEMS = {_stem(w) for w in _QUERY_STOPWORDS}


def _load_into_memory(files_index: dict) -> None:
    """
    Разворачивает индекс по файлам в плоский список чанков для поиска.
    Для каждого чанка на лету считается словарь основ слов (для лексического
    канала) и ДЛИНА ВЕКТОРА (2026-07-27: раньше её пересчитывал каждый поиск
    для каждой статьи, хотя она не меняется никогда — см. _cosine_fast);
    в файл индекса ни то, ни другое не пишется — только в память.
    """
    global _KNOWLEDGE_INDEX, _KNOWLEDGE_FILES, _COMMON_STEMS
    flat = []
    for entry in files_index.values():
        for chunk in entry.get("chunks", []):
            flat.append({
                "title": chunk["title"],
                "content": chunk["content"],
                "vector": chunk["vector"],
                "norm": _norm(chunk["vector"]),
                "stems": _text_stems(chunk["title"] + " " + chunk["content"]),
            })
    # Сверхчастые основы (в более чем половине чанков) исключаем из лексики
    counts = {}
    for chunk in flat:
        for s in chunk["stems"]:
            counts[s] = counts.get(s, 0) + 1
    _COMMON_STEMS = {s for s, n in counts.items() if n > max(1, len(flat) // 2)}
    _KNOWLEDGE_INDEX = flat
    _KNOWLEDGE_FILES = len(files_index)
    logger.info("🚀 База знаний RAG загружена: %d чанков из %d файлов (частых основ: %d)",
                len(flat), len(files_index), len(_COMMON_STEMS))


def sync_knowledge_base() -> tuple[int, int] | None:
    """
    Синхронизирует базу знаний с папкой одобренных статей KNOWLEDGE_APPROVED_DIR.

    Поштучно: эмбеддинги пересчитываются только для новых и изменённых
    файлов (по mtime), записи удалённых файлов выкидываются из индекса.
    После синхронизации индекс загружается в память. Безопасна для вызова
    из фонового потока; ошибка на одном файле не ломает остальные.

    Возвращает честный итог (проиндексировано, всего статей на диске) —
    его показывает панель /rag после пересборки; None — если RAG отключён.
    """
    if not RAG_ENABLED:
        logger.info("🚀 RAG отключён в конфигурации — синхронизация пропущена")
        return None

    os.makedirs(KNOWLEDGE_APPROVED_DIR, exist_ok=True)
    files_index = _load_index_file()

    files_on_disk = {}
    for fname in sorted(os.listdir(KNOWLEDGE_APPROVED_DIR)):
        if fname.lower().endswith(".md"):
            files_on_disk[fname] = os.path.getmtime(os.path.join(KNOWLEDGE_APPROVED_DIR, fname))

    changed = False
    did_embed = False  # была ли уже индексация в этом проходе (для паузы между статьями)

    # Новые и изменённые файлы
    for fname, mtime in files_on_disk.items():
        entry = files_index.get(fname)
        if entry and entry.get("mtime") == mtime:
            continue  # файл не менялся — вектора актуальны

        file_path = os.path.join(KNOWLEDGE_APPROVED_DIR, fname)
        try:
            chunks = parse_article_file(file_path)
        except Exception as e:
            logger.error("⚠️ Не удалось разобрать файл базы знаний %s: %s", fname, e)
            continue

        if not chunks:
            if fname in files_index:
                del files_index[fname]
                changed = True
            logger.warning("⚠️ Файл базы знаний %s пуст — пропущен", fname)
            continue

        # Пауза между статьями — чтобы серия запросов не влетала в минутный
        # лимит Google (см. _INDEX_PAUSE_SEC). Первая статья идёт без паузы.
        if did_embed:
            time.sleep(_INDEX_PAUSE_SEC)
        did_embed = True

        embedded = []
        failed = False
        try:
            for chunk in chunks:
                vector = get_embedding(chunk["full_text"])
                if not vector:
                    failed = True
                    break
                embedded.append({"title": chunk["title"], "content": chunk["content"], "vector": vector})
        except RagQuotaError:
            # Квота исчерпана надолго (не помогли даже паузы 10/30/60 сек) —
            # дальше дёргать API бессмысленно. Этот и оставшиеся файлы догонит
            # следующая синхронизация; старые вектора (если были) остаются.
            logger.error("⚠️ Не удалось получить вектора для %s — файл будет переиндексирован позже", fname)
            logger.error("⚠️ Лимит Google не отпустил даже после пауз — индексация остановлена досрочно, "
                         "недостающее догонит следующая синхронизация")
            break

        if failed:
            # Старую версию файла (если была) оставляем в индексе — лучше
            # устаревшие вектора, чем дыра в базе. Догоним при следующем sync.
            logger.error("⚠️ Не удалось получить вектора для %s — файл будет переиндексирован позже", fname)
            continue

        files_index[fname] = {"mtime": mtime, "chunks": embedded}
        changed = True
        logger.info("🚀 Статья проиндексирована: %s (чанков: %d)", fname, len(embedded))

    # Удалённые файлы
    for fname in [f for f in files_index if f not in files_on_disk]:
        del files_index[fname]
        changed = True
        logger.info("🚀 Статья удалена из индекса: %s", fname)

    if changed:
        _save_index_file(files_index)

    _load_into_memory(files_index)

    # Честный итог: сколько статей с диска реально имеют вектора в индексе.
    indexed = sum(1 for f in files_on_disk if f in files_index)
    return indexed, len(files_on_disk)


def index_lag() -> int:
    """
    Сколько статей требуют пересчёта векторов: новые/изменённые на диске
    плюс записи удалённых файлов, оставшиеся в индексе. 0 — индекс полный.

    Дёшево: только сравнение списков файлов и mtime, БЕЗ запросов к Google —
    ежечасный цикл добора (jobs.py::rag_catchup_loop) зовёт её каждый час.
    Правило «файл актуален» (есть в индексе И mtime совпадает) обязано
    совпадать с проверкой в sync_knowledge_base — меняешь одно, поправь второе.
    """
    if not RAG_ENABLED:
        return 0
    try:
        files_index = _load_index_file()
        lag = 0
        on_disk = set()
        for fname in os.listdir(KNOWLEDGE_APPROVED_DIR):
            if not fname.lower().endswith(".md"):
                continue
            on_disk.add(fname)
            entry = files_index.get(fname)
            mtime = os.path.getmtime(os.path.join(KNOWLEDGE_APPROVED_DIR, fname))
            if not entry or entry.get("mtime") != mtime:
                lag += 1
        lag += sum(1 for f in files_index if f not in on_disk)
        return lag
    except Exception as e:
        logger.error("⚠️ Не удалось проверить полноту индекса базы знаний: %s", e)
        return 0


def rebuild_knowledge_base() -> tuple[int, int] | None:
    """
    Полная пересборка индекса (кнопка «Пересобрать» в панели /rag):
    старый индекс удаляется целиком, вектора ВСЕХ одобренных статей
    считаются заново. Нужна, если индекс вызывает подозрения или после
    ручных правок файлов задним числом (mtime мог не измениться).

    Возвращает итог sync_knowledge_base (проиндексировано, всего) или
    None, если пересборка не запускалась.
    """
    if not RAG_ENABLED:
        logger.info("🚀 RAG отключён в конфигурации — пересборка пропущена")
        return None
    if os.path.exists(RAG_INDEX_FILE):
        try:
            os.remove(RAG_INDEX_FILE)
            logger.info("🚀 Старый векторный индекс удалён — начинаю полную пересборку")
        except Exception as e:
            logger.error("⚠️ Не удалось удалить старый векторный индекс: %s", e)
            return None
    return sync_knowledge_base()


def _live_rag_enabled() -> bool:
    """
    Живой выключатель базы знаний: кнопка «📖 База знаний» в панели /rag.
    Хранится в settings под ключом 'rag_enabled' ("1"/"0"), по умолчанию
    включена.

    ⚠️ НЕ ПУТАТЬ с RAG_ENABLED из config.py. Тот — жёсткий рубильник модуля
    (переменная окружения): при нём не грузится индекс и не работает даже
    диагностика, а меняется он только перезапуском бота. Этот — живой
    тумблер поверх него, переключается из Телеграма и действует сразу.
    Выключен любой из двух — база знаний в ответы не подмешивается.

    До 2026-07-27 тумблер был персональным (admin_no_rag_<id>) и глушил базу
    только в личке админа. Максим попросил сделать его общим: выключил —
    базы нет ни у кого, нигде.
    """
    try:
        from database.history import get_setting
        return get_setting("rag_enabled", "1") == "1"
    except Exception:
        return True


def is_active() -> bool:
    """
    Работает ли база знаний ПРЯМО СЕЙЧАС — все три условия сразу: жёсткий
    рубильник RAG_ENABLED, загруженный в память индекс и живой тумблер панели.

    Единственное место, где эти условия сведены вместе: retrieve_relevant_context
    спрашивает её же, так что разъехаться им не на чем — правило «тумблер
    проверяется в одной точке» этим не нарушено, а закреплено.

    Наружу нужна ради медиа (services/gemini.py): разбор фото, голосового или
    видео ради поискового запроса стоит отдельного похода к модели — секунды
    ожидания человека и деньги. Затевать его при выключенной базе незачем.
    """
    return bool(RAG_ENABLED and _KNOWLEDGE_INDEX and _live_rag_enabled())


def _live_top_k() -> int:
    """
    Живое число фрагментов в контекст: настраивается кнопками панели /rag,
    хранится в settings (строкой). При любой ошибке — дефолт из config.
    """
    try:
        from database.history import get_setting
        return max(1, min(10, int(get_setting("rag_top_k", str(RAG_TOP_K)))))
    except Exception:
        return RAG_TOP_K


def _live_min_similarity() -> float:
    """
    Живой порог сходства: настраивается кнопками панели /rag,
    хранится в settings (строкой). При любой ошибке — дефолт из config.
    """
    try:
        from database.history import get_setting
        return max(0.05, min(0.95, float(get_setting("rag_min_similarity", str(RAG_MIN_SIMILARITY)))))
    except Exception:
        return RAG_MIN_SIMILARITY


def _live_peak_margin() -> float:
    """
    Живой запас «пик над полкой»: настраивается кнопками панели /rag,
    хранится в settings (строкой). При любой ошибке — дефолт из config.

    Читать ОДИН раз на запрос и передавать в _chunk_passes параметром —
    не звать на каждый чанк (иначе чтение настройки на каждую статью базы).
    """
    try:
        from database.history import get_setting
        return max(0.0, min(0.30, float(get_setting("rag_peak_margin", str(RAG_PEAK_MARGIN)))))
    except Exception:
        return RAG_PEAK_MARGIN


def normalize_query(text: str) -> str:
    """
    Приводит текст запроса к нижнему регистру, удаляет знаки препинания и лишние пробелы.
    Это существенно повышает вероятность попадания в кэш эмбеддингов.
    """
    text = text.lower().strip()
    # Удаляем знаки препинания, оставляя только слова, пробелы и дефисы
    text = re.sub(r'[^\w\s-]', '', text)
    # Заменяем множественные пробельные символы на один пробел
    text = re.sub(r'\s+', ' ', text)
    return text


# Кэш векторов запросов: {нормализованный_текст: вектор}. Хранит ТОЛЬКО
# успешные ответы (лимит _QUERY_EMBED_CACHE_MAX записей ~ 1.5 МБ).
_QUERY_EMBED_CACHE: dict = {}
_QUERY_EMBED_CACHE_MAX = 128


def _get_cached_embedding(normalized_text: str, remember: bool = True) -> list:
    """
    Вектор запроса с кэшем. Неудача (пустой вектор из-за сбоя сети) НЕ
    запоминается — иначе временный сбой отключал бы базу знаний для этого
    вопроса до вытеснения записи из кэша. При переполнении кэша выкидывается
    самая старая запись (dict в Python хранит порядок вставки).

    remember=False — вектор возвращается, но в кэш не кладётся (запросы,
    собранные из разбора вложения: они уникальны и только вытесняли бы
    настоящие вопросы людей). Читать из кэша это не мешает — вдруг совпало.
    """
    cached = _QUERY_EMBED_CACHE.get(normalized_text)
    if cached is not None:
        return cached
    # ⚠️ ТЕКСТА ЗАПРОСА В ЛОГЕ НЕТ — решение Максима 2026-08-11. Строка
    # остаётся ради ЦЕНЫ: сюда доходят только запросы МИМО кэша, то есть
    # каждая такая строка — платный поход к Google. Не возвращать текст.
    logger.info("%s RAG: запрос эмбеддинга (%d символов)", RAG_ICON, len(normalized_text))
    vector = get_embedding(normalized_text, is_query=True)
    if vector and remember:
        if len(_QUERY_EMBED_CACHE) >= _QUERY_EMBED_CACHE_MAX:
            _QUERY_EMBED_CACHE.pop(next(iter(_QUERY_EMBED_CACHE)))
        _QUERY_EMBED_CACHE[normalized_text] = vector
    return vector


def _rank_chunks(query_vector: list, query_stems: set) -> tuple:
    """
    Гибридное ранжирование всех чанков.

    Балл чанка = косинусное сходство + лексический бонус (доля основ слов
    запроса, найденных в тексте чанка, × RAG_LEX_BOOST).
    Возвращает (список [(балл, сходство, лекс.бонус, чанк), …] по убыванию,
    медиана баллов) — медиана нужна правилу «пик против полки».
    """
    # Слова-пустышки выкидываем совсем; сверхчастые основы («танк» и т.п.)
    # не дают бонуса, но остаются в знаменателе — редкое слово в длинном
    # вопросе весит меньше, чем в коротком («умка» в «танк умка» = половина).
    meaningful = query_stems - _QUERY_STOPSTEMS
    rare_stems = meaningful - _COMMON_STEMS
    # Длина вектора запроса — один раз на весь поиск, а не на каждую статью.
    query_norm = _norm(query_vector)
    scored = []
    for chunk in _KNOWLEDGE_INDEX:
        sim = _cosine_fast(query_vector, query_norm, chunk["vector"], chunk["norm"])
        lex = 0.0
        if meaningful and rare_stems:
            lex = RAG_LEX_BOOST * len(rare_stems & chunk["stems"]) / len(meaningful)
        scored.append((sim + lex, sim, lex, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    mid = len(scored) // 2
    baseline = scored[mid][0] if len(scored) % 2 else (scored[mid - 1][0] + scored[mid][0]) / 2
    return scored, baseline


def _chunk_passes(score: float, baseline: float, floor: float, margin: float = None) -> tuple:
    """
    Решение по чанку: (проходит?, причина-строка для лога/диагностики).

    Правила (2026-07-05, калибровка на реальных запросах):
    1) балл ниже вспомогательного порога (панель /rag) — отказ;
    2) балл ≥ RAG_STRONG_SIM — безусловный пропуск (явное совпадение);
    3) иначе балл должен отрываться от медианы всех чанков на margin:
       у настоящего вопроса есть «пик» (одна статья заметно ближе прочих),
       у болтовни — «полка» (все статьи одинаково средне похожи).

    margin — живой запас из панели /rag (регулятор «📈 Запас над фоном»);
    None означает «взять живое значение», но вызывающему коду лучше прочитать
    его один раз на запрос и передать сюда (экономия чтений настройки).
    """
    if margin is None:
        margin = _live_peak_margin()
    if score < floor:
        return False, "ниже порога"
    if score >= RAG_STRONG_SIM:
        return True, "сильное совпадение"
    if score - baseline >= margin:
        return True, "пик над полкой"
    return False, "полка (нет пика)"


def retrieve_relevant_context(query: str, top_k: int = None, min_similarity: float = None,
                              remember_query: bool = True) -> str:
    """
    Гибридный поиск по базе знаний (смысл + буквальные слова + «пик против
    полки»). Возвращает строку с объединённым контекстом релевантных статей
    для добавления в системный промпт ИИ.

    Фильтра ключевых слов нет (убран 2026-07-05): в поиск идёт любой непустой
    запрос. Порог (вспомогательный) и число статей настраиваются кнопками
    панели /rag (settings), параметры функции — только для тестов.

    remember_query=False — не класть вектор запроса в кэш. Так ходят запросы,
    собранные из разбора вложения (services/gemini.py): они уникальны почти
    всегда, и кэш из 128 записей они бы вымыли за пару дней, сделав ПОВТОРНЫЕ
    ВОПРОСЫ ЛЮДЕЙ снова платными.
    """
    # Общий тумблер из панели /rag (плюс рубильник и пустой индекс) — всё это
    # спрашивается у rag.is_active(). Проверка стоит ЗДЕСЬ, в единственной
    # точке входа поиска, — тогда её слушаются ВСЕ, кто подмешивает базу: и
    # обычные ответы, и режим «Сам в разговор», и всё, что появится потом.
    # Разносить эту проверку по вызывающим нельзя: разъедется. Снаружи можно
    # только СПРОСИТЬ is_active() — так делает разбор медиа, чтобы не платить
    # за описание при погашенной базе.
    # Диагностику («🔍 Проверить поиск») тумблер НЕ глушит — она в test_search.
    if not is_active():
        # Раньше пустой индекс уходил отсюда МОЛЧА, и «почему в логе ничего
        # нет» приходилось выяснять вручную (разбор с Максимом 16.08.2026).
        logger.info("%s База знаний сейчас не работает (рубильник, пустой индекс "
                    "или тумблер панели) — поиск пропущен", RAG_ICON)
        return ""
    if top_k is None:
        top_k = _live_top_k()
    if min_similarity is None:
        min_similarity = _live_min_similarity()

    # Нормализуем запрос перед поиском в кэше
    norm_query = normalize_query(query)
    if not norm_query:
        logger.info("%s После нормализации запрос пуст — семантический поиск пропущен", RAG_ICON)
        return ""

    # Получаем эмбеддинг запроса пользователя из кэша (или через API с автоматической записью в кэш)
    query_vector = _get_cached_embedding(norm_query, remember=remember_query)
    if not query_vector:
        logger.warning("⚠️ Не удалось получить эмбеддинг запроса — контекст пуст")
        return ""

    scored, baseline = _rank_chunks(query_vector, _text_stems(norm_query))
    margin = _live_peak_margin()  # один раз на запрос, не на каждую статью

    relevant_contexts = []
    chosen_titles = []
    for score, sim, lex, chunk in scored[:top_k]:
        ok, reason = _chunk_passes(score, baseline, min_similarity, margin)
        if ok:
            logger.info("%s Найдена статья (балл %.3f = смысл %.3f + слова %.3f, %s): %s",
                        RAG_ICON, score, sim, lex, reason, chunk["title"])
            chosen_titles.append(chunk["title"])
            relevant_contexts.append(
                f"=== РАЗДЕЛ: {chunk['title']} ===\n{chunk['content']}"
            )
        else:
            logger.info("%s Статья отсеяна (балл %.3f, медиана %.3f, %s): %s",
                        RAG_ICON, score, baseline, reason, chunk["title"])

    if not relevant_contexts:
        return ""

    # Итоговая строка: ЧТО именно уехало модели. «Найдена статья» выше говорит
    # лишь о том, что статья прошла отбор, — а по этой строке видно готовый
    # состав подсказки, без гадания (просьба Максима 2026-07-27).
    logger.info("%s Модели отправлены статьи (%d): %s",
                RAG_ICON, len(chosen_titles), ", ".join(chosen_titles))

    return "\n\n".join(relevant_contexts)


def test_search(query: str) -> dict:
    """
    Диагностика поиска для кнопки «🔍 Проверить поиск» панели /rag.

    Возвращает словарь: top_k, threshold, baseline (медиана-«полка»),
    results — лучшие статьи с баллом (similarity = итог, lex = вклад слов),
    отметкой passes и причиной reason; либо error с человекочитаемой
    причиной. Делает сетевой вызов (эмбеддинг) — вызывать из рабочего потока.
    """
    top_k = _live_top_k()
    threshold = _live_min_similarity()
    out = {"top_k": top_k, "threshold": threshold, "results": [], "baseline": 0.0}

    if not RAG_ENABLED:
        out["error"] = "RAG выключен (RAG_ENABLED в .env)"
        return out
    if not _KNOWLEDGE_INDEX:
        out["error"] = "Индекс базы знаний пуст — нажмите «🔄 Пересобрать RAG»"
        return out
    norm_query = normalize_query(query)
    if not norm_query:
        out["error"] = "После очистки от знаков препинания вопрос пуст"
        return out
    query_vector = _get_cached_embedding(norm_query)
    if not query_vector:
        out["error"] = "Не удалось получить эмбеддинг запроса (детали в логе)"
        return out

    scored, baseline = _rank_chunks(query_vector, _text_stems(norm_query))
    out["baseline"] = baseline
    margin = _live_peak_margin()
    out["margin"] = margin
    # Показываем чуть больше, чем уйдёт модели: видно, что «на грани»
    for i, (score, sim, lex, chunk) in enumerate(scored[:max(top_k, 5)]):
        ok, reason = _chunk_passes(score, baseline, threshold, margin)
        out["results"].append({
            "similarity": score,
            "lex": lex,
            "title": chunk["title"],
            "passes": i < top_k and ok,
            "reason": reason,
        })
    return out
