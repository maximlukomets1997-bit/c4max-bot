"""
Сборка вопросов викторины по статьям базы знаний (2026-08-05, решение Максима).

ЗАЧЕМ. Вопросы викторины раньше лежали списком В КОДЕ (data/quiz_questions.py,
12 штук на весь бот) — их выучивали за пару вечеров, и любые рейтинги
превращались в состязание памяти о двенадцати строках. Здесь они собираются по
тем же статьям, что уже лежат в базе знаний: 80 статей о технике War Thunder
Mobile — это сотни честных вопросов, и каждая новая статья добавляет свои.

КАК УСТРОЕНО. Этот модуль ходит в модель и НИЧЕГО не знает ни про Telegram, ни
про кнопки: на вход — статьи, на выход — строки в таблице quiz_bank со статусом
«черновик». Показывает и одобряет их панель /quizadm
(handlers/admin/panel_quiz.py), в игру берёт handlers/quiz.py.

⚠️ РАБОТА ЗДЕСЬ СИНХРОННАЯ И ДОЛГАЯ (минуты на десяток статей): звать только
через run_in_executor, иначе бот замрёт на всё время сборки. Так же сделана
пересборка базы знаний в панели /rag.
"""

import json
import logging
import os
import re

from config import (QUIZ_EXPLANATION_MAX, QUIZ_GEN_MAX_ARTICLES, QUIZ_GEN_PER_ARTICLE,
                    QUIZ_OPTION_MAX, QUIZ_OPTIONS_COUNT, QUIZ_QUESTION_MAX)
from database.history import (add_quiz_question, clear_quiz_failure, count_quiz_failures,
                              get_quiz_articles_covered, list_quiz_failures, note_quiz_failure,
                              set_quiz_question_approved)
from services import knowledge_store

logger = logging.getLogger(__name__)

# Сколько текста статьи отдаём модели. Статьи базы знаний короткие (3–6 тыс.
# знаков), но в неё может приехать простыня, а платить за неё незачем.
_ARTICLE_CHARS_MAX = 8000

# Системный промпт сборки. ⚠️ Живёт ЗДЕСЬ, в коде, а НЕ в настройках бота
# (в отличие от промптов новостей, RAG и «Сам в разговор», которые Максим
# правит командами): его задача — не тон ответа, а СТРОГИЙ ФОРМАТ JSON, и
# случайная правка формата из панели ломала бы разбор молча.
_SYSTEM_PROMPT = """Ты составляешь вопросы для викторины по игре War Thunder Mobile.

Тебе дают статью базы знаний об одной единице техники или игровой механике.
Составь по ней {count} вопроса с четырьмя вариантами ответа.

ЖЁСТКИЕ ПРАВИЛА:
1. Ответ на вопрос ОБЯЗАН содержаться в тексте статьи. Ничего не додумывай:
   если в статье нет цифры — не спрашивай про неё.
2. Ровно {options} варианта ответа, верный ровно один. Неверные должны быть
   правдоподобными, но однозначно неверными по тексту статьи.
3. Вопрос — не длиннее {q_max} знаков, вариант ответа — не длиннее {o_max},
   разбор — не длиннее {e_max} знаков. Это ограничения Telegram, не пожелание.
4. Разбор пиши в стиле бывалого командира, коротко и по делу. Он объясняет,
   ПОЧЕМУ верный ответ верен. Начинай сразу с сути, без вступлений и
   обращений.
5. Вопрос должен быть понятен без статьи: упоминай технику по названию, а не
   «эта машина» или «данный корабль».
6. Пиши только по-русски.

Ответь ТОЛЬКО массивом JSON, без пояснений и без разметки кода:
[{{"question": "...", "options": ["...", "...", "...", "..."], "correct_idx": 0, "explanation": "..."}}]"""

# ⚠️ ДЕЖУРНОЙ ШАПКИ У РАЗБОРА БОЛЬШЕ НЕТ (2026-08-21, решение Максима). До этого
# правило 4 требовало начинать разбор словами «Рапорт Полковника:», и она стояла
# у всех 219 вопросов — в файле, в банке и во всплывающем пояснении Telegram.
# Максим её убрал: в коротком разборе на 200 знаков 19 из них уходили впустую.
# Не возвращать «для красоты» — вернётся только в новых вопросах, и набор
# разъедется пополам.


def _article_title(text: str) -> str:
    """Заголовок статьи (первая строка «# …») — для промпта и логов."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:80]
    return ""


def _looks_like_questions(data) -> bool:
    """Похоже ли разобранное на список вопросов (а не на случайный массив)."""
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        return False
    return any(isinstance(item, dict) and item.get("question") for item in data)


def _extract_json(body: str) -> list | None:
    """
    Достаёт массив вопросов из ответа модели.

    ⚠️ ГЛАВНОЕ, НА ЧЁМ ЗДЕСЬ УЖЕ НАСТУПИЛИ (2026-08-05, боевая сборка на
    qwen3.7-max): у ДУМАЮЩИХ моделей ответ приходит как
    «<thought>рассуждения</thought>\\nответ» (см. gemini._openai_stream_request),
    и в рассуждениях почти всегда есть свои скобки — модель вслух прикидывает
    варианты. Прежний разбор «от первой [ до последней ]» захватывал кусок
    размышлений и падал на трети статей, причём деньги за запрос уже были
    потрачены. Поэтому: сначала СРЕЗАЕМ блок размышлений, а потом ищем JSON
    не одним срезом, а перебором — json.JSONDecoder().raw_decode пробуется в
    КАЖДОЙ позиции, где начинается «[» или «{», и берётся первый разобранный
    кусок, который похож на список вопросов. Обёртка ```json при таком разборе
    перестаёт что-либо значить сама собой.
    """
    body = (body or "").strip()
    if not body:
        return None

    # Блок размышлений вырезаем целиком; незакрытый <thought> (ответ обрезан
    # на середине) — вместе со всем хвостом, годного JSON там всё равно нет.
    body = re.sub(r"<thought>.*?</thought>", " ", body, flags=re.DOTALL)
    body = re.sub(r"<thought>.*$", " ", body, flags=re.DOTALL).strip()
    if not body:
        return None

    decoder = json.JSONDecoder()
    for idx, char in enumerate(body):
        if char != "[":
            continue
        try:
            data, _end = decoder.raw_decode(body[idx:])
        except ValueError:
            continue
        if isinstance(data, list) and _looks_like_questions(data):
            return data
        # ⚠️ Разобравшийся, но НЕ похожий на вопросы массив пропускаем молча и
        # ищем дальше. Первым таким почти всегда оказывается список вариантов
        # ответа («options») внутри вопроса или перечисление из рассуждений —
        # вернуть его значило бы отдать мусор вместо вопросов и получить в
        # причине неудачи «все вопросы отбракованы» вместо честного «не
        # разобрать ответ».

    # ⚠️ ПОСЛЕДНИЙ ЗАХОД — ПО ОТДЕЛЬНЫМ ОБЪЕКТАМ (2026-08-05, по итогам сборки,
    # где 11 статей из 40 остались без результата). Массив целиком мог не
    # разобраться по двум причинам: ответ ОБОРВАЛСЯ на середине (ни `max_tokens`,
    # ни `finish_reason` мы не задаём и не проверяем, а думающая модель пишет
    # длинно) или модель насыпала объекты без обрамляющих скобок. Спасать тут
    # есть что: первые вопросы в таком ответе целые и годные, а мы выбрасывали
    # весь оплаченный запрос целиком.
    salvaged = []
    pos = 0
    while True:
        start = body.find("{", pos)
        if start == -1:
            break
        try:
            item, end = decoder.raw_decode(body[start:])
        except ValueError:
            pos = start + 1
            continue
        pos = start + end
        if isinstance(item, dict) and item.get("question"):
            salvaged.append(item)
    return salvaged or None


def _clean_question(item: dict) -> dict | None:
    """
    Проверяет один вопрос от модели и приводит его к виду, годному для
    send_poll. Возвращает None, если вопрос негоден — молча выбрасываем:
    у сборки на входе десятки статей, и один кривой ответ не повод ронять всю.

    Что проверяем (по опыту прогонов — каждое правило поймано на живых ответах):
      • есть текст вопроса и ровно QUIZ_OPTIONS_COUNT вариантов;
      • варианты не повторяются (модель любит дать два одинаковых);
      • correct_idx — число внутри списка (приходило и строкой, и «1.»);
      • длины укладываются в лимиты Telegram, иначе опрос не отправится.
    """
    if not isinstance(item, dict):
        return None

    question = str(item.get("question") or "").strip()
    explanation = str(item.get("explanation") or "").strip()
    options = item.get("options")
    if not question or not isinstance(options, list):
        return None

    options = [str(o).strip() for o in options if str(o).strip()]
    if len(options) != QUIZ_OPTIONS_COUNT:
        return None
    if len({o.lower() for o in options}) != len(options):
        return None

    # correct_idx приходил и числом, и строкой «2», и даже «2.» — берём первое
    # целое число из строкового представления.
    raw_idx = item.get("correct_idx", item.get("correct"))
    match = re.search(r"\d+", str(raw_idx))
    if not match:
        return None
    correct_idx = int(match.group())
    if not 0 <= correct_idx < len(options):
        return None

    if len(question) > QUIZ_QUESTION_MAX:
        return None
    if any(len(o) > QUIZ_OPTION_MAX for o in options):
        return None
    # Разбор — единственное, что режем, а не выбрасываем: смысл вопроса он не
    # меняет, а модель регулярно переваливает за 200 знаков на пару слов.
    if len(explanation) > QUIZ_EXPLANATION_MAX:
        explanation = explanation[:QUIZ_EXPLANATION_MAX - 1].rstrip() + "…"

    return {
        "question": question,
        "options": options,
        "correct_idx": correct_idx,
        "explanation": explanation,
    }


def articles_without_questions() -> list[dict]:
    """
    Одобренные статьи базы знаний, по которым вопросов ещё нет вовсе.

    ⚠️ Берём ТОЛЬКО папку approved: статьи в pending ещё не приняты Максимом,
    и собирать вопросы по тому, что может быть переписано или удалено, —
    выбрасывать деньги и получать вопросы про несуществующую технику.
    """
    covered = get_quiz_articles_covered()
    return [a for a in knowledge_store.list_articles()
            if a["folder"] == "approved" and a["fname"] not in covered]


def generate_for_article(article: dict, per_article: int = None) -> tuple[list, str]:
    """
    Собирает вопросы по ОДНОЙ статье и кладёт их в банк черновиками.

    Возвращает пару (записанные вопросы, причина неудачи). Причина — короткая
    человеческая строка («модель не ответила», «не разобрать ответ», …) или
    пустая, если всё вышло. ⚠️ Возвращать просто пустой список, как было до
    2026-08-05, оказалось мало: 11 статей из 40 остались без вопросов, и по
    итогу нельзя было понять НИ какие именно, НИ почему — а значит, и повторить
    было нечем. Причину пишет вызывающий (`generate_batch`) в таблицу
    `quiz_failed`, откуда её берёт кнопка «🔁 Повторить неудачные».
    """
    per_article = per_article or QUIZ_GEN_PER_ARTICLE
    try:
        _, text = knowledge_store.read_article(article["folder"], article["fname"])
    except OSError as e:
        logger.warning("⚠️ Викторина: не прочитать статью %s: %s", article["fname"], e)
        return [], "не прочитать файл статьи"

    text = text.strip()
    if len(text) < 200:
        logger.info("🎮 Викторина: статья %s слишком короткая для вопросов", article["fname"])
        return [], "статья слишком короткая"

    prompt = _SYSTEM_PROMPT.format(
        count=per_article, options=QUIZ_OPTIONS_COUNT,
        q_max=QUIZ_QUESTION_MAX, o_max=QUIZ_OPTION_MAX, e_max=QUIZ_EXPLANATION_MAX,
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Статья «{_article_title(text)}»:\n\n{text[:_ARTICLE_CHARS_MAX]}"},
    ]

    # Тот же вход в модели, что у форматирования новостей: активная модель с
    # цепочкой подстраховки, учётом расходов и токенов. Своего похода в сеть
    # здесь заводить нельзя — расход мимо статистики бота.
    #
    # ⚠️ РАЗМЫШЛЕНИЯ МОДЕЛИ ЗДЕСЬ НЕ ТРОГАЕМ — решение Максима 2026-08-05.
    # Сборка идёт так же, как любой другой запрос бота: думающая модель думает.
    # История: в v3.90 сюда был вписан `thinking_override=False` (на qwen3.7-max
    # один вопрос стоил 3900 токенов, из них 2200 — размышления, ≈$0.011 и 45–50
    # сек на статью), и правка ОТКАЧЕНА по прямому указанию владельца: экономия
    # тут не главное, а на сложных статьях без размышлений вопросы получаются
    # слабее. Не «оптимизировать» обратно без нового решения Максима.
    # ⚠️ И помни, что из этого следует для разбора: ответ думающей модели
    # приходит с блоком <thought>…</thought>, поэтому `_extract_json` обязан его
    # срезать — на этом уже наступили в первой боевой сборке.
    from services.gemini import _gemini_chat_request
    data, used_model = _gemini_chat_request(messages, kind="викторина")
    if data is None:
        logger.warning("⚠️ Викторина: модель не ответила по статье %s", article["fname"])
        return [], "модель не ответила"

    try:
        body = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.warning("⚠️ Викторина: неожиданный формат ответа по статье %s", article["fname"])
        return [], "неожиданный формат ответа"

    items = _extract_json(body)
    if not items:
        # Кусок ответа в логе — иначе такую неудачу нечем разбирать: запрос
        # уже оплачен, а что именно прислала модель, никто не увидит.
        logger.warning("⚠️ Викторина: не разобрать ответ по статье %s (модель %s). Ответ: %s",
                       article["fname"], used_model, (body or "")[:300].replace("\n", " "))
        return [], "не разобрать ответ модели"

    saved = []
    for item in items[:per_article]:
        clean = _clean_question(item)
        if not clean:
            continue
        qid = add_quiz_question(article["fname"], clean["question"], clean["options"],
                                clean["correct_idx"], clean["explanation"])
        if qid:
            clean["id"] = qid
            saved.append(clean)

    logger.info("🎮 Викторина: по статье %s собрано вопросов %d из %d",
                article["fname"], len(saved), len(items))
    if not saved:
        # Модель ответила, разбор прошёл, а годного не осталось: варианты
        # повторялись, не влезли в лимиты Telegram или всё это уже есть в
        # банке. Причина отдельная от «не разобрать» — повтор тут помогает
        # реже, и по списку неудач это должно быть видно.
        return [], "все вопросы отбракованы"
    return saved, ""


def _run_over(batch: list, left: int) -> dict:
    """
    Общий проход по списку статей — им пользуются ОБЕ кнопки: и «🧠 Собрать
    вопросы», и «🔁 Повторить неудачные». Второй сборки этого цикла заводить
    нельзя: учёт неудач разъедется с учётом успехов при первой же правке.

    Здесь же ведётся таблица `quiz_failed`: удача снимает статью с учёта,
    неудача заводит её туда (или растит счётчик попыток) вместе с причиной.
    """
    saved = failed = 0
    for article in batch:
        got, reason = generate_for_article(article)
        if got:
            saved += len(got)
            clear_quiz_failure(article["fname"])
        else:
            failed += 1
            note_quiz_failure(article["fname"], reason or "не вышло")

    logger.info("🎮 Викторина: заход окончен — статей %d, вопросов %d, "
                "неудач %d, осталось статей %d", len(batch), saved, failed, left)
    return {"articles": len(batch), "saved": saved, "failed": failed, "left": left}


def generate_batch(limit_articles: int = None) -> dict:
    """
    Один заход кнопки «🧠 Собрать вопросы»: проходит статьи, по которым
    вопросов ещё нет, и собирает по QUIZ_GEN_PER_ARTICLE штук с каждой.

    Возвращает итог для отчёта человеку:
      {"articles": сколько статей обработано, "saved": сколько вопросов легло
       в черновики, "failed": по скольким статьям не вышло, "left": сколько
       статей осталось на следующее нажатие}

    ⚠️ СИНХРОННАЯ И ДОЛГАЯ — звать через run_in_executor (см. шапку модуля).
    """
    limit_articles = limit_articles or QUIZ_GEN_MAX_ARTICLES
    pending = articles_without_questions()
    batch = pending[:limit_articles]
    return _run_over(batch, max(len(pending) - len(batch), 0))


def retry_failed(limit_articles: int = None) -> dict:
    """
    Заход кнопки «🔁 Повторить неудачные»: идёт РОВНО по тем статьям, что
    записаны в `quiz_failed`, и ни по каким другим.

    Зачем отдельная кнопка, если обычная сборка их и так подхватит: подхватит,
    но вперемешку с новыми и в порядке общего списка — то есть при 41 статье
    в очереди до неудачных руки дойдут не в этот заход, а человек ждёт именно
    их. Здесь же видно и результат: сколько из проблемных наконец далось.

    ⚠️ Статья, которой уже нет в базе знаний (удалили или переименовали),
    молча снимается с учёта: держать в очереди на повтор то, чего не
    существует, — это вечная строка «⚠️ Не разобрались: 1» без способа её убрать.

    ⚠️ СИНХРОННАЯ И ДОЛГАЯ — звать через run_in_executor.
    """
    limit_articles = limit_articles or QUIZ_GEN_MAX_ARTICLES
    known = {a["fname"]: a for a in knowledge_store.list_articles() if a["folder"] == "approved"}

    batch, gone = [], 0
    for row in list_quiz_failures(limit=limit_articles):
        article = known.get(row["article"])
        if article:
            batch.append(article)
        else:
            clear_quiz_failure(row["article"])
            gone += 1
    if gone:
        logger.info("🎮 Викторина: снято с учёта неудач статей, которых больше нет: %d", gone)

    result = _run_over(batch, max(count_quiz_failures() - len(batch), 0))
    result["gone"] = gone
    return result


def stats() -> dict:
    """Цифры для шапки панели: статьи всего, без вопросов и в очереди на повтор."""
    approved = [a for a in knowledge_store.list_articles() if a["folder"] == "approved"]
    return {"articles_total": len(approved),
            "articles_left": len(articles_without_questions()),
            "failed": count_quiz_failures()}


# ───────────────────────────────────────────────
#  Эталонный набор вопросов (файл в репозитории)
# ───────────────────────────────────────────────
#
# ⚠️ ЗАЧЕМ ОН ЕСТЬ (2026-08-05, решение Максима). Первая машинная сборка дала
# негодный результат: модель писала вопросы вида «максимальное пробитие» и
# «максимальная скорость назад» ОДИНАКОВЫЕ для всех танков — формально
# правильные, играть в такое невозможно. Максим забраковал набор целиком и
# поручил составить вопросы вручную, по каждой статье.
#
# Написанные вручную вопросы лежат в репозитории (`quiz/questions.json`) и
# приезжают на сервер обычным обновлением кода — писать в боевую базу с
# машины разработки нельзя, её видит только сам бот. Кнопка «📥 Загрузить мои
# вопросы» переносит их из файла в банк.
#
# ⚠️ ФАЙЛ ТОЛЬКО ЧИТАЕТСЯ, бот в него не пишет НИКОГДА. Файл, который бот
# переписывает сам, ломает обновление кода с GitHub — на этом уже наступили с
# knowledge_base_vectors.json.

SEED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "quiz", "questions.json")


def seed_stats() -> dict:
    """
    Что лежит в эталонном файле: сколько вопросов и по скольким статьям.
    Пустой словарь — файла нет или он битый (кнопка загрузки тогда не нужна).
    """
    try:
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"questions": 0, "articles": 0}
    if not isinstance(data, list):
        return {"questions": 0, "articles": 0}
    return {"questions": len(data),
            "articles": len({item.get("article") for item in data if isinstance(item, dict)})}


def load_seed(approved: bool = True) -> dict:
    """
    Переносит вопросы из эталонного файла в банк.

    approved=True — сразу В ИГРУ: это выверенные вручную вопросы, а не машинная
    заготовка, и гонять их через одобрение по одному (а там третья сотня) —
    работа ради работы. Кнопки удаления никуда не делись: плохой вопрос всегда
    можно убрать из списка «✅ В игре».

    Повторное нажатие безопасно: `add_quiz_question` не заводит дубль по паре
    «статья + текст вопроса», такие вопросы просто пропускаются.

    Возвращает {"added": сколько добавлено, "skipped": сколько уже было,
    "bad": сколько не прошло проверку, "total": сколько всего в файле}.
    """
    try:
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        logger.error("⚠️ Викторина: не прочитать эталонный файл вопросов: %s", e)
        return {"added": 0, "skipped": 0, "bad": 0, "total": 0}

    if not isinstance(data, list):
        logger.error("⚠️ Викторина: эталонный файл вопросов — не список")
        return {"added": 0, "skipped": 0, "bad": 0, "total": 0}

    added = skipped = bad = 0
    for item in data:
        clean = _clean_question(item) if isinstance(item, dict) else None
        article = (item or {}).get("article") if isinstance(item, dict) else None
        if not clean or not article:
            bad += 1
            continue
        qid = add_quiz_question(article, clean["question"], clean["options"],
                                clean["correct_idx"], clean["explanation"])
        if qid is None:
            skipped += 1
            continue
        added += 1
        if approved:
            set_quiz_question_approved(qid, True)

    logger.info("🎮 Викторина: эталонные вопросы загружены — добавлено %d, "
                "пропущено как дубли %d, негодных %d (всего в файле %d)",
                added, skipped, bad, len(data))
    return {"added": added, "skipped": skipped, "bad": bad, "total": len(data)}
