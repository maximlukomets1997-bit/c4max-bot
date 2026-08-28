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


CHECKS = (
    ("деньги — расчёт стоимости запросов", check_money),
    ("деньги — сам прайс не менялся", check_price_list),
    ("пометка мута — разбор ответа модели", check_mute_tag),
    ("права доступа — кнопки и иерархия", check_permissions),
)


def main() -> int:
    sys.path.insert(0, ROOT)

    # ⚠️ ПЕРВЫМ ДЕЛОМ уводим базу во временную папку — как в preflight.py.
    # Сейчас ни одна проверка в базу не ходит, но следующая может, и лучше
    # пусть она с самого начала пишет в пустышку, а не в боевую history.db.
    tmp_dir = tempfile.mkdtemp(prefix="c4max-selftest-")
    from database import history as hist
    hist.DB_PATH = os.path.join(tmp_dir, "selftest.db")

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
