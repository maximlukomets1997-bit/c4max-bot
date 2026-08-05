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
import re

from config import (QUIZ_EXPLANATION_MAX, QUIZ_GEN_MAX_ARTICLES, QUIZ_GEN_PER_ARTICLE,
                    QUIZ_OPTION_MAX, QUIZ_OPTIONS_COUNT, QUIZ_QUESTION_MAX)
from database.history import add_quiz_question, get_quiz_articles_covered
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
4. Разбор пиши в стиле бывалого командира, коротко и по делу, начиная с
   «Рапорт Полковника:». Он объясняет, ПОЧЕМУ верный ответ верен.
5. Вопрос должен быть понятен без статьи: упоминай технику по названию, а не
   «эта машина» или «данный корабль».
6. Пиши только по-русски.

Ответь ТОЛЬКО массивом JSON, без пояснений и без разметки кода:
[{{"question": "...", "options": ["...", "...", "...", "..."], "correct_idx": 0, "explanation": "Рапорт Полковника: ..."}}]"""


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
    fallback = None
    for idx, char in enumerate(body):
        if char not in "[{":
            continue
        try:
            data, _end = decoder.raw_decode(body[idx:])
        except ValueError:
            continue
        if _looks_like_questions(data):
            return [data] if isinstance(data, dict) else data
        # Разобралось, но на вопросы не похоже (например, массив из
        # рассуждений) — запоминаем как крайний вариант и ищем дальше.
        if fallback is None and isinstance(data, list) and data:
            fallback = data
    return fallback


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


def generate_for_article(article: dict, per_article: int = None) -> list[dict]:
    """
    Собирает вопросы по ОДНОЙ статье и кладёт их в банк черновиками.
    Возвращает список записанных вопросов (пустой — модель не справилась).
    """
    per_article = per_article or QUIZ_GEN_PER_ARTICLE
    try:
        _, text = knowledge_store.read_article(article["folder"], article["fname"])
    except OSError as e:
        logger.warning("⚠️ Викторина: не прочитать статью %s: %s", article["fname"], e)
        return []

    text = text.strip()
    if len(text) < 200:
        logger.info("🎮 Викторина: статья %s слишком короткая для вопросов", article["fname"])
        return []

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
        return []

    try:
        body = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        logger.warning("⚠️ Викторина: неожиданный формат ответа по статье %s", article["fname"])
        return []

    items = _extract_json(body)
    if not items:
        # Кусок ответа в логе — иначе такую неудачу нечем разбирать: запрос
        # уже оплачен, а что именно прислала модель, никто не увидит.
        logger.warning("⚠️ Викторина: не разобрать JSON по статье %s (модель %s). Ответ: %s",
                       article["fname"], used_model, (body or "")[:300].replace("\n", " "))
        return []

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
    return saved


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

    saved = failed = 0
    for article in batch:
        got = generate_for_article(article)
        if got:
            saved += len(got)
        else:
            failed += 1

    left = max(len(pending) - len(batch), 0)
    logger.info("🎮 Викторина: сборка окончена — статей %d, вопросов %d, "
                "неудач %d, осталось статей %d", len(batch), saved, failed, left)
    return {"articles": len(batch), "saved": saved, "failed": failed, "left": left}


def stats() -> dict:
    """Цифры для шапки панели: сколько статей всего и сколько ещё без вопросов."""
    approved = [a for a in knowledge_store.list_articles() if a["folder"] == "approved"]
    return {"articles_total": len(approved), "articles_left": len(articles_without_questions())}
