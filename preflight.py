#!/usr/bin/env python3
# ───────────────────────────────────────────────
#  preflight.py — предполётные проверки перед выкаткой (2026-07-31)
#
#  ЗАЧЕМ. Обновление кода на сервере идёт САМО, раз в 5 минут
#  (jobs.auto_update_loop). До сих пор перед выкаткой проверялись ровно две
#  вещи: компилируются ли файлы и грузятся ли четыре ключевых модуля. Этого
#  мало — самые частые поломки этого проекта компиляцию проходят насквозь:
#
#    • новое имя забыли вписать в re-export handlers/admin/__init__.py
#      → ImportError при старте, бот не поднимается вообще;
#    • добавили модель в config, а кнопку в раскладку — нет
#      → модель есть, выбрать её нельзя (карта зовёт это место самым частым);
#    • добавили кнопку, а ветку в роутер — нет
#      → кнопка нажимается и молча ничего не делает;
#    • панель переросла 4096 символов или 100 кнопок
#      → Telegram отказывается её слать, раздел просто не открывается.
#
#  Всё это ловится за пару секунд, БЕЗ токена, БЕЗ сети и БЕЗ боевой базы.
#  Зовёт проверки deploy.sh сразу после проверки синтаксиса: не прошли —
#  обновление откатывается на прежнюю версию, и бот остаётся живым.
#
#  ⚠️ ГЛАВНОЕ ПРАВИЛО ЭТОГО ФАЙЛА: он НИКОГДА не трогает боевую базу.
#  Первое, что делает main(), — переводит database/history.py на временную
#  базу во временной папке. Полезешь добавлять проверку, которая пишет в БД, —
#  убедись, что она идёт ПОСЛЕ этого перевода.
#
#  ⚠️ ВТОРОЕ ПРАВИЛО: проверки не должны ходить в сеть и требовать ключей.
#  Проверка, которая падает из-за молчащего Google, — это не проверка кода,
#  а лотерея, и однажды она откатит совершенно исправное обновление.
#
#  Запуск руками:  python preflight.py
#  Код возврата:   0 — всё в порядке, 1 — есть поломки (текст в выводе).
# ───────────────────────────────────────────────

import ast
import asyncio
import os
import sys
import tempfile

# Файлы и папки, которые НЕ импортируем при проверке импортов:
#   • сами проверки (мы уже внутри);
#   • точки входа со своими побочными действиями при загрузке — main.py
#     заводит логи, reset_db.py стирает таблицы, watchdog_local.py читает .env
#     и шлёт сообщения. Их синтаксис проверяет compileall в deploy.sh.
_SKIP_MODULES = {"preflight", "main", "reset_db", "watchdog_local"}

# Папки проекта, по которым ходим в поисках модулей.
# ⚠️ Пакет `data` УДАЛЁН 2026-08-05 вместе с последним своим файлом
# (data/quiz_questions.py — 12 вопросов викторины, зашитых в код). Вопросы
# теперь живут в базе, собираются по статьям базы знаний панелью /quizadm.
# ⚠️ Папку `web` (сайт-админка) сюда забыли добавить, когда её завели
# 30.08.2026 — и `web/longjobs.py` не проверялся ВООБЩЕ: его импортируют
# изнутри функций, то есть в момент нажатия кнопки на сайте. Список кормит
# и сбор кнопок (`_source_files` → `_callback_literals`), но кнопок Telegram
# в папке сайта нет, поэтому их счётчик от этой папки не меняется.
_PACKAGES = ("services", "handlers", "database", "jobs", "web")

ROOT = os.path.dirname(os.path.abspath(__file__))


# ───────────────────────────────────────────────
#  Мелкие помощники
# ─────────────────────────────────────────────

def _iter_module_names():
    """Имена всех модулей проекта в порядке импорта: сначала корень, потом пакеты."""
    for name in sorted(os.listdir(ROOT)):
        if name.endswith(".py") and name[:-3] not in _SKIP_MODULES:
            yield name[:-3]
    for pkg in _PACKAGES:
        folder = os.path.join(ROOT, pkg)
        if not os.path.isdir(folder):
            continue
        for dirpath, _dirs, files in os.walk(folder):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                mod = rel[:-3].replace(os.sep, ".")
                if mod.endswith(".__init__"):
                    mod = mod[: -len(".__init__")]
                yield mod


def _source_files():
    """Пути ко всем .py проекта — для проверок, читающих исходный текст."""
    out = [os.path.join(ROOT, n) for n in os.listdir(ROOT) if n.endswith(".py")]
    for pkg in _PACKAGES:
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, pkg)):
            out += [os.path.join(dirpath, n) for n in files if n.endswith(".py")]
    return sorted(out)


def _tree(path):
    with open(path, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


# ───────────────────────────────────────────────
#  1. Импорт всех модулей проекта
# ─────────────────────────────────────────────

def check_imports():
    """
    Загружает КАЖДЫЙ модуль бота. Ловит то, чего не видит компиляция: опечатку
    в имени, забытый re-export в пакете handlers/admin, пропавшую библиотеку,
    ошибку в самом config. Это же место поймает и кольцевой импорт.
    """
    import importlib
    problems = []
    count = 0
    for mod in _iter_module_names():
        try:
            importlib.import_module(mod)
            count += 1
        except Exception as e:
            problems.append(f"модуль {mod} не загружается: {type(e).__name__}: {e}")
    return problems, f"загружено модулей: {count}"


# ───────────────────────────────────────────────
#  2. Модели: реестр ↔ кнопки ↔ цепочки подстраховки
# ─────────────────────────────────────────────

def check_models():
    """
    Карта проекта зовёт это место самым частым источником расхождений: модель
    добавляют в config.AVAILABLE_MODELS, а кнопка сама не появляется — её
    раскладка собрана руками в handlers/admin/panel_main.py.
    Заодно проверяем, что все цепочки подстраховки ссылаются на существующие
    модели: имя с опечаткой молча выпадет из цепочки, и это заметят только
    в тот день, когда основная модель откажет.
    """
    import config
    from handlers.admin.panel_main import _MODEL_BUTTON_ROWS, _IMAGE_BUTTON_ROW

    problems = []
    models = set(config.AVAILABLE_MODELS)
    on_buttons = {name for row in _MODEL_BUTTON_ROWS for name in row}

    for name in sorted(models - on_buttons):
        problems.append(f"модель {name} есть в AVAILABLE_MODELS, но её нет на кнопках "
                        f"(_MODEL_BUTTON_ROWS в handlers/admin/panel_main.py)")
    for name in sorted(on_buttons - models):
        problems.append(f"кнопка модели {name} есть, а самой модели в AVAILABLE_MODELS нет")

    images = set(config.AVAILABLE_IMAGE_MODELS)
    image_buttons = set(_IMAGE_BUTTON_ROW)
    for name in sorted(images - image_buttons):
        problems.append(f"модель картинок {name} есть в конфиге, но не на кнопках (_IMAGE_BUTTON_ROW)")
    for name in sorted(image_buttons - images):
        problems.append(f"кнопка картинок {name} есть, а модели в AVAILABLE_IMAGE_MODELS нет")

    # Цепочки подстраховки и запасная модель
    chains = {
        "FALLBACK_MODEL": [config.FALLBACK_MODEL],
        "AUDIO_FALLBACK_CHAIN": list(getattr(config, "AUDIO_FALLBACK_CHAIN", [])),
        "VIDEO_FALLBACK_CHAIN": list(getattr(config, "VIDEO_FALLBACK_CHAIN", [])),
    }
    for chain_name, names in chains.items():
        for name in names:
            if name not in models:
                problems.append(f"{chain_name}: модели {name} нет в AVAILABLE_MODELS")

    # Видео уходит только моделям с полем "video" — иначе запрос пойдёт туда,
    # где его не поймут (см. раздел 3 карты).
    for name in chains["VIDEO_FALLBACK_CHAIN"]:
        info = config.AVAILABLE_MODELS.get(name, {})
        if info and not info.get("video"):
            problems.append(f"VIDEO_FALLBACK_CHAIN: у модели {name} нет поля \"video\": True")

    return problems, f"моделей {len(models)}, кнопок {len(on_buttons)}, картинок {len(images)}"


# ───────────────────────────────────────────────
#  3. Провайдеры: значок, панель API, отчёты о расходах
# ─────────────────────────────────────────────

def check_providers():
    """
    Провайдер описывается ОДНОЙ записью в `config.PROVIDERS` (2026-08-03), и
    из неё собираются панель «📡 Настройки API», отчёты о расходах и экран
    «💰 Счета и квоты». Поэтому проверяем не «упомянут ли он в тексте функций»
    (так было до реестра), а три вещи по существу:
      1. у каждого провайдера моделей есть запись в реестре;
      2. запись ПОЛНАЯ — ни одного пропущенного поля (опечатка в имени поля
         иначе всплыла бы только на боевой панели);
      3. провайдер с деньгами попал в кортежи порядка на экране счетов —
         это последние списки, которые ведутся руками, и забытый в них
         провайдер потерял бы кнопку правки остатка.
    """
    import config
    from handlers.admin import panel_balance

    problems = []
    fields = {"icon", "title", "calls_label", "money_label", "cost_key",
              "balance_key", "console_url", "balance_btn",
              "monthly_reset", "report_approx", "quota_tokens"}

    providers = {info.get("provider") for info in config.AVAILABLE_MODELS.values()}
    providers.discard(None)
    for provider in sorted(providers):
        if provider not in config.PROVIDERS:
            problems.append(f"у провайдера {provider} нет записи в config.PROVIDERS — "
                            f"он не покажется ни в панели API, ни в отчётах")

    for pid, meta in config.PROVIDERS.items():
        missing = fields - set(meta)
        if missing:
            problems.append(f"в записи провайдера {pid} нет полей: {', '.join(sorted(missing))}")
        if meta.get("cost_key") and not meta.get("console_url"):
            problems.append(f"у провайдера {pid} есть копилка расхода, но нет console_url — "
                            f"сумма в панели API станет ссылкой в никуда")
        if meta.get("balance_key") and not meta.get("balance_btn"):
            problems.append(f"у провайдера {pid} есть счёт, но нет надписи кнопки balance_btn")
        if meta.get("cost_key") and pid not in panel_balance._COST_ORDER:
            problems.append(f"провайдер {pid} не попал в panel_balance._COST_ORDER — "
                            f"его не будет на экране «Обнулить потрачено»")
        if meta.get("balance_key") and pid not in panel_balance._BALANCE_ORDER:
            problems.append(f"провайдер {pid} не попал в panel_balance._BALANCE_ORDER — "
                            f"его остаток нельзя будет поправить кнопкой")

    return problems, f"провайдеров: {len(providers)} ({', '.join(sorted(providers))})"


# ───────────────────────────────────────────────
#  4. Таблицы базы: init_db ↔ reset_db.USER_TABLES
# ─────────────────────────────────────────────

def check_tables():
    """
    Новая таблица заводится в двух местах, и второе забывают: без неё
    reset_db.py оставит таблицу нетронутой при сбросе. Сверяем по факту —
    создаём базу во временной папке и смотрим, что в ней получилось.
    """
    import sqlite3
    from database import history as hist

    problems = []
    hist.init_db()
    # Путь берём из config — там же, где его увела main() (см. пояснение там).
    import config
    with sqlite3.connect(config.DB_PATH) as con:
        real = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )} - {"sqlite_sequence"}

    # reset_db читаем текстом: импортировать его нельзя — он про стирание.
    reset_src = _tree(os.path.join(ROOT, "reset_db.py"))
    listed = set()
    for node in ast.walk(reset_src):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "USER_TABLES":
                    listed = {el.value for el in node.value.elts
                              if isinstance(el, ast.Constant)}

    # settings в USER_TABLES нет намеренно: она стирается только при
    # KEEP_SETTINGS=False и дописывается там отдельно.
    for name in sorted(real - listed - {"settings"}):
        problems.append(f"таблица {name} создаётся в init_db, но её нет в reset_db.USER_TABLES")
    for name in sorted(listed - real):
        problems.append(f"таблица {name} значится в reset_db.USER_TABLES, но init_db её не создаёт")

    return problems, f"таблиц в базе: {len(real)}, в списке сброса: {len(listed)}"


# ───────────────────────────────────────────────
#  5. Звания викторины: границы по возрастанию, без дыр
# ─────────────────────────────────────────────

def check_ranks():
    """
    QUIZ_RANKS обязан идти по возрастанию без пропусков: попавший в «дыру»
    получит первое звание списка (см. config.py). Проверка дешёвая, а поломка
    тихая — человек просто получает не своё звание.
    """
    from config import QUIZ_RANKS
    problems = []
    prev_max = None
    for rank in QUIZ_RANKS:
        low, high = rank["min"], rank["max"]
        if low > high:
            problems.append(f"звание «{rank['name']}»: min {low} больше max {high}")
        if prev_max is not None and low != prev_max + 1:
            problems.append(f"звание «{rank['name']}»: начинается с {low}, "
                            f"а предыдущее кончилось на {prev_max} — дыра или нахлёст")
        prev_max = high
    return problems, f"званий: {len(QUIZ_RANKS)}"


# ───────────────────────────────────────────────
#  6. Кнопки ↔ роутер: у каждой кнопки есть обработчик
# ─────────────────────────────────────────────

def _router_patterns():
    """
    Достаёт из handlers/admin/router.py всё, что он умеет разбирать:
    точные значения (data == "…", data in (…)) и приставки (data.startswith("…")).
    Читаем сам роутер, а не свой список: второй список — это второй источник
    правды, и он разъедется с кодом в первую же правку.
    """
    exact, prefix = set(), set()
    for node in ast.walk(_tree(os.path.join(ROOT, "handlers", "admin", "router.py"))):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "data":
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                    exact.add(comp.value)
                elif isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                    exact.update(el.value for el in comp.elts if isinstance(el, ast.Constant))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "data"):
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    prefix.add(arg.value)
                elif isinstance(arg, (ast.Tuple, ast.List)):
                    prefix.update(el.value for el in arg.elts if isinstance(el, ast.Constant))
    return exact, prefix


def _callback_literals():
    """
    Все callback_data из кода панелей: (что за строка, файл, строка).
    У собранных из кусков (f-строки) берём НЕИЗМЕННОЕ НАЧАЛО — «usr:card:»
    из f"usr:card:{id}". Этого хватает: роутер и разбирает такие кнопки по
    приставке.
    """
    out = []
    for path in _source_files():
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "callback_data":
                    continue
                value = kw.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    out.append((value.value, rel, value.lineno))
                elif isinstance(value, ast.JoinedStr):
                    head = ""
                    for part in value.values:
                        if isinstance(part, ast.Constant) and isinstance(part.value, str):
                            head += part.value
                        else:
                            break
                    if head:
                        out.append((head, rel, value.lineno))

    # ⚠️ КНОПКИ, СОБРАННЫЕ f-СТРОКОЙ С ПЕРЕМЕННОЙ В НАЧАЛЕ, разбор выше НЕ
    # видит: неизменного начала у них нет, и они молча выпадают из проверки.
    # Так случилось 2026-08-03 с восемью кнопками подтверждения сброса
    # промптов (`f"{name}_reset_confirm"`) — счётчик проверенных кнопок тихо
    # упал со 140 до 132, и никто бы не заметил. Достраиваем их ИЗ ТОЙ ЖЕ
    # таблицы, по которой их строит панель.
    try:
        from handlers.admin.panel_prompts import _PROMPTS
        for name in _PROMPTS:
            for action in ("confirm", "cancel"):
                out.append((f"{name}_reset_{action}",
                            "handlers/admin/panel_prompts.py (собрана f-строкой)", 0))
    except Exception:
        pass

    # Та же болезнь у кнопок подтверждения «🧹Очистить РАЗГОВОРЫ»:
    # f"{prefix}:wipe_yes" в _handle_proactive_wipe. Приставка стоит В НАЧАЛЕ,
    # значит неизменного начала у строки нет и разбор выше её не видит —
    # достраиваем руками.
    # ⚠️ 2026-08-10: приставка теперь ОДНА. Подтверждение осталось только у
    # кнопки из панели промптов; в /adm его убрали по просьбе Максима, и пары
    # «adm:wipe_yes/no» больше не существует. Проверка честно поймала их как
    # «нажатие ничего не сделает» — оставляю здесь только живую приставку.
    for action in ("wipe_yes", "wipe_no"):
        out.append((f"proactive:{action}",
                    "handlers/admin/panel_prompts.py (собрана f-строкой)", 0))
    return out


def check_callbacks():
    """
    Кнопка, которой нет в роутере, нажимается и молча ничего не делает —
    самая обидная поломка: выглядит как «бот сломался», а в логе тишина.
    """
    exact, prefix = _router_patterns()
    problems = []
    seen = 0
    for data, rel, line in _callback_literals():
        seen += 1
        if data in exact:
            continue
        if any(data.startswith(p) for p in prefix):
            continue
        problems.append(f"кнопка «{data}» ({rel}:{line}) не разбирается роутером "
                        f"— нажатие ничего не сделает")
    return problems, f"кнопок проверено: {seen}, веток в роутере: {len(exact)} точных + {len(prefix)} по приставке"


# ───────────────────────────────────────────────
#  7. Сборка панелей: собираются, влезают в лимиты Telegram
# ─────────────────────────────────────────────

class _FakeApplication:
    """Заглушка приложения: панель базы знаний кладёт в bot_data карту статей."""
    def __init__(self):
        self.bot_data = {}


class _FakeContext:
    """
    Заглушка контекста для панелей, которым он нужен (панель базы знаний).
    Держит ровно то, что панели читают и пишут при СБОРКЕ: user_data (режим
    проверки поиска) и application.bot_data (карта токенов статей — длинные
    имена файлов в callback_data не влезают). Бота здесь нет намеренно:
    сборка панели не должна ничего отправлять, а если однажды начнёт —
    проверка честно упадёт на его отсутствии, и это будет верный сигнал.
    """
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.application = _FakeApplication()
        self.bot_data = self.application.bot_data


def check_panels():
    """
    Собирает каждую панель на ВРЕМЕННОЙ базе и меряет её по лимитам Telegram:
    4096 символов текста и ~100 кнопок на сообщение. Панель, перешагнувшая
    лимит, не отправляется вовсе — раздел просто перестаёт открываться, и в
    логе будет только ошибка отправки, без намёка на причину.

    Владельца при сборке изображает id 1: панели собираются по правам
    смотрящего, а нам нужен самый полный вариант — в нём больше всего кнопок.
    """
    from services import roles, user_settings
    from handlers.admin.panel_balance import _build_balance_panel, _build_cost_panel
    from handlers.admin.panel_main import (_build_api_keyboard,
                                           _build_model_panel_text_and_keyboard,
                                           build_adm_keyboard)
    from handlers.admin.panel_mod import _build_mod_panel_text_and_keyboard
    from handlers.admin.panel_prompts import (_build_proactive_stats_panel,
                                              _build_prompt_panel_text_and_keyboard)
    from handlers.admin.panel_quiz import _build_panel as _build_quiz_panel
    from handlers.admin.panel_rag import _build_rag_panel
    from handlers.admin.panel_updates import _build_updates_panel
    from handlers.admin.panel_users import _build_staff_log_panel, _build_users_panel
    import config

    roles.load()
    user_settings.load()
    owner = config.ADMIN_IDS[0] if config.ADMIN_IDS else 1
    ctx = _FakeContext()

    def kb_ctx(screen: str):
        """
        Контекст с ОТКРЫТЫМ экраном панели базы знаний (2026-08-12): у неё их
        три — разделы, список раздела, настройки поиска, — и собираться обязан
        каждый. С пустым user_data проверялся бы только первый.
        ⚠️ На пустой базе (свежий клон, CI) список раздела показать нечего, и
        сборщик честно возвращает экран разделов. Проверка от этого не врёт:
        она сторожит сборку и лимиты, а не наличие статей.
        """
        c = _FakeContext()
        c.user_data["kb_screen"] = screen
        return c

    active = config.GEMINI_MODEL
    active_image = next(iter(config.AVAILABLE_IMAGE_MODELS), "")

    panels = {
        "/adm": lambda: ("", build_adm_keyboard(owner)),
        "модели и API": lambda: _build_model_panel_text_and_keyboard(active, active_image, owner),
        "клавиатура API": lambda: ("", _build_api_keyboard(owner)),
        "счета и квоты": _build_balance_panel,
        "правка «потрачено»": _build_cost_panel,
        "промпты": lambda: _build_prompt_panel_text_and_keyboard(owner, "c4max_bot"),
        "итоги участия": _build_proactive_stats_panel,
        "модерация": lambda: _build_mod_panel_text_and_keyboard(owner),
        "список пользователей": lambda: _build_users_panel(owner, 999),
        "журнал персонала": _build_staff_log_panel,
        # ⬇️ Обновления: список читается из журнала git. В CI клон бывает
        # обрезанным (одна запись) — панель тогда просто короче, но собраться
        # обязана и на пустой истории.
        "обновления": lambda: _build_updates_panel(0),
        "база знаний": lambda: _build_rag_panel(ctx, owner),
        "база знаний: раздел": lambda: _build_rag_panel(kb_ctx("ground"), owner),
        "база знаний: настройки": lambda: _build_rag_panel(kb_ctx("settings"), owner),
        "викторина": lambda: _build_quiz_panel(ctx),
    }

    problems = []
    for name, build in panels.items():
        try:
            result = build()
        except Exception as e:
            problems.append(f"панель «{name}» не собирается: {type(e).__name__}: {e}")
            continue
        text, markup = result if isinstance(result, tuple) else ("", result)
        if text and len(text) > 4096:
            problems.append(f"панель «{name}»: текст {len(text)} символов — Telegram примет только 4096")
        buttons = sum(len(row) for row in markup.inline_keyboard) if markup else 0
        if buttons > 100:
            problems.append(f"панель «{name}»: {buttons} кнопок — Telegram примет около 100")
    return problems, f"панелей собрано: {len(panels)}"


# ───────────────────────────────────────────────
#  8. Регистрация обработчиков
# ─────────────────────────────────────────────

def check_handlers():
    """
    Собирает настоящее приложение Telegram (без сети и без боевого токена) и
    просит его зарегистрировать все обработчики — ровно то, что делает бот при
    старте. Ловит опечатку в имени обработчика и неверный фильтр: такое
    проявляется только в момент запуска, то есть уже на сервере.
    """
    from telegram.ext import ApplicationBuilder
    from handlers import setup_handlers

    # Токен ненастоящий, но правильной формы: сеть тут не нужна, а на кривой
    # форме telegram упал бы ещё на сборке.
    app = ApplicationBuilder().token("123456789:AAEeTestTokenForPreflightChecksOnly").build()
    setup_handlers(app)
    total = sum(len(v) for v in app.handlers.values())
    if total == 0:
        return ["ни одного обработчика не зарегистрировано"], ""
    return [], f"обработчиков: {total} в {len(app.handlers)} группах"


def check_web():
    """
    Веб-админка: сверяет таблицу адресов web/routes.py::ROUTES с тем, что
    реально собралось в приложении, и — главное — что КАЖДЫЙ адрес с пометкой
    "owner" правда закрыт входом.

    Проверка вида «забыл ли автор проверку доступа» здесь не теоретическая:
    у кнопок бота от этого страхует запрет по умолчанию в services/roles.py,
    а у страниц сайта такого запрета нет — обёртку ставит сборка приложения,
    и достаточно один раз добавить адрес мимо таблицы, чтобы страница
    открылась кому угодно. Поэтому обёртку тут не «читают», а дёргают:
    обработчик зовётся с пустыми куками и обязан ответить «войдите».

    ⚠️ Список открытых адресов зашит НАРОЧНО. Новый открытый адрес уронит
    проверку — это не помеха, а вопрос «ты точно хочешь пускать сюда без
    входа?», заданный до выкатки, а не после.
    """
    problems = []
    try:
        from web import ROUTES, build_app
    except ImportError as e:
        return [f"пакет web не собирается: {e}"], ""

    allowed_open = {"/enter", "/health"}

    # 1) Таблица сама по себе: обработчик вызываемый, доступ известного вида,
    #    открытые адреса — только из разрешённого списка.
    for method, path, handler, access in ROUTES:
        if not callable(handler):
            problems.append(f"{method} {path}: обработчик не вызывается")
        if access not in ("owner", "open"):
            problems.append(f"{method} {path}: неизвестный уровень входа «{access}»")
        if access == "open" and path not in allowed_open:
            problems.append(f"{method} {path}: открыт без входа, а в списке открытых его нет")

    # 2) Приложение собирается, и в нём ровно те адреса, что в таблице.
    try:
        app = build_app()
    except Exception as e:
        return problems + [f"приложение сайта не собирается: {e}"], ""

    built = {(r.method, r.resource.canonical) for r in app.router.routes()
             if r.method != "HEAD" and not r.resource.canonical.startswith("/static")}
    listed = {(m, p) for m, p, _, _ in ROUTES}
    for extra in sorted(built - listed):
        problems.append(f"{extra[0]} {extra[1]}: адрес есть в приложении, но не в ROUTES")
    for missing in sorted(listed - built):
        problems.append(f"{missing[0]} {missing[1]}: адрес есть в ROUTES, но не собрался")

    # 3) Каждый «owner» правда закрыт: зовём собранный обработчик без кук.
    class _NoCookies:
        cookies = {}

    closed = 0
    for route in app.router.routes():
        if route.method == "HEAD" or route.resource.canonical.startswith("/static"):
            continue
        access = next((a for m, p, _, a in ROUTES
                       if m == route.method and p == route.resource.canonical), None)
        if access != "owner":
            continue
        try:
            response = asyncio.run(route.handler(_NoCookies()))
        except Exception as e:
            problems.append(f"{route.method} {route.resource.canonical}: "
                            f"обработчик упал на проверке входа ({e})")
            continue
        if getattr(response, "status", None) != 401:
            problems.append(f"{route.method} {route.resource.canonical}: "
                            f"пускает без входа (ответ {getattr(response, 'status', '?')})")
        else:
            closed += 1

    # 4) Оформление на месте — без него страницы открываются голым текстом.
    css = os.path.join(ROOT, "web", "static", "style.css")
    if not os.path.exists(css):
        problems.append("нет web/static/style.css")

    return problems, f"адресов сайта: {len(ROUTES)}, из них под входом: {closed}"


# ───────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────

CHECKS = (
    ("импорт модулей", check_imports),
    ("модели и кнопки", check_models),
    ("провайдеры", check_providers),
    ("таблицы базы", check_tables),
    ("звания викторины", check_ranks),
    ("кнопки и роутер", check_callbacks),
    ("сборка панелей", check_panels),
    ("регистрация обработчиков", check_handlers),
    ("адреса веб-админки", check_web),
)


def main() -> int:
    sys.path.insert(0, ROOT)

    # ⚠️ ПЕРВЫМ ДЕЛОМ уводим базу во временную папку. Всё, что проверки пишут
    # и читают, происходит там; боевая history.db не открывается ни разу.
    #
    # ⚠️ ПОДМЕНЯЕМ config.DB_PATH, А НЕ history.DB_PATH (02.09.2026). Соединение
    # с базой открывает database/_core.py и берёт путь из config в момент
    # открытия — правка внутренностей history на него больше не влияет. Пока
    # здесь стояло hist.DB_PATH, проверка честно покраснела «init_db не создаёт
    # таблицы»; молчаливый исход был бы хуже — запись в боевую history.db.
    tmp_dir = tempfile.mkdtemp(prefix="c4max-preflight-")
    import config
    config.DB_PATH = os.path.join(tmp_dir, "preflight.db")
    from database import history as hist   # ниже: hist.close_db() при выходе

    # Логи проверок не нужны: они утопили бы вывод, а deploy.sh показывает
    # человеку последние строки. Оставляем только собственную печать.
    import logging
    logging.disable(logging.CRITICAL)

    failures = []
    try:
        for title, check in CHECKS:
            try:
                problems, note = check()
            except Exception as e:
                problems, note = [f"проверка сорвалась: {type(e).__name__}: {e}"], ""
            if problems:
                failures.extend(problems)
                print(f"❌ {title}")
                for p in problems:
                    print(f"   • {p}")
            else:
                print(f"✅ {title}" + (f" — {note}" if note else ""))
    finally:
        # Временная база больше не нужна. Убираем за собой сами: проверки
        # зовутся при каждом обновлении, а мусор в /tmp никто не выметает.
        import shutil
        hist.close_db()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("───────────────────────────")
    if failures:
        # ⚠️ Поломки повторяем В САМОМ КОНЦЕ намеренно: deploy.sh кладёт в
        # сообщение об откате ПОСЛЕДНИЕ строки вывода (tail -3). Без повтора
        # Максим получил бы «не прошло, поломок 3» без единого слова о том,
        # что именно сломалось, и лез бы на сервер за подробностями.
        for problem in failures[:2]:
            print(f"❌ {problem}")
        print(f"НЕ ПРОШЛО: поломок {len(failures)} (подробности выше)")
        return 1
    print("ВСЁ В ПОРЯДКЕ: можно выкатывать")
    return 0


if __name__ == "__main__":
    sys.exit(main())
