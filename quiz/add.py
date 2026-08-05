#!/usr/bin/env python3
"""
Помощник наполнения quiz/questions.json (разовый, для составления вопросов).

Читает JSON-пачку со стандартного ввода, проверяет каждый вопрос теми же
правилами, что и бот (services.quiz_bank._clean_question), и дописывает в
общий файл. Дубли по паре «статья + текст вопроса» отбрасывает.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.quiz_bank import _clean_question  # noqa: E402

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions.json")


def main():
    batch = json.load(sys.stdin)
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = []

    seen = {(item["article"], item["question"]) for item in data}
    added = bad = dup = 0
    for item in batch:
        clean = _clean_question(item)
        if not clean or not item.get("article"):
            bad += 1
            print("НЕГОДЕН:", item.get("question", "?")[:70])
            continue
        key = (item["article"], clean["question"])
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        data.append({"article": item["article"], **clean})
        added += 1

    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    articles = len({item["article"] for item in data})
    print(f"добавлено {added}, дублей {dup}, негодных {bad} | "
          f"в файле {len(data)} вопросов по {articles} статьям")


if __name__ == "__main__":
    main()
