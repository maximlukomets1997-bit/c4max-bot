#!/usr/bin/env python3
# ───────────────────────────────────────────────
#  selftest.py — проверки ПОВЕДЕНИЯ (2026-08-28)
#
#  ЗАЧЕМ. `preflight.py` проверяет ПРОВОДКУ: грузятся ли модули, сходятся ли
#  кнопки с роутером, влезают ли панели в лимиты Telegram. Он отвечает на
#  вопрос «бот запустится?» — и до сих пор это была ЕДИНСТВЕННАЯ автоматическая
#  проверка в проекте. На вопрос «а считает ли он правильно?» не отвечал никто,
#  и каждая правка проверялась только руками.
#
#  Этот файл отвечает на второй вопрос. Он берёт функции, где ошибка ДОРОГА и
#  НЕЗАМЕТНА, и сверяет их ответы с ожидаемыми:
#
#    • ДЕНЬГИ — стоимость запросов к четырём провайдерам. Ошибка всплывёт
#      суточным отчётом на следующий день, а до тех пор счёт будет врать тихо.
#    • ПОМЕТКА МУТА — разбор [МУТ:секунды] из ответа модели. Ошибка либо
#      наказывает невиновного, либо оставляет пометку висеть в чате.
#    • ПРАВА ДОСТУПА — кто какую кнопку может нажать. Ошибка либо открывает
#      чужую панель, либо запирает владельца.
#
#  ⚠️ ПРАВИЛА ЭТОГО ФАЙЛА — те же, что у preflight.py, и по тем же причинам:
#    • боевую базу НЕ трогаем (main() первым делом уводит её во временную);
#    • в сеть НЕ ходим и ключей не требуем — проверка, падающая из-за
#      молчащего Google, однажды откатит совершенно исправное обновление.
#
#  ⚠️ ЧЕГО ЭТОТ ФАЙЛ НЕ ЛОВИТ. Он проверяет функции ПО ОТДЕЛЬНОСТИ. Ошибку
#  вида «обе части верны, а соединены неправильно» он не увидит — для этого
#  нужен живой бот. Ручной прогон после правок никуда не девается, он просто
#  становится короче.
#
#  ⚠️ ОЖИДАНИЯ СЧИТАЮТСЯ НЕЗАВИСИМО ОТ КОДА. В проверках денег суммы
#  выводятся арифметикой прямо здесь, из цен config. Списать формулу из
#  services/gemini.py означало бы проверять код им же самим: обе стороны
#  ошиблись бы одинаково, и проверка позеленела бы на сломанном расчёте.
#
#  Запуск руками:  python selftest.py
#  Код возврата:   0 — всё в порядке, 1 — есть поломки (текст в выводе).
# ───────────────────────────────────────────────

import os
import pathlib
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))

# Насколько две суммы в долларах считаются одинаковыми. Числа дробные, и
# сравнивать их «в лоб» нельзя: 0.1 + 0.2 в любом языке даёт не ровно 0.3.
# Десять знаков после запятой — заведомо точнее любой реальной суммы.
_MONEY_EPS = 1e-10


def _same_money(got, expected) -> bool:
    """Совпали ли суммы с точностью до копеечной пыли."""
    if got is None or expected is None:
        return got is expected
    return abs(got - expected) < _MONEY_EPS


# ───────────────────────────────────────────────
#  1. ДЕНЬГИ
# ─────────────────────────────────────────────

def check_money():
    """
    Стоимость запроса к каждому провайдеру.

    Проверяются не только «обычные» ответы, но и кривые, которые API реально
    присылает: кэш-полей нет вовсе; кэша больше, чем всего входа. Второе — не
    выдумка: поле приходит от провайдера, и если однажды оно окажется больше
    prompt_tokens, наивный расчёт уйдёт в МИНУС и тихо уменьшит суточный счёт.
    """
    from config import QWEN_PRICES, DEEPSEEK_PRICES, XIAOMI_PRICES, IMAGE_PRICES
    from services.gemini import _qwen_cost, _deepseek_cost, _xiaomi_cost, _image_cost

    problems = []
    done = 0

    def expect(title, got, want):
        nonlocal done
        done += 1
        if not _same_money(got, want):
            problems.append(f"{title}: ожидалось ${want:.8f}, получилось "
                            f"${got if got is not None else 0:.8f}")

    # ── Qwen: вход частично из кэша ──
    model = "qwen3.8-max"
    p = QWEN_PRICES[model]
    usage = {"prompt_tokens": 1000, "completion_tokens": 500,
             "prompt_tokens_details": {"cached_tokens": 400}}
    want = (400 * p["cache_hit"] + 600 * p["cache_miss"] + 500 * p["output"]) / 1_000_000
    expect("Qwen, вход частично из кэша", _qwen_cost(model, usage), want)

    # ── Qwen: кэш-поля нет → весь вход по ПОЛНОЙ цене (не занижаем расход) ──
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    want = (1000 * p["cache_miss"] + 500 * p["output"]) / 1_000_000
    expect("Qwen, кэш-поля нет", _qwen_cost(model, usage), want)

    # ── Qwen: кэша БОЛЬШЕ, чем всего входа (кривой ответ API) ──
    # Ожидание: лишнее обрезается, в минус не уходим.
    usage = {"prompt_tokens": 100, "completion_tokens": 0,
             "prompt_tokens_details": {"cached_tokens": 900}}
    want = (100 * p["cache_hit"]) / 1_000_000
    got = _qwen_cost(model, usage)
    expect("Qwen, кэша больше чем входа", got, want)
    done += 1
    if got is not None and got < 0:
        problems.append("Qwen: стоимость получилась ОТРИЦАТЕЛЬНОЙ — "
                        "такой запрос уменьшит суточный счёт")

    # ── Неизвестная модель → None, а не ноль и не падение ──
    done += 1
    if _qwen_cost("такой-модели-нет", {"prompt_tokens": 10}) is not None:
        problems.append("Qwen: у неизвестной модели должна быть цена None, "
                        "иначе расход посчитается как ноль и незаметно потеряется")

    # ── DeepSeek: пик и вне пика — РАЗНЫЕ суммы, пик дороже ──
    model = "deepseek-v4-pro"
    table = DEEPSEEK_PRICES[model]
    usage = {"prompt_cache_hit_tokens": 300, "prompt_cache_miss_tokens": 700,
             "completion_tokens": 400}
    for peak in (True, False):
        pr = table["peak" if peak else "offpeak"]
        want = (300 * pr["cache_hit"] + 700 * pr["cache_miss"]
                + 400 * pr["output"]) / 1_000_000
        expect(f"DeepSeek, {'пик' if peak else 'вне пика'}",
               _deepseek_cost(model, usage, peak), want)

    done += 1
    if not (_deepseek_cost(model, usage, True) > _deepseek_cost(model, usage, False)):
        problems.append("DeepSeek: пиковый тариф вышел НЕ дороже дневного — "
                        "проверь колонки цен в config.DEEPSEEK_PRICES")

    # ── DeepSeek: кэш-полей нет → весь вход считается «без кэша» ──
    usage = {"prompt_tokens": 1000, "completion_tokens": 200}
    pr = table["offpeak"]
    want = (1000 * pr["cache_miss"] + 200 * pr["output"]) / 1_000_000
    expect("DeepSeek, кэш-полей нет", _deepseek_cost(model, usage, False), want)

    # ── Xiaomi: кэш ограничен размером входа ──
    model = "mimo-v2.5"
    p = XIAOMI_PRICES[model]
    usage = {"prompt_tokens": 800, "completion_tokens": 300,
             "prompt_tokens_details": {"cached_tokens": 200}}
    want = (200 * p["cache_hit"] + 600 * p["cache_miss"] + 300 * p["output"]) / 1_000_000
    expect("Xiaomi, вход частично из кэша", _xiaomi_cost(model, usage), want)

    usage = {"prompt_tokens": 800, "completion_tokens": 300}
    want = (800 * p["cache_miss"] + 300 * p["output"]) / 1_000_000
    expect("Xiaomi, кэш-поля нет", _xiaomi_cost(model, usage), want)

    # ── Картинки: считаются по МОДАЛЬНОСТЯМ, картинка дороже текста ──
    model = "gemini-3.1-flash-image"
    p = IMAGE_PRICES[model]
    usage = {"promptTokensDetails": [{"modality": "TEXT", "tokenCount": 20}],
             "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120},
                                         {"modality": "TEXT", "tokenCount": 10}]}
    want = (20 * p["in"] + 1120 * p["img_out"] + 10 * p["txt_out"]) / 1_000_000
    expect("Картинка, разбор по модальностям", _image_cost(model, usage), want)

    # ── Картинки: модальностей нет → откат на общий счётчик входа ──
    usage = {"promptTokenCount": 20,
             "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}]}
    want = (20 * p["in"] + 1120 * p["img_out"]) / 1_000_000
    expect("Картинка, модальностей нет", _image_cost(model, usage), want)

    # ── Нули не должны падать: на бесплатных вариантах приходят именно они ──
    done += 1
    zero = _qwen_cost("qwen3.8-max", {"prompt_tokens": 0, "completion_tokens": 0})
    if zero != 0.0:
        problems.append(f"Qwen: пустой запрос должен стоить ровно 0, вышло {zero}")

    return problems, f"{done} проверок: Qwen, DeepSeek, Xiaomi, картинки"


# ───────────────────────────────────────────────
#  2. ПОМЕТКА МУТА
# ─────────────────────────────────────────────

def check_mute_tag():
    """
    Разбор пометки [МУТ:секунды] из ответа модели.

    ⚠️ Отдельно проверяется правило «пометка в РАЗМЫШЛЕНИЯХ — не команда»
    (докстринг _extract_mute): рассуждая вслух, модель может привести пометку
    как пример. Наказать за это нельзя, но и показывать её людям нельзя —
    из текста она обязана исчезнуть в обоих случаях.
    """
    from config import PROACTIVE_MUTE_MAX_SEC
    from services.proactive import _extract_mute

    problems = []
    done = 0

    def expect(title, answer, want_text, want_sec):
        nonlocal done
        done += 1
        text, sec = _extract_mute(answer)
        if sec != want_sec:
            problems.append(f"{title}: срок мута ожидался {want_sec}, вышло {sec}")
        if text != want_text:
            problems.append(f"{title}: текст ожидался {want_text!r}, вышло {text!r}")

    expect("обычная пометка", "Остынь. [МУТ:300]", "Остынь.", 300)
    expect("латинское MUTE", "Остынь. [MUTE:300]", "Остынь.", 300)
    expect("другой регистр", "Остынь. [мут:300]", "Остынь.", 300)
    expect("пробелы внутри", "Остынь. [ МУТ : 300 ]", "Остынь.", 300)
    expect("пометки нет", "Просто реплика.", "Просто реплика.", None)
    expect("ноль секунд", "Остынь. [МУТ:0]", "Остынь.", None)

    # Потолок: просят больше суток — получают ровно потолок
    expect("срок сверх потолка",
           f"Остынь. [МУТ:{PROACTIVE_MUTE_MAX_SEC * 5}]", "Остынь.",
           PROACTIVE_MUTE_MAX_SEC)

    # Пометка ТОЛЬКО в мыслях: не команда, но из текста вырезана.
    # ⚠️ Сами размышления _extract_mute НЕ срезает и не должна: их снимает
    # strip_thoughts дальше по пути (services/proactive.py). Здесь проверяем
    # ровно одно — пометка исчезла, мысли остались нетронутыми.
    expect("пометка только в размышлениях",
           "<thought>можно было бы [МУТ:600]</thought>Спокойно, ребята.",
           "<thought>можно было бы </thought>Спокойно, ребята.", None)

    # Пометка и в мыслях, и в ответе: команда — та, что в видимой части
    done += 1
    text, sec = _extract_mute("<thought>примерно [МУТ:600]</thought>Хватит. [МУТ:120]")
    if sec != 120:
        problems.append(f"пометка в мыслях И в ответе: ожидался срок 120, вышло {sec}")
    if "МУТ" in text.upper():
        problems.append(f"пометка в мыслях И в ответе: пометка осталась в тексте: {text!r}")

    # ── Служебная пометка не должна доезжать до чата НИ В КАКОМ виде ──
    # ⚠️ Здесь проверяется правило, которое уже нарушалось (28.08.2026):
    # пометка, которую не удалось РАЗОБРАТЬ, всё равно обязана быть ВЫРЕЗАНА.
    # Мута при этом нет — по мусору наказывать нельзя, — но и показывать
    # людям служебный маркер нельзя тем более.
    for junk in ("[МУТ:900]",            # обычная
                 "[МУТ:604800]",         # шесть цифр: раньше уезжало в чат
                 "[МУТ:99999999999]",    # больше семи цифр — разобрать нельзя
                 "[МУТ:абв]",            # вообще не число
                 "[МУТ: 300 сек]",       # число с приписками
                 "[MUTE:604800]"):       # то же латиницей
        done += 1
        text, _ = _extract_mute(f"Всё, тишина. {junk}")
        if "[" in text or "МУТ" in text.upper() or "MUTE" in text.upper():
            problems.append(f"пометка {junk} осталась в тексте для чата: {text!r}")

    # ── А вот обычные слова в скобках вырезать НЕЛЬЗЯ ──
    # Шаблон вырезания широкий, и легко перестараться: «[МУТАЦИЯ]» — это
    # обычное слово, а не команда, и текст человека портить нельзя.
    for keep in ("[МУТАЦИЯ]", "[мутный тип]"):
        done += 1
        text, sec = _extract_mute(f"Смотри: {keep} вот так.")
        if keep not in text:
            problems.append(f"обычное слово {keep} вырезано из текста как пометка: {text!r}")
        if sec is not None:
            problems.append(f"обычное слово {keep} принято за команду мута: {sec}")

    return problems, f"{done} проверок: потолок, регистр, латиница, мысли, мусор"


# ───────────────────────────────────────────────
#  3. ПРАВА ДОСТУПА
# ─────────────────────────────────────────────

def check_permissions():
    """
    Кто какую кнопку может нажать и к кому применить меры.

    ⚠️ Главное правило, ради которого эта проверка существует, — ЗАПРЕТ ПО
    УМОЛЧАНИЮ (шапка services/roles.py): кнопки, которой нет в таблице, не
    должно быть у модератора. Забытая новая кнопка обязана оказаться
    недоступной, а не открыться всем.

    Кэш прав заполняется здесь руками, база не нужна: `_perms` ходит в неё
    только при `_loaded = False`.
    """
    from services import roles

    problems = []
    done = 0

    # Расстановка: владелец, модератор с «мут», модератор с «карточки+правка»,
    # обычный участник. Кэш и список владельцев подменяем на время проверки.
    OWNER, MOD_MUTE, MOD_CARDS, PLAIN = 1, 2, 3, 4
    saved_cache = dict(roles._cache)
    saved_loaded = roles._loaded
    saved_admins = roles.ADMIN_IDS
    roles.ADMIN_IDS = (OWNER,)
    roles._cache.clear()
    roles._cache[MOD_MUTE] = {"user_id": MOD_MUTE, "p_mod": 1, "p_ban": 0,
                              "p_cards": 0, "p_cards_edit": 0}
    roles._cache[MOD_CARDS] = {"user_id": MOD_CARDS, "p_mod": 0, "p_ban": 0,
                               "p_cards": 1, "p_cards_edit": 1}
    roles._loaded = True

    try:
        def expect_press(title, user, data, want):
            nonlocal done
            done += 1
            got = roles.may_press(user, data)
            if got != want:
                problems.append(
                    f"{title}: кнопка «{data}» — ожидалось "
                    f"{'разрешено' if want else 'ЗАПРЕЩЕНО'}, "
                    f"вышло {'разрешено' if got else 'запрещено'}")

        # ── Запрет по умолчанию: незнакомая кнопка ──
        expect_press("незнакомая кнопка у модератора", MOD_MUTE, "совсем:новая:кнопка", False)
        expect_press("незнакомая кнопка у обычного участника", PLAIN, "совсем:новая:кнопка", False)
        expect_press("незнакомая кнопка у владельца", OWNER, "совсем:новая:кнопка", True)
        expect_press("пустая кнопка", MOD_MUTE, "", False)

        # ── Владельцу можно всё ──
        expect_press("владелец и владельческая кнопка", OWNER, "usr:role:5:on", True)
        expect_press("владелец и модераторская кнопка", OWNER, "mod:unmute:1:2", True)

        # ── Право по кнопке ──
        expect_press("модератор с «мут» → размут", MOD_MUTE, "mod:unmute:1:2", True)
        expect_press("модератор без «мут» → размут", MOD_CARDS, "mod:unmute:1:2", False)
        expect_press("модератор с «мут» → роли", MOD_MUTE, "usr:role:5:on", False)
        expect_press("модератор с «карточки» → список", MOD_CARDS, "usr:list", True)
        expect_press("модератор с «мут» → список карточек", MOD_MUTE, "usr:list", False)

        # ── Право зависит от ДЕЙСТВИЯ внутри кнопки ──
        # usr:do:<id>:<действие>:… — «мут» и «бан» это РАЗНЫЕ права.
        expect_press("модератор с «мут» → мут участнику", MOD_MUTE, "usr:do:9:mute:0:600", True)
        expect_press("модератор с «мут» → БАН участнику", MOD_MUTE, "usr:do:9:ban:0:0", False)
        expect_press("модератор с «мут» → неизвестное действие",
                     MOD_MUTE, "usr:do:9:чтотоновое:0:0", False)

        # ── Право зависит от НАСТРОЙКИ внутри кнопки ──
        expect_press("правка карточек → лимит картинок",
                     MOD_CARDS, "usr:set:9:img:inc", True)
        expect_press("правка карточек → неизвестная настройка",
                     MOD_CARDS, "usr:set:9:новая:inc", False)

        # ── Иерархия: к кому можно применять меры ──
        def expect_act(title, actor, target, want):
            nonlocal done
            done += 1
            got = roles.can_act_on(actor, target)
            if got != want:
                problems.append(
                    f"{title}: ожидалось {'можно' if want else 'НЕЛЬЗЯ'}, "
                    f"вышло {'можно' if got else 'нельзя'}")

        expect_act("владелец → обычный участник", OWNER, PLAIN, True)
        expect_act("владелец → модератор", OWNER, MOD_MUTE, True)
        expect_act("модератор → обычный участник", MOD_MUTE, PLAIN, True)
        expect_act("модератор → другой модератор", MOD_MUTE, MOD_CARDS, False)
        expect_act("модератор → владелец", MOD_MUTE, OWNER, False)
        expect_act("модератор → сам себя", MOD_MUTE, MOD_MUTE, False)

        # ── Владельца снять с должности нельзя ни при каких правах ──
        done += 1
        if not roles.is_owner(OWNER):
            problems.append("владелец перестал считаться владельцем")

    finally:
        # Возвращаем всё как было: кэш общий на процесс, и проверка не должна
        # оставлять после себя выдуманных модераторов.
        roles.ADMIN_IDS = saved_admins
        roles._cache.clear()
        roles._cache.update(saved_cache)
        roles._loaded = saved_loaded

    return problems, f"{done} проверок: запрет по умолчанию, права кнопок, иерархия"


# ───────────────────────────────────────────────
#  1б. САМ ПРАЙС
# ─────────────────────────────────────────────
#
#  ⚠️ ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА, если стоимость уже проверяется выше.
#  Потому что та проверка ловит ошибку в РАСЧЁТЕ, но слепа к самим ЦЕНАМ:
#  она берёт их из config — значит, меняются обе стороны сразу и всё
#  «сходится». Убедился живым опытом 28.08.2026: нарочно занизил цену Qwen
#  вдвое, и 54 проверки бодро позеленели. Такая правка — опечатка в прайсе,
#  случайный откат, чужая рука — прошла бы насквозь, а суточный счёт тихо
#  врал бы вдвое.
#
#  Поэтому цены зашиты ЗДЕСЬ отдельным списком, сверенным с прайсами
#  провайдеров. Изменились цены по-настоящему — проверка покраснеет, и это
#  правильно: обновляешь config, обновляешь и этот список, глядя на прайс.
#  Списывать сюда значения из config автоматически НЕЛЬЗЯ — это вернёт ровно
#  ту слепоту, ради устранения которой список и заведён.
_PRICES_EXPECTED = {
    "QWEN_PRICES": {
        "qwen3.8-max":  {"cache_hit": 0.25, "cache_miss": 2.00, "output": 6.00},
        "qwen3.7-max":  {"cache_hit": 0.50, "cache_miss": 2.50, "output": 7.50},
        "qwen3.7-plus": {"cache_hit": 0.08, "cache_miss": 0.40, "output": 1.60},
    },
    "XIAOMI_PRICES": {
        "mimo-v2.5":     {"cache_hit": 0.0028, "cache_miss": 0.14,  "output": 0.28},
        "mimo-v2.5-pro": {"cache_hit": 0.0036, "cache_miss": 0.435, "output": 0.87},
    },
    "IMAGE_PRICES": {
        "gemini-3.1-flash-image":      {"in": 0.50, "img_out": 60.0, "txt_out": 1.50},
        "gemini-3.1-flash-lite-image": {"in": 0.25, "img_out": 30.0, "txt_out": 1.50},
    },
}

_DEEPSEEK_EXPECTED = {
    "deepseek-v4-flash": {
        "peak":    {"cache_hit": 0.014, "cache_miss": 0.44, "output": 1.32},
        "offpeak": {"cache_hit": 0.007, "cache_miss": 0.22, "output": 0.66},
    },
    "deepseek-v4-pro": {
        "peak":    {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96},
        "offpeak": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
    },
}


def check_price_list():
    """Цены в config совпадают со сверенным прайсом провайдеров."""
    import config

    problems = []
    done = 0

    def compare(where, want, got):
        nonlocal done
        for model, fields in want.items():
            if model not in got:
                done += 1
                problems.append(f"{where}: модель «{model}» пропала из прайса")
                continue
            for field, value in fields.items():
                done += 1
                actual = got[model].get(field)
                if not _same_money(actual, value):
                    problems.append(
                        f"{where} · {model} · {field}: в прайсе было {value}, "
                        f"в config стоит {actual} — цена изменилась у провайдера "
                        f"или это опечатка")
        for model in got:
            if model not in want:
                done += 1
                problems.append(f"{where}: модель «{model}» появилась в config, "
                                f"но её цены никто не сверял — впиши в selftest.py")

    for name, want in _PRICES_EXPECTED.items():
        compare(name, want, getattr(config, name))

    # DeepSeek лежит на уровень глубже — у него две колонки цен
    got = config.DEEPSEEK_PRICES
    for model, windows in _DEEPSEEK_EXPECTED.items():
        if model not in got:
            done += 1
            problems.append(f"DEEPSEEK_PRICES: модель «{model}» пропала из прайса")
            continue
        for window, fields in windows.items():
            for field, value in fields.items():
                done += 1
                actual = (got[model].get(window) or {}).get(field)
                if not _same_money(actual, value):
                    problems.append(
                        f"DEEPSEEK_PRICES · {model} · {window} · {field}: "
                        f"в прайсе было {value}, в config стоит {actual}")
    for model in got:
        if model not in _DEEPSEEK_EXPECTED:
            done += 1
            problems.append(f"DEEPSEEK_PRICES: модель «{model}» появилась в config, "
                            f"но её цены никто не сверял — впиши в selftest.py")

    return problems, f"{done} цен сверено с прайсами провайдеров"


# ───────────────────────────────────────────────
#  4. РАЗМЫШЛЕНИЯ МОДЕЛИ НЕ УТЕКАЮТ В ЧАТ
# ─────────────────────────────────────────────

def _utf16_len(text: str) -> int:
    """
    Длина строки в кодовых единицах UTF-16 — так её меряет Telegram.

    ⚠️ Считаем СВОИМ способом, а не через utf16_len из telegramify: проверка
    смещений должна быть независима от библиотеки, которая эти смещения
    и расставляет. Иначе общая ошибка в мерке осталась бы незамеченной.
    """
    return len(text.encode("utf-16-le")) // 2


def check_thoughts():
    """
    Служебные блоки <thought>…</thought> не должны доезжать до чата.

    ⚠️ Это правило уже нарушалось (28.08.2026, найдено аудитом): в аварийной
    ветке send_formatted стояло `or raw_answer`, и если ответ состоял из одних
    размышлений, в чат уходил СЫРОЙ текст вместе с тегами. Здесь проверяются
    все пути сразу, включая аварийный.
    """
    import utils_format as uf

    problems = []
    done = 0

    def no_raw_tags(title, text):
        nonlocal done
        done += 1
        low = (text or "").lower()
        if "<thought" in low or "</thought" in low:
            problems.append(f"{title}: в готовый текст попал служебный тег: {text[:80]!r}")

    # ── Вырезание как таковое ──
    done += 1
    if uf.strip_thoughts("<thought>раз</thought>Ответ.") != "Ответ.":
        problems.append("strip_thoughts не вырезал одиночный блок размышлений")

    done += 1
    many = uf.strip_thoughts("<thought>раз</thought>А<thought>два</thought>Б")
    if "раз" in many or "два" in many:
        problems.append(f"strip_thoughts оставил текст размышлений: {many!r}")

    done += 1
    body, th = uf._extract_thoughts("<thought>размышляю</thought>Готовый ответ.")
    if body != "Готовый ответ." or "размышляю" not in th:
        problems.append(f"_extract_thoughts разделил неверно: тело={body!r}, мысли={th!r}")

    # ── Сборка сообщения: тумблер ВКЛЮЧЁН (умолчание) ──
    raw = "<thought>я подумал вот так</thought>Короткий ответ."
    text, ents = uf.build_text_and_entities(raw)
    no_raw_tags("мысли включены", text)
    done += 1
    if "Короткий ответ." not in text:
        problems.append(f"мысли включены: тело ответа потерялось: {text!r}")
    done += 1
    if "я подумал вот так" not in text:
        problems.append("мысли включены: сама цитата с размышлениями не собралась")
    done += 1
    if not any(e.type == "expandable_blockquote" for e in ents):
        problems.append("мысли включены: цитата не помечена как сворачиваемая — "
                        "размышления развернутся на весь экран")

    # ── Сборка сообщения: тумблер ВЫКЛЮЧЕН ──
    saved = uf.thoughts_enabled
    uf.thoughts_enabled = lambda: False
    try:
        text, _ = uf.build_text_and_entities(raw)
        no_raw_tags("мысли выключены", text)
        done += 1
        if "я подумал вот так" in text:
            problems.append("мысли выключены тумблером, но всё равно попали в сообщение")
        done += 1
        if "Короткий ответ." not in text:
            problems.append(f"мысли выключены: тело ответа потерялось: {text!r}")

        # ⚠️ Исключение из правила: видимой части НЕТ вовсе. Тогда мысли
        # показываем, иначе вышло бы пустое сообщение — Telegram такие не
        # принимает, и человек остался бы вообще без ответа.
        text, _ = uf.build_text_and_entities("<thought>только размышления</thought>")
        no_raw_tags("только мысли, тумблер выключен", text)
        done += 1
        if not text.strip():
            problems.append("ответ из одних размышлений при выключенном тумблере дал "
                            "ПУСТОЕ сообщение — Telegram его не примет")
    finally:
        uf.thoughts_enabled = saved

    # ── Аварийная ветка: форматирование сорвалось ──
    # ⚠️ Ровно то место, где 28.08.2026 в чат уходили сырые теги.
    import asyncio

    class FakeBot:
        def __init__(self): self.sent = []

        async def send_message(self, chat_id=None, text=None, **kw):
            self.sent.append(text)
            return None

    def send_with_broken_formatting(raw_answer):
        bot = FakeBot()
        broken = uf.build_text_and_entities
        uf.build_text_and_entities = lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("нарочно сломанное форматирование"))
        try:
            asyncio.run(uf.send_formatted(bot, 1, raw_answer))
        finally:
            uf.build_text_and_entities = broken
        return bot.sent

    sent = send_with_broken_formatting("<thought>мысли</thought>Обычный ответ.")
    done += 1
    if not sent:
        problems.append("аварийная отправка: в чат не ушло НИЧЕГО")
    for part in sent:
        no_raw_tags("аварийная отправка", part)
    done += 1
    if sent and "мысли" in " ".join(sent):
        problems.append("аварийная отправка: текст размышлений попал в чат")

    # Ответ ТОЛЬКО из размышлений + сорвавшееся форматирование
    sent = send_with_broken_formatting("<thought>одни лишь мысли</thought>")
    for part in sent:
        no_raw_tags("аварийная отправка, ответ из одних мыслей", part)
    done += 1
    if not sent or not (sent[0] or "").strip():
        problems.append("аварийная отправка при ответе из одних размышлений: "
                        "в чат ушла пустота — Telegram такое сообщение отвергнет")
    done += 1
    if sent and "одни лишь мысли" in sent[0]:
        problems.append("аварийная отправка: показали человеку сами размышления")

    return problems, f"{done} проверок: вырезание, тумблер, аварийная отправка"


# ───────────────────────────────────────────────
#  5. РАЗМЕТКА ДЛИННЫХ ОТВЕТОВ НЕ РАЗЪЕЗЖАЕТСЯ
# ─────────────────────────────────────────────

def check_long_answers():
    """
    Длинный ответ режется на части, и разметка не съезжает.

    ⚠️ Telegram меряет смещения в UTF-16, а не в символах: эмодзи 🧠 — это
    ДВЕ единицы, а не одна. Ошибка в мерке не роняет бота — она сдвигает
    выделения, и жирным оказывается кусок соседнего слова. Поэтому длины
    здесь считаются своим `_utf16_len`, независимо от библиотеки.
    """
    import asyncio
    import utils_format as uf

    problems = []
    done = 0

    def entities_fit(title, text, entities):
        """Каждое выделение обязано лежать ВНУТРИ своего текста."""
        nonlocal done
        limit = _utf16_len(text)
        for e in entities:
            done += 1
            if e.offset < 0 or e.length < 0:
                problems.append(f"{title}: выделение с отрицательными числами "
                                f"(offset={e.offset}, length={e.length})")
            elif e.offset + e.length > limit:
                problems.append(
                    f"{title}: выделение вылезло за конец текста — "
                    f"{e.offset}+{e.length} > {limit}. В чате жирным окажется "
                    f"не то, что задумано, либо Telegram отвергнет сообщение")

    # ── Обычный ответ с разметкой и эмодзи ──
    raw = "🧠 **Жирно** и `код` в одной строке."
    text, ents = uf.build_text_and_entities(raw)
    entities_fit("ответ с эмодзи и разметкой", text, ents)

    # ── Ответ с мыслями: смещения цитаты считаются отдельно и складываются ──
    raw = "<thought>эмодзи 🧠 внутри мыслей</thought>**Жирный** ответ с 🎯 эмодзи."
    text, ents = uf.build_text_and_entities(raw)
    entities_fit("ответ с мыслями и эмодзи", text, ents)
    done += 1
    if not ents:
        problems.append("ответ с мыслями: разметка потерялась целиком")

    # ── Длинный ответ: режется и влезает в лимит ──
    class FakeBot:
        def __init__(self): self.sent = []

        async def send_message(self, chat_id=None, text=None, entities=None, **kw):
            self.sent.append((text, entities or []))
            return None

    # Текст заведомо длиннее одного сообщения, с разметкой и эмодзи
    block = "Строка с **жирным** словом и эмодзи 🎯 для счёта в UTF-16.\n"
    long_raw = block * 120
    bot = FakeBot()
    asyncio.run(uf.send_formatted(bot, 1, long_raw))

    done += 1
    if len(bot.sent) < 2:
        problems.append(f"длинный ответ ({_utf16_len(long_raw)} единиц UTF-16) "
                        f"не разрезан: частей {len(bot.sent)}")

    for i, (part_text, part_ents) in enumerate(bot.sent, 1):
        done += 1
        size = _utf16_len(part_text or "")
        if size > uf.MAX_UTF16:
            problems.append(f"часть {i}: {size} единиц UTF-16 при лимите "
                            f"{uf.MAX_UTF16} — Telegram её не примет")
        entities_fit(f"часть {i}", part_text or "", part_ents)

    # ── Ничего не потерялось при нарезке ──
    done += 1
    joined = "".join(t for t, _ in bot.sent)
    if "🎯" not in joined:
        problems.append("после нарезки эмодзи пропали из текста")
    done += 1
    # Слов в исходнике и в склейке частей должно быть поровну
    want_words = long_raw.count("жирным")
    got_words = joined.count("жирным")
    if got_words != want_words:
        problems.append(f"при нарезке потерялся текст: слово «жирным» было "
                        f"{want_words} раз, стало {got_words}")

    # ── Разметка не должна пропадать во ВТОРОЙ части ──
    # ⚠️ Жалоба Максима 11.08.2026: «первая часть с разметкой приходит,
    # а вторая без». Тогда доказать было нечем — теперь проверяется.
    done += 1
    if len(bot.sent) > 1 and not bot.sent[1][1]:
        problems.append("во второй части сообщения нет НИ ОДНОГО выделения, "
                        "хотя разметка в тексте есть — она теряется при нарезке")

    return problems, f"{done} проверок: смещения UTF-16, нарезка, потери текста"


# ───────────────────────────────────────────────
#  6. ПОТОЛКИ ОЖИДАНИЯ
# ─────────────────────────────────────────────

def check_wait_budgets():
    """
    Перебор моделей укладывается в общий потолок.

    ⚠️ Проверяется ПОДДЕЛЬНЫМИ ЧАСАМИ: настоящие ждать нельзя (проверка на
    каждой выкатке), а без подмены времени потолок ни разу не сработает и
    проверка окажется декоративной. Сеть тоже подставная — каждая «зависшая»
    модель двигает часы ровно на свой таймаут и бросает таймаут.

    ⚠️ Проверяется и то, что первой попытке время НЕ урезается: иначе она
    уходила бы в запрос с заведомо недостаточным сроком — хуже, чем не
    пробовать вовсе.
    """
    import requests
    import config as c
    from services import gemini as g

    problems = []
    done = 0
    calls = []

    class Clock:
        def __init__(self): self.t = 1000.0
        def monotonic(self): return self.t
        def perf_counter(self): return self.t
        def sleep(self, s): self.t += s
        def time(self): return 1700000000.0

    clock = Clock()

    class FakeHttp:
        def post(self, url, json=None, headers=None, timeout=None, **kw):
            calls.append(timeout)
            clock.t += timeout                 # модель провисела весь таймаут
            raise requests.exceptions.ReadTimeout(f"timeout={timeout}")

    saved_time, saved_http = g.time, g._http
    saved_notify = g._notify_chain_dead
    saved_blocked = dict(g._quota_blocked)
    g.time = clock
    g._http = lambda: FakeHttp()
    g._notify_chain_dead = lambda *a, **kw: None    # письма владельцу глушим
    g._quota_blocked.clear()

    try:
        def run(title, fn, budget, base, chain_len):
            nonlocal done
            calls.clear()
            g._quota_blocked.clear()
            t0 = clock.t
            result = fn()
            spent = clock.t - t0

            done += 1
            if spent > budget + 2:
                problems.append(f"{title}: перебор занял {spent:.0f} с при потолке "
                                f"{budget} с — человек ждёт лишнее")
            done += 1
            if calls and calls[0] != base:
                problems.append(f"{title}: ПЕРВОЙ попытке урезали время — "
                                f"{calls[0]} с вместо {base} с")
            done += 1
            if len(calls) >= chain_len and chain_len > 1:
                problems.append(f"{title}: потолок не сработал, перебрана вся "
                                f"цепочка из {chain_len} моделей")
            return result

        n_audio = len(c.AUDIO_FALLBACK_CHAIN)
        n_video = len([m for m in c.VIDEO_FALLBACK_CHAIN
                       if c.AVAILABLE_MODELS.get(m, {}).get("video")])
        n_media = len(c.PROACTIVE_MEDIA_CHAIN)

        # Личка: человек ждёт ответа
        answer = run("голосовое в личке", lambda: g.ask_gemini_audio(1, 1, "QQ"),
                     g._DIRECT_AUDIO_BUDGET_SEC, c.GEMINI_TIMEOUT, n_audio)
        done += 1
        if not answer:
            problems.append("голосовое в личке: человек не получил вообще ничего — "
                            "должна уйти заглушка, а не пустота")

        answer = run("видео в личке", lambda: g.ask_gemini_video(1, 1, "QQ"),
                     g._DIRECT_VIDEO_BUDGET_SEC, c.VIDEO_TIMEOUT, n_video)
        done += 1
        if not answer:
            problems.append("видео в личке: человек не получил вообще ничего")

        # Группа: бот работает фоном, потолок жёстче
        run("альбом из 10 фото в группе",
            lambda: g._describe_image("QQ", 0, ["QQ"] * 9),
            g._MEDIA_CHAIN_BUDGET_SEC, g._describe_timeout(10), n_media)
        run("голосовое в группе", lambda: g._transcribe_audio("QQ"),
            g._MEDIA_CHAIN_BUDGET_SEC, g._AUDIO_DESCRIBE_TIMEOUT, n_media)
        run("видео в группе", lambda: g._describe_video("QQ"),
            g._MEDIA_CHAIN_BUDGET_SEC, 90, n_media)

    finally:
        g.time, g._http = saved_time, saved_http
        g._notify_chain_dead = saved_notify
        g._quota_blocked.clear()
        g._quota_blocked.update(saved_blocked)

    return problems, f"{done} проверок: личка, группа, первая попытка не урезана"


# ───────────────────────────────────────────────
#  7. АЛЬБОМ НЕ СЧИТАЕТСЯ ФЛУДОМ
# ─────────────────────────────────────────────

def check_album_not_flood():
    """
    Альбом фотографий — ОДНО отправление, а не пять.

    ⚠️ ЭТО УЖЕ ЛОМАЛОСЬ И БИЛО ПО ЖИВЫМ ЛЮДЯМ (19.07.2026): Telegram шлёт
    альбом несколькими сообщениями с общим media_group_id, и без поправки
    альбом из пяти фото мгновенно выбирал порог «5 сообщений за окно» —
    человек получал мут ни за что. Поэтому проверка не про красоту кода,
    а про то, чтобы бот не наказывал за обычную отправку фотографий.
    """
    from services import antispam as asp

    problems = []
    done = 0

    def rec(album_id="", msg_id=1):
        """Запись всплеска в том же порядке, что кладёт _register_and_check."""
        return (0.0, -100, msg_id, "", False, album_id)

    def expect_count(title, records, want):
        nonlocal done
        done += 1
        got = asp._count_messages(records)
        if got != want:
            problems.append(f"{title}: ожидалось {want} отправлений, насчитано {got}")

    # Пять кадров ОДНОГО альбома — одно отправление
    expect_count("альбом из 5 кадров", [rec("aaa", i) for i in range(5)], 1)
    # Десять кадров одного альбома — по-прежнему одно
    expect_count("альбом из 10 кадров", [rec("aaa", i) for i in range(10)], 1)
    # Два РАЗНЫХ альбома — два отправления (залп альбомами ловится)
    expect_count("два разных альбома",
                 [rec("aaa", 1), rec("aaa", 2), rec("bbb", 3), rec("bbb", 4)], 2)
    # Обычные сообщения считаются поштучно
    expect_count("пять обычных сообщений", [rec("", i) for i in range(5)], 5)
    # Смесь: три обычных + альбом
    expect_count("три обычных и альбом",
                 [rec("", 1), rec("", 2), rec("", 3), rec("ccc", 4), rec("ccc", 5)], 4)
    expect_count("пусто", [], 0)

    # ── Порог на живом счётчике ──
    # ⚠️ Счётчик общий на процесс; свой user_id и уборка за собой обязательны.
    UID = -777001
    try:
        asp._reset_user(UID)
        done += 1
        fired = False
        for i in range(6):
            fired = asp._register_and_check(UID, -100, i, msg_count=5, window_sec=60,
                                            media_group_id="album-1")
        if fired:
            problems.append("шесть кадров ОДНОГО альбома подняли тревогу флуда — "
                            "человек получит мут за обычную отправку фотографий")

        asp._reset_user(UID)
        done += 1
        fired = False
        for i in range(5):
            fired = asp._register_and_check(UID, -100, 100 + i, text=f"сообщение {i}",
                                            msg_count=5, window_sec=60)
        if not fired:
            problems.append("пять обычных сообщений подряд НЕ подняли тревогу — "
                            "антифлуд не сработает вовсе")

        # Окно: старое сообщение выпадает и порог не добирается
        asp._reset_user(UID)
        done += 1
        fired = asp._register_and_check(UID, -100, 200, text="одно",
                                        msg_count=5, window_sec=0)
        if fired:
            problems.append("при нулевом окне одно сообщение подняло тревогу — "
                            "старые записи не выбрасываются")
    finally:
        asp._reset_user(UID)

    return problems, f"{done} проверок: альбом как одно отправление, порог, окно"


# ───────────────────────────────────────────────
#  8. КОПИЛКА АЛЬБОМА В ПРОАКТИВНОМ РЕЖИМЕ
# ─────────────────────────────────────────────

def check_album_collect():
    """
    Копилка кадров альбома (27.08.2026): все фото одного отправления уходят
    модели ОДНИМ запросом, а не первым кадром из шести.

    ⚠️ Ключ альбома хранится не для красоты: без него кадры СЛЕДУЮЩЕГО
    отправления подмешались бы в идущую проверку.
    """
    from config import PROACTIVE_ALBUM_MAX_PHOTOS
    from services import proactive as pro

    problems = []
    done = 0

    class FakePhoto:
        def __init__(self, fid): self.file_id = fid

    class FakeMsg:
        def __init__(self, album_id, msg_id, photo=True):
            self.media_group_id = album_id
            self.message_id = msg_id
            self.photo = [FakePhoto(f"file{msg_id}")] if photo else None

    CHAT = -777002
    saved = dict(pro._albums)
    try:
        pro._albums.pop(CHAT, None)

        # Копилки нет — кадр не принимается (иначе он потерялся бы молча)
        done += 1
        if pro._album_add(CHAT, FakeMsg("aaa", 1)):
            problems.append("кадр принят в копилку, которой не существует")

        # Открыли копилку первым кадром, докладываем остальные
        pro._album_open(CHAT, "aaa", "file1", 1)
        for i in range(2, 5):
            done += 1
            if not pro._album_add(CHAT, FakeMsg("aaa", i)):
                problems.append(f"кадр {i} того же альбома не принят в копилку")

        done += 1
        album = pro._albums.get(CHAT) or {}
        if len(album.get("file_ids") or []) != 4:
            problems.append(f"в копилке {len(album.get('file_ids') or [])} кадров "
                            f"вместо 4 — модель увидит не всё отправление")

        # Кадр ЧУЖОГО альбома не принимается
        done += 1
        if pro._album_add(CHAT, FakeMsg("bbb", 99)):
            problems.append("кадр ДРУГОГО альбома подмешался в идущую проверку")

        # Потолок числа кадров
        pro._albums.pop(CHAT, None)
        pro._album_open(CHAT, "ccc", "file0", 0)
        for i in range(1, PROACTIVE_ALBUM_MAX_PHOTOS + 6):
            pro._album_add(CHAT, FakeMsg("ccc", i))
        done += 1
        got = len((pro._albums.get(CHAT) or {}).get("file_ids") or [])
        if got > PROACTIVE_ALBUM_MAX_PHOTOS:
            problems.append(f"в копилке {got} кадров при потолке "
                            f"{PROACTIVE_ALBUM_MAX_PHOTOS} — запрос к модели раздуется")

        # Сообщения альбома считаются ВСЕ, даже сверх потолка кадров:
        # по ним потом удаляются сообщения при муте.
        done += 1
        ids = (pro._albums.get(CHAT) or {}).get("message_ids") or []
        if len(ids) < PROACTIVE_ALBUM_MAX_PHOTOS:
            problems.append(f"запомнено {len(ids)} сообщений альбома — при муте "
                            f"часть кадров останется висеть в чате")
    finally:
        pro._albums.clear()
        pro._albums.update(saved)

    return problems, f"{done} проверок: сбор кадров, чужой альбом, потолок"


# ───────────────────────────────────────────────
#  9. ФИЛЬТР ССЫЛОК: БЕЛЫЙ СПИСОК И МУТ ЗА ПОВТОРЫ
# ─────────────────────────────────────────────

def check_link_filter():
    """
    Фильтр ссылок: кого пропускает, что удаляет и когда мутит (02.09.2026).

    ⚠️ РАДИ ЧЕГО. У проверки домена по белому списку есть классическая дыра:
    сверять «содержит» вместо «это он или его поддомен». Тогда `wtmobile.com`
    в белом списке пропускает и `evil-wtmobile.com`, и `wtmobile.com.evil.ru` —
    то есть фильтр перестаёт быть фильтром, а выглядит рабочим.

    ⚠️ Вторая тихая половина — МУТ ЗА ПОВТОРЫ. Он считается по памяти
    процесса; сломанный счётчик либо не наказывает никогда, либо наказывает
    с первой ссылки. И то, и другое замечает не админ, а живой человек в чате.

    Ветка зовётся ЦЕЛИКОМ, с поддельными ботом и сообщением: проверяем
    поведение, а не текст исходника. Сеть и Telegram не участвуют.
    """
    import asyncio

    from config import LINKFILTER_MUTE_COUNT, LINKFILTER_WHITELIST
    from database import history as hist
    from services import antispam

    problems = []
    done = 0
    CHAT, STRANGER = -100777, 900001

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    # ── 1. Белый список: свой домен и поддомен против похожего чужого ──
    good = LINKFILTER_WHITELIST[0]
    for url, want, why in (
        (f"https://{good}/news", True, "сам домен из белого списка не признан своим"),
        (f"https://news.{good}/x", True, "поддомен своего домена не признан своим"),
        (f"http://evil-{good}/x", False,
         f"«evil-{good}» принят за свой домен — чужая ссылка пройдёт фильтр"),
        (f"https://{good}.evil.ru/x", False,
         f"«{good}.evil.ru» принят за свой — так маскируют чужие ссылки"),
        (f"https://user@{good}:443/x", True,
         "логин@ и :порт сбили разбор домена — своя ссылка удалилась бы"),
        ("https://совсем.чужой.сайт/x", False, "чужой домен принят за свой"),
    ):
        expect(f"{why} ({url})", antispam._is_whitelisted(url) is want)

    # ── 2. Ссылки достаются и из текста, и из подписи, и из-под текста ──
    class _Ent:
        def __init__(self, kind, url=""):
            self.type = kind
            self.url = url

    class _Msg:
        """Сообщение Telegram ровно в том объёме, который читает фильтр."""
        def __init__(self, text=None, caption=None, ents=None, cap_ents=None,
                     photo=False, message_id=555):
            self.text = text
            self.caption = caption
            self.photo = photo
            self.message_id = message_id
            self._ents = ents or {}
            self._cap = cap_ents or {}

        def parse_entities(self, types=None):
            return self._ents

        def parse_caption_entities(self, types=None):
            return self._cap

    plain = _Msg(text="смотри https://чужой.сайт/раз",
                 ents={_Ent("url"): "https://чужой.сайт/раз"})
    expect("явная ссылка в тексте не найдена",
           antispam._extract_links(plain) == ["https://чужой.сайт/раз"])

    hidden = _Msg(text="смотри тут",
                  ents={_Ent("text_link", "https://чужой.сайт/два"): "тут"})
    expect("ссылка, спрятанная под текст, не найдена — так их и маскируют",
           antispam._extract_links(hidden) == ["https://чужой.сайт/два"])

    capt = _Msg(caption="фото https://чужой.сайт/три", photo=True,
                cap_ents={_Ent("url"): "https://чужой.сайт/три"})
    expect("ссылка в подписи к фото не найдена",
           antispam._extract_links(capt) == ["https://чужой.сайт/три"])

    # ── 3. Ветка целиком: что удаляется, что нет, когда мут ──
    class _Sent:
        message_id = 999

    class _Bot:
        id = 111222
        def __init__(self):
            self.deleted = []
            self.muted = []
            self.said = []

        async def get_chat_member(self, chat_id, user_id):
            class _M:
                status = "member"
            return _M()

        async def delete_message(self, chat_id, message_id):
            self.deleted.append(message_id)

        async def send_message(self, chat_id, text):
            self.said.append(text)
            return _Sent()

        async def restrict_chat_member(self, **kw):
            self.muted.append(kw.get("user_id"))
            return True

    class _User:
        def __init__(self, uid):
            self.id = uid
            self.first_name = "Чужак"
            self.username = None

    def foreign_msg(mid=555):
        return _Msg(text="держи https://чужой.сайт/раз", message_id=mid,
                    ents={_Ent("url"): "https://чужой.сайт/раз"})

    def run(bot, uid, msg):
        return asyncio.run(antispam.check_and_delete_links(bot, CHAT,
                                                           _User(uid), msg))

    saved_flag = hist.get_setting("linkfilter_enabled", "0")
    saved_strikes = dict(antispam._link_strikes)
    try:
        # Тумблер выключен — фильтр не трогает ничего.
        hist.set_setting("linkfilter_enabled", "0")
        bot = _Bot()
        expect("при выключенном фильтре сообщение всё равно удалено",
               run(bot, STRANGER, foreign_msg()) is False and not bot.deleted)

        hist.set_setting("linkfilter_enabled", "1")

        # Своя ссылка — не трогаем.
        antispam._link_strikes.clear()
        bot = _Bot()
        own = _Msg(text=f"наша новость https://{good}/news",
                   ents={_Ent("url"): f"https://{good}/news"})
        expect("ссылка на свой домен удалена — фильтр съедает собственные "
               "новости бота", run(bot, STRANGER, own) is False and not bot.deleted)

        # Чужая ссылка — удаляем, пишем в журнал, сохраняем улику.
        antispam._link_strikes.clear()
        bot = _Bot()
        before = len(hist.get_recent_moderation_actions(50))
        expect("чужая ссылка не удалена", run(bot, STRANGER, foreign_msg()) is True)
        expect("сообщение со ссылкой не удалено у Telegram", bot.deleted == [555])
        expect("человеку не сказали, почему сообщение исчезло",
               bool(bot.said) and "ссылки" in bot.said[0].lower())
        log = hist.get_recent_moderation_actions(50)
        # ⚠️ Ищем запись ПО ВИДУ, а не «последнюю»: если фильтр заодно выдаст
        # мут, последней окажется он, и проверка ругалась бы не на то.
        linkdel = [r for r in log if r["action"] == "linkdel"]
        expect(f"удаление ссылки не записано в журнал модерации видом "
               f"«linkdel» (записей стало {len(log)} против {before}, "
               f"из них linkdel — {len(linkdel)})", len(linkdel) == 1)
        expect("текст удалённого сообщения не сохранён — улики будут пустыми",
               bool(linkdel) and bool(hist.get_mute_evidence(linkdel[0]["id"])))

        # Мут за повторы: ровно на LINKFILTER_MUTE_COUNT-м удалении, не раньше.
        antispam._link_strikes.clear()
        antispam._muted_until.clear()
        bot = _Bot()
        for i in range(1, LINKFILTER_MUTE_COUNT + 1):
            run(bot, STRANGER, foreign_msg(600 + i))
            if i < LINKFILTER_MUTE_COUNT:
                expect(f"мут выдан на {i}-й ссылке, а порог — "
                       f"{LINKFILTER_MUTE_COUNT}", not bot.muted)
        expect(f"после {LINKFILTER_MUTE_COUNT} удалённых ссылок мут не выдан — "
               f"повторы остаются безнаказанными", bot.muted == [STRANGER])

        # Личное разрешение «ссылки можно» — не трогаем вовсе.
        antispam._link_strikes.clear()
        hist.set_user_settings(STRANGER, links_allowed=1)
        from services import user_settings
        user_settings.refresh(STRANGER)
        bot = _Bot()
        expect("у человека с личным разрешением ссылка всё равно удалена",
               run(bot, STRANGER, foreign_msg()) is False and not bot.deleted)

    finally:
        hist.set_setting("linkfilter_enabled", saved_flag)
        try:
            hist.set_user_settings(STRANGER, links_allowed=None)
            from services import user_settings
            user_settings.refresh(STRANGER)
        except Exception:
            pass
        antispam._link_strikes.clear()
        antispam._link_strikes.update(saved_strikes)
        antispam._muted_until.clear()
        with hist._lock:
            conn = hist._get_connection()
            conn.execute("DELETE FROM moderation_log")
            conn.execute("DELETE FROM mute_evidence")
            conn.commit()

    return problems, (f"{done} проверок: белый список против похожих доменов, "
                      f"скрытые ссылки и подписи, удаление, журнал, мут за повторы")


def check_greeter():
    """
    Приветствие новичков и проверка «я не бот» (02.09.2026).

    ⚠️ РАДИ ЧЕГО. Здесь уже наступали, и поломка записана прямо в коде: бот
    здоровался с человеком, которого САМ ЖЕ только что замутил. Мут меняет
    статус участника на «ограничен», и если смотреть только на новый статус,
    это неотличимо от «пришёл новый». Отсюда правило: событие — это ПЕРЕХОД,
    и проверять надо пару «было → стало», а не одну её половину.

    ⚠️ Вторая половина — кнопка «Я не бот». Она живёт ВНЕ гейта прав (её жмёт
    обычный участник, не персонал), поэтому единственное, что отделяет её от
    любого прохожего, — сверка «нажал тот, кому адресовано». Сломайся она —
    спам-ботов пропускал бы кто угодно, и выглядело бы это как исправная
    работа капчи.
    """
    import asyncio

    from telegram.constants import ChatMemberStatus as _S

    from database import history as hist
    from services import greeter

    problems = []
    done = 0
    CHAT, NEWBIE, STRANGER = -100888, 900777, 900778

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    class _M:
        """Запись о членстве: статус и, у ограниченных, «всё ещё в группе?»."""
        def __init__(self, status, is_member=False):
            self.status = status
            self.is_member = is_member

    class _Upd:
        def __init__(self, old, new):
            self.old_chat_member = old
            self.new_chat_member = new

    LEFT = _M(_S.LEFT)
    MEMBER = _M(_S.MEMBER)
    MUTED = _M(_S.RESTRICTED, is_member=True)      # замучен, но в группе
    GONE_RESTRICTED = _M(_S.RESTRICTED, is_member=False)  # ограничен и вышел

    # ── 1. Переход «было → стало» ──
    expect("вступление в группу не распознано",
           greeter._joined(_Upd(LEFT, MEMBER)) is True)
    expect("МУТ УЧАСТНИКА принят за вступление — бот поздоровается с тем, "
           "кого сам только что наказал",
           greeter._joined(_Upd(MEMBER, MUTED)) is False)
    expect("снятие мута принято за вступление — бот здоровался бы повторно",
           greeter._joined(_Upd(MUTED, MEMBER)) is False)
    expect("уход из группы не распознан",
           greeter._left(_Upd(MEMBER, LEFT)) is True)
    expect("мут участника принят за уход — бот снял бы ожидание проверки",
           greeter._left(_Upd(MEMBER, MUTED)) is False)
    expect("замученный участник не считается состоящим в группе",
           greeter._is_in(MUTED) is True)
    expect("ограниченный и вышедший считается состоящим в группе",
           greeter._is_in(GONE_RESTRICTED) is False)
    expect("вступление ограниченного, но вернувшегося не распознано",
           greeter._joined(_Upd(GONE_RESTRICTED, MEMBER)) is True)

    # ── 2. Текст приветствия ──
    class _FakeBot:
        username = "C4_Max_bot"

    text = greeter._welcome_text("Вася <хитрый>", NEWBIE, captcha=True,
                                 seconds=300, bot=_FakeBot())
    expect("имя новичка не экранировано — «<» в имени порвёт разметку и "
           "приветствие не отправится вовсе", "Вася <хитрый>" not in text)
    expect("экранированного имени в приветствии нет", "&lt;хитрый&gt;" in text)
    expect("имя не сделано ссылкой на профиль по номеру",
           f"tg://user?id={NEWBIE}" in text)
    expect("при включённой проверке в тексте не сказано, сколько на неё "
           "времени (300 секунд = 5 минут)", "5" in text)
    plain_text = greeter._welcome_text("Вася", NEWBIE, captcha=False,
                                       seconds=300, bot=_FakeBot())
    expect("без проверки «я не бот» текст всё равно требует нажать кнопку",
           len(plain_text) < len(text))
    short = greeter._welcome_text("Вася", NEWBIE, captcha=True,
                                  seconds=30, bot=_FakeBot())
    expect("срок меньше минуты показан как «0 минут» — обещание, которого "
           "не бывает", "0 мин" not in short)

    # ── 3. Кнопку «Я не бот» жмёт только тот, кому она адресована ──
    class _Answer:
        def __init__(self):
            self.said = []
            self.edited = []

    class _Bot:
        id = 111222
        def __init__(self, holder):
            self.holder = holder
            self.freed = []

        async def get_chat(self, chat_id):
            class _C:
                permissions = None
            return _C()

        async def restrict_chat_member(self, **kw):
            self.freed.append(kw.get("user_id"))
            return True

    class _Query:
        def __init__(self, presser, holder):
            self.data = f"join:ok:{CHAT}:{NEWBIE}"
            self._bot = _Bot(holder)
            self.holder = holder
            self.message = None

            class _U:
                id = presser
                first_name = "Кто-то"
            self.from_user = _U()

        def get_bot(self):
            return self._bot

        async def answer(self, text="", show_alert=False):
            self.holder.said.append(text)

        async def edit_message_text(self, *a, **kw):
            self.holder.edited.append(a[0] if a else "")

    saved_pending = dict(greeter._pending)
    try:
        # Чужой человек нажимает чужую кнопку.
        holder = _Answer()
        q = _Query(STRANGER, holder)
        greeter._pending[(CHAT, NEWBIE)] = 42
        asyncio.run(greeter.handle_join_callback(q, None, q.data))
        expect("ЧУЖОЙ прошёл проверку за новичка — капчу пропускает любой "
               "прохожий, и спам-боты проходят вместе с ним",
               not q.get_bot().freed)
        expect("чужому не сказали, что приветствие адресовано не ему",
               bool(holder.said) and "не тебе" in holder.said[0])
        expect("ожидание проверки снято чужим нажатием",
               (CHAT, NEWBIE) in greeter._pending)

        # Тот, кому адресовано.
        holder = _Answer()
        q = _Query(NEWBIE, holder)
        asyncio.run(greeter.handle_join_callback(q, None, q.data))
        expect("новичку не вернули права после проверки",
               q.get_bot().freed == [NEWBIE])
        expect("ожидание проверки осталось висеть после успешного нажатия — "
               "отложенная проверка кикнет прошедшего",
               (CHAT, NEWBIE) not in greeter._pending)
        expect("прохождение проверки не записано в журнал вступлений",
               hist.get_join_counts(1).get("ok", 0) > 0)

        # Битые данные кнопки не роняют ветку.
        holder = _Answer()
        q = _Query(NEWBIE, holder)
        asyncio.run(greeter.handle_join_callback(q, None, "join:ok:мусор"))
        expect("битые данные кнопки не отбиты сообщением",
               bool(holder.said) and "екоррект" in holder.said[0])

    finally:
        greeter._pending.clear()
        greeter._pending.update(saved_pending)
        with hist._lock:
            conn = hist._get_connection()
            conn.execute("DELETE FROM join_log WHERE chat_id = ?", (CHAT,))
            conn.commit()

    return problems, (f"{done} проверок: мут не считается вступлением, текст "
                      f"и срок приветствия, капчу жмёт только адресат")


def check_parsing():
    """
    Разбор статьи базы знаний и разбор вопроса викторины (02.09.2026).

    ⚠️ РАДИ ЧЕГО. Обе ошибки ТИХИЕ и портят данные, а не роняют бота.
    Статья разобралась не так — поиск отвечает мимо, и понять это можно
    только по странным ответам. Вопрос прошёл негодным — опрос просто не
    отправится в Telegram, уже в игре, при живых людях.

    ⚠️ Разбор статьи проверяется в ОБОИХ режимах нарезки: рабочий сейчас
    «1 файл = 1 чанк», но режим переключается переменной окружения, и
    сломанная вторая ветка молчала бы до дня переключения.
    """
    import shutil as _shutil
    import tempfile as _tempfile

    from services import quiz_bank, rag

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    tmp = _tempfile.mkdtemp(prefix="c4max-selftest-parse-")
    saved_mode = rag.RAG_CHUNK_MODE

    def write(name, text):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    try:
        full = write("Т-72Б3.md",
                     "---\ntitle: Т-72Б3 «Урал»\nkind: tank\n---\n"
                     "# Заголовок из текста\n"
                     "Вступление статьи.\n"
                     "## Броня\nЛоб корпуса 500 мм.\n"
                     "## Вооружение\nПушка 125 мм.\n")

        # ── 1. Режим «1 файл = 1 чанк» (рабочий) ──
        rag.RAG_CHUNK_MODE = "file"
        one = rag.parse_article_file(full)
        expect(f"в режиме «файл» статья разобралась на {len(one)} кусков "
               f"вместо одного", len(one) == 1)
        expect("название статьи взято не из шапки — в шапке оно точнее, "
               "чем заголовок в тексте",
               bool(one) and one[0]["title"] == "Т-72Б3 «Урал»")
        expect("в текст для поиска не подставлено название статьи — куски "
               "разных машин перестанут различаться",
               bool(one) and "Т-72Б3 «Урал»" in one[0]["full_text"])
        expect("шапка-метаданные уехала в текст статьи вместе с содержимым",
               bool(one) and "kind: tank" not in one[0]["content"])
        expect("разделы статьи пропали из цельного куска — модель увидит "
               "не всю статью", bool(one) and "Пушка 125 мм" in one[0]["content"])

        # ── 2. Режим «по разделам» ──
        rag.RAG_CHUNK_MODE = "sections"
        many = rag.parse_article_file(full)
        expect(f"в режиме «разделы» вышло {len(many)} кусков вместо трёх "
               f"(вступление + два раздела)", len(many) == 3)
        titles = [c["title"] for c in many]
        expect(f"в названиях кусков нет имени статьи — разделы «Броня» из "
               f"разных статей смешаются между собой: {titles}",
               all("Т-72Б3" in t for t in titles))
        expect("раздел не назван своим именем",
               any("Броня" in t for t in titles))

        # ── 3. Название: откат к имени файла ──
        rag.RAG_CHUNK_MODE = "file"
        bare = write("Ил-28.md", "Просто текст без заголовка и шапки.\n")
        got = rag.parse_article_file(bare)
        expect("без шапки и заголовка название не взято из имени файла — "
               "статья осталась бы безымянной",
               bool(got) and got[0]["title"] == "Ил-28")

        empty = write("Пусто.md", "---\ntitle: Пусто\n---\n\n")
        expect("пустая статья дала кусок — в поиск попал бы пустой вектор",
               rag.parse_article_file(empty) == [])

        # ── 4. Разбор вопроса викторины ──
        from config import (QUIZ_EXPLANATION_MAX, QUIZ_OPTIONS_COUNT,
                            QUIZ_QUESTION_MAX)

        def q(**over):
            item = {"question": "Какая броня у Т-72Б3?",
                    "options": [f"вариант {i}" for i in range(QUIZ_OPTIONS_COUNT)],
                    "correct_idx": 1, "explanation": "Разбор."}
            item.update(over)
            return quiz_bank._clean_question(item)

        expect("годный вопрос забракован", q() is not None)
        expect("вопрос без текста принят — опрос не отправится",
               q(question="   ") is None)
        expect(f"принято не {QUIZ_OPTIONS_COUNT} вариантов — Telegram такой "
               f"опрос не примет", q(options=["раз", "два"]) is None)
        dup = [f"вариант {i}" for i in range(QUIZ_OPTIONS_COUNT - 1)] + ["ВАРИАНТ 0"]
        expect("два одинаковых варианта прошли проверку — у вопроса стало "
               "два верных ответа", q(options=dup) is None)
        expect("верный ответ строкой «2.» не разобран — модель регулярно так "
               "отвечает, и вопрос терялся бы зря",
               (q(correct_idx="2.") or {}).get("correct_idx") == 2)
        expect("номер верного ответа за пределами списка принят — в игре "
               "верного ответа не окажется вовсе", q(correct_idx=9) is None)
        expect("вопрос длиннее лимита Telegram принят",
               q(question="я" * (QUIZ_QUESTION_MAX + 1)) is None)
        long_expl = q(explanation="э" * (QUIZ_EXPLANATION_MAX + 50))
        expect("длинный разбор выбросил вопрос целиком — его положено "
               "подрезать, а не терять", long_expl is not None)
        expect(f"подрезанный разбор длиннее лимита "
               f"({len((long_expl or {}).get('explanation', ''))} знаков)",
               bool(long_expl)
               and len(long_expl["explanation"]) <= QUIZ_EXPLANATION_MAX)

    finally:
        rag.RAG_CHUNK_MODE = saved_mode
        _shutil.rmtree(tmp, ignore_errors=True)

    return problems, (f"{done} проверок: оба режима нарезки статьи, название "
                      f"и пустышки, отбраковка и подрезка вопросов")


def check_report_render():
    """
    Сборка ТЕКСТА отчёта: ни один провайдер и ни один вызов не теряется
    (02.09.2026).

    ⚠️ ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ check_daily_report. Тот проверяет АРИФМЕТИКУ —
    расход за период, перенос после месячного обнуления, недельную копилку.
    Здесь проверяется РИСОВАЛКА: те же верные цифры можно нарисовать так, что
    блок провайдера пропадёт со страницы, и отчёт будет выглядеть исправным.

    ⚠️ Поломка такого рода в проекте уже была и записана в самом коде: блоки
    отчёта когда-то перечисляли руками, и забытый провайдер молча уезжал в
    «прочие». Поэтому проверка берёт ожидания ИЗ РЕЕСТРА `config.PROVIDERS`,
    а не из списка, переписанного сюда: список, сверяемый сам с собой, не
    проверяет ничего.
    """
    from config import AVAILABLE_MODELS, PROVIDERS
    from services import daily_report as rep

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    # По одной живой модели на каждого провайдера + вызовы модели, которой в
    # реестре нет вовсе (её место — блок «Прочие вызовы»).
    calls = {}
    for pid in PROVIDERS:
        for name, meta in AVAILABLE_MODELS.items():
            if meta.get("provider") == pid:
                calls[name] = 7
                break
    calls["выдуманная-модель-из-прошлого"] = 4

    totals = {"calls": calls, "burned": {}, "qwen_reset": ()}
    for pid, meta in PROVIDERS.items():
        if meta["cost_key"]:
            totals[f"{pid}_cost"] = 0.5
    current = {f"{pid}_balance": 9.0 for pid in PROVIDERS}
    current["qwen_tokens"] = {}

    text = rep.render("📊 <b>РАСХОД</b>", "за сутки", "", totals, current)

    # ── 1. Ни один провайдер не пропал ──
    for pid, meta in PROVIDERS.items():
        expect(f"провайдер «{pid}» пропал из отчёта — его вызовы и деньги "
               f"стали невидимы", meta["calls_label"] in text)
        if meta["cost_key"]:
            expect(f"у провайдера «{pid}» нет строки расхода",
                   meta["money_label"] in text)
        if meta["balance_key"]:
            expect(f"у провайдера «{pid}» пропал остаток на счету",
                   "Остаток на счету" in text)

    # ── 2. Особенности реестра доезжают до текста ──
    approx = [pid for pid, m in PROVIDERS.items() if m["report_approx"]]
    expect(f"расход {approx} расчётный по прайсу, но знака «≈» в отчёте нет — "
           f"цифра читается как точная", not approx or "≈$" in text)

    # ── 3. Модель вне реестра не теряется ──
    expect("вызовы модели, которой нет в реестре, пропали из отчёта — "
           "деньги за них никуда не попадут", "Прочие вызовы" in text)
    expect("модель вне реестра не названа по имени",
           "выдуманная-модель-из-прошлого" in text)

    # ── 4. Пометка «счётчик правили вручную» ──
    paid = next((pid for pid, m in PROVIDERS.items() if m["cost_key"]), None)
    if paid:
        manual = dict(totals)
        manual[f"{paid}_manual"] = True
        expect("правку счётчика вручную в отчёте не видно — цифра выглядит "
               "измеренной", "правили вручную"
               in rep.render("📊", "за сутки", "", manual, current))
        expect("пометка «правили вручную» стоит там, где счётчик не правили",
               "правили вручную" not in text)

    # ── 5. Пустой период не притворяется работой ──
    zero = rep.render("📊", "за сутки", "", {"calls": {}, "burned": {}}, current)
    expect("на пустом периоде отчёт не собрался вовсе", bool(zero.strip()))
    expect("на пустом периоде появился блок «Прочие вызовы»",
           "Прочие вызовы" not in zero)

    return problems, (f"{done} проверок: все {len(PROVIDERS)} провайдеров на "
                      f"месте, модель вне реестра не теряется, пометки")


def check_news_send():
    """
    Рассылка новости в чат: текст не пропадает ни в одной из веток
    (02.09.2026).

    ⚠️ ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ. Получение новостей с сайта — это СЕТЬ, и
    проверка, падающая из-за чужого сервера, однажды откатит совершенно
    исправное обновление. Проверяется только отправка: она вся наша.

    ⚠️ РАДИ ЧЕГО. У Telegram подпись к альбому ограничена 1024 знаками, и
    поэтому отправка ветвится: короткий текст уходит подписью к картинкам,
    длинный — ОТДЕЛЬНЫМ сообщением следом. В такой развилке текст теряется
    целиком и молча: картинки в чате есть, новость выглядит доставленной, а
    прочитать её нельзя.

    Проверяется поддельным ботом, который просто записывает, что его просили
    отправить. Ни сети, ни Telegram.
    """
    import asyncio

    from jobs import news

    problems = []
    done = 0
    GROUP = -100999

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    class _Sent:
        message_id = 1

    class _Bot:
        id = 111222
        username = "C4_Max_bot"
        first_name = "C4_Max"

        def __init__(self):
            self.albums = []     # списки media
            self.photos = []     # одиночные фото
            self.texts = []      # отдельные текстовые сообщения
            self.ents = []       # выделения: в них живут адреса ссылок

        async def send_media_group(self, chat_id, media):
            self.albums.append(media)
            for m in media:
                self.ents.extend(getattr(m, "caption_entities", None) or [])
            return [_Sent()]

        async def send_photo(self, chat_id, photo, caption=None, **kw):
            self.photos.append((photo, caption))
            self.ents.extend(kw.get("caption_entities") or [])
            return _Sent()

        async def send_message(self, chat_id, text=None, **kw):
            self.texts.append(text or "")
            self.ents.extend(kw.get("entities") or [])
            return _Sent()

    URL = "https://wtmobile.com/news/1"
    SHORT = "Вышло обновление. Коротко и по делу."
    LONG = "Очень длинная новость. " * 80          # заведомо больше 1024

    def send(text, images):
        bot = _Bot()
        asyncio.run(news.send_news_to_chat(bot, GROUP, text, images[0] if images else "",
                                           URL, images[1:] if len(images) > 1 else None))
        return bot

    def said(bot):
        """
        Всё, что бот отправил текстом — подписями и сообщениями.

        ⚠️ Адреса ссылок сюда добавляются ОТДЕЛЬНО: разметка превращает
        «[Читать на сайте](адрес)» в текст без адреса плюс выделение, в
        котором адрес и лежит. Искать адрес в одном тексте — значит не найти
        его никогда и решить, что ссылка потеряна.
        """
        return " ".join(bot.texts + [c or "" for _, c in bot.photos]
                        + [m.caption or "" for al in bot.albums for m in al]
                        + [getattr(e, "url", "") or "" for e in bot.ents])

    # ── 1. Две картинки + короткий текст: один альбом с подписью ──
    bot = send(SHORT, ["https://x/1.jpg", "https://x/2.jpg"])
    expect(f"две картинки ушли не альбомом (альбомов {len(bot.albums)}, "
           f"одиночных фото {len(bot.photos)})", len(bot.albums) == 1)
    expect("короткий текст не ушёл подписью к альбому, а значит новость "
           "пришла отдельным сообщением там, где могла быть одним",
           not bot.texts)
    expect("текст новости пропал из подписи", SHORT[:20] in said(bot))

    # ── 2. Две картинки + ДЛИННЫЙ текст: альбом плюс сообщение ──
    bot = send(LONG, ["https://x/1.jpg", "https://x/2.jpg"])
    expect("длинные новости с альбомом больше не отправляются альбомом",
           len(bot.albums) == 1)
    expect("ДЛИННЫЙ ТЕКСТ НОВОСТИ ПРОПАЛ: подпись альбома его не вмещает, а "
           "отдельным сообщением он не ушёл — в чате остались одни картинки",
           bool(bot.texts))
    expect("подпись к альбому длиннее лимита Telegram — альбом не отправится",
           all(len(m.caption or "") <= 1024 for al in bot.albums for m in al))

    # ── 3. Одна картинка ──
    bot = send(SHORT, ["https://x/1.jpg"])
    expect("одна картинка ушла альбомом", not bot.albums and len(bot.photos) == 1)
    expect("текст не ушёл подписью к единственной картинке",
           bool(bot.photos) and bool(bot.photos[0][1]))

    bot = send(LONG, ["https://x/1.jpg"])
    expect("при длинном тексте и одной картинке текст не ушёл отдельно",
           bool(bot.texts))

    # ── 4. Без картинок ──
    bot = send(SHORT, [])
    expect("новость без картинок не отправлена вовсе",
           bool(bot.texts) and not bot.albums and not bot.photos)

    # ── 5. Лимит Telegram на альбом ──
    bot = send(SHORT, [f"https://x/{i}.jpg" for i in range(14)])
    expect(f"в альбом попало {len(bot.albums[0]) if bot.albums else 0} "
           f"картинок — Telegram принимает не больше десяти",
           bool(bot.albums) and len(bot.albums[0]) <= 10)

    # ── 6. Ссылка «читать на сайте» есть во всех случаях ──
    for label, images in (("без картинок", []),
                          ("одна картинка", ["https://x/1.jpg"]),
                          ("альбом", ["https://x/1.jpg", "https://x/2.jpg"])):
        expect(f"ссылка на сайт пропала из новости ({label}) — читать целиком "
               f"человеку негде", URL in said(send(SHORT, images)))

    return problems, (f"{done} проверок: альбом и подпись, длинный текст не "
                      f"теряется, лимит картинок, ссылка на сайт")


# ───────────────────────────────────────────────
#  10. КЛЮЧИ СУТОК, СРОКОВ И НЕДЕЛЬ
# ─────────────────────────────────────────────

def check_time_keys():
    """
    Метки «эти сутки», «этот срок», «эта неделя» — по ним бот решает,
    рассылал ли он уже вопрос дня и недельный дайджест.

    ⚠️ Ошибка здесь не роняет бота: он просто молча шлёт дважды или не шлёт
    вовсе. Считаются метки по КИЕВСКОМУ времени, поэтому даты подставляем
    с явным часовым поясом, а не «наивные».
    """
    from datetime import datetime, timedelta, timezone
    from services import quiz_daily as qd
    from services import group_digest as gd

    problems = []
    done = 0
    kyiv = timezone(timedelta(hours=3))

    def at(y, m, d, hh=12, mm=0):
        return datetime(y, m, d, hh, mm, tzinfo=kyiv)

    def expect(title, got, want):
        nonlocal done
        done += 1
        if got != want:
            problems.append(f"{title}: ожидалось «{want}», вышло «{got}»")

    # ── Сутки ──
    expect("обычный день", qd.day_key(at(2026, 8, 28)), "2026-08-28")
    expect("первая минута суток", qd.day_key(at(2026, 8, 28, 0, 0)), "2026-08-28")
    expect("последняя минута суток", qd.day_key(at(2026, 8, 28, 23, 59)), "2026-08-28")
    expect("смена месяца", qd.day_key(at(2026, 9, 1, 0, 1)), "2026-09-01")
    expect("смена года", qd.day_key(at(2027, 1, 1, 0, 1)), "2027-01-01")

    # Соседние сутки обязаны различаться, иначе рассылка пропустит день
    done += 1
    if qd.day_key(at(2026, 8, 28, 23, 59)) == qd.day_key(at(2026, 8, 29, 0, 1)):
        problems.append("метки суток по разные стороны полуночи совпали — "
                        "вопрос дня не отправится на следующий день")

    # ── Срок внутри суток ──
    expect("срок 12:00", qd._slot_key(at(2026, 8, 28), 12), "2026-08-28#12")
    expect("срок 18:00", qd._slot_key(at(2026, 8, 28), 18), "2026-08-28#18")
    done += 1
    if qd._slot_key(at(2026, 8, 28), 12) == qd._slot_key(at(2026, 8, 28), 18):
        problems.append("метки двух сроков одних суток совпали — "
                        "второй вопрос дня не отправится")

    # ── Неделя ──
    expect("неделя середины года", gd.week_key(at(2026, 8, 28)), "2026-W35")
    done += 1
    if gd.week_key(at(2026, 8, 24)) != gd.week_key(at(2026, 8, 30)):
        problems.append("понедельник и воскресенье одной недели дали РАЗНЫЕ метки — "
                        "дайджест уйдёт дважды за неделю")
    done += 1
    if gd.week_key(at(2026, 8, 30)) == gd.week_key(at(2026, 8, 31)):
        problems.append("воскресенье и понедельник СОСЕДНИХ недель дали одну метку — "
                        "дайджест пропустит неделю")

    return problems, f"{done} проверок: сутки, сроки, недели, границы"


# ───────────────────────────────────────────────
#  9-бис. ШАГ ЦИКЛА РАСПИСАНИЯ
# ─────────────────────────────────────────────

def check_schedule_step():
    """
    Цикл расписания обязан просыпаться В НАЧАЛЕ ЧАСА, а не «через час после
    запуска бота».

    ⚠️ ЗАЧЕМ ЭТА ПРОВЕРКА (31.08.2026). Пауза была `min(до полуночи, 3600)`,
    и точка пробуждения прилипала к минуте старта: бот поднялся в 06:46 —
    вопрос дня, назначенный на 12:00, ушёл в 12:47. Опоздание менялось при
    каждой выкатке, потому что бот перезапускается. Само расписание при этом
    было верным — промахивался момент проверки, и глазами это не видно:
    в панели написано «12:00», в логах — 12:47.

    Проверяем ТРИ вещи, и третья — главная: без неё проверка была бы
    декоративной. Можно починить расчёт и забыть переключить на него цикл.
    """
    from datetime import datetime, timedelta, timezone
    from services import daily_report as dr

    problems = []
    done = 0
    kyiv = timezone(timedelta(hours=3))

    def at(hh, mm, ss=0):
        return datetime(2026, 8, 31, hh, mm, ss, tzinfo=kyiv)

    # ── 1. Куда попадёт пробуждение из разных моментов ──
    # Момент старта не должен влиять ни на что: из любой минуты часа
    # следующая проверка обязана лечь на начало СЛЕДУЮЩЕГО часа.
    for hh, mm, ss, want_hour in (
        (6, 46, 50, 7),    # тот самый случай из жалобы Максима
        (11, 59, 58, 12),  # почти полдень — вопрос дня не должен опоздать на час
        (12, 0, 0, 13),    # ровно в начале часа — спим целый час, а не крутимся
        (12, 0, 5, 13),
        (23, 30, 0, 0),    # через полночь: попадаем в новые сутки
        (0, 0, 1, 1),
    ):
        done += 1
        start = at(hh, mm, ss)
        wake = start + timedelta(seconds=dr.seconds_to_next_hour(start))
        if wake.hour != want_hour:
            problems.append(
                f"старт {hh:02d}:{mm:02d}:{ss:02d} — проснулись в {wake.strftime('%H:%M:%S')}, "
                f"а ждали начало часа {want_hour:02d}:00 (это и есть «вопрос приходит когда попало»)")
        elif wake.minute != 0:
            problems.append(
                f"старт {hh:02d}:{mm:02d}:{ss:02d} — проснулись в {wake.strftime('%H:%M:%S')}: "
                f"минута не нулевая, шаг снова прилип к минуте запуска")

    # ── 2. Пауза в разумных границах ──
    # Меньше 30 секунд — цикл крутится вхолостую; больше часа с запасом —
    # проспали бы срок.
    for hh, mm, ss in ((6, 46, 50), (12, 0, 0), (12, 59, 59), (23, 59, 59)):
        done += 1
        pause = dr.seconds_to_next_hour(at(hh, mm, ss))
        if not 30 <= pause <= 3605:
            problems.append(f"пауза из {hh:02d}:{mm:02d}:{ss:02d} вышла {pause:.0f} сек — "
                            f"вне разумных границ 30…3605")

    # ── 3. ГЛАВНОЕ: цикл правда зовёт этот расчёт ──
    # Починить расчёт и забыть переключить на него цикл — ровно та ошибка,
    # ради которой эта проверка и заведена.
    src = open(os.path.join(ROOT, "jobs", "reports.py"), encoding="utf-8").read()
    done += 1
    if "seconds_to_next_hour()" not in src:
        problems.append("цикл в jobs/reports.py НЕ зовёт seconds_to_next_hour — "
                        "расчёт починен, а спит цикл по-старому")
    done += 1
    if "3600)" in src.replace("delay = 3600", "").replace("= 3600\n", ""):
        problems.append("в цикле остался сон ровно на 3600 секунд — "
                        "пробуждения снова прилипнут к минуте запуска бота")

    return problems, f"{done} проверок: начало часа, границы паузы, цикл зовёт расчёт"


# ───────────────────────────────────────────────
#  10. ОТБОР СТАТЕЙ БАЗЫ ЗНАНИЙ
# ─────────────────────────────────────────────

def check_rag_pick():
    """
    Правило «пик против полки»: статья идёт модели, только если её балл
    заметно отрывается от остальных.

    ⚠️ Смысл правила: у настоящего вопроса про технику одна статья ближе
    прочих (пик), у болтовни все статьи одинаково средне похожи (полка).
    Сломается — бот начнёт подмешивать случайные статьи в ответ на «привет»
    либо перестанет находить нужные вовсе.
    """
    from config import RAG_STRONG_SIM, RAG_MIN_SIMILARITY
    from services import rag

    problems = []
    done = 0

    def expect(title, score, baseline, floor, margin, want_ok, want_why):
        nonlocal done
        done += 1
        ok, why = rag._chunk_passes(score, baseline, floor, margin)
        if ok != want_ok or why != want_why:
            problems.append(
                f"{title}: ожидалось ({'взять' if want_ok else 'отсеять'}, "
                f"«{want_why}»), вышло ({'взять' if ok else 'отсеять'}, «{why}»)")

    floor, margin = RAG_MIN_SIMILARITY, 0.14

    expect("балл ниже порога", floor - 0.01, 0.30, floor, margin, False, "ниже порога")
    expect("сильное совпадение", RAG_STRONG_SIM + 0.01, 0.69, floor, margin,
           True, "сильное совпадение")
    expect("пик над полкой", floor + 0.05, floor + 0.05 - margin, floor, margin,
           True, "пик над полкой")
    expect("полка без пика", floor + 0.05, floor + 0.04, floor, margin,
           False, "полка (нет пика)")
    # Ровно на пороге — берём (порог «не ниже», а не «строго выше»)
    expect("ровно на пороге силы", RAG_STRONG_SIM, 0.10, floor, margin,
           True, "сильное совпадение")
    # Ровно на границе отрыва — тоже берём
    expect("отрыв ровно на запас", floor + 0.05, floor + 0.05 - margin, floor, margin,
           True, "пик над полкой")

    # ── Нормализация запроса: от неё зависит попадание в кэш ──
    def expect_norm(title, raw, want):
        nonlocal done
        done += 1
        got = rag.normalize_query(raw)
        if got != want:
            problems.append(f"{title}: ожидалось {want!r}, вышло {got!r}")

    expect_norm("регистр и знаки", "Какая броня у Merkava?!", "какая броня у merkava")
    expect_norm("лишние пробелы", "  танк   умка  ", "танк умка")
    expect_norm("дефис сохраняется", "T-72 броня", "t-72 броня")
    expect_norm("пустая строка", "   ", "")

    # ── Мера близости ──
    done += 1
    same = rag.cosine_similarity([1.0, 0.0], [1.0, 0.0])
    if abs(same - 1.0) > 1e-9:
        problems.append(f"одинаковые векторы дали близость {same}, а не 1.0")
    done += 1
    orth = rag.cosine_similarity([1.0, 0.0], [0.0, 1.0])
    if abs(orth) > 1e-9:
        problems.append(f"перпендикулярные векторы дали близость {orth}, а не 0")

    return problems, f"{done} проверок: пик против полки, нормализация, близость"


# ───────────────────────────────────────────────
#  11. ЗВАНИЯ ВИКТОРИНЫ
# ─────────────────────────────────────────────

def check_quiz_ranks():
    """
    Лестница званий: без дыр, без перекрытий, покрывает любое число ответов.

    ⚠️ Дыра в лестнице не роняет бота — человек просто «застревает» на
    прежнем звании и не понимает почему. Проверка целостности здесь дешевле
    любого разбирательства постфактум.
    """
    from config import QUIZ_RANKS
    from database import history as hist

    problems = []
    done = 0

    done += 1
    if not QUIZ_RANKS:
        return ["список званий пуст"], "0 проверок"

    done += 1
    if QUIZ_RANKS[0]["min"] != 0:
        problems.append(f"лестница начинается не с нуля: первое звание с "
                        f"{QUIZ_RANKS[0]['min']} верных ответов — новичок останется без звания")

    prev = None
    for r in QUIZ_RANKS:
        done += 1
        if r["min"] > r["max"]:
            problems.append(f"звание «{r['name']}»: нижняя граница {r['min']} "
                            f"больше верхней {r['max']}")
        for field in ("name", "icon", "desc"):
            done += 1
            if not str(r.get(field) or "").strip():
                problems.append(f"звание «{r.get('name')}»: пустое поле «{field}»")
        if prev is not None:
            done += 1
            if r["min"] != prev["max"] + 1:
                problems.append(
                    f"разрыв в лестнице между «{prev['name']}» (до {prev['max']}) "
                    f"и «{r['name']}» (с {r['min']}): значения между ними "
                    f"не покрыты ни одним званием")
        prev = r

    done += 1
    if QUIZ_RANKS[-1]["max"] < 9999:
        problems.append(f"последнее звание кончается на {QUIZ_RANKS[-1]['max']} — "
                        f"самый упорный игрок останется без звания")

    # ── Расчёт звания по числу ответов (на границах) ──
    UID = -777003
    try:
        for correct, want_name in ((0, QUIZ_RANKS[0]["name"]),
                                   (QUIZ_RANKS[0]["max"], QUIZ_RANKS[0]["name"]),
                                   (QUIZ_RANKS[1]["min"], QUIZ_RANKS[1]["name"]),
                                   (QUIZ_RANKS[-1]["min"], QUIZ_RANKS[-1]["name"])):
            # Пишем прямо в таблицу временной базы: отдельной функции
            # «поставить счёт» в проекте нет, а гонять настоящие ответы на
            # опросы ради четырёх границ — дороже и менее наглядно.
            with hist._lock:
                conn = hist._get_connection()
                conn.execute("DELETE FROM quiz_stats WHERE user_id=?", (UID,))
                conn.execute(
                    "INSERT INTO quiz_stats (user_id, username, correct_answers, total_attempts) "
                    "VALUES (?, ?, ?, ?)", (UID, "проверка", correct, max(correct, 1)))
                conn.commit()
            done += 1
            got = hist.get_user_stats(UID)["rank"]
            if got != want_name:
                problems.append(f"при {correct} верных ответах ожидалось звание "
                                f"«{want_name}», выдано «{got}»")

        # Последнее звание: следующей ступени нет
        done += 1
        if hist.get_user_stats(UID)["next_rank_needed"] != -1:
            problems.append("у высшего звания указана следующая ступень — "
                            "в личном деле появится прогресс к несуществующему званию")
    finally:
        with hist._lock:
            conn = hist._get_connection()
            conn.execute("DELETE FROM quiz_stats WHERE user_id=?", (UID,))
            conn.commit()

    return problems, f"{done} проверок: лестница без дыр, границы званий"


# ───────────────────────────────────────────────
#  12. ВИКТОРИНА: ФАЙЛ ВОПРОСОВ ПРОТИВ БАНКА
# ─────────────────────────────────────────────

def check_quiz_seed_sync():
    """
    Сверка эталонного файла с банком видит расхождение — и чинит его.

    ⚠️ РАДИ ЧЕГО ПРОВЕРКА СУЩЕСТВУЕТ. Кнопка «📥 Мои вопросы в черновики»
    пропускает вопрос, который в банке уже есть, ЦЕЛИКОМ: сверяет только пару
    «статья + текст вопроса». Значит правка ВАРИАНТОВ, ВЕРНОГО ОТВЕТА или
    РАЗБОРА в файле обычной отправкой кода не доезжает в игру НИКАК, и увидеть
    это нельзя ничем — файл в репозитории новый, у людей старый. Так и вышло
    21.08.2026: «Рапорт Полковника:» вычищали прямо в боевой базе руками.

    Проверка идёт по настоящему пути: пишет свой файл вопросов, заливает его
    кнопкой, ломает файл тремя разными способами и требует, чтобы сверка
    назвала КАЖДОЕ расхождение своим именем, а обновление их вылечило.

    ⚠️ Отдельно проверяется то, ЧЕГО делать нельзя: обновление не смеет
    трогать вопросы, которых в файле нет (машинная сборка), сбрасывать статус
    «в игре» и обнулять счётчик показов. Иначе одна кнопка тихо вернула бы в
    черновики всё, что Максим уже одобрил.
    """
    import json as _json
    import shutil as _shutil
    import tempfile as _tempfile

    from database import history as hist
    from services import quiz_bank

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    def write_seed(items):
        with open(seed_path, "w", encoding="utf-8") as f:
            _json.dump(items, f, ensure_ascii=False)

    def wipe_bank():
        for q in hist.list_all_quiz_questions():
            hist.delete_quiz_question(q["id"])

    base = [
        {"article": "проверка-1.md", "question": "Первый проверочный вопрос?",
         "options": ["раз", "два", "три", "четыре"], "correct_idx": 0,
         "explanation": "Разбор первого."},
        {"article": "проверка-2.md", "question": "Второй проверочный вопрос?",
         "options": ["раз", "два", "три", "четыре"], "correct_idx": 1,
         "explanation": "Разбор второго."},
        {"article": "проверка-3.md", "question": "Третий проверочный вопрос?",
         "options": ["раз", "два", "три", "четыре"], "correct_idx": 2,
         "explanation": "Разбор третьего."},
    ]

    tmp_dir = _tempfile.mkdtemp(prefix="c4max-selftest-seed-")
    seed_path = os.path.join(tmp_dir, "questions.json")
    saved_path = quiz_bank.SEED_PATH
    quiz_bank.SEED_PATH = seed_path

    try:
        wipe_bank()
        write_seed(base)

        # ── 1. Пустой банк: всё «не залито», расхождений нет ──
        diff = quiz_bank.seed_diff()
        expect(f"на пустом банке сверка не увидела файл: {diff}", diff["file_ok"])
        expect(f"в файле 3 вопроса, сверка насчитала {diff['total']}", diff["total"] == 3)
        expect(f"на пустом банке «не залито» должно быть 3, а не {diff['missing']}",
               diff["missing"] == 3)
        expect(f"на пустом банке не может быть совпадений, а их {diff['same']}",
               diff["same"] == 0 and diff["changed"] == 0 and diff["extra"] == 0)

        # ── 2. Залили кнопкой — расхождений не осталось ──
        loaded = quiz_bank.load_seed(approved=False)
        expect(f"загрузка добавила {loaded['added']} вопросов вместо 3",
               loaded["added"] == 3)
        diff = quiz_bank.seed_diff()
        expect(f"сразу после загрузки всё обязано сойтись, а вышло {diff}",
               diff["same"] == 3 and diff["changed"] == 0
               and diff["missing"] == 0 and diff["extra"] == 0)

        # Один вопрос отправляем в игру и отмечаем показ: дальше проверим,
        # что обновление ни того, ни другого не тронуло.
        bank = {q["question"]: q for q in hist.list_all_quiz_questions()}
        live_id = bank["Первый проверочный вопрос?"]["id"]
        hist.set_quiz_question_approved(live_id, True)
        hist.note_quiz_question_asked(live_id)

        # ── 3. Ломаем файл ТРЕМЯ разными способами ──
        broken = _json.loads(_json.dumps(base))
        broken[0]["explanation"] = "Разбор первого, переписанный."
        broken[1]["correct_idx"] = 3
        broken[2]["options"] = ["раз", "два", "три", "пять"]
        write_seed(broken)

        diff = quiz_bank.seed_diff()
        expect(f"после трёх правок файла сверка насчитала расхождений "
               f"{diff['changed']} вместо 3", diff["changed"] == 3)
        expect(f"правки не заводят новых вопросов, а сверка увидела "
               f"{diff['missing']} не залитых и {diff['extra']} лишних",
               diff["missing"] == 0 and diff["extra"] == 0)

        named = {i["question"]: i["what"] for i in diff["items"]}
        expect(f"у первого вопроса разошёлся разбор, а сверка говорит "
               f"«{named.get('Первый проверочный вопрос?')}»",
               named.get("Первый проверочный вопрос?") == "разбор")
        expect(f"у второго вопроса разошёлся верный ответ, а сверка говорит "
               f"«{named.get('Второй проверочный вопрос?')}»",
               named.get("Второй проверочный вопрос?") == "ВЕРНЫЙ ОТВЕТ")
        expect(f"у третьего вопроса разошлись варианты, а сверка говорит "
               f"«{named.get('Третий проверочный вопрос?')}»",
               named.get("Третий проверочный вопрос?") == "варианты ответа")

        # ── 4. ГЛАВНОЕ: кнопка загрузки этого НЕ чинит ──
        # Если однажды она научится чинить сама — эта проверка покраснеет, и
        # разбираться придётся не с молчаливой пропажей правок, а с проверкой.
        loaded = quiz_bank.load_seed(approved=False)
        expect(f"кнопка загрузки добавила {loaded['added']} вопросов там, где "
               f"добавлять нечего — она обязана пропускать знакомые",
               loaded["added"] == 0 and loaded["skipped"] == 3)
        after_load = quiz_bank.seed_diff()["changed"]
        expect(f"после кнопки загрузки расхождений осталось {after_load} из 3: "
               f"либо она их вылечила сама (тогда кнопка обновления не нужна), "
               f"либо сверка перестала их видеть",
               after_load == 3)

        # ── 5. Обновление лечит ──
        applied = quiz_bank.seed_apply()
        expect(f"обновление поправило {applied['updated']} из 3",
               applied["updated"] == 3 and applied["changed"] == 3)
        diff = quiz_bank.seed_diff()
        expect(f"после обновления всё обязано сойтись, а вышло {diff}",
               diff["changed"] == 0 and diff["same"] == 3)

        # ── 6. Обновление не тронуло статус и счётчик показов ──
        live = hist.get_quiz_question(live_id)
        expect("обновление вернуло в черновики вопрос, который был в игре",
               live["approved"] is True)
        expect(f"обновление сбило счётчик показов: {live['asked_count']} вместо 1",
               live["asked_count"] == 1)
        expect(f"разбор не догнал файл: «{live['explanation']}»",
               live["explanation"] == "Разбор первого, переписанный.")

        # ── 7. Машинный вопрос: его в файле нет, трогать нельзя ──
        own_id = hist.add_quiz_question("проверка-4.md", "Собранный моделью вопрос?",
                                        ["раз", "два", "три", "четыре"], 0, "Свой разбор.")
        diff = quiz_bank.seed_diff()
        expect(f"вопрос вне файла обязан считаться «в банке своё», а вышло "
               f"extra={diff['extra']}", diff["extra"] == 1)
        expect("вопрос вне файла попал в расхождения — обновление затрёт "
               "машинную сборку", diff["changed"] == 0)
        quiz_bank.seed_apply()
        own = hist.get_quiz_question(own_id)
        expect("обновление переписало вопрос, которого в файле нет",
               own["explanation"] == "Свой разбор.")

        # ── 8. Правка САМОГО ТЕКСТА вопроса — это другой вопрос ──
        # Такую сверка обязана показать как «не залито» + «в банке своё», а не
        # чинить молча: угадывать, какая старая запись кем заменяется, нельзя.
        renamed = _json.loads(_json.dumps(broken))
        renamed[0]["question"] = "Первый проверочный вопрос, переписанный?"
        write_seed(renamed)
        diff = quiz_bank.seed_diff()
        expect(f"переписанный текст вопроса обязан быть «не залито 1», а вышло "
               f"{diff['missing']}", diff["missing"] == 1)
        expect(f"старая запись обязана остаться видимой как «в банке своё» "
               f"(машинная 1 + осиротевшая 1 = 2), а вышло {diff['extra']}",
               diff["extra"] == 2)

        # ── 9. Битый файл не роняет ни сверку, ни обновление ──
        with open(seed_path, "w", encoding="utf-8") as f:
            f.write("{ это не JSON")
        diff = quiz_bank.seed_diff()
        expect("на битом файле сверка обязана сказать «файла нет», а не "
               "показать расхождения", not diff["file_ok"] and diff["changed"] == 0)
        expect("обновление на битом файле что-то переписало",
               quiz_bank.seed_apply()["updated"] == 0)

    finally:
        quiz_bank.SEED_PATH = saved_path
        _shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            wipe_bank()
        except Exception:
            pass

    return problems, (f"{done} проверок: сверка файла с банком, три вида "
                      f"расхождений, загрузка их не чинит, обновление чинит")


# ───────────────────────────────────────────────
#  13. СУТОЧНЫЙ ОТЧЁТ: РАСХОД ЗА ПЕРИОД
# ─────────────────────────────────────────────

def check_daily_report():
    """
    Расход за период = (текущее + отложенное) − снимок.

    ⚠️ Ошибка здесь всплывает только НА СЛЕДУЮЩИЙ ДЕНЬ и выглядит как «бот
    вдруг стал дорого стоить» — либо, что хуже, как «расход почти нулевой»,
    и тогда о перерасходе узнаешь по пустому счёту у провайдера.

    ⚠️ «Отложенное» — не выдумка: первого числа месяца бот обнуляет вызовы и
    копилки Qwen и картинок, часть которых относится к ТЕКУЩИМ, ещё не
    отчитанным суткам. Уничтожаемую часть откладывают в settings, и отчёт
    обязан её прибавить, иначе отчёт за 1-е число покажет только то, что
    накапало после обнуления.
    """
    from services import daily_report as dr

    problems = []
    done = 0

    def expect_spent(title, current, base, carried, want_value, want_manual):
        nonlocal done
        done += 1
        value, manual = dr._spent(current, base, carried)
        if not _same_money(value, want_value):
            problems.append(f"{title}: расход ожидался {want_value}, вышло {value}")
        done += 1
        if manual != want_manual:
            problems.append(
                f"{title}: признак «счётчик правили руками» ожидался "
                f"{want_manual}, вышло {manual}")

    # Обычные сутки: копилка выросла с 0.212128 до 0.280422
    expect_spent("обычный расход", 0.280422, 0.212128, 0.0, 0.068294, False)
    # Ничего не тратили
    expect_spent("нулевой период", 5.0, 5.0, 0.0, 0.0, False)
    # После месячного обнуления: копилку обнулили, накапало 0.5, отложено 2.0
    expect_spent("с переносом после обнуления", 0.5, 0.0, 2.0, 2.5, False)
    # ⚠️ Счётчик правили руками: разница ушла в минус. Отчёт обязан показать
    # НОЛЬ и поднять признак, а не отрицательные деньги.
    expect_spent("счётчик уменьшили вручную", 1.0, 5.0, 0.0, 0.0, True)

    done += 1
    value, _ = dr._spent(1.0, 5.0, 0.0)
    if value < 0:
        problems.append(f"отрицательный расход {value} — в отчёте появятся "
                        f"деньги со знаком минус")

    # ── Раскладка вызовов по провайдерам ──
    from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, PROVIDERS

    some_gemini = next(m for m, v in AVAILABLE_MODELS.items() if v["provider"] == "gemini")
    some_qwen = next(m for m, v in AVAILABLE_MODELS.items() if v["provider"] == "qwen")
    some_image = next(iter(AVAILABLE_IMAGE_MODELS))

    calls = {some_gemini: 7, some_qwen: 3, some_image: 2, "модель-которой-нет": 5}
    groups = dr._calls_by_group(calls)

    done += 1
    if dict(groups.get("gemini") or {}).get(some_gemini) != 7:
        problems.append(f"вызовы {some_gemini} не попали в блок Gemini")
    done += 1
    if dict(groups.get("qwen") or {}).get(some_qwen) != 3:
        problems.append(f"вызовы {some_qwen} не попали в блок Qwen")
    done += 1
    if dict(groups.get("image") or {}).get(some_image) != 2:
        problems.append(f"вызовы {some_image} не попали в блок картинок")

    # ⚠️ Модель, удалённую из настроек, но с вызовами за период, терять нельзя:
    # иначе «Всего вызовов» разойдётся с суммой строк, и понять почему —
    # невозможно.
    done += 1
    other = dict(groups.get("other") or {})
    if other.get("модель-которой-нет") != 5:
        problems.append("вызовы удалённой из настроек модели потерялись — "
                        "итог отчёта разойдётся с суммой строк")

    # Модель без вызовов остаётся в списке с нулём (её видно в отчёте)
    done += 1
    if not any(name == some_gemini for name, _ in (groups.get("gemini") or [])):
        problems.append("модель пропала из своего блока")

    # Порядок внутри блока — по числу вызовов, больше сверху
    done += 1
    gem = groups.get("gemini") or []
    if gem and gem != sorted(gem, key=lambda p: (-p[1], p[0])):
        problems.append("порядок моделей в блоке не по числу вызовов")

    # ── Недельная копилка ──
    # ⚠️ Работает на ВРЕМЕННОЙ базе (main увёл DB_PATH), боевую не трогаем.
    from database import history as hist

    try:
        dr._week_clear()
        dr.week_add_day("2026-08-25 21:00:00", "2026-08-26 21:00:00",
                        {"calls": {some_qwen: 4}, "burned": {some_qwen: 1000}})
        dr.week_add_day("2026-08-26 21:00:00", "2026-08-27 21:00:00",
                        {"calls": {some_qwen: 6}, "burned": {some_qwen: 2000}})
        acc = dr._week_read()

        done += 1
        if int(acc.get("days") or 0) != 2:
            problems.append(f"в недельной копилке {acc.get('days')} суток вместо 2")
        done += 1
        if (acc.get("calls") or {}).get(some_qwen) != 10:
            problems.append(f"вызовы за неделю сложились неверно: "
                            f"{(acc.get('calls') or {}).get(some_qwen)} вместо 10")
        done += 1
        if (acc.get("burned") or {}).get(some_qwen) != 3000:
            problems.append(f"сожжённые токены за неделю сложились неверно: "
                            f"{(acc.get('burned') or {}).get(some_qwen)} вместо 3000")

        # ⚠️ Сутки, в которые квоту ЗАВЕЛИ ЗАНОВО (остаток вырос, разница
        # отрицательная), в сумму идти не должны — иначе недельная строка
        # соврёт. Такие модели помечаются, и отчёт честно скажет «≥».
        dr.week_add_day("2026-08-27 21:00:00", "2026-08-28 21:00:00",
                        {"calls": {}, "burned": {some_qwen: -5000}})
        acc = dr._week_read()
        done += 1
        if (acc.get("burned") or {}).get(some_qwen) != 3000:
            problems.append("заведение новой квоты испортило недельную сумму "
                            "сожжённых токенов")
        done += 1
        if some_qwen not in (acc.get("qwen_reset") or []):
            problems.append("сутки с заведением квоты не помечены — "
                            "недельный отчёт покажет точное число вместо «≥»")
    finally:
        dr._week_clear()

    return problems, f"{done} проверок: расход, перенос, раскладка, копилка недели"


def check_settings_spec():
    """
    Единый список простых настроек (services/settings_spec.py) против тех, кто
    эти настройки РЕАЛЬНО читает в боте.

    ⚠️ Ради чего проверка существует. 30.08.2026 пределы и начальные значения
    съехались в один файл из трёх разных мест, потому что настройки стало
    крутить два хозяина — кнопки в Telegram и страница сайта. Цена ошибки тут
    тихая: разойдись начальное значение у списка и у читалки — панель
    показывала бы одно, а бот вёл себя по-другому, и до первого нажатия
    кнопки этого не увидел бы никто.

    Поэтому сверяем не комментарии, а поведение: для каждого тумблера зовём
    его настоящую читалку из services/antispam.py, services/greeter.py,
    utils_format.py и требуем совпадения — и при пустой базе (начальное
    значение), и при "1", и при "0".
    """
    import database.history as hist
    from services import settings_spec as spec

    problems = []
    done = 0

    def expect(title, got, want):
        nonlocal done
        done += 1
        if got != want:
            problems.append(f"{title}: получилось {got!r}, ожидалось {want!r}")

    # ─── тумблеры против своих настоящих читалок ───
    from services.antispam import is_enabled, is_linkfilter_enabled
    from services import greeter
    from utils_format import thoughts_enabled

    readers = {
        "antispam_enabled":   is_enabled,
        "linkfilter_enabled": is_linkfilter_enabled,
        "greet_enabled":      greeter.is_enabled,
        "greet_captcha":      greeter.captcha_enabled,
        "greet_kick":         greeter.kick_enabled,
        "thoughts_enabled":   thoughts_enabled,
    }
    for key, reader in readers.items():
        # Начальное значение: список и читалка обязаны сойтись на чистой базе.
        with _no_row(hist, key):
            expect(f"{key}: начальное значение", spec.read(key), reader())
        for raw, want in (("1", True), ("0", False)):
            hist.set_setting(key, raw)
            expect(f"{key} = {raw}: список", spec.read(key), want)
            expect(f"{key} = {raw}: читалка бота", reader(), want)

    # ─── числа против своих читалок ───
    from services.antispam import get_thresholds
    hist.set_setting("antispam_msg_count", "7")
    hist.set_setting("antispam_window_sec", "9")
    hist.set_setting("antispam_mute_sec", "600")
    expect("пороги антиспама: список против читалки",
           (spec.read("antispam_msg_count"), spec.read("antispam_window_sec"),
            spec.read("antispam_mute_sec")),
           get_thresholds())

    hist.set_setting("greet_timeout_sec", "900")
    expect("срок проверки: список против читалки",
           spec.read("greet_timeout_sec"), greeter.timeout_sec())

    # ─── пределы: за границу не выпускаем ───
    hist.set_setting("antispam_msg_count", "2")
    expect("порог флуда: ниже минимума не уходит", spec.adjust("antispam_msg_count", -1), 2)
    hist.set_setting("antispam_msg_count", "50")
    expect("порог флуда: выше максимума не уходит", spec.adjust("antispam_msg_count", +1), 50)
    hist.set_setting("greet_timeout_sec", "60")
    expect("срок проверки: ниже минимума не уходит", spec.adjust("greet_timeout_sec", -1), 60)
    hist.set_setting("rag_top_k", "10")
    expect("статей в ответ: выше максимума не уходит", spec.adjust("rag_top_k", +1), 10)

    # ─── шаг: тот же, что был у кнопок до переезда ───
    hist.set_setting("antispam_mute_sec", "300")
    expect("мут: шаг 60 секунд", spec.adjust("antispam_mute_sec", +1), 360)
    hist.set_setting("proactive_context_msgs", "25")
    expect("стенограмма: шаг 5", spec.adjust("proactive_context_msgs", +1), 30)
    hist.set_setting("rag_min_similarity", "0.58")
    expect("порог сходства: шаг 0.02", spec.adjust("rag_min_similarity", +1), 0.60)
    hist.set_setting("rag_peak_margin", "0.14")
    expect("запас над фоном: шаг 0.01", spec.adjust("rag_peak_margin", -1), 0.13)

    # ⚠️ Дробное обязано лечь в базу СТРОКОЙ с двумя знаками. Без округления
    # арифметика с плавающей точкой пишет туда 0.13000000000000003, и это
    # значение потом читают все, включая отбор статей.
    expect("запас над фоном: в базе две цифры после точки",
           hist.get_setting("rag_peak_margin", ""), "0.13")

    # ─── прямая запись (поле и ползунок на сайте) ───
    expect("прямая запись: подрезается сверху", spec.write("antispam_msg_count", 999), 50)
    expect("прямая запись: подрезается снизу", spec.write("antispam_msg_count", -5), 2)
    # Значение между шагами обязано прижаться к сетке, иначе кнопки ➖/➕ в
    # Telegram пойдут по сдвинутой шкале и разойдутся с сайтом навсегда.
    expect("прямая запись: прижимается к шагу", spec.write("antispam_mute_sec", 350), 360)
    expect("прямая запись: дробное прижимается к шагу",
           spec.write("rag_min_similarity", 0.611), 0.62)

    # ⚠️ ГЛАВНАЯ ПРОВЕРКА ЭТОГО БЛОКА: сетка у кнопок ➖/➕ и у прямой записи
    # ОДНА И ТА ЖЕ. Шагнули кнопкой — записали то же самое напрямую — значение
    # не должно сдвинуться ни на волос. Разойдись сетки, и сайт с кнопками
    # ходили бы по разным лестницам: правка с одной стороны каждый раз слегка
    # двигала бы значение. Именно здесь это и поймалось при написании.
    for key, item in spec.SPEC.items():
        if item["kind"] == "toggle":
            continue
        for steps in (-2, -1, 1, 2):
            _reset_setting(hist, key, item)
            after_button = spec.adjust(key, steps)
            after_write = spec.write(key, after_button)
            expect(f"{key}: сетка кнопок и прямой записи совпадает (шагов {steps})",
                   after_write, after_button)
    expect("прямая запись: тумблер понимает «выключено»",
           spec.write("antispam_enabled", "0"), False)
    expect("прямая запись: тумблер понимает «включено»",
           spec.write("antispam_enabled", "on"), True)
    try:
        spec.write("antispam_msg_count", "не число")
        problems.append("прямая запись: мусор проглочен молча")
    except ValueError:
        done += 1

    # ─── мусор в базе не роняет бота ───
    hist.set_setting("antispam_msg_count", "пять")
    expect("мусор в числе = начальное значение",
           spec.read("antispam_msg_count"), spec.SPEC["antispam_msg_count"]["default"])

    # ─── кнопки ➖/➕ базы знаний крутят существующие настройки ───
    # Соответствие «пара кнопок → настройка» живёт в panel_rag; опечатка в
    # ключе проявилась бы только при живом нажатии, уже на сервере.
    from handlers.admin.panel_rag import _KB_ADJUST_KEYS
    for prefix, key in _KB_ADJUST_KEYS.items():
        done += 1
        if key not in spec.SPEC:
            problems.append(f"кнопки {prefix}: настройки «{key}» нет в списке")
    for prefix in _KB_ADJUST_KEYS:
        for other in _KB_ADJUST_KEYS:
            if prefix != other and other.startswith(prefix):
                problems.append(f"приставки кнопок пересекаются: «{prefix}» и «{other}»")

    # ─── персональные пределы в карточке = общим ───
    # ⚠️ У карточки участника свои регуляторы тех же трёх порогов. До
    # 30.08.2026 границы там стояли отдельной копией с припиской «держим
    # такими же»; разойдись они — человеку можно было бы выставить порог,
    # недостижимый для всех остальных, и заметить это было бы нечем.
    from handlers.admin.panel_users import _USER_LIMITS
    for code, lim in _USER_LIMITS.items():
        if code == "img":
            continue          # у лимита картинок общей настройки нет вовсе
        key = lim["field"]
        done += 1
        if key not in spec.SPEC:
            problems.append(f"карточка крутит «{key}», а в общем списке его нет")
            continue
        item = spec.SPEC[key]
        for field in ("min", "max", "step"):
            done += 1
            if lim[field] != item[field]:
                problems.append(f"«{key}»: в карточке {field}={lim[field]}, "
                                f"в общей настройке {item[field]}")

    # ─── у каждой настройки есть раздел, и раздел объявлен ───
    known = {code for code, _ in spec.SECTIONS}
    for key, item in spec.SPEC.items():
        done += 1
        if item["section"] not in known:
            problems.append(f"{key}: раздел «{item['section']}» не объявлен в SECTIONS")
        if item["kind"] != "toggle":
            for field in ("min", "max", "step"):
                if field not in item:
                    problems.append(f"{key}: у числа нет «{field}»")

    return problems, (f"{done} проверок: {len(spec.SPEC)} настроек, "
                      f"{len(readers)} сверок с читалками бота, пределы, шаги, прямая запись")


def _flip_last(text: str) -> str:
    """Меняет последний знак строки на заведомо другой — чтобы порча подписи
    была порчей при любом её содержимом, а не через раз."""
    return text[:-1] + ("1" if text[-1] == "0" else "0")


def _reset_setting(hist, key, item):
    """Ставит настройку в её начальное значение — точка отсчёта для сверки сеток."""
    default = item["default"]
    if item["kind"] == "float":
        hist.set_setting(key, f"{default:.{item.get('digits', 2)}f}")
    else:
        hist.set_setting(key, str(default))


class _no_row:
    """Временно убирает строку настройки из базы — чтобы проверить, что
    список и читалка сходятся на НАЧАЛЬНОМ значении, а не на записанном."""

    def __init__(self, hist, key):
        self.hist, self.key = hist, key

    def __enter__(self):
        with self.hist._lock:
            conn = self.hist._get_connection()
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (self.key,)).fetchone()
            self.saved = row["value"] if row else None
            conn.execute("DELETE FROM settings WHERE key=?", (self.key,))
            conn.commit()
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            self.hist.set_setting(self.key, self.saved)
        return False


def check_prompts_spec():
    """
    Список промптов (services/prompts_spec.py) против таблицы промптов панели
    бота и против настоящих читалок из database/history.py.

    ⚠️ Ради чего проверка существует. Промпты описаны в ДВУХ местах: панель
    знает тексты подсказок и подтверждений для Telegram, сайт — куда какой
    текст уходит. Заведёшь шестой промпт в панели и забудешь в списке — он
    просто не появится на сайте, молча и без единой ошибки. Обратный случай
    хуже: сайт покажет поле, которого бот не знает, и правка уйдёт в никуда.

    Отдельно проверяется, что поле для правки показывает ХРАНИМЫЙ текст, а не
    то, что отдаёт читалка: у половины промптов читалка подставляет запасное
    значение из config, и правь мы показанное ею — в базу уехала бы копия
    запасного текста вместо пустоты.
    """
    import database.history as hist
    from services import prompts_spec
    from handlers.admin.panel_prompts import _PROMPTS

    problems = []
    done = 0

    # ─── два списка описывают одни и те же промпты ───
    # У панели ключи лежат в "keys" (у основного промпта их два: свой текст
    # и дополнения), у списка — по одному на карточку.
    panel_keys = {key for spec in _PROMPTS.values() for key in spec["keys"]}
    site_keys = set(prompts_spec.BY_KEY)
    done += 1
    for key in sorted(panel_keys - site_keys):
        problems.append(f"промпт «{key}» есть в панели бота, но не на сайте")
    for key in sorted(site_keys - panel_keys):
        problems.append(f"промпт «{key}» есть на сайте, но не в панели бота")

    # ─── у каждого есть название и пояснение ───
    for item in prompts_spec.PROMPTS:
        done += 1
        if not item.get("title") or not item.get("hint"):
            problems.append(f"промпт «{item['key']}»: нет названия или пояснения")

    # ─── читалки, на которые ссылается список, существуют ───
    for item in prompts_spec.PROMPTS:
        if not item["reader"]:
            continue
        done += 1
        if not hasattr(hist, item["reader"]):
            problems.append(f"промпт «{item['key']}»: читалки "
                            f"{item['reader']} в database/history нет")

    # ─── правка и стирание доходят до бота ───
    for item in prompts_spec.PROMPTS:
        key = item["key"]
        saved = hist.get_setting(key, "")
        try:
            prompts_spec.write(key, "  проверка текста  ")
            done += 1
            if prompts_spec.read(key) != "проверка текста":
                problems.append(f"«{key}»: пробелы по краям не срезаны")
            done += 1
            if hist.get_setting(key, "") != "проверка текста":
                problems.append(f"«{key}»: текст не дошёл до settings")
            prompts_spec.write(key, "")
            done += 1
            if prompts_spec.read(key) != "":
                problems.append(f"«{key}»: очистка не сработала")
        finally:
            hist.set_setting(key, saved)

    # ─── поле показывает ХРАНИМОЕ, а не запасное ───
    # ⚠️ ПРОВЕРКА ДОЛЖНА РАБОТАТЬ, ДАЖЕ КОГДА ЗАПАСНЫЕ ТЕКСТЫ ПУСТЫ. Сегодня
    # они пусты (убраны 16.08.2026), и просто «прочитать при пустой настройке»
    # ничего не доказывает: подмена читалки на запасной текст такую проверку
    # прошла бы насквозь (наступил на это при написании 30.08.2026). Поэтому
    # временно КЛАДЁМ в константу метку и требуем, чтобы поле её не показало,
    # а читалка бота — показала: тогда видно, что это два разных пути.
    import config as cfg
    for item in prompts_spec.PROMPTS:
        if not item.get("fallback"):
            continue
        key, const = item["key"], item["fallback"]
        saved = hist.get_setting(key, "")
        saved_const = getattr(cfg, const)
        try:
            hist.set_setting(key, "")
            setattr(cfg, const, "ЗАПАСНОЙ ТЕКСТ")
            done += 1
            if prompts_spec.read(key) != "":
                problems.append(f"«{key}»: поле показывает запасной текст "
                                f"вместо пустоты — правка запишет его копию")
            done += 1
            reader = getattr(hist, item["reader"])
            if reader() != "ЗАПАСНОЙ ТЕКСТ":
                problems.append(f"«{key}»: читалка бота НЕ берёт запасной "
                                f"текст из config.{const} — проверка выше "
                                f"ничего не значит")
        finally:
            setattr(cfg, const, saved_const)
            hist.set_setting(key, saved)

    # ─── собранный системный промпт = основной + дополнения ───
    saved_main = hist.get_setting("custom_system_prompt", "")
    saved_add = hist.get_setting("prompt_additions", "")
    try:
        hist.set_setting("custom_system_prompt", "ОСНОВА")
        hist.set_setting("prompt_additions", "ДОБАВКА")
        text, length = prompts_spec.assembled_system_prompt()
        done += 2
        if "ОСНОВА" not in text or "ДОБАВКА" not in text:
            problems.append("собранный промпт не содержит основу или дополнения")
        if length != len(text):
            problems.append("длина собранного промпта посчитана неверно")
    finally:
        hist.set_setting("custom_system_prompt", saved_main)
        hist.set_setting("prompt_additions", saved_add)

    return problems, (f"{done} проверок: {len(prompts_spec.PROMPTS)} промптов, "
                      f"сверка с панелью бота и читалками, правка и стирание")


def check_audit_codes():
    """
    Журнал персонала знает КАЖДЫЙ код действия, который в него пишут.

    ⚠️ Ради чего проверка существует. Код действия — это просто строка в
    вызове `_audit(...)`. Забыл завести ему подпись в `_ACTION_TITLES` — и
    журнал молча рисует «❔ quiz_nuke» голым кодом. Ошибка тихая: само
    действие работает, ломается только его название задним числом, когда
    кто-то полезет разбираться «кто это сделал».

    Так уже случилось трижды: `thinking` (чинили в v4.83), `quiz_nuke` и
    `quiz_seed` (нашлись 30.08.2026 при переносе викторины на сайт — их
    писали с 05.08). Три раза подряд — значит, дело не во внимательности.
    """
    from handlers.admin.panel_users import _ACTION_TITLES

    problems = []
    done = 0

    # Все места, где пишут в журнал: панели бота и действия сайта.
    files = sorted(pathlib.Path(ROOT, "handlers", "admin").glob("*.py"))
    files += [pathlib.Path(ROOT, "web", "actions.py")]

    # _audit(user_id, "код", …) / write_audit(…) / _staff_audit(…)
    pattern = re.compile(r'\b(?:_audit|write_audit|_staff_audit)\('
                         r'[^,)]+,\s*"([a-z_]+)"')
    found = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for code in pattern.findall(text):
            found.setdefault(code, set()).add(path.name)

    if not found:
        return ["не нашёл ни одного вызова журнала — проверка сломалась"], ""

    for code, where in sorted(found.items()):
        done += 1
        if code not in _ACTION_TITLES:
            problems.append(f"код «{code}» пишут в журнал ({', '.join(sorted(where))}), "
                            f"а подписи у него нет — журнал покажет ❔")

    return problems, (f"{done} кодов действий проверено по "
                      f"{len(files)} файлам, все с подписями")


def check_web_pages():
    """
    Страницы сайта собираются и показывают то, что нужно.

    ⚠️ Ради чего. Страница — это строка, собранная из данных; опечатка в
    имени поля не роняет ничего, она просто молча ничего не показывает.
    Так вышло с верным ответом викторины: страница брала `correct`, а в
    данных лежит `correct_idx` — вопросы одобрялись бы вслепую, не видя,
    какой ответ считается правильным. Поэтому проверяем не «собралось ли»,
    а «видно ли в собранном то, ради чего страницу открывают».
    """
    import asyncio

    import database.history as hist
    from web import pages

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    # ── викторина: верный ответ обязан быть подсвечен ──
    hist.add_quiz_question("Проверочная статья", "Сколько будет два плюс два?",
                           ["три", "четыре", "пять", "шесть"], 1, "Потому что.")
    drafts = hist.list_quiz_questions(approved=False, limit=5)
    expect("вопрос не попал в банк", bool(drafts))
    if drafts:
        html = pages.page_quiz(None, "подпись", mode="draft")
        expect("вопрос не виден на странице викторины", "два плюс два" in html)
        # Ровно один вариант помечен верным, и это именно «четыре».
        marked = re.findall(r'<li class="right">([^<]*)</li>', html)
        expect(f"верным помечено {marked} вместо ['четыре']", marked == ["четыре"])
        hist.delete_quiz_question(drafts[0]["id"])

    # ── база знаний: страница собирается и не падает без бота ──
    html = pages.page_kb(None, "подпись")
    expect("страница базы знаний не собралась", "База знаний" in html)
    expect("нет кнопки пересборки указателя", "Пересобрать указатель" in html)
    expect("нет проверки поиска", "Проверить поиск" in html)

    # ── сводка и промпты ──
    expect("сводка не собралась", "Админка C4_Max" in pages.page_summary("подпись"))
    expect("страница промптов не собралась", "Промпты" in pages.page_prompts("подпись"))
    expect("список людей не собрался", "Пользователи" in pages.page_users("подпись"))

    # ── карточка участника собирается даже на пустом человеке ──
    card = asyncio.run(pages.page_user_card(None, 999000111, "подпись"))
    expect("карточка участника не собралась", "Персональные настройки" in card)

    # ── обслуживание: все разделы на месте ──
    sys_html = pages.page_system(None, "подпись")
    for title in ("Счета и квоты", "Отчёты", "Логи", "Обновления",
                  "Дайджест недели", "Копия базы", "Опасное"):
        expect(f"на странице обслуживания нет раздела «{title}»",
               title in sys_html)

    # ⚠️ Список скачиваемого ЗАКРЫТЫЙ. Открой его для произвольного пути — и
    # адрес вида ?what=../../.env отдал бы ключи от всех нейросетей.
    from web.routes import _DOWNLOADS
    expect(f"в список скачивания попало лишнее: {_DOWNLOADS}",
           set(_DOWNLOADS) == {"log", "archive", "chatlog", "backup"})

    # ── оформление: цвета только через переменные, и палитры не разъехались ──
    # ⚠️ У CSS нет компилятора: переменная, ссылающаяся сама на себя, опечатка
    # в её имени и цвет, забытый в одной из тем, не роняют ничего — цвет просто
    # оказывается не тот. На всё это я наступил при заведении тем 30.08.2026.
    css = pathlib.Path(ROOT, "web", "static", "style.css").read_text(encoding="utf-8")

    # Палитры: тёмная (:root) и светлая (:root[data-theme="light"]).
    palettes = {}
    for sel, body in re.findall(r"(:root[^{]*)\{([^}]*)\}", css):
        palettes[sel.strip()] = dict(
            re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body))
    expect(f"палитр в оформлении не две, а {len(palettes)}: {sorted(palettes)}",
           len(palettes) == 2)

    for sel, defined in palettes.items():
        for name, value in defined.items():
            expect(f"{sel}: цвет {name} ссылается сам на себя — "
                   f"на странице его не будет", f"var({name})" not in value)

    # ⚠️ ГЛАВНАЯ СВЕРКА: обе темы описывают ОДИН И ТОТ ЖЕ набор цветов.
    # Забудешь цвет в светлой — в ней подставится тёмный, и на белом фоне
    # окажется чёрное пятно. Ничего при этом не падает.
    if len(palettes) == 2:
        (sel_a, a), (sel_b, b) = palettes.items()
        done += 1
        for name in sorted(set(a) - set(b)):
            problems.append(f"цвет {name} есть в «{sel_a}», но не в «{sel_b}» — "
                            f"во второй теме подставится чужой")
        for name in sorted(set(b) - set(a)):
            problems.append(f"цвет {name} есть в «{sel_b}», но не в «{sel_a}» — "
                            f"во второй теме подставится чужой")

    all_defined = set().union(*palettes.values()) if palettes else set()
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", css))
    done += 1
    for name in sorted(used - all_defined):
        problems.append(f"оформление зовёт {name}, а такой переменной нет")

    # ⚠️ Цвет, зашитый мимо палитр, не переключится вместе с темой.
    # Первая версия этой строки требовала после цвета символ забоя (\x08):
    # так «\b» превратился в управляющий символ по дороге через оболочку.
    # Проверка при этом бодро зеленела и не находила НИЧЕГО.
    outside = re.sub(r":root[^{]*\{[^}]*\}", "", css)
    stray = set(re.findall(r"#[0-9a-fA-F]{3,8}", outside))
    expect(f"в оформлении зашиты цвета мимо палитр: {sorted(stray)}", not stray)

    # ── тема доезжает до страницы ──
    import database.history as _h
    saved_theme = _h.get_setting(pages.THEME_SETTING_KEY, "")
    try:
        _h.set_setting(pages.THEME_SETTING_KEY, "light")
        light_page = pages.page_summary("подпись")
        expect("светлая тема не помечена на странице",
               'data-theme="light"' in light_page)
        # ⚠️ ПОМЕТКА СТРАНИЦЫ И СЕЛЕКТОР ПАЛИТРЫ — ОДНА И ТА ЖЕ СТРОКА.
        # Переименуй её в оформлении, и светлая тема молча останется тёмной:
        # палитр по-прежнему две, набор цветов совпадает, ничего не падает.
        # Ровно этот подлом прошёл мимо первой версии проверки.
        mark = re.search(r'data-theme="([a-z]+)"', light_page)
        done += 1
        if not mark:
            problems.append("на странице нет пометки темы вовсе")
        elif not any(f'[data-theme="{mark.group(1)}"]' in sel for sel in palettes):
            problems.append(
                f'страница помечена data-theme="{mark.group(1)}", а палитры '
                f'с таким селектором в оформлении нет: {sorted(palettes)}')
        _h.set_setting(pages.THEME_SETTING_KEY, "dark")
        expect("тёмная тема помечена как светлая",
               'data-theme="light"' not in pages.page_summary("подпись"))
        _h.set_setting(pages.THEME_SETTING_KEY, "мусор")
        expect("мусор в настройке темы не откатился на тёмную",
               pages.current_theme() == "dark")
    finally:
        _h.set_setting(pages.THEME_SETTING_KEY, saved_theme)

    # ── верхняя полоса есть на КАЖДОЙ странице и ведёт во все разделы ──
    # ⚠️ Полоса собирается в каждой странице отдельным вызовом. Забудешь её
    # в новой странице — с неё будет некуда уйти, кроме как «назад» браузером,
    # и заметишь это только руками. Поэтому проверяем каждую поимённо.
    import asyncio as _aio

    made = {
        "/":        lambda: pages.page_summary("подпись"),
        "/prompts": lambda: pages.page_prompts("подпись"),
        "/users":   lambda: pages.page_users("подпись"),
        "/kb":      lambda: pages.page_kb(None, "подпись"),
        "/quiz":    lambda: pages.page_quiz(None, "подпись"),
        "/journal": lambda: pages.page_journal("подпись"),
        "/system":  lambda: pages.page_system(None, "подпись"),
    }
    for where, build in made.items():
        html_page = build()
        done += 1
        if 'class="topbar"' not in html_page:
            problems.append(f"на странице {where} нет верхней полосы")
            continue
        for href, _label in pages.NAV:
            if href == where:
                # Свой раздел подсвечен и не нажимается.
                done += 1
                if f'<span class="navlink on">' not in html_page:
                    problems.append(f"на {where} свой раздел не подсвечен")
            elif f'href="{href}"' not in html_page:
                problems.append(f"на странице {where} нет кнопки раздела {href}")
        done += 1
        if "Вид" not in html_page:
            problems.append(f"на странице {where} нет выбора темы")

    # ⚠️ СПИСОК РАЗДЕЛОВ СВЕРЯЕТСЯ С АДРЕСАМИ САЙТА, а не сам с собой. Иначе
    # убранный из списка раздел просто исчезает со всех страниц, а проверка,
    # берущая ожидания из того же списка, этого не замечает (поймано подломом).
    from web.routes import ROUTES
    # Страницы с навигацией — это все владельческие GET-адреса, кроме
    # действий (выход, скачивание) и подстраниц с номером в адресе.
    page_routes = {p for m, p, _h, a in ROUTES
                   if m == "GET" and a == "owner"
                   and "{" not in p and p not in ("/exit", "/download")}
    nav_hrefs = {href for href, _ in pages.NAV}
    done += 1
    for extra in sorted(page_routes - nav_hrefs):
        problems.append(f"страница {extra} есть, а кнопки раздела к ней нет")
    for orphan in sorted(nav_hrefs - page_routes):
        problems.append(f"в полосе есть кнопка {orphan}, а такой страницы нет")

    # ⚠️ Кнопка темы несёт адрес возврата: без него смена темы с любой
    # страницы, кроме сводки, выбрасывала бы на сводку.
    kb_page = pages.page_kb(None, "подпись")
    done += 1
    if 'name="back" value="/kb"' not in kb_page:
        problems.append("кнопка темы на странице базы знаний не помнит, "
                        "куда вернуться")

    # ── возврат пускает только свой путь ──
    # ⚠️ Поле формы задаёт адрес перехода. Пропусти чужой — и владельца уведёт
    # с админки на чужую страницу по нажатию собственной кнопки.
    from web.routes import _safe_back
    for raw, want in (
        ("/kb", "/kb"),
        ("/users/123", "/users/123"),
        ("https://чужой.сайт", "/"),
        ("//чужой.сайт", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        (None, "/"),
    ):
        done += 1
        if _safe_back(raw) != want:
            problems.append(f"возврат по адресу {raw!r} дал {_safe_back(raw)!r}, "
                            f"а должен {want!r}")

    # ── браузер не может показать устаревшее оформление ──
    # ⚠️ ЭТО НЕ ТЕОРИЯ. 30.08.2026 сайт показывал светлую тему при выбранной
    # тёмной: сервер отдавал всё верно, а браузер держал утреннюю копию
    # style.css — заголовка про кэш у неё не было вовсе, адрес не менялся.
    # Поэтому проверяются ОБЕ части лечения: отпечаток в адресе и заголовок.
    import asyncio as _a
    import hashlib as _hl

    import aiohttp as _ah
    from aiohttp import web as _aw

    from web import build_app

    page = pages.page_summary("подпись")
    done += 1
    if "/static/style.css?v=" not in page:
        problems.append("адрес оформления без отпечатка — браузер вправе "
                        "показывать старую копию сколько угодно")

    # Отпечаток обязан считаться ПО СОДЕРЖИМОМУ: иначе он не сменится, когда
    # оформление поправят, и старая копия так и останется у браузера.
    real = _hl.md5(pathlib.Path(ROOT, "web", "static",
                                "style.css").read_bytes()).hexdigest()[:10]
    done += 1
    if f"style.css?v={real}" not in page:
        problems.append(f"отпечаток в адресе оформления не совпал с самим "
                        f"файлом (ждали {real})")

    async def _headers():
        runner = _aw.AppRunner(build_app(None), access_log=None)
        await runner.setup()
        site = _aw.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        out = {}
        try:
            async with _ah.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/static/style.css") as r:
                    out["static"] = r.headers.get("Cache-Control", "")
                async with s.get(f"http://127.0.0.1:{port}/health") as r:
                    out["page"] = r.headers.get("Cache-Control", "")
        finally:
            await runner.cleanup()
        return out

    got = _a.run(_headers())
    done += 1
    if got.get("static") != "no-cache":
        problems.append(f"оформление отдаётся с Cache-Control "
                        f"{got.get('static')!r} — браузер не будет "
                        f"перепроверять, не устарело ли")
    done += 1
    if got.get("page") != "no-store":
        problems.append(f"страницы отдаются с Cache-Control {got.get('page')!r} — "
                        f"из кэша покажется устаревшее положение тумблеров")

    # ── чужой текст экранируется ──
    # ⚠️ Имя, заголовок статьи и текст вопроса приходят от людей. Символ «<»
    # в них не должен доезжать до страницы как разметка.
    hist.add_quiz_question("<b>статья</b>", "<script>alert(1)</script>",
                           ["<i>раз</i>", "два"], 0, "")
    html = pages.page_quiz(None, "подпись", mode="draft")
    expect("чужая разметка доехала до страницы", "<script>alert(1)" not in html)
    expect("экранированный текст не показан", "&lt;script&gt;" in html)
    for q in hist.list_quiz_questions(approved=False, limit=5):
        hist.delete_quiz_question(q["id"])

    # ── листание истории обновлений (этап 8, 01.09.2026) ──
    # ⚠️ Ради чего. До этого сайт показывал 15 последних правок и дальше пути
    # не было. Листалка, которая рисуется, но всегда показывает одно и то же —
    # ровно та поломка, которую глазами не отличить от рабочей.
    from services import update_log
    history_len = len(update_log.recent()) if update_log.available() else 0
    from handlers.admin.panel_updates import _PAGE_SIZE as _UPD_PAGE
    paging = "истории git нет — листание не проверено"
    if history_len > _UPD_PAGE:
        first = pages.page_system(None, "подпись", upd_page=0)
        second = pages.page_system(None, "подпись", upd_page=1)
        expect("на первой странице обновлений нет кнопки «Раньше»",
               "upd=1" in first)
        expect("на первой странице есть «Позже» — уходить некуда, она первая",
               "Позже" not in first)
        expect("со второй страницы нет возврата к свежим",
               "upd=0" in second and "Позже" in second)
        # Настоящее листание: наборы правок на страницах обязаны различаться.
        newest = update_log.recent()[0]
        from handlers.admin.panel_updates import _label
        expect("вторая страница показывает те же правки, что первая — "
               "листалка нарисована, но не листает",
               _label(newest) in first and _label(newest) not in second)
        # Номер страницы за пределом не роняет страницу и не отдаёт пустоту.
        far = pages.page_system(None, "подпись", upd_page=999)
        expect("номер страницы за пределом отдал пустой список",
               "история недоступна" not in far)
        paging = f"листание обновлений на {history_len} правках"

    return problems, (f"{done} проверок: викторина, база знаний, промпты, "
                      f"люди, обслуживание, экранирование, {paging}")


def check_journal_page():
    """
    Страница журналов показывает то, ради чего её открывают, и не показывает
    лишнего (этап 6, 01.09.2026).

    ⚠️ РАДИ ЧЕГО. Это первая страница сайта, где лежит ЧУЖАЯ ПЕРЕПИСКА —
    тексты сообщений, удалённых ботом. Две ошибки здесь тихие и обе дорогие:
    улики не показались (страница бесполезна, а выглядит рабочей) или имя
    участника доехало до страницы разметкой (символ «<» ломает вёрстку — ровно
    на этом уже наступали в панели бота).

    ⚠️ Отдельно проверяется, что улики НЕ показываются, пока их не открыли.
    Без этой половины проверку прошла бы страница, вываливающая переписку всех
    наказанных сразу.
    """
    from database import history as hist
    from web import pages

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    def wipe():
        with hist._lock:
            conn = hist._get_connection()
            conn.execute("DELETE FROM moderation_log")
            conn.execute("DELETE FROM mute_evidence")
            conn.execute("DELETE FROM staff_log")
            conn.commit()

    SECRET = "секретное слово из удалённого сообщения"
    try:
        wipe()

        # ── 1. Пустые журналы — это не поломка ──
        empty = pages.page_journal("подпись")
        expect("пустой журнал модерации не объясняет себя",
               "бот никого не наказывал" in empty)
        expect("пустой журнал персонала не объясняет себя",
               "персонал ничего не делал" in empty)

        # ── 2. Улики видно только после того, как их открыли ──
        mute_id = hist.log_moderation_action("mute", -100, 555, "Вася <хитрый>")
        hist.save_mute_evidence(mute_id, [{"text": SECRET, "has_photo": False},
                                          {"text": "", "has_photo": True}])
        link_id = hist.log_moderation_action("linkdel", -100, 556, "Петя")
        kick_id = hist.log_moderation_action("kick", -100, 557, "Спамер",
                                             admin_name="Максим")

        closed = pages.page_journal("подпись")
        expect("чужая переписка показана на странице, хотя улики не открывали",
               SECRET not in closed)
        opened = pages.page_journal("подпись", evidence=mute_id)
        expect("улики открыли, а текста удалённого сообщения на странице нет",
               SECRET in opened)
        expect("сообщение без текста показано пустой строкой вместо пометки",
               "фото/медиа" in opened)

        # ── 3. Кнопка улик — только там, где улики бывают ──
        expect("у мута нет кнопки улик", f"evidence={mute_id}" in closed)
        expect("у удалённой ссылки нет кнопки улик", f"evidence={link_id}" in closed)
        expect("у кика есть кнопка улик — улик у него не бывает, "
               "нажатие показало бы пустоту", f"evidence={kick_id}" not in closed)

        # ── 4. Имя участника — чужой текст ──
        expect("имя участника доехало до страницы разметкой",
               "Вася <хитрый>" not in closed)
        expect("имя участника не экранировано", "&lt;хитрый&gt;" in closed)

        # ── 5. Виды записей журнала и счётчик сводки не разъехались ──
        # ⚠️ Ради чего. 20.07.2026 завели новый вид мута и забыли вписать его
        # в счётчик: в списке действий он был, а в строке «за 7 дней» пропадал,
        # и панель занижала цифру. Теперь названия видов лежат одним словарём,
        # и проверка требует, чтобы счётчик знал ровно те же виды.
        from handlers.admin.panel_mod import MOD_ACTION_TITLES
        # ⚠️ ИЩЕМ ПО ВСЕМУ ПАКЕТУ database/, А НЕ В ОДНОМ ФАЙЛЕ (02.09.2026).
        # Раньше здесь был жёстко прописан путь database/history.py. Этот файл
        # режется на части, функция уехала в database/moderation.py — и
        # проверка честно покраснела «нашёл set()». Краснеть на переезде она не
        # должна: её предмет — СОДЕРЖИМОЕ функции, а не то, в каком файле та
        # лежит. Обход всего пакета переживёт и оставшиеся переезды.
        db_src = "\n".join(p.read_text(encoding="utf-8")
                           for p in sorted(pathlib.Path(ROOT, "database").glob("*.py")))
        counts_src = re.search(r"def get_moderation_counts(.|\n)*?return counts", db_src)
        counted = set(re.findall(r'r\["action"\] (?:==|in) \(?([^)\n:]+)',
                                 counts_src.group(0))) if counts_src else set()
        counted = {w.strip().strip('"').strip("'")
                   for chunk in counted for w in chunk.split(",") if w.strip()}
        expect(f"не разобрал, какие виды записей считает сводка (нашёл {counted})",
               len(counted) >= 5)
        for kind in sorted(set(MOD_ACTION_TITLES) - counted):
            problems.append(f"вид записи «{kind}» показывается в журнале, но "
                            f"сводка «за N дней» его не считает — цифра занижена")
            done += 1
        for kind in sorted(counted - set(MOD_ACTION_TITLES)):
            problems.append(f"вид записи «{kind}» считается сводкой, но названия "
                            f"у него нет — в журнале он будет «❔ {kind}»")
            done += 1

    finally:
        wipe()

    return problems, (f"{done} проверок: улики видно только открытыми, чужой "
                      f"текст экранирован, виды записей сведены со счётчиком")


def check_journal_clears():
    """
    Очистки журналов с сайта оставляют ТЕ ЖЕ следы, что кнопки бота, и не
    срабатывают с первого нажатия (этап 6, 01.09.2026).

    ⚠️ РАДИ ЧЕГО. Обе очистки необратимы, и обе тихие: сработавшая без
    подтверждения выглядит как «страница перезагрузилась».

    ⚠️ И ещё одно, менее очевидное. Очистка журнала МОДЕРАЦИИ обязана
    оставить надзорную запись «кто стёр улики» — тем же кодом `modlog_clear`,
    что у кнопки бота. А очистка журнала ПЕРСОНАЛА обязана НЕ писать ничего:
    запись легла бы в только что стёртый журнал и осталась бы там
    единственной строкой. Разойдись это с ботом — один и тот же поступок
    оставлял бы разные следы в зависимости от места нажатия.

    Ветка зовётся целиком, с поддельным запросом: проверяем поведение, а не
    текст исходника. Вход подменён намеренно — его проверяет check_web_auth.
    """
    import asyncio

    from database import history as hist
    from web import auth, routes

    problems = []
    done = 0
    OWNER = 4242

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    class _Request:
        """Поддельный запрос: ровно то, что читает ветка журналов."""
        def __init__(self, method="GET", form=None, query=None):
            self.method = method
            self.cookies = {}
            self.query = query or {}
            self._form = form or {}

        async def post(self):
            return self._form

    saved_ok, saved_user = auth.csrf_ok, auth.current_user
    auth.csrf_ok = lambda request, given: True
    auth.current_user = lambda request: OWNER

    def wipe():
        with hist._lock:
            conn = hist._get_connection()
            conn.execute("DELETE FROM moderation_log")
            conn.execute("DELETE FROM mute_evidence")
            conn.execute("DELETE FROM staff_log")
            conn.commit()

    def fill():
        mid = hist.log_moderation_action("mute", -100, 555, "Вася")
        hist.save_mute_evidence(mid, [{"text": "улика", "has_photo": False}])
        hist.log_moderation_action("kick", -100, 556, "Петя", admin_name="Максим")
        hist.log_staff_action(OWNER, "Максим", "quiz_seed", 0, "проверка")
        return mid

    def post(do, confirm=False):
        form = {"csrf": "x", "do": do}
        if confirm:
            form["confirm"] = "1"
        return asyncio.run(routes.journal(_Request("POST", form)))

    try:
        # ── 1. Первое нажатие только спрашивает ──
        wipe()
        mid = fill()
        answer = post("modclear")
        expect("после первого нажатия не показан вопрос «Да, выполнить»",
               "Да, выполнить" in answer.text)
        expect("журнал модерации стёрся с ПЕРВОГО нажатия, без подтверждения",
               len(hist.get_recent_moderation_actions(10)) == 2)
        expect("улики стёрлись с первого нажатия",
               len(hist.get_mute_evidence(mid)) == 1)

        answer = post("staffclear")
        expect("журнал персонала стёрся с ПЕРВОГО нажатия, без подтверждения",
               len(hist.get_recent_staff_actions(10)) == 1)

        # ── 2. Очистка модерации: стирает и ОСТАВЛЯЕТ надзорный след ──
        before = len(hist.get_recent_staff_actions(50))
        post("modclear", confirm=True)
        expect("журнал модерации не стёрся после подтверждения",
               not hist.get_recent_moderation_actions(10))
        expect("улики пережили очистку журнала", not hist.get_mute_evidence(mid))
        staff = hist.get_recent_staff_actions(50)
        expect(f"очистка журнала модерации не оставила следа в журнале персонала "
               f"(было {before}, стало {len(staff)})", len(staff) == before + 1)
        expect(f"след очистки записан кодом «{staff[0]['action'] if staff else '—'}», "
               f"а кнопка бота пишет «modlog_clear» — журнал назовёт одно "
               f"действие двумя именами",
               bool(staff) and staff[0]["action"] == "modlog_clear")
        expect("в следе очистки не сказано, сколько записей стёрли",
               bool(staff) and any(ch.isdigit() for ch in (staff[0].get("details") or "")))

        # ── 3. Очистка персонала: стирает и НЕ пишет о себе ──
        post("staffclear", confirm=True)
        expect("журнал персонала не стёрся после подтверждения",
               not hist.get_recent_staff_actions(10))

    finally:
        auth.csrf_ok, auth.current_user = saved_ok, saved_user
        wipe()

    return problems, (f"{done} проверок: первое нажатие спрашивает, очистка "
                      f"модерации оставляет след, очистка персонала — нет")


def check_prompts_extras():
    """
    Личный тумблер промпта и экран участия на странице промптов (этап 7).

    ⚠️ РАДИ ЧЕГО ТУМБЛЕР. Настройка `admin_no_prompt_<id>` хранится НАОБОРОТ:
    "1" означает «промпт ВЫКЛЮЧЕН». Тумблер показывает состояние промпта, то
    есть перевёрнутое значение. Забыть про переворот — значит нарисовать
    кнопку, врущую в обе стороны сразу, и заметить это можно только по
    поведению бота в личке, то есть очень нескоро.

    ⚠️ И второе: настройка ЛИЧНАЯ, ключ несёт id админа. Общая на всех
    отключила бы промпт сразу всем, кто входит на сайт.

    ⚠️ РАДИ ЧЕГО УЧАСТИЕ. Половина этого экрана считается в памяти и
    обнуляется перезапуском. Без предупреждения об этом цифры выглядят
    противоречиво («за неделю 300 проверок, отсеяно 12») — и на экране бота
    предупреждение есть.
    """
    from database import history as hist
    from web import actions, pages

    problems = []
    done = 0
    ME, OTHER = 4242, 4343

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    try:
        # ── 1. Состояние тумблера против перевёрнутого хранения ──
        hist.delete_setting(f"admin_no_prompt_{ME}")
        page = pages.page_prompts("подпись", viewer_id=ME)
        expect("без настройки промпт обязан считаться включённым",
               "применяется" in page and "не применяется" not in page)

        hist.set_setting(f"admin_no_prompt_{ME}", "1")
        page = pages.page_prompts("подпись", viewer_id=ME)
        expect('в настройке "1" — промпт ВЫКЛЮЧЕН, а тумблер показывает '
               'обратное', "не применяется" in page)

        # ── 2. Нажатие переключает, и в обе стороны ──
        now_on = actions.toggle_personal_prompt(ME)
        expect(f"нажатие вернуло {now_on}, а промпт был выключен — ждали True",
               now_on is True)
        expect('после включения в настройке должно лежать "0"',
               hist.get_setting(f"admin_no_prompt_{ME}", "0") == "0")
        expect("второе нажатие не выключило промпт обратно",
               actions.toggle_personal_prompt(ME) is False)

        # ── 3. Тумблер ЛИЧНЫЙ ──
        hist.delete_setting(f"admin_no_prompt_{OTHER}")
        actions.toggle_personal_prompt(ME)
        expect("переключение у одного админа задело настройку другого",
               hist.get_setting(f"admin_no_prompt_{OTHER}", "0") == "0")
        expect("страница другого админа показывает чужое состояние",
               "не применяется" not in pages.page_prompts("подпись",
                                                          viewer_id=OTHER))

        # ── 4. Участие: цифры из журнала доезжают до страницы ──
        for _ in range(3):
            hist.log_proactive_check(-100999, "reply", "модель", 1.5, 10, "text")
        hist.log_proactive_check(-100999, "silent", "модель", 0.5, 0, "photo")
        page = pages.page_prompts("подпись", viewer_id=ME)
        expect("на странице нет раздела участия в разговоре",
               "Участие в разговоре" in page)
        expect("проверки из журнала не доехали до страницы (ждали 4)",
               ">4<" in page)
        expect("исход «промолчал» не показан, хотя он есть в журнале",
               "промолчал" in page)
        expect("не сказано, что отсев живёт в памяти и обнуляется "
               "перезапуском — цифры выглядели бы противоречиво",
               "обнуляются перезапуском" in page)

    finally:
        for uid in (ME, OTHER):
            hist.delete_setting(f"admin_no_prompt_{uid}")
        with hist._lock:
            conn = hist._get_connection()
            conn.execute("DELETE FROM proactive_log WHERE chat_id = ?", (-100999,))
            conn.commit()

    return problems, (f"{done} проверок: перевёрнутое хранение тумблера, "
                      f"он личный, цифры участия и оговорка про память")


def check_web_wiring():
    """
    Сайт не врёт цифрами и не теряет кнопки.

    ⚠️ Ради чего проверка существует. Разбор ошибок 30.08.2026 нашёл ТРИ
    промаха одного сорта, и ни один не падал:
      • «Снять и скачать» отдавало САМУЮ СТАРУЮ копию базы: список копий идёт
        от старых к свежим, а код брал первую;
      • сборка вопросов всегда рапортовала «добавлено 0» — читался ключ
        «added», а возвращается «saved»;
      • обработчик очистки журнала базы знаний был написан, а кнопки к нему
        не было — ветка висела недостижимой.
    Всё это — «работает, но неправда». Такое ловится только сверкой с
    источником, а не чтением кода.
    """
    import re as _re

    from web import actions, pages, routes

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    # ── 1. Порядок копий базы: берём последнюю, потому что список от старых ──
    from services import backup
    doc = (backup.list_backups.__doc__ or "")
    expect("докстринг list_backups перестал говорить о порядке — "
           "проверьте, с какого конца брать свежую копию",
           "от старых к свежим" in doc)
    src = _re.search(r"else:\s*\n\s*from services import backup(.|\n)*?_read_file_bytes",
                     pathlib.Path(ROOT, "web", "routes.py").read_text(encoding="utf-8"))
    expect("скачивание копии базы берёт не последнюю (самую свежую) запись",
           bool(src) and "copies[-1]" in src.group(0))

    # ── 2. Ключи, которые сайт читает у сборки вопросов, реально возвращаются ──
    quiz_src = pathlib.Path(ROOT, "services", "quiz_bank.py").read_text(encoding="utf-8")
    run_over = _re.search(r"def _run_over(.|\n)*?return \{([^}]*)\}", quiz_src)
    returned = set(_re.findall(r'"([a-z_]+)"', run_over.group(2))) if run_over else set()
    expect(f"не разобрал, что возвращает _run_over (нашёл {returned})",
           {"articles", "saved", "failed"} <= returned)
    act_src = pathlib.Path(ROOT, "web", "actions.py").read_text(encoding="utf-8")
    describe = _re.search(r"def quiz_generate(.|\n)*?return longjobs", act_src)
    used = set(_re.findall(r"result\.get\('([a-z_]+)'", describe.group(0))) if describe else set()
    expect(f"сайт читает у сборки вопросов ключи {sorted(used)}, "
           f"а возвращаются {sorted(returned)}",
           used and used <= returned)

    # ── 3. Каждая форма страницы имеет обработчик, и наоборот ──
    # ⚠️ СМОТРИМ НА НАРИСОВАННОЕ, А НЕ НА ИСХОДНИК. Первая версия этой
    # проверки искала литералы в тексте pages.py — и пропустила подлом, где
    # кнопка в исходнике осталась, а на страницу не попадала. Кнопка, которой
    # не видно, всё равно что её нет.
    import asyncio as _asyncio
    import database.history as _hist

    # Обстановка, при которой на страницах есть ВСЕ кнопки: черновик и
    # игровой вопрос, известная группа, участник.
    _hist.add_quiz_question("Проверка проводки", "Вопрос-черновик?",
                            ["раз", "два"], 0, "")
    draft = _hist.list_quiz_questions(approved=False, limit=1)
    if draft:
        _hist.set_quiz_question_approved(draft[0]["id"], True)
    _hist.add_quiz_question("Проверка проводки", "Второй вопрос-черновик?",
                            ["раз", "два"], 0, "")
    # ⚠️ Кнопка «♻️ Обновить из файла» рисуется, ТОЛЬКО когда банк отстал от
    # эталонного файла (2026-09-01). Чтобы она попала на страницу, кладём в
    # банк первый вопрос файла с нарочно испорченным разбором. Без этого
    # проверка «обработчик есть, а кнопки нет» краснела бы на исправном коде.
    from services import quiz_bank as _qb
    _seed_items = _qb._read_seed() or []
    for _raw in _seed_items:
        _c = _qb._clean_question(_raw) if isinstance(_raw, dict) else None
        if _c and _raw.get("article"):
            _hist.add_quiz_question(_raw["article"], _c["question"], _c["options"],
                                    _c["correct_idx"], "разбор нарочно отстал")
            break
    with _hist._lock:
        _conn = _hist._get_connection()
        _conn.execute("INSERT OR REPLACE INTO known_chats (chat_id, title, last_seen) "
                      "VALUES (?, ?, datetime('now'))", (-100999, "Проверочная группа"))
        _conn.commit()
    _hist.add_quiz_attempt(777000111, "проверка", True)
    _hist.note_quiz_failure("проверочная.md", "проверка проводки")

    # ⚠️ Статью заводим во ВРЕМЕННОЙ папке: настоящие статьи базы знаний —
    # единственное, чего нет ни в git, ни в базе, и трогать их проверкой нельзя.
    import shutil as _shutil
    import tempfile as _tempfile

    import services.knowledge_store as _ks
    from services import roles as _roles

    art_dir = _tempfile.mkdtemp(prefix="c4max-selftest-kb-")
    saved_folders = dict(_ks._FOLDERS)
    _ks._FOLDERS["pending"] = os.path.join(art_dir, "pending")
    _ks._FOLDERS["approved"] = os.path.join(art_dir, "approved")
    os.makedirs(_ks._FOLDERS["pending"], exist_ok=True)
    os.makedirs(_ks._FOLDERS["approved"], exist_ok=True)
    with open(os.path.join(_ks._FOLDERS["pending"], "проверка.md"), "w",
              encoding="utf-8") as f:
        f.write("# Проверка проводки" + os.linesep + "Текст." + os.linesep)

    _roles.make_moderator(777000111, 1)

    drawn = ""
    try:
        drawn += pages.page_kb(None, "подпись", section="pending",
                               open_article="pending/проверка.md")
        drawn += pages.page_quiz(None, "подпись", mode="draft")
        drawn += pages.page_quiz(None, "подпись", mode="live")
        drawn += pages.page_journal("подпись")
        # viewer_id обязателен: без него личный тумблер промпта не рисуется,
        # и проверка «кнопка ↔ обработчик» не увидела бы его пропажу.
        drawn += pages.page_prompts("подпись", viewer_id=777000111)
        drawn += pages.page_system(None, "подпись",
                                   digest_chat=-100999, digest_body="текст")
        drawn += _asyncio.run(pages.page_user_card(None, 777000111, "подпись"))
    finally:
        _roles.unmake_moderator(777000111)
        _ks._FOLDERS.update(saved_folders)
        _shutil.rmtree(art_dir, ignore_errors=True)
        _hist.clear_quiz_failures()
        for q in _hist.list_quiz_questions(approved=False, limit=20):
            _hist.delete_quiz_question(q["id"])
        for q in _hist.list_quiz_questions(approved=True, limit=20):
            _hist.delete_quiz_question(q["id"])

    routes_src = pathlib.Path(ROOT, "web", "routes.py").read_text(encoding="utf-8")
    sent = set(_re.findall(r'name="do" value="([a-z_]+)"', drawn))
    handled = set(_re.findall(r'do == "([a-z_]+)"', routes_src)) | {"ok", "del"}
    done += 2
    for extra in sorted(sent - handled):
        problems.append(f"страница рисует кнопку «{extra}», а обработчика нет")
    for orphan in sorted(handled - sent):
        problems.append(f"обработчик «{orphan}» есть, а кнопки к нему "
                        f"на страницах не рисуется")

    # ── 4. Разметка Telegram не доезжает до страницы обычным текстом ──
    expect("значение «не задан» показывается вместе с тегами",
           pages.plain("<i>не задан</i>") == "не задан")
    expect("экранированные символы остаются в тексте отчёта",
           pages.plain("цена &lt;0.01") == "цена <0.01")

    # ── 5. Дайджест уходит В ГРУППУ только тем текстом, который показали ──
    # ⚠️ Решение из кнопки бота: неделя скользящая, пересчёт в момент отправки
    # дал бы другие цифры. Пустой текст обязан быть отказом, а не пересчётом.
    import asyncio

    class _FakeApp:
        class bot:
            @staticmethod
            async def send_message(**kw):
                raise AssertionError("отправка не должна была случиться")

    try:
        asyncio.run(actions.digest_send(1, -100, "   ", _FakeApp))
        problems.append("дайджест ушёл в группу с пустым текстом")
    except actions.ActionError:
        done += 1
    except AssertionError as e:
        problems.append(str(e))

    return problems, (f"{done} проверок: порядок копий, ключи сборки вопросов, "
                      f"формы ↔ обработчики, разметка, дайджест")


def check_login_link_message():
    """
    Сообщение со ссылкой входа само исчезает — и ровно тогда, когда ссылка
    перестаёт работать (просьба Максима 30.08.2026).

    ⚠️ Ради чего проверка существует. Тут ОДИН срок работает в трёх местах:
    подпись самой ссылки, надпись «работает N минут» и время самоудаления.
    Разойдись они — в переписке остался бы висеть мёртвый ключ от админки,
    либо надпись обещала бы срок, которого нет. Зашитое число такое расхождение
    даёт молча, поэтому проверка МЕНЯЕТ срок и требует, чтобы за ним поехало
    и то, и другое.

    Ветка живёт в handlers/admin/router.py и вызывается целиком, с поддельными
    Telegram-объектами: проверяем поведение, а не текст исходника.
    """
    import asyncio

    from services import roles
    from web import auth as web_auth

    problems = []
    done = 0
    OWNER = 4242

    sent = []          # что бот отправил
    deletes = []       # что поставлено на самоудаление

    class _Msg:
        message_id = 777
        chat_id = OWNER

    class _Bot:
        @staticmethod
        async def send_message(**kw):
            sent.append(kw)
            return _Msg()

    class _Query:
        data = "web:link"
        message = _Msg()

        class from_user:
            id = OWNER

        @staticmethod
        async def answer(*a, **kw):
            return None

    class _Update:
        callback_query = _Query()

    class _Ctx:
        bot = _Bot()
        user_data = {}

    import config as cfg
    import handlers.admin.router as router

    saved = (cfg.WEB_PUBLIC_URL, web_auth.TELEGRAM_TOKEN,
             web_auth.LOGIN_LINK_TTL_SEC, roles.ADMIN_IDS,
             dict(roles._cache), roles._loaded, router.schedule_delete)
    try:
        cfg.WEB_PUBLIC_URL = "https://проверка.example"
        web_auth.TELEGRAM_TOKEN = "123456789:AAEeTestTokenForSelfTestOnly"
        roles.ADMIN_IDS = (OWNER,)
        roles._cache.clear()
        roles._loaded = True
        router.schedule_delete = lambda bot, chat, mid, delay: deletes.append(
            (chat, mid, delay))

        # ⚠️ Срок берём НЕ ТОТ, что стоит в коде: с настоящими пятью минутами
        # зашитая «5 минут» прошла бы проверку насквозь.
        web_auth.LOGIN_LINK_TTL_SEC = 600

        asyncio.run(router.handle_callback_query(_Update(), _Ctx()))

        done += 1
        if len(sent) != 1:
            problems.append(f"ссылка входа не отправлена (сообщений: {len(sent)})")
            return problems, ""

        text = sent[0].get("text", "")
        done += 1
        if "Открыть в браузере" not in text:
            problems.append("в сообщении нет самой ссылки")
        done += 1
        if "10 минут" not in text:
            problems.append(f"надпись не поехала за сроком: ждали «10 минут», "
                            f"в тексте {text[-90:]!r}")

        done += 1
        if not deletes:
            problems.append("сообщение со ссылкой НЕ поставлено на самоудаление — "
                            "мёртвый ключ от админки останется висеть в переписке")
        else:
            chat, mid, delay = deletes[0]
            done += 3
            if chat != OWNER:
                problems.append(f"удаление назначено не в тот чат: {chat}")
            if mid != _Msg.message_id:
                problems.append(f"удаление назначено не тому сообщению: {mid}")
            if delay != 600:
                problems.append(f"срок самоудаления {delay} не совпал со сроком "
                                f"жизни ссылки 600")
    finally:
        (cfg.WEB_PUBLIC_URL, web_auth.TELEGRAM_TOKEN,
         web_auth.LOGIN_LINK_TTL_SEC, roles.ADMIN_IDS,
         cache, roles._loaded, router.schedule_delete) = saved
        roles._cache.clear()
        roles._cache.update(cache)

    return problems, (f"{done} проверок: ссылка отправлена, надпись и "
                      f"самоудаление едут за сроком её жизни")


def check_web_auth():
    """
    Вход в веб-админку: пускает ли она того, кого надо, и, ГЛАВНОЕ, отшивает
    ли всех остальных.

    ⚠️ Ради чего проверка существует. У кнопок бота от чужого нажатия
    страхует запрет по умолчанию в services/roles.py, а сайт стоит в
    интернете, и единственное, что отделяет админку от прохожего, — совпала
    подпись или нет. Ошибиться тут можно тихо: перепутанный ключ подписи или
    забытая проверка срока не мешают ВЛАДЕЛЬЦУ войти, поэтому руками такое
    не замечается вовсе.

    Отдельно проверяются два ключа. Telegram считает подпись по-разному для
    мини-приложения (ключ выведен из слова WebAppData) и для входа из
    браузера (ключ — просто хэш токена). Подпись, посчитанная не тем ключом,
    обязана быть отвергнута — иначе одна дверь открывалась бы ключом от другой.
    """
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    from web import auth

    problems = []
    done = 0

    TOKEN = "123456789:AAEeTestTokenForSelfTestOnly"
    OWNER, STRANGER = 111, 222

    saved_token, saved_admins = auth.TELEGRAM_TOKEN, auth.ADMIN_IDS
    auth.TELEGRAM_TOKEN, auth.ADMIN_IDS = TOKEN, (OWNER,)

    def expect(title, got, want):
        nonlocal done
        done += 1
        if got != want:
            problems.append(f"{title}: получилось {got!r}, ожидалось {want!r}")

    def sign(pairs: dict, secret: bytes) -> dict:
        """Подписывает набор полей так же, как это делает Telegram."""
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        out = dict(pairs)
        out["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return out

    webapp_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    widget_key = hashlib.sha256(TOKEN.encode()).digest()

    try:
        now = int(time.time())

        # ─── мини-приложение (кнопка «🌐 Админка» в боте) ───
        good = sign({"auth_date": str(now),
                     "user": '{"id":%d,"first_name":"O"}' % OWNER}, webapp_key)
        expect("своя подпись мини-приложения",
               auth.check_webapp(urlencode(good)), OWNER)

        bad = dict(good)
        bad["user"] = '{"id":%d,"first_name":"X"}' % STRANGER
        expect("подменённые данные при той же подписи",
               auth.check_webapp(urlencode(bad)), None)

        # Тот же набор, подписанный ключом ДРУГОЙ двери.
        wrong_key = sign({"auth_date": str(now),
                          "user": '{"id":%d,"first_name":"O"}' % OWNER}, widget_key)
        expect("подпись мини-приложения не тем ключом",
               auth.check_webapp(urlencode(wrong_key)), None)

        stale = sign({"auth_date": str(now - auth.WEB_AUTH_MAX_AGE_SEC - 60),
                      "user": '{"id":%d,"first_name":"O"}' % OWNER}, webapp_key)
        expect("просроченная подпись мини-приложения",
               auth.check_webapp(urlencode(stale)), None)

        expect("пустые данные мини-приложения", auth.check_webapp(""), None)

        # ─── вход из браузера (второй ключ) ───
        w_good = sign({"id": str(OWNER), "auth_date": str(now),
                       "first_name": "O"}, widget_key)
        expect("своя подпись входа из браузера", auth.check_widget(w_good), OWNER)

        w_wrong = sign({"id": str(OWNER), "auth_date": str(now),
                        "first_name": "O"}, webapp_key)
        expect("подпись входа из браузера не тем ключом",
               auth.check_widget(w_wrong), None)

        # ─── кто вообще имеет право войти ───
        expect("владелец допущен", auth.is_allowed(OWNER), True)
        expect("посторонний с ВЕРНОЙ подписью не допущен",
               auth.is_allowed(STRANGER), False)
        expect("никто не допущен", auth.is_allowed(None), False)

        # ─── наша кука со входом ───
        cookie = auth.make_session(OWNER)
        expect("своя кука читается", auth.read_session(cookie), OWNER)
        expect("кука с подменённым id",
               auth.read_session(cookie.replace(str(OWNER), str(STRANGER), 1)), None)
        # ⚠️ Портим последний знак ЗАВЕДОМО ДРУГИМ. Прежняя запись подставляла
        # «0» вслепую, и раз в шестнадцать прогонов подпись оставалась целой —
        # проверка мигала. Срок в куке меняется каждый прогон, поэтому такое
        # ловится не сразу и выглядит как «само прошло».
        expect("кука с испорченной подписью",
               auth.read_session(_flip_last(cookie)), None)
        expect("мусор вместо куки", auth.read_session("что-то не то"), None)
        expect("пустая кука", auth.read_session(None), None)

        # Просроченная кука: собираем руками, срок в прошлом.
        body = f"{OWNER}.{int(time.time()) - 10}"
        old = body + "." + hmac.new(TOKEN.encode(), body.encode(),
                                    hashlib.sha256).hexdigest()
        expect("просроченная кука", auth.read_session(old), None)

        # ─── одноразовая ссылка «открыть в браузере» ───
        token = auth.make_login_token(OWNER)
        expect("своя ссылка входа читается", auth.read_login_token(token), OWNER)
        expect("ссылка с испорченной подписью",
               auth.read_login_token(_flip_last(token)), None)

        body = f"{OWNER}.{int(time.time()) - 10}"
        old_link = body + "." + hmac.new(TOKEN.encode(), f"login:{body}".encode(),
                                         hashlib.sha256).hexdigest()
        expect("просроченная ссылка входа", auth.read_login_token(old_link), None)

        # ⚠️ Ссылка и кука подписаны РАЗНЫМИ приставками намеренно: иначе
        # пятиминутная ссылка работала бы как недельная кука и наоборот.
        expect("кука не годится вместо ссылки", auth.read_login_token(cookie), None)
        expect("ссылка не годится вместо куки", auth.read_session(token), None)

        # ─── подпись с чужими буквами не роняет обработчик ───
        # ⚠️ ЗАЧЕМ ЭТО ЗДЕСЬ. hmac.compare_digest на СТРОКАХ требует латиницы
        # и бросает TypeError на всём остальном. Свои подписи всегда латиницей,
        # а чужая приходит какая угодно — и присланная кириллицей роняла
        # обработчик пятисотой ошибкой вместо честного отказа (поймано живой
        # проверкой 30.08.2026). Теперь сравнение идёт по байтам.
        expect("кириллица вместо подписи куки", auth.read_session("1.2.мусор"), None)
        expect("кириллица вместо подписи ссылки", auth.read_login_token("1.2.мусор"), None)
        cyr = dict(good)
        cyr["hash"] = "подделка"
        expect("кириллица вместо подписи мини-приложения",
               auth.check_webapp(urlencode(cyr)), None)
        expect("кириллица вместо подписи формы",
               auth._same(auth.csrf_for(cookie), "подделка"), False)
        expect("верная подпись формы принимается",
               auth._same(auth.csrf_for(cookie), auth.csrf_for(cookie)), True)
        expect("подпись формы от ДРУГОГО входа не годится",
               auth._same(auth.csrf_for(cookie), auth.csrf_for(cookie + "x")), False)

        # ─── токена нет вовсе (бот без .env) ───
        auth.TELEGRAM_TOKEN = ""
        expect("без токена мини-приложение не пускает",
               auth.check_webapp(urlencode(good)), None)
        expect("без токена браузер не пускает", auth.check_widget(w_good), None)
        expect("без токена кука не читается", auth.read_session(cookie), None)
    finally:
        auth.TELEGRAM_TOKEN, auth.ADMIN_IDS = saved_token, saved_admins

    return problems, (f"{done} проверок: две схемы подписи, срок, подмена id, "
                      f"кука, ссылка, подпись формы, чужие буквы")


def check_update_notice():
    """
    Уведомление «⬇️ Обновился сам…» само уходит из лички через свой срок
    (03.09.2026, просьба Максима: «пусть удаляется через 10 минут»).

    ⚠️ Ради чего проверка существует. Уведомление отправляется ПРЯМО ПЕРЕД
    перезапуском, поэтому обычный отложенный удалитель (utils.schedule_delete)
    для него не годится — задача умерла бы вместе со старым процессом. Срок
    считает УЖЕ ДРУГОЙ процесс по времени, записанному в след. Значит ошибиться
    можно двумя тихими способами: забыть время при записи следа (уведомление
    повиснет навсегда) или зашить срок числом мимо config (он разъедется с
    тем, что обещано человеку). Проверка закрывает оба.

    Всё считается без Телеграма и без сети: бот подделан, база временная.
    """
    import asyncio
    import time
    import config as cfg
    from jobs import update as upd

    problems = []
    done = 0

    def expect(title, got, want):
        nonlocal done
        done += 1
        if got != want:
            problems.append(f"{title}: ожидалось {want!r}, вышло {got!r}")

    # ── 1. Расчёт срока ──
    now = 1_000_000.0
    ttl = cfg.UPDATE_NOTICE_TTL_SEC
    expect("свежее не трогаем", upd.notice_expired(now - 1, now), False)
    expect("за секунду до срока живо", upd.notice_expired(now - ttl + 1, now), False)
    expect("ровно на сроке — убираем", upd.notice_expired(now - ttl, now), True)
    expect("давно отвисело — убираем", upd.notice_expired(now - ttl * 10, now), True)

    # ⚠️ Время НЕИЗВЕСТНО (старый формат следа) — по сроку не трогаем никогда:
    # гадать о возрасте чужого сообщения нельзя.
    expect("время неизвестно (ноль) — не трогаем", upd.notice_expired(0, now), False)
    expect("время неизвестно (None) — не трогаем", upd.notice_expired(None, now), False)

    # ── 1б. От какого момента считать возраст ──
    # ⚠️ Ради чего. Сначала правило было «времени нет — не трогаем», и оно
    # выглядело осторожным. На живом боте 03.09.2026 вышло наоборот: ПЕРВОЕ
    # уведомление после перехода на новый код зависло навсегда — отправлял его
    # ещё старый код, который время писать не умел. Теперь такой след считается
    # от ЗАПУСКА бота, но только если помечен текущей сборкой.
    START = now - 3600
    expect("время записано — берём его",
           upd.notice_since("abc", 777.0, "abc", START), 777.0)
    expect("времени нет, метка СВОЯ — считаем от запуска",
           upd.notice_since("abc", 0, "abc", START), START)
    expect("времени нет, метка ЧУЖАЯ — не трогаем",
           upd.notice_since("old", 0, "abc", START), 0.0)
    expect("времени нет, метки нет вовсе — не трогаем",
           upd.notice_since("", 0, "abc", START), 0.0)
    expect("времени нет, а текущей метки не знаем — не трогаем",
           upd.notice_since("abc", 0, "", START), 0.0)

    # И то же самое сквозь расчёт срока: след без времени со своей меткой,
    # бот работает дольше срока → пора убирать.
    expect("свой след без времени, бот давно работает — убираем",
           upd.notice_expired(upd.notice_since("abc", 0, "abc", now - ttl - 5), now), True)
    expect("свой след без времени, бот только поднялся — ещё живо",
           upd.notice_expired(upd.notice_since("abc", 0, "abc", now - 5), now), False)
    expect("чужой след без времени не убираем даже через сутки",
           upd.notice_expired(upd.notice_since("old", 0, "abc", now - 86400), now), False)

    # ── 2. Срок берётся ИЗ CONFIG, а не зашит числом ──
    # Тот же приём, что у ссылки входа: двигаем константу и требуем, чтобы
    # поведение поехало за ней. Зашитые 600 эту проверку не прошли бы.
    saved_ttl = cfg.UPDATE_NOTICE_TTL_SEC
    try:
        cfg.UPDATE_NOTICE_TTL_SEC = 60
        expect("срок укоротили — старое отвисело", upd.notice_expired(now - 120, now), True)
        cfg.UPDATE_NOTICE_TTL_SEC = 100_000
        expect("срок удлинили — то же самое ещё живо",
               upd.notice_expired(now - 120, now), False)
    finally:
        cfg.UPDATE_NOTICE_TTL_SEC = saved_ttl

    # ── 3. След: запись и чтение, все три формата ──
    from database.history import set_setting
    from config import UPDATE_NOTICE_MSGS_KEY

    upd._save_notice("abc123", [[42, 777]], 555.0)
    build, msgs, sent_at = upd._load_notice()
    done += 1
    if (build, msgs, sent_at) != ("abc123", [[42, 777]], 555.0):
        problems.append(f"след не пережил запись-чтение: {(build, msgs, sent_at)!r}")

    # Старый формат 2026-08-05 (без времени) — читается, время нулевое.
    set_setting(UPDATE_NOTICE_MSGS_KEY, '{"build": "old", "msgs": [[1, 2]]}')
    expect("формат без времени: время нулевое", upd._load_notice()[2], 0.0)
    expect("формат без времени: сообщения на месте", upd._load_notice()[1], [[1, 2]])

    # Самый старый формат (голый список пар) — тоже читается.
    set_setting(UPDATE_NOTICE_MSGS_KEY, '[[3, 4]]')
    expect("древний формат: сообщения на месте", upd._load_notice()[1], [[3, 4]])
    expect("древний формат: время нулевое", upd._load_notice()[2], 0.0)

    # Мусор в базе не роняет разбор.
    set_setting(UPDATE_NOTICE_MSGS_KEY, "не json вовсе")
    expect("мусор в следе не роняет", upd._load_notice(), ("", [], 0.0))

    # ── 4. Само удаление: что убрали и что осталось в следе ──
    deleted = []

    class _Bot:
        @staticmethod
        async def delete_message(chat_id, message_id):
            deleted.append((chat_id, message_id))

    class _App:
        bot = _Bot()

    # Отвисевшее — убираем и след снимаем.
    upd._save_notice("abc123", [[42, 777], [43, 778]], time.time() - ttl - 5)
    asyncio.run(upd.drop_expired_notice(_App()))
    expect("отвисевшее удалено", deleted, [(42, 777), (43, 778)])
    expect("след снят", upd._load_notice()[1], [])

    # Свежее — не трогаем.
    deleted.clear()
    upd._save_notice("abc123", [[42, 999]], time.time())
    asyncio.run(upd.drop_expired_notice(_App()))
    expect("свежее не удалено", deleted, [])
    expect("след свежего на месте", upd._load_notice()[1], [[42, 999]])

    # Времени нет, метка ЧУЖАЯ — не трогаем (снесли бы чужое сообщение вслепую).
    deleted.clear()
    saved_build, saved_start = cfg.BOT_BUILD, upd._STARTED_AT
    try:
        cfg.BOT_BUILD = "текущая"
        set_setting(UPDATE_NOTICE_MSGS_KEY, '{"build": "чужая", "msgs": [[5, 6]]}')
        asyncio.run(upd.drop_expired_notice(_App()))
        expect("чужой след без времени не удаляем", deleted, [])

        # Времени нет, но метка СВОЯ и бот работает дольше срока — убираем.
        # Это и есть случай, из-за которого 03.09.2026 первое уведомление после
        # перехода на новый код зависло навсегда.
        deleted.clear()
        upd._STARTED_AT = time.time() - ttl - 5
        set_setting(UPDATE_NOTICE_MSGS_KEY, '{"build": "текущая", "msgs": [[7, 8]]}')
        asyncio.run(upd.drop_expired_notice(_App()))
        expect("свой след без времени, бот давно работает — удалено", deleted, [(7, 8)])
        expect("след при этом снят", upd._load_notice()[1], [])

        # Тот же след, но бот только что поднялся — рано.
        deleted.clear()
        upd._STARTED_AT = time.time()
        set_setting(UPDATE_NOTICE_MSGS_KEY, '{"build": "текущая", "msgs": [[9, 10]]}')
        asyncio.run(upd.drop_expired_notice(_App()))
        expect("свой след без времени, бот только поднялся — не трогаем", deleted, [])
    finally:
        cfg.BOT_BUILD, upd._STARTED_AT = saved_build, saved_start

    # Следа нет — не падаем и в Телеграм не ходим.
    deleted.clear()
    upd._save_notice("abc123", [])
    asyncio.run(upd.drop_expired_notice(_App()))
    expect("пустой след не роняет", deleted, [])

    # ── 5. Отправка кладёт в след ВРЕМЯ ──
    # Забыть его — самая тихая из возможных ошибок: уведомление повиснет
    # навсегда, и никто этого не заметит. Поэтому смотрим сам вызов в коде.
    src = pathlib.Path(ROOT, "jobs", "update.py").read_text(encoding="utf-8")
    save_at_send = re.search(r"_save_notice\(read_build_mark\(\)[^)]*\)", src)
    done += 1
    if not save_at_send or "time.time()" not in save_at_send.group(0):
        problems.append("отправка уведомления не кладёт в след время — "
                        "сообщение повиснет навсегда")

    return problems, (f"{done} проверок: расчёт срока и точки отсчёта, срок из "
                      f"config, три формата следа, удаление и его отсутствие")


CHECKS = (
    ("деньги — расчёт стоимости запросов", check_money),
    ("деньги — сам прайс не менялся", check_price_list),
    ("пометка мута — разбор ответа модели", check_mute_tag),
    ("права доступа — кнопки и иерархия", check_permissions),
    ("размышления модели не утекают в чат", check_thoughts),
    ("длинные ответы — разметка не разъезжается", check_long_answers),
    ("потолки ожидания — перебор моделей", check_wait_budgets),
    ("антиспам — альбом не считается флудом", check_album_not_flood),
    ("копилка альбома — все кадры уходят модели", check_album_collect),
    ("фильтр ссылок — белый список и мут за повторы", check_link_filter),
    ("приветствие новичков и проверка «я не бот»", check_greeter),
    ("разбор статей и вопросов викторины", check_parsing),
    ("отчёт — ни один провайдер не теряется", check_report_render),
    ("рассылка новостей — текст не пропадает", check_news_send),
    ("метки суток, сроков и недель", check_time_keys),
    ("шаг цикла расписания — начало часа", check_schedule_step),
    ("база знаний — пик против полки", check_rag_pick),
    ("звания викторины — лестница без дыр", check_quiz_ranks),
    ("викторина — файл вопросов против банка", check_quiz_seed_sync),
    ("суточный отчёт — расход за период", check_daily_report),
    ("единый список настроек против читалок бота", check_settings_spec),
    ("список промптов против панели бота", check_prompts_spec),
    ("журнал персонала знает все коды действий", check_audit_codes),
    ("страницы сайта показывают то, что нужно", check_web_pages),
    ("страница журналов: улики и чужой текст", check_journal_page),
    ("очистки журналов оставляют верные следы", check_journal_clears),
    ("личный тумблер промпта и цифры участия", check_prompts_extras),
    ("сайт: цифры и кнопки не разъехались с источником", check_web_wiring),
    ("ссылка входа исчезает вместе со своим сроком", check_login_link_message),
    ("вход в веб-админку — подпись и срок", check_web_auth),
    ("уведомление об обновлении уходит по сроку", check_update_notice),
)


def main() -> int:
    sys.path.insert(0, ROOT)

    # ⚠️ ПЕРВЫМ ДЕЛОМ уводим базу во временную папку — как в preflight.py.
    # Сейчас ни одна проверка в базу не ходит, но следующая может, и лучше
    # пусть она с самого начала пишет в пустышку, а не в боевую history.db.
    # ⚠️ ПОДМЕНЯЕМ config.DB_PATH, А НЕ history.DB_PATH (02.09.2026) — по той же
    # причине, что и в preflight.py: соединение открывает database/_core.py и
    # спрашивает путь у config в момент открытия. Со старой строкой проверки
    # писали бы В БОЕВУЮ history.db, и заметить это было бы нечем.
    tmp_dir = tempfile.mkdtemp(prefix="c4max-selftest-")
    import config
    config.DB_PATH = os.path.join(tmp_dir, "selftest.db")
    from database import history as hist
    # Схему создаём сразу: проверки потолков зовут настоящие ask_gemini_*,
    # а те по дороге читают историю переписки и настройки. Без таблиц они
    # падают на «no such table», и проверка краснеет не по делу.
    hist.init_db()

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
        import shutil
        hist.close_db()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("───────────────────────────")
    if failures:
        # Поломки повторяем в конце — deploy.sh кладёт в сообщение об откате
        # последние строки вывода (см. тот же приём в preflight.py).
        for problem in failures[:2]:
            print(f"❌ {problem}")
        print(f"НЕ ПРОШЛО: поломок {len(failures)} (подробности выше)")
        return 1
    print("ПОВЕДЕНИЕ В ПОРЯДКЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
