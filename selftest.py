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
#  9. КЛЮЧИ СУТОК, СРОКОВ И НЕДЕЛЬ
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
#  12. СУТОЧНЫЙ ОТЧЁТ: РАСХОД ЗА ПЕРИОД
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
        expect("кука с испорченной подписью", auth.read_session(cookie[:-1] + "0"), None)
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
               auth.read_login_token(token[:-1] + "0"), None)

        body = f"{OWNER}.{int(time.time()) - 10}"
        old_link = body + "." + hmac.new(TOKEN.encode(), f"login:{body}".encode(),
                                         hashlib.sha256).hexdigest()
        expect("просроченная ссылка входа", auth.read_login_token(old_link), None)

        # ⚠️ Ссылка и кука подписаны РАЗНЫМИ приставками намеренно: иначе
        # пятиминутная ссылка работала бы как недельная кука и наоборот.
        expect("кука не годится вместо ссылки", auth.read_login_token(cookie), None)
        expect("ссылка не годится вместо куки", auth.read_session(token), None)

        # ─── токена нет вовсе (бот без .env) ───
        auth.TELEGRAM_TOKEN = ""
        expect("без токена мини-приложение не пускает",
               auth.check_webapp(urlencode(good)), None)
        expect("без токена браузер не пускает", auth.check_widget(w_good), None)
        expect("без токена кука не читается", auth.read_session(cookie), None)
    finally:
        auth.TELEGRAM_TOKEN, auth.ADMIN_IDS = saved_token, saved_admins

    return problems, f"{done} проверок: две схемы подписи, срок, подмена id, кука, ссылка"


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
    ("метки суток, сроков и недель", check_time_keys),
    ("база знаний — пик против полки", check_rag_pick),
    ("звания викторины — лестница без дыр", check_quiz_ranks),
    ("суточный отчёт — расход за период", check_daily_report),
    ("вход в веб-админку — подпись и срок", check_web_auth),
)


def main() -> int:
    sys.path.insert(0, ROOT)

    # ⚠️ ПЕРВЫМ ДЕЛОМ уводим базу во временную папку — как в preflight.py.
    # Сейчас ни одна проверка в базу не ходит, но следующая может, и лучше
    # пусть она с самого начала пишет в пустышку, а не в боевую history.db.
    tmp_dir = tempfile.mkdtemp(prefix="c4max-selftest-")
    from database import history as hist
    hist.DB_PATH = os.path.join(tmp_dir, "selftest.db")
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
