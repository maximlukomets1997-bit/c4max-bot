#!/usr/bin/env python3
"""
map.py — собирает карту проекта ИЗ КОДА, прямо сейчас.

Ничего не помнит и ничего не хранит: читает .py-файлы, разбирает импорты
(в том числе относительные и те, что спрятаны внутри функций) и печатает
таблицу «файл → сколько модулей его тянут → кто именно».

Зачем это скриптом, а не текстом в карте: текст устаревает молча, а этот
вывод не может соврать — он собран из тех самых файлов, которые лежат на
диске. Если вывод разошёлся с описанием в карте, прав вывод.

Запуск из корня проекта:
    python .claude/skills/project-map/scripts/map.py
    python .claude/skills/project-map/scripts/map.py --hubs
    python .claude/skills/project-map/scripts/map.py --module services/gemini.py
"""

import argparse
import ast
import collections
import io
import os
import sys

# Вывод в UTF-8: под Windows консоль по умолчанию отдаёт cp1251 и кириллица
# в перенаправленном выводе превращается в мусор.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# Что не считаем кодом проекта: кэш питона, служебная папка Claude Code
# (там лежат КОПИИ проекта из старых worktree — чужой код прошлых версий),
# и папки-пульты с .bat-файлами.
SKIP_PARTS = ("__pycache__", ".claude", ".git", ".venv", "venv", "Для ")


def project_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if any(part in rel for part in SKIP_PARTS):
            continue
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(os.path.join(dirpath, name))
    return out


def module_name(path, root):
    rel = os.path.relpath(path, root).replace(os.sep, "/")[:-3]
    if rel.endswith("/__init__"):
        rel = rel[:-9]
    return rel.replace("/", ".")


def build(root):
    files = project_files(root)
    mods = {module_name(p, root): p for p in files}
    tops = {m.split(".")[0] for m in mods}
    info = {}

    for path in files:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        me = module_name(path, root)
        if os.path.basename(path) == "__init__.py":
            package = me
        elif "." in me:
            package = me.rsplit(".", 1)[0]
        else:
            package = ""

        imports = set()
        # ast.walk, а не tree.body: в этом проекте 300+ импортов спрятаны
        # ВНУТРИ функций (так разрывают кольцевые зависимости). Смотреть
        # только на шапку файла — значит не увидеть половину связей.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in mods:
                        imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.module:
                        base = (package + "." + node.module) if package else node.module
                    else:
                        base = package
                else:
                    base = node.module or ""
                    if base.split(".")[0] not in tops:
                        continue
                matched = False
                for alias in node.names:
                    candidate = base + "." + alias.name
                    if candidate in mods:
                        imports.add(candidate)
                        matched = True
                if not matched and base in mods:
                    imports.add(base)
        imports.discard(me)

        pub = [n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               and not n.name.startswith("_")]

        info[me] = {
            "path": os.path.relpath(path, root).replace(os.sep, "/"),
            "loc": src.count("\n") + 1,
            "imports": sorted(imports),
            "public": pub,
        }

    dependents = collections.defaultdict(set)
    for me, data in info.items():
        for dep in data["imports"]:
            dependents[dep].add(me)
    return info, dependents


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="корень проекта (по умолчанию текущая папка)")
    ap.add_argument("--hubs", action="store_true", help="только список узлов-хабов")
    ap.add_argument("--module", help="подробности по одному файлу, например services/rag.py")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    info, dependents = build(root)

    if args.module:
        target = args.module.replace(os.sep, "/")
        found = [m for m, d in info.items() if d["path"] == target or m == target]
        if not found:
            print("Такого файла в проекте нет: " + target)
            return 1
        me = found[0]
        data = info[me]
        deps = sorted(dependents.get(me, []))
        print("Файл:            " + data["path"])
        print("Модуль:          " + me)
        print("Строк:           %d" % data["loc"])
        print("Тянут его:       %d модулей" % len(deps))
        for d in deps:
            print("                 " + info[d]["path"])
        print("Сам тянет:       " + (", ".join(data["imports"]) or "ничего из проекта"))
        print("Публичные имена: " + (", ".join(data["public"]) or "нет"))
        return 0

    if args.hubs:
        print("Узлы, на которых держится проект (чем больше зависимых, тем дороже ошибка):")
        for me, who in sorted(dependents.items(), key=lambda kv: -len(kv[1]))[:15]:
            print("  %-28s тянут %2d модулей" % (info[me]["path"], len(who)))
        return 0

    print("| файл | строк | тянут его | кто именно |")
    print("|---|---:|---:|---|")
    for me, data in sorted(info.items(), key=lambda kv: kv[1]["path"]):
        who = sorted(info[d]["path"] for d in dependents.get(me, []))
        print("| `%s` | %d | %d | %s |" % (data["path"], data["loc"], len(who), ", ".join(who) or "—"))
    print()
    print("Всего файлов: %d" % len(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
