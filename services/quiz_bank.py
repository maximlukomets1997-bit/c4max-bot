"""
Банк вопросов викторины: файл вопросов и сверка с ним (2026-08-05, решение Максима).

ЗАЧЕМ. Вопросы викторины раньше лежали списком В КОДЕ (data/quiz_questions.py,
12 штук на весь бот) — их выучивали за пару вечеров, и любые рейтинги
превращались в состязание памяти о двенадцати строках. Теперь их пишут по
статьям базы знаний — по две-три на статью, и каждая новая статья добавляет
свои.

КАК УСТРОЕНО. Вопросы пишет Claude по статьям и привозит файлом
quiz/questions.json в обновлении кода. Этот модуль переносит их из файла в
таблицу quiz_bank (кнопка «📥 Мои вопросы в черновики»), сверяет файл с банком
и догоняет банк до файла (кнопка «♻️ Обновить из файла»). Показывает и одобряет
вопросы панель /quizadm (handlers/admin/panel_quiz.py), в игру берёт
handlers/quiz.py.

⚠️ МАШИННОЙ СБОРКИ БОЛЬШЕ НЕТ (04.10.2026, решение Максима). До этого дня
здесь же жила кнопка «🧠 Собрать вопросы»: модель писала вопросы по статьям
сама. Первая же её сборка дала негодный набор (см. «Эталонный набор» ниже),
а с тех пор все вопросы банка написаны вручную — машинных в нём не осталось
ни одного. Вместе со сборкой ушли повтор неудачных статей, таблица
quiz_failed и рычаг «думай в полную силу» в services/gemini.py. Вернуть —
история GitHub, версия v5.51 и раньше.
"""

import json
import logging
import os
import re

from config import (QUIZ_EXPLANATION_MAX, QUIZ_OPTION_MAX, QUIZ_OPTIONS_COUNT,
                    QUIZ_QUESTION_MAX)
from database.history import (add_quiz_question, get_quiz_articles_covered,
                              set_quiz_question_approved)
from services import knowledge_store

logger = logging.getLogger(__name__)


def _clean_question(item: dict) -> dict | None:
    """
    Проверяет один вопрос из файла и приводит его к виду, годному для
    send_poll. Возвращает None, если вопрос негоден: один кривой вопрос не
    повод ронять загрузку всего файла.

    Что проверяем (каждое правило поймано на живых данных ещё во времена
    машинной сборки):
      • есть текст вопроса и ровно QUIZ_OPTIONS_COUNT вариантов;
      • варианты не повторяются;
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
    # меняет, а переваливает за 200 знаков он чаще всего на пару слов.
    if len(explanation) > QUIZ_EXPLANATION_MAX:
        explanation = explanation[:QUIZ_EXPLANATION_MAX - 1].rstrip() + "…"

    return {
        "question": question,
        "options": options,
        "correct_idx": correct_idx,
        "explanation": explanation,
    }


def articles_without_questions(articles: list | None = None) -> list[dict]:
    """
    Одобренные статьи базы знаний, по которым вопросов ещё нет вовсе —
    экран «📋 Статьи без вопросов» и строка-счётчик в панели викторины.

    ⚠️ Берём ТОЛЬКО папку approved: статьи в pending ещё не приняты Максимом,
    и писать вопросы по тому, что может быть переписано или удалено, — работа
    впустую и вопросы про несуществующую технику.

    articles — уже прочитанный список knowledge_store.list_articles() (03.10.2026):
    его передаёт stats(), чтобы не читать все статьи с диска дважды подряд.
    Не передан — читаем сами.
    """
    covered = get_quiz_articles_covered()
    if articles is None:
        articles = knowledge_store.list_articles()
    return [a for a in articles
            if a["folder"] == "approved" and a["fname"] not in covered]


def stats() -> dict:
    """Цифры для шапки панели и страницы сайта: статьи всего и без вопросов."""
    articles = knowledge_store.list_articles()
    approved = [a for a in articles if a["folder"] == "approved"]
    return {"articles_total": len(approved),
            "articles_left": len(articles_without_questions(articles))}


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
# машины разработки нельзя, её видит только сам бот. Кнопка «📥 Мои вопросы в
# черновики» переносит их из файла в банк.
#
# ⚠️ ФАЙЛ ТОЛЬКО ЧИТАЕТСЯ, бот в него не пишет НИКОГДА. Файл, который бот
# переписывает сам, ломает обновление кода с GitHub — на этом уже наступили с
# knowledge_base_vectors.json.

SEED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "quiz", "questions.json")


def _read_seed(quiet: bool = True) -> list | None:
    """
    Содержимое эталонного файла списком. None — файла нет, он битый или в нём
    не список; тогда звать нечего и показывать нечего.

    ⚠️ Один читатель на всех (2026-09-01). Раньше файл открывали три места
    своими руками, и каждое по-своему решало, что делать с битым JSON. Правка
    в одном из них молча расходилась с остальными.
    """
    try:
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        if not quiet:
            logger.error("⚠️ Викторина: не прочитать эталонный файл вопросов: %s", e)
        return None
    if not isinstance(data, list):
        if not quiet:
            logger.error("⚠️ Викторина: эталонный файл вопросов — не список")
        return None
    return data


def seed_stats() -> dict:
    """
    Что лежит в эталонном файле: сколько вопросов и по скольким статьям.
    Нули в обоих полях — файла нет или он битый (кнопка загрузки тогда не нужна).
    """
    data = _read_seed()
    if data is None:
        return {"questions": 0, "articles": 0}
    return {"questions": len(data),
            "articles": len({item.get("article") for item in data if isinstance(item, dict)})}


def seed_diff() -> dict:
    """
    Сверяет эталонный файл с банком ЦЕЛИКОМ — не только по имени вопроса
    (2026-09-01, решение Максима).

    ⚠️ РАДИ ЧЕГО ЭТО ЕСТЬ. Кнопка «📥 Мои вопросы в черновики» сверяет файл с
    банком по паре «статья + текст вопроса» и уже знакомый вопрос пропускает
    ЦЕЛИКОМ. Значит правка ВАРИАНТОВ, ВЕРНОГО ОТВЕТА или РАЗБОРА обычной
    отправкой кода не доезжает никуда: файл в репозитории новый, в игре —
    старый, и это расхождение ничем не видно. Так уже было 21.08.2026, когда
    «Рапорт Полковника:» пришлось вычищать прямо в боевой базе руками.

    Возвращает:
      file_ok  — файл на месте и читается;
      total    — годных вопросов в файле, bad — не прошедших проверку формата;
      same     — есть в банке и совпадает целиком;
      changed  — есть в банке, но содержимое разошлось (их чинит seed_apply);
      missing  — в файле есть, в банке нет (их зальёт кнопка загрузки);
      extra    — в банке есть, в файле нет: машинная сборка (была до
                 04.10.2026) или вопрос,
                 у которого в файле поправили САМ ТЕКСТ (тогда он же считается
                 и в missing — старую запись надо убирать руками);
      items    — расхождения списком, с номером записи в банке и с тем, что
                 именно разошлось.
    """
    from database.history import list_all_quiz_questions

    data = _read_seed()
    if data is None:
        return {"file_ok": False, "total": 0, "bad": 0, "same": 0,
                "changed": 0, "missing": 0, "extra": 0, "items": []}

    bank = {(q["article"], q["question"]): q for q in list_all_quiz_questions()}
    seen = set()
    total = bad = same = missing = 0
    items = []

    for raw in data:
        clean = _clean_question(raw) if isinstance(raw, dict) else None
        article = raw.get("article") if isinstance(raw, dict) else None
        if not clean or not article:
            bad += 1
            continue
        total += 1
        # ⚠️ Сравниваем ПРИЧЁСАННЫЙ вопрос, а не сырой из файла: в банк он
        # попал через _clean_question (тот подрезает длинный разбор), и сырой
        # текст показывал бы вечное расхождение там, где всё в порядке.
        # ⚠️ Ключ — «статья + текст вопроса», ТОТ ЖЕ, по которому решает
        # add_quiz_question («этот уже есть, пропускаю»). Разойдутся ключи —
        # сверка начнёт показывать расхождения там, где их нет.
        key = (article, clean["question"])
        current = bank.get(key)
        if current is None:
            missing += 1
            continue
        seen.add(key)
        what = []
        if list(current["options"]) != list(clean["options"]):
            what.append("варианты ответа")
        if int(current["correct_idx"]) != int(clean["correct_idx"]):
            what.append("ВЕРНЫЙ ОТВЕТ")
        if (current["explanation"] or "").strip() != clean["explanation"].strip():
            what.append("разбор")
        if not what:
            same += 1
            continue
        items.append({
            "qid": current["id"],
            "article": article,
            "question": clean["question"],
            "approved": current["approved"],
            "what": ", ".join(what),
            "options": clean["options"],
            "correct_idx": clean["correct_idx"],
            "explanation": clean["explanation"],
        })

    return {"file_ok": True, "total": total, "bad": bad, "same": same,
            "changed": len(items), "missing": missing,
            "extra": len(bank) - len(seen), "items": items}


def seed_apply() -> dict:
    """
    Догоняет банк до эталонного файла: переписывает варианты, верный ответ и
    разбор у тех вопросов, где они разошлись (2026-09-01).

    ⚠️ ЧЕГО НЕ ДЕЛАЕТ, и это важнее того, что делает:
      • не трогает вопросы, которых в файле нет (машинная сборка до
        04.10.2026) — их автор не файл, и затирать их файлом было бы враньём;
      • не заводит новых и не удаляет старых: добавляет только кнопка
        загрузки, удаляет только человек;
      • не меняет статус «в игре / черновик» и счётчик показов.

    Возвращает {"updated": сколько поправлено, "changed": сколько было}.
    """
    from database.history import update_quiz_question_body

    diff = seed_diff()
    updated = 0
    for item in diff["items"]:
        update_quiz_question_body(item["qid"], item["options"],
                                  item["correct_idx"], item["explanation"])
        updated += 1
        logger.info("🎮 Викторина: вопрос #%s (%s) догнан до файла — разошлось: %s",
                    item["qid"], item["article"], item["what"])

    logger.info("🎮 Викторина: сверка с эталонным файлом — поправлено %d из %d "
                "расхождений", updated, diff["changed"])
    return {"updated": updated, "changed": diff["changed"]}


def load_seed(approved: bool = True) -> dict:
    """
    Переносит вопросы из эталонного файла в банк.

    ⚠️ КНОПКА ПАНЕЛИ ЗОВЁТ С approved=False — В ЧЕРНОВИКИ (28.08.2026, решение
    Максима). Раньше грузили сразу в игру, и это было верно ДЛЯ ТОГО СЛУЧАЯ:
    заезжала разовая пачка из 219 выверенных вручную вопросов, и одобрять их
    по одному значило бы работать ради работы. Теперь набор пополняется по
    2–3 вопроса на каждую новую статью, и Максим смотрит их перед тем, как
    они попадут людям — цена одобрения упала, ценность просмотра выросла.

    approved=True оставлен рабочим: понадобится снова залить большую готовую
    пачку — звать с ним, минуя черновики. Кнопки удаления есть в обоих
    списках, плохой вопрос убирается и из «✅ В игре», и из черновиков.

    Повторное нажатие безопасно: `add_quiz_question` не заводит дубль по паре
    «статья + текст вопроса», такие вопросы просто пропускаются.

    ⚠️ И ПО ЭТОЙ ЖЕ ПРИЧИНЕ ОНА НЕ ЧИНИТ УЖЕ ЗАЛИТОЕ. Поправил в файле разбор
    или варианты у существующего вопроса — сюда это не доедет, пара «статья +
    вопрос» совпала, и запись пропущена целиком. Такие расхождения показывает
    `seed_diff`, а переписывает `seed_apply` (кнопка «♻️ Обновить из файла»).

    Возвращает {"added": сколько добавлено, "skipped": сколько уже было,
    "bad": сколько не прошло проверку, "total": сколько всего в файле}.
    """
    data = _read_seed(quiet=False)
    if data is None:
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
