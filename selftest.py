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


CHECKS = (
    ("деньги — расчёт стоимости запросов", check_money),
    ("деньги — сам прайс не менялся", check_price_list),
    ("пометка мута — разбор ответа модели", check_mute_tag),
    ("права доступа — кнопки и иерархия", check_permissions),
    ("размышления модели не утекают в чат", check_thoughts),
    ("длинные ответы — разметка не разъезжается", check_long_answers),
    ("потолки ожидания — перебор моделей", check_wait_budgets),
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
