#!/usr/bin/env python3
"""
impact.py — «что заденет правка этого имени».

Даёшь имя функции, переменной, ключа настройки или callback-приставки —
получаешь: где оно объявлено, где используется, какие файлы придётся
перечитать и накрывает ли это место предполётная проверка.

Смысл: в этом проекте нет тестов, поэтому единственная защита от поломки —
заранее знать всех, кто на правку смотрит. Список собирается ИЗ КОДА, а не
из описаний, поэтому не может отстать от него.

Запуск из корня проекта:
    python .claude/skills/project-map/scripts/impact.py send_formatted
    python .claude/skills/project-map/scripts/impact.py rag_enabled
    python .claude/skills/project-map/scripts/impact.py "adm:wipe"
"""

import argparse
import ast
import io
import os
import re
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SKIP_PARTS = ("__pycache__", ".claude", ".git", ".venv", "venv", "Для ")
# Файлы, которые тоже надо считать читателями: не .py, но ломаются от правок.
EXTRA_EXT = (".sh", ".yml", ".yaml", ".bat", ".json", ".txt")


def walk_files(root, exts):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if any(part in rel for part in SKIP_PARTS):
            continue
        for name in sorted(filenames):
            if name.endswith(exts):
                out.append(os.path.join(dirpath, name))
    return out


def find_definitions(root, symbol):
    """Где имя ОБЪЯВЛЕНО: функция, класс или присваивание на любом уровне."""
    hits = []
    for path in walk_files(root, (".py",)):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            name = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == symbol:
                        name = symbol
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
            if name == symbol:
                kind = type(node).__name__
                hits.append((os.path.relpath(path, root).replace(os.sep, "/"),
                             getattr(node, "lineno", 0), kind))
    return hits


def find_uses(root, symbol):
    """Где имя ВСТРЕЧАЕТСЯ, включая строки: ключи настроек и callback_data
    живут в кавычках, и обычный поиск по коду их не отличает."""
    exts = (".py",) + EXTRA_EXT
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", symbol):
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    else:
        pattern = re.compile(re.escape(symbol))
    out = {}
    for path in walk_files(root, exts):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        lines = [(i + 1, ln.strip()) for i, ln in enumerate(text.split("\n")) if pattern.search(ln)]
        if lines:
            out[os.path.relpath(path, root).replace(os.sep, "/")] = lines
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", help="имя функции / переменной / ключа настройки / callback-приставки")
    ap.add_argument("--root", default=".")
    ap.add_argument("--full", action="store_true", help="печатать все строки, а не первые три на файл")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    symbol = args.symbol

    defs = find_definitions(root, symbol)
    uses = find_uses(root, symbol)

    print("═" * 60)
    print("ИМЯ: " + symbol)
    print("═" * 60)

    print("\n1. ГДЕ ОБЪЯВЛЕНО")
    if defs:
        for path, line, kind in defs:
            print("   %s:%d  (%s)" % (path, line, kind))
        if len(defs) > 1:
            print("   ⚠️ объявлений больше одного — правка в одном месте другие не затронет")
    else:
        print("   объявления не нашлось — вероятно, это строковый ключ или приставка кнопки")

    def_files = {d[0] for d in defs}
    readers = [p for p in uses if p not in def_files]

    print("\n2. КТО ЭТО ЧИТАЕТ — файлов: %d" % len(readers))
    if not readers:
        print("   никто. Либо имя мёртвое, либо зовётся через getattr/строку — проверь руками")
    for path in sorted(readers):
        lines = uses[path]
        print("   %s  (%d)" % (path, len(lines)))
        shown = lines if args.full else lines[:3]
        for num, text in shown:
            print("      %5d | %s" % (num, text[:110]))
        if not args.full and len(lines) > 3:
            print("      ... ещё %d" % (len(lines) - 3))

    print("\n3. ЧТО ЭТО ЗНАЧИТ ДЛЯ ПРАВКИ")
    groups = {
        "handlers/admin/": "админ-панели и кнопки — проверь роутер и сборку панели",
        "handlers/": "обработчики сообщений и команд — проверь регистрацию в handlers/__init__.py",
        "jobs/": "фоновые задачи — ошибка всплывёт не сразу, а через минуты или сутки",
        "services/": "общая логика — задето несколько экранов сразу",
        "database/": "база — правка схемы требует переезда данных, а не только кода",
        "config.py": "настройки — читают почти все модули",
        "preflight.py": "имя участвует в предполётной проверке: сломаешь — проверка покраснеет",
        "deploy.sh": "выкатка на сервер — ошибка остановит обновление боевого бота",
        ".github/": "проверка в GitHub Actions",
    }
    seen = []
    for prefix, note in groups.items():
        if any(p.startswith(prefix) or p == prefix for p in uses):
            seen.append("   • " + prefix + " — " + note)
    print("\n".join(seen) if seen else "   следов в известных слоях нет")

    covered = any(p == "preflight.py" for p in uses)
    print("\n4. ПРОВЕРКА")
    print("   предполётная проверка это имя %s" % ("ВИДИТ" if covered else "НЕ видит"))
    print("   тестов в проекте нет — после правки нужен ручной прогон затронутых экранов")
    print("   обязательный минимум: python preflight.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
