#!/usr/bin/env python3
"""
quiz_tool.py — проверка и запись вопросов викторины (навык quiz-questions).

Запуск из корня проекта:
    python .claude/skills/quiz-questions/scripts/quiz_tool.py pull
    python .claude/skills/quiz-questions/scripts/quiz_tool.py size  <статья.md>
    python .claude/skills/quiz-questions/scripts/quiz_tool.py check <пачка.json> --article <статья.md> [...]
    python .claude/skills/quiz-questions/scripts/quiz_tool.py add   <пачка.json> --article <статья.md> [...]

pull  — заменяет домашнюю knowledge/approved свежей копией с сервера. Дома
        папка отстаёт всегда (в GitHub она не ездит, статьи появляются только
        на сервере): 31.10.2026 дома не было 12 статей из 132 и у 49 текст
        был старый. Нужен ssh-адрес c4max — в облачной сессии его нет.
size  — сколько вопросов положено статье: 2 обычной, 3 большой.
check — проверяет пачку и ничего не пишет.
add   — раскладывает верные ответы по позициям, проверяет и дописывает
        quiz/questions.json, не трогая старые вопросы ни на байт.

Пачка — JSON-массив объектов {article, question, options, correct_idx,
explanation}, как в quiz/questions.json. Код выхода 1 — есть ошибки.
"""
import argparse
import json
import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, ROOT)
PATH = os.path.join(ROOT, "quiz", "questions.json")
APPROVED = os.path.join(ROOT, "knowledge", "approved")

# Вывод в UTF-8: под Windows консоль по умолчанию в cp1251.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Порог «большой» статьи: 1.6 медианы одобренных (31.08.2026: медиана 4.3 КБ,
# бомбардировщики 7.5–8.8 КБ — верхушка базы). Без папки базы — 7 000 байт.
BIG_FACTOR = 1.6
BIG_FALLBACK = 7000
# Верный вариант длиннее самого длинного неверного не больше чем в 1.15 раза.
LEN_RATIO = 1.15


def limits():
    """Лимиты Telegram — из config.py, как у самого бота."""
    try:
        import config
        return (config.QUIZ_QUESTION_MAX, config.QUIZ_OPTION_MAX,
                config.QUIZ_EXPLANATION_MAX, config.QUIZ_OPTIONS_COUNT)
    except Exception:
        return 300, 100, 200, 4


def read_file():
    """(сырые байты, список вопросов) файла вопросов."""
    with open(PATH, "rb") as f:
        raw = f.read()
    return raw, json.loads(raw.decode("utf-8"))


def dump(data) -> bytes:
    """Ровно так, как хранится файл: отступ 1, LF, без перевода строки в конце."""
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def norm_num(s: str) -> str:
    """«1 000» → «1000», «12,5» → «12.5», «−9» → «9»."""
    s = s.replace(" ", " ").replace(" ", "").replace(",", ".")
    return s.lstrip("+-−")


NUM_RX = re.compile(r"\d+(?:[  ]\d{3})*(?:[.,]\d+)?")


def numbers(text: str) -> set:
    return {norm_num(m.group()) for m in NUM_RX.finditer(text or "")}


SERVER = "c4max"
SERVER_KB = "/home/c4bot/bot/knowledge"


def cmd_pull(args) -> int:
    """
    Домашняя knowledge/approved ← копия с сервера: новые и изменённые статьи
    записываются, статьи, которых на сервере нет, убираются. Трогает только
    файлы .md этой папки.

    ⚠️ Архив распаковывает Python, а не tar из Windows: тот читает русские
    имена файлов не в той кодировке и пишет «╨в╤Г-4.md» вместо «Ту-4.md»
    (поймано 31.10.2026).
    """
    import io
    import shutil
    import subprocess
    import tarfile
    import tempfile

    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", SERVER,
                            f"cd {SERVER_KB} && tar czf - approved"],
                           capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"❌ сервер не ответил ({e}) — папка не тронута")
        return 1
    if r.returncode != 0 or not r.stdout:
        err = r.stderr.decode("utf-8", "replace").strip()[:300]
        print(f"❌ ssh {SERVER} не отдал статьи (код {r.returncode}): {err} — папка не тронута")
        return 1

    tmp = tempfile.mkdtemp(prefix="kb_pull_")
    try:
        with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:gz") as t:
            t.extractall(tmp, filter="data")
        src = os.path.join(tmp, "approved")
        fresh = {f for f in os.listdir(src) if f.lower().endswith(".md")} if os.path.isdir(src) else set()
        if not fresh:
            print("❌ в архиве с сервера нет ни одной статьи — папка не тронута")
            return 1
        os.makedirs(APPROVED, exist_ok=True)
        old = {f for f in os.listdir(APPROVED) if f.lower().endswith(".md")}
        added, changed = [], []
        for f in sorted(fresh):
            with open(os.path.join(src, f), "rb") as fh:
                data = fh.read()
            dst = os.path.join(APPROVED, f)
            if f in old:
                with open(dst, "rb") as fh:
                    if fh.read() == data:
                        continue
                changed.append(f)
            else:
                added.append(f)
            with open(dst, "wb") as fh:
                fh.write(data)
        removed = sorted(old - fresh)
        for f in removed:
            os.remove(os.path.join(APPROVED, f))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"Статей на сервере: {len(fresh)}. Дома: новых {len(added)}, обновлено "
          f"{len(changed)}, убрано {len(removed)}, без изменений "
          f"{len(fresh) - len(added) - len(changed)}.")
    for title, items in (("новые", added), ("убраны", removed)):
        if items:
            print(f"  {title}: {', '.join(items)}")
    return 0


def cmd_size(args) -> int:
    size = os.path.getsize(args.article)
    sizes = []
    if os.path.isdir(APPROVED):
        sizes = [os.path.getsize(os.path.join(APPROVED, f))
                 for f in os.listdir(APPROVED) if f.lower().endswith(".md")]
    if sizes:
        med = statistics.median(sizes)
        threshold = BIG_FACTOR * med
        how = f"медиана одобренных {med:.0f} байт × {BIG_FACTOR} = {threshold:.0f}"
    else:
        threshold = BIG_FALLBACK
        how = f"папки knowledge/approved нет — порог {BIG_FALLBACK} байт"
    big = size >= threshold
    print(f"{os.path.basename(args.article)}: {size} байт ({how})")
    print(f"→ {'БОЛЬШАЯ — 3 вопроса' if big else 'обычная — 2 вопроса'}")
    return 0


def check(batch, articles, existing) -> tuple[list, list]:
    """(ошибки, замечания) по пачке."""
    q_max, o_max, e_max, n_opts = limits()
    errors, notes = [], []
    article_nums = set()
    for path in articles:
        with open(path, encoding="utf-8") as f:
            article_nums |= numbers(f.read())
    seen = {(q["article"], q["question"].strip()) for q in existing}
    longest = 0
    if not isinstance(batch, list) or not batch:
        return ["пачка — не JSON-массив или пустая"], []
    for n, q in enumerate(batch, 1):
        tag = f"#{n} «{str(q.get('question', ''))[:50]}»"
        art = str(q.get("article") or "").strip()
        text = str(q.get("question") or "").strip()
        opts = q.get("options")
        expl = str(q.get("explanation") or "").strip()
        idx = q.get("correct_idx")
        if not art.endswith(".md"):
            errors.append(f"{tag}: поле article — не имя файла статьи .md")
        if not text:
            errors.append(f"{tag}: нет текста вопроса")
        if not isinstance(opts, list) or len(opts) != n_opts:
            errors.append(f"{tag}: вариантов не {n_opts}")
            continue
        opts = [str(o).strip() for o in opts]
        if len({o.lower() for o in opts}) != len(opts) or not all(opts):
            errors.append(f"{tag}: варианты повторяются или есть пустой")
        if not isinstance(idx, int) or not 0 <= idx < len(opts):
            errors.append(f"{tag}: correct_idx не число 0–{len(opts) - 1}")
            continue
        if len(text) > q_max:
            errors.append(f"{tag}: вопрос {len(text)} знаков, предел {q_max}")
        for o in opts:
            if len(o) > o_max:
                errors.append(f"{tag}: вариант «{o[:40]}…» {len(o)} знаков, предел {o_max}")
        if not expl:
            notes.append(f"{tag}: нет разбора")
        elif len(expl) > e_max:
            errors.append(f"{tag}: разбор {len(expl)} знаков, предел {e_max} (бот его обрежет)")
        right = opts[idx]
        wrong = [o for i, o in enumerate(opts) if i != idx]
        if len(right) > LEN_RATIO * max(len(o) for o in wrong):
            errors.append(f"{tag}: верный вариант выдаёт себя длиной "
                          f"({len(right)} против {max(len(o) for o in wrong)})")
        if len(right) > max(len(o) for o in wrong):
            longest += 1
        if any(re.search(r"\s[—–-]\s*\d", o) for o in opts):
            errors.append(f"{tag}: в варианте «Имя — число»: ответ читается по цифре")
        if (art, text) in seen:
            errors.append(f"{tag}: такой вопрос по этой статье уже есть в файле")
        seen.add((art, text))
        if articles:
            missing = (numbers(text) | numbers(right) | numbers(expl)) - article_nums
            if missing:
                errors.append(f"{tag}: чисел нет в статье: {', '.join(sorted(missing))}")
    if not articles:
        notes.append("статьи не переданы (--article) — числа со статьёй НЕ сверены")
    notes.append(f"верный — самый длинный вариант в {longest} из {len(batch)} вопросов пачки")
    return errors, notes


def balance(batch, existing):
    """
    Ставит верный ответ каждого вопроса на самую редкую позицию во всём файле
    (при равенстве — меньший номер). Неверные сохраняют свой порядок.
    Постоянно: тот же вход — тот же выход.
    """
    _, _, _, n_opts = limits()
    counts = [0] * n_opts
    for q in existing:
        counts[q["correct_idx"]] += 1
    out = []
    for q in batch:
        opts = [str(o).strip() for o in q["options"]]
        right = opts[q["correct_idx"]]
        wrong = [o for i, o in enumerate(opts) if i != q["correct_idx"]]
        pos = min(range(n_opts), key=lambda i: (counts[i], i))
        counts[pos] += 1
        new = wrong[:pos] + [right] + wrong[pos:]
        out.append({"article": q["article"].strip(), "question": q["question"].strip(),
                    "options": new, "correct_idx": pos,
                    "explanation": str(q.get("explanation") or "").strip()})
    return out


def spread(items) -> list:
    _, _, _, n_opts = limits()
    counts = [0] * n_opts
    for q in items:
        counts[q["correct_idx"]] += 1
    return counts


def report(errors, notes) -> None:
    for e in errors:
        print("❌", e)
    for n in notes:
        print("ℹ️", n)
    print("✅ пачка годна" if not errors else f"— ошибок: {len(errors)}")


def cmd_check(args) -> int:
    _, existing = read_file()
    with open(args.batch, encoding="utf-8") as f:
        batch = json.load(f)
    errors, notes = check(batch, args.article, existing)
    report(errors, notes)
    return 1 if errors else 0


def cmd_add(args) -> int:
    raw, existing = read_file()
    if dump(existing) != raw:
        print("❌ quiz/questions.json хранится не в ожидаемом виде (отступ 1, LF, "
              "без перевода строки в конце) — дописывать нельзя, разберись сначала")
        return 1
    with open(args.batch, encoding="utf-8") as f:
        batch = json.load(f)
    errors, notes = check(batch, args.article, existing)
    if errors:
        report(errors, notes)
        print("Ничего не записано.")
        return 1
    placed = balance(batch, existing)
    new_raw = dump(existing + placed)
    # Старые вопросы — байт в байт: новое только дописано в хвост массива.
    if not new_raw.startswith(raw[:-2]):
        print("❌ запись изменила бы старые вопросы — ничего не записано")
        return 1
    if json.loads(new_raw.decode("utf-8"))[:len(existing)] != existing:
        print("❌ сверка старых вопросов не сошлась — ничего не записано")
        return 1
    with open(PATH, "wb") as f:
        f.write(new_raw)
    total = spread(existing + placed)
    report([], notes)
    print(f"Записано вопросов: {len(placed)}; в файле {len(existing) + len(placed)} "
          f"по {len({q['article'] for q in existing + placed})} статьям.")
    print(f"Верный ответ по позициям 1–4: в пачке {spread(placed)}, во всём файле {total}.")
    if max(total) - min(total) > 2:
        print("❌ перекос по позициям во всём файле больше 2 — проверь раскладку")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Вопросы викторины: проверка и запись")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull", help="свежие статьи с сервера в knowledge/approved")
    s = sub.add_parser("size", help="квота вопросов по размеру статьи")
    s.add_argument("article")
    for name in ("check", "add"):
        c = sub.add_parser(name)
        c.add_argument("batch", help="JSON-массив вопросов")
        c.add_argument("--article", action="append", default=[],
                       help="файл статьи для сверки чисел (можно несколько)")
    args = p.parse_args()
    return {"pull": cmd_pull, "size": cmd_size, "check": cmd_check,
            "add": cmd_add}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
