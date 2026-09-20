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
    from services.gemini import (_qwen_cost, _deepseek_cost, _xiaomi_cost, _image_cost,
                                 _deepseek_peak_now)

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
    model = "deepseek-flash"
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

    # ── DeepSeek: ВЫБОР колонки по календарю (добавлено 10.09.2026) ──
    # Считать сумму по заданной колонке проверки умели и раньше; а вот КТО
    # выбирает колонку, не проверял никто — и правило «выходные вне пика»
    # (у провайдера с 23.08.2026) полмесяца жило в боте неверным молча.
    # ⚠️ Часы поддельные: настоящее время даёт лишь одну точку шкалы, и в
    # понедельник проверка не увидела бы поломку выходных. Приём тот же, что
    # у потолков ожидания ниже, — подмена атрибута в модуле.
    from datetime import datetime as _dt, timezone as _tz
    from services import gemini as _g

    class _FixedClock:
        """Часы, стоящие на заданном моменте. Подменяют модулю datetime."""
        def __init__(self, moment): self._moment = moment
        def now(self, tz=None): return self._moment

    # (день, час, ждём пик?) — 2026-09-07 понедельник, 12-е суббота, 13-е воскресенье
    peak_cases = [
        (7,  2, True,  "будни, окно 01–04"),
        (7,  7, True,  "будни, окно 06–10"),
        (7,  5, False, "будни, между окнами"),
        (7,  0, False, "будни, до первого окна"),
        (7, 23, False, "будни, поздний вечер"),
        (12, 2, False, "СУББОТА в часы пика"),
        (13, 7, False, "ВОСКРЕСЕНЬЕ в часы пика"),
        (11, 7, True,  "пятница, окно 06–10"),
    ]
    saved_dt = _g.datetime
    try:
        for day, hour, want_peak, human in peak_cases:
            _g.datetime = _FixedClock(_dt(2026, 9, day, hour, 30, tzinfo=_tz.utc))
            done += 1
            got = _deepseek_peak_now()
            if got != want_peak:
                problems.append(
                    f"DeepSeek, выбор тарифа ({human}, {day}.09 {hour}:30 UTC): "
                    f"получили «{'пик' if got else 'вне пика'}», "
                    f"ждали «{'пик' if want_peak else 'вне пика'}» — "
                    f"проверь config.DEEPSEEK_PEAK_UTC и DEEPSEEK_PEAK_DAYS")
    finally:
        _g.datetime = saved_dt

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

    # ── СВЕРКА ОСТАТКА С ПЛАТФОРМОЙ (14.09.2026) ──────────────────────
    #
    # Собственный счёт бота занижен на оборванных потоках: токены провайдер
    # сгенерировал и деньги списал, а отчёт о них не пришёл (замер 14.09:
    # 4 обрыва из 114 запросов, отставание 4.7 цента). Поэтому остаток
    # берётся у платформы, а разницу разбирает plan_balance_sync.
    #
    # ⚠️ ПРОВЕРЯЕТСЯ ИМЕННО АРИФМЕТИКА РЕШЕНИЯ, а не запись в базу: до копилок
    # settings проверки проекта не доходят вовсе (см. шапку database/money.py),
    # и ради этого вся развилка вынесена в отдельную функцию без базы.
    from config import BALANCE_SYNC_EPSILON
    from database.money import plan_balance_sync

    def expect_sync(title, ours, real, want_balance, want_add, want_reason):
        nonlocal done
        done += 1
        balance, add, reason = plan_balance_sync(ours, real, BALANCE_SYNC_EPSILON)
        if reason != want_reason:
            problems.append(f"сверка остатка ({title}): решение «{reason}», ждали «{want_reason}»")
        if not _same_money(balance, want_balance):
            problems.append(f"сверка остатка ({title}): остаток ${balance:.6f}, "
                            f"ждали ${want_balance:.6f}")
        if not _same_money(add, want_add):
            problems.append(f"сверка остатка ({title}): в расход добавлено ${add:.6f}, "
                            f"ждали ${want_add:.6f}")

    # Наш остаток БОЛЬШЕ настоящего — недосчитали расход, доначисляем разницу.
    expect_sync("мы недосчитали расход", 3.946671, 3.90, 3.90, 0.046671, "недостача")
    # Наш остаток МЕНЬШЕ настоящего — счёт пополнили; расход уменьшать нельзя.
    expect_sync("счёт пополнили", 0.81, 10.81, 10.81, 0.0, "пополнение")
    # Разница меньше цента — платформа округляет остаток, это не расхождение.
    expect_sync("в пределах округления", 3.8988, 3.90, 3.8988, 0.0, "совпало")
    # Ровно порог — уже расхождение (сравнение строгое, граница описана в config).
    expect_sync("ровно на пороге", 3.91, 3.90, 3.90, 0.01, "недостача")

    # ⚠️ ПЛАТФОРМА МОЛЧИТ — НИЧЕГО НЕ ТРОГАЕМ. Главное обещание правки:
    # неответ не должен ни обнулять остаток, ни двигать «потрачено», ни
    # обновлять отметку «сверено». Гоняется САМ проход цикла, а не его куски:
    # проверять отдельно разбор ответа значило бы проверить механизм и не
    # заметить, что его перестали звать.
    import asyncio as _asyncio
    import jobs.balance as _bal
    import database.history as _hist

    written = []
    saved_providers, saved_sync = _bal._providers, _hist.sync_provider_balance
    try:
        _hist.sync_provider_balance = lambda *a, **kw: (written.append(a) or (0.0, 0.0, "совпало"))

        _bal._providers = lambda: {"deepseek": lambda: None}          # платформа не ответила
        _asyncio.run(_bal.sync_balances_once())
        done += 1
        if written:
            problems.append(f"платформа молчит, а остаток всё равно переписали: {written}")

        _bal._providers = lambda: {"deepseek": lambda: 7.77}          # платформа ответила
        _asyncio.run(_bal.sync_balances_once())
        done += 1
        if not written or not _same_money(written[-1][1], 7.77):
            problems.append(f"ответ платформы до записи не дошёл: {written}")
    finally:
        _bal._providers, _hist.sync_provider_balance = saved_providers, saved_sync

    return problems, (f"{done} проверок: Qwen, DeepSeek, Xiaomi, картинки, "
                      f"сверка остатка с платформой")


def check_qwen_quota():
    """
    Квота Qwen: оборванный ответ списывается ОЦЕНОЧНО, срок квоты виден,
    переход остатка через черту приносит владельцу ОДНО письмо (19.09.2026).

    ⚠️ РАДИ ЧЕГО. Обычный расход бот берёт из отчёта провайдера и тот точен до
    токена, но при обрыве потока отчёта нет вовсе, а Alibaba токены уже вычла.
    Сверка с консолью 18.09.2026: остаток в панели был завышен на 92 тысячи
    токенов у qwen3.7-plus и на 81 тысячу у qwen3.8-max — ровно там, где в логе
    были обрывы. Ошибка тихая: бот работает, счётчик просто врёт всё сильнее.

    ⚠️ ГОНЯЕТСЯ НАСТОЯЩИЙ ПОТОК `_openai_stream_request` на поддельных часах и
    поддельной сети: проверять `_charge_broken_stream` отдельно значило бы не
    заметить, что её забыли позвать из обрыва, — а это и есть та поломка.

    ⚠️ ТРИ ГРАНИЦЫ, без которых оценка вредит: не списывать дважды (отчёт уже
    пришёл), не трогать чужих провайдеров (у DeepSeek свой путь — сверка счёта),
    не списывать пустоту.
    """
    import json as _json
    import requests
    import config as c
    from services import gemini as g
    from database import history as hist
    from handlers.admin.panel_balance import _split_quota_input, _quota_until, _build_balance_panel

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    QWEN = next((m for m, meta in c.AVAILABLE_MODELS.items()
                 if meta.get("provider") == "qwen"), None)
    OTHER = next((m for m, meta in c.AVAILABLE_MODELS.items()
                  if meta.get("provider") == "deepseek"), None)
    if not QWEN or not OTHER:
        return ["в реестре не нашлось модели Qwen или DeepSeek — проверять нечего"], "пропущено"

    class Clock:
        def __init__(self): self.t = 1000.0
        def monotonic(self): return self.t
        def perf_counter(self): return self.t
        def sleep(self, s): self.t += s
        def time(self): return 1700000000.0

    clock = Clock()
    SENT = "запрос на сорок знаков ровно, без хвостов!"      # 41 знак
    THINK = "мысли модели" * 10                              # 120 знаков
    ANSWER = "кусок ответа" * 10                             # 120 знаков

    def sse(obj):
        return ("data: " + _json.dumps(obj, ensure_ascii=False)).encode()

    class FakeResponse:
        """Поток, который шлёт куски и «зависает»: часы уезжают за потолок."""
        def __init__(self, with_usage=False):
            self.with_usage = with_usage
        def raise_for_status(self): pass
        def close(self): pass
        def iter_lines(self):
            yield sse({"choices": [{"delta": {"reasoning_content": THINK}}]})
            yield sse({"choices": [{"delta": {"content": ANSWER}}]})
            if self.with_usage:
                yield sse({"usage": {"prompt_tokens": 10, "completion_tokens": 20,
                                     "total_tokens": 30}, "choices": []})
            clock.t += c.GEMINI_STREAM_DEADLINE + 1      # провисли дольше потолка
            yield sse({"choices": [{"delta": {"content": "хвост"}}]})

    saved_time, saved_http = g.time, g._http
    g.time = clock
    try:
        for model, with_usage, want_charge, title in (
            (QWEN, False, True, "Qwen, поток оборван"),
            (QWEN, True, False, "Qwen, отчёт о токенах уже пришёл"),
            (OTHER, False, False, "DeepSeek, поток оборван"),
        ):
            key = f"qwen_tokens_{QWEN}"
            hist.set_setting(key, "100000")
            g._http = lambda wu=with_usage: type("H", (), {
                "post": lambda self, *a, **kw: FakeResponse(wu)})()
            clock.t = 1000.0
            try:
                g._openai_stream_request(model, [{"role": "user", "content": SENT}],
                                         "http://example", "key", {})
                broke = False
            except requests.exceptions.Timeout:
                broke = True
            except Exception as e:
                broke = False
                problems.append(f"{title}: вместо таймаута вылетело {type(e).__name__}: {e}")
            expect(f"{title}: обрыв не превратился в таймаут — цепочка подстраховки не сработает",
                   broke)

            left = int(hist.get_setting(key, "0") or 0)
            spent = 100000 - left
            want = int((len(SENT) + len(THINK) + len(ANSWER)) / c.QWEN_CHARS_PER_TOKEN) if want_charge else 0
            expect(f"{title}: из квоты списано {spent} токенов, а ждали {want}", spent == want)

        # ⚠️ ЧУЖОЙ ПРОВАЙДЕР — ПРОВЕРЯЕМ ВОЗВРАТ, А НЕ ОСТАТОК QWEN. Первая
        # версия этой проверки смотрела только на ключ Qwen и на снятой защите
        # НЕ КРАСНЕЛА: списание уходило в несуществующий ключ
        # qwen_tokens_deepseek-… и молча ничего не меняло.
        expect("DeepSeek: оценка вообще не должна считаться — у него сверка счёта",
               g._charge_broken_stream(OTHER, [{"role": "user", "content": SENT}],
                                       [THINK], [ANSWER]) == 0)
        expect("DeepSeek: в настройках завёлся лишний ключ квоты",
               hist.get_setting(f"qwen_tokens_{OTHER}", "") in ("", None))

        # Пустой обрыв (не пришло ничего, кроме запроса) всё равно списывает
        # отправленное: провайдер контекст уже прочитал.
        hist.set_setting(f"qwen_tokens_{QWEN}", "100000")
        spent = g._charge_broken_stream(QWEN, [{"role": "user", "content": SENT}], [], [])
        expect(f"пустой обрыв: списано {spent}, а ждали {int(len(SENT) / c.QWEN_CHARS_PER_TOKEN)}",
               spent == int(len(SENT) / c.QWEN_CHARS_PER_TOKEN))
        expect("обрыв без текста вообще: списывать нечего",
               g._charge_broken_stream(QWEN, [], [], []) == 0)
    finally:
        g.time, g._http = saved_time, saved_http

    # ── Срок квоты: разбор ввода и показ на экране ──
    for raw, want_num, want_date in (
        ("66614 19.10.2026", "66614", "19.10.2026"),
        ("66614 19.10.2026 ", "66614", "19.10.2026"),
        ("66614 9.1.2027", "66614", "09.01.2027"),
        ("66614", "66614", ""),
        ("66614 завтра", "66614 завтра", ""),
        ("66614 19.10.26", "66614 19.10.26", ""),
    ):
        got_num, got_date = _split_quota_input(raw)
        expect(f"разбор «{raw}»: вышло число «{got_num}» и срок «{got_date}», "
               f"а ждали «{want_num}» и «{want_date}»",
               got_num.strip() == want_num.strip() and got_date == want_date)

    hist.set_setting(f"qwen_quota_until_{QWEN}", "19.10.2026")
    hist.set_setting(f"qwen_tokens_{QWEN}", "66614")
    expect("срок квоты не попал в приписку", "19.10.2026" in _quota_until(QWEN))
    text, _ = _build_balance_panel()
    expect("на экране «Счета и квоты» не видно остатка квоты", "66 614" in text)
    expect("на экране «Счета и квоты» не видно срока квоты", "квота до 19.10.2026" in text)
    hist.delete_setting(f"qwen_quota_until_{QWEN}")
    expect("срок убрали, а приписка осталась", _quota_until(QWEN) == "")

    # ── Письма владельцу о квоте (19.09.2026) ──
    # ⚠️ ПОВОД — ПЕРЕХОД ЧЕРЕЗ ЧЕРТУ, а не «остаток ниже черты»: иначе письмо
    # уходило бы на каждый ответ в минусе. Поэтому главное здесь — не «письмо
    # есть», а «письмо ОДНО»: второй ответ за чертой обязан промолчать.
    # ⚠️ Письмо ловится подменой _notify_admins: настоящее ушло бы владельцу
    # в Telegram, а selftest гоняется на сервере при каждой выкатке.
    WARN = c.QWEN_QUOTA_WARN_TOKENS
    key = f"qwen_tokens_{QWEN}"
    others = [m for m, meta in c.AVAILABLE_MODELS.items()
              if meta.get("provider") == "qwen" and m != QWEN]

    def num(n):
        return f"{n:,}".replace(",", " ")

    letters = []
    saved_notify, saved_request = g._notify_admins, g._qwen_chat_request
    saved_active = hist.get_setting("active_model", "")
    g._notify_admins = letters.append
    try:
        def spend(before, tokens):
            """Ставит остаток (None — квоты нет), списывает и отдаёт письма."""
            if before is None:
                hist.delete_setting(key)
            else:
                hist.set_setting(key, str(before))
            letters.clear()
            g._spend_qwen_quota(QWEN, tokens)
            return list(letters)

        # Пара «было → стало» — на ней держится всё остальное.
        hist.set_setting(key, "100")
        got = hist.spend_qwen_tokens(QWEN, 30)
        expect(f"списание вернуло {got}, а ждали пару (100, 70)", got == (100, 70))
        hist.delete_setting(key)
        got = hist.spend_qwen_tokens(QWEN, 30)
        expect(f"квоты нет, а списание вернуло {got} вместо пустоты", got is None)

        hist.set_setting("active_model", OTHER)
        got = spend(5000, 8000)
        expect(f"квота перешла ноль: писем {len(got)}, а ждали одно", len(got) == 1)
        text = got[0] if got else ""
        expect("письмо «кончилась»: нет заголовка", "Кончилась бесплатная квота" in text)
        expect("письмо «кончилась»: нет имени модели",
               c.AVAILABLE_MODELS[QWEN]["name"] in text)
        expect("письмо «кончилась»: нет остатка -3 000", "Остаток по счёту бота: -3 000" in text)
        price = c.QWEN_PRICES.get(QWEN)
        if price:
            expect("письмо «кончилась»: нет цен из прайса",
                   f"${price['cache_miss']:g} за миллион входа" in text
                   and f"${price['output']:g} за миллион ответа" in text)
        expect("письмо «кончилась»: модель не активная, а написано «активная»",
               "активная модель" not in text)
        expect("письмо «кончилась»: в списке других моделей не все Qwen",
               all(f"• {m} — " in text for m in others))
        expect("письмо «кончилась»: модель попала в список «других»",
               f"• {QWEN} — " not in text)

        got = spend(-3000, 8000)
        expect(f"квота уже в минусе: ушло писем {len(got)}, а ждали ни одного", not got)

        got = spend(WARN + 1000, 5000)
        expect(f"остаток опустился ниже порога: писем {len(got)}, а ждали одно", len(got) == 1)
        text = got[0] if got else ""
        expect("письмо «на исходе»: нет заголовка", "на исходе" in text)
        left = WARN - 4000
        expect(f"письмо «на исходе»: нет остатка {num(left)}", f"Осталось {num(left)}" in text)
        n = left // 5000
        expect(f"письмо «на исходе»: нет «примерно на {n} ответов»",
               f"примерно на {n} " in text)

        got = spend(WARN - 4000, 5000)
        expect(f"ниже порога, но выше нуля: писем {len(got)}, а ждали ни одного", not got)

        # Границы: ровно дошли до черты — перешли; уже стояли на черте — нет.
        got = spend(WARN + 1, 1)
        expect(f"остаток ровно дошёл до порога: писем {len(got)}, а ждали одно", len(got) == 1)
        got = spend(WARN, 1)
        expect(f"остаток уже стоял на пороге: писем {len(got)}, а ждали ни одного", not got)
        got = spend(100, 100)
        expect(f"остаток ровно дошёл до нуля: писем {len(got)}, а ждали одно «кончилась»",
               len(got) == 1 and "Кончилась" in got[0])
        got = spend(WARN + 10, WARN + 20)
        expect(f"проскочили порог и ноль разом: писем {len(got)}, а ждали одно «кончилась»",
               len(got) == 1 and "Кончилась" in got[0])
        got = spend(None, 8000)
        expect(f"квота не задана: писем {len(got)}, а ждали ни одного", not got)

        # Оборванный ответ ведёт через ту же дверь: оценка тоже будит письмо.
        hist.set_setting(key, "10")
        letters.clear()
        g._charge_broken_stream(QWEN, [{"role": "user", "content": SENT}], [THINK], [ANSWER])
        expect(f"оборванный ответ увёл квоту в минус: писем {len(letters)}, а ждали одно",
               len(letters) == 1)

        # Обычный ответ — через НАСТОЯЩУЮ очередь _gemini_chat_request:
        # проверяй мы одну _spend_qwen_quota, никто не заметил бы, что главный
        # путь списывает мимо неё и молчит.
        hist.set_setting("active_model", QWEN)
        hist.set_setting(key, "5000")
        letters.clear()
        g._qwen_chat_request = lambda *a, **kw: {
            "choices": [{"message": {"content": "ответ"}}],
            "usage": {"prompt_tokens": 7000, "completion_tokens": 1000, "total_tokens": 8000}}
        data, used = g._gemini_chat_request([{"role": "user", "content": "вопрос"}])
        expect(f"обычный ответ: ответила {used}, а ждали {QWEN}", data is not None and used == QWEN)
        now_left = hist.get_setting(key, "")
        expect(f"обычный ответ: остаток {now_left}, а ждали -3000", str(now_left) == "-3000")
        expect(f"обычный ответ увёл квоту в минус: писем {len(letters)}, а ждали одно",
               len(letters) == 1)
        expect("письмо об активной модели: нет строки «Это активная модель»",
               bool(letters) and "Это активная модель" in letters[0])
    finally:
        g._notify_admins, g._qwen_chat_request = saved_notify, saved_request
        if saved_active:
            hist.set_setting("active_model", saved_active)
        else:
            hist.delete_setting("active_model")
        hist.delete_setting(key)

    return problems, (f"{done} проверок: оценка на обрыве, отчёт не списывается дважды, "
                      f"чужие провайдеры не задеты, разбор срока и показ на экране, "
                      f"письма «на исходе» и «кончилась» — по одному на переход через черту")


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


def check_ai_mute_name():
    """
    Мут от бота подписан человеком, а не номером (16.09.2026).

    ⚠️ РАДИ ЧЕГО. Строку «🗣 Бот сам выдал мут: 5288487947» Максим читает в
    личке, а такую же подпись получает запись журнала наказаний «🤚 мут от
    бота» — в боте и на сайте. Из всех наказаний номер вместо имени стоял
    ТОЛЬКО здесь: антифлуд, фильтр ссылок и ручной мут имя подставляют. Ошибка
    тихая — бот работает, наказание выдано, просто по строке не понять, кого
    наказали.

    ⚠️ ГОНЯЕТСЯ НАСТОЯЩИЙ ПУТЬ ВЫДАЧИ (`proactive._apply_mute`), поддельные
    только Telegram и получатели письма. Проверять `target_name_with_nick`
    отдельно значило бы не заметить, что её забыли позвать или отдали имя
    в письмо, а в журнал — по-прежнему номер: это и есть та поломка, ради
    которой проверка написана.

    ⚠️ ОЖИДАЕМЫЕ СТРОКИ ВЫПИСАНЫ ЗДЕСЬ РУКАМИ, а не собраны тем же кодом:
    иначе обе стороны ошиблись бы одинаково и проверка позеленела бы на
    любой подписи.
    """
    import asyncio

    import config
    from database import history as hist
    from services import proactive

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    CHAT, OWNER, BOT_ID = -1009991001, 555100001, 555100999
    # Трое: с именем и ником, только с ником, и вовсе не известный боту.
    FULL, NICK_ONLY, STRANGER = 555100011, 555100012, 555100013
    QUOTE = "Допизделся что-ли"

    class _Bot:
        """Telegram без сети: муты и письма ложатся в записную книжку."""
        id = BOT_ID

        def __init__(self):
            self.sent = []
            self.muted = []

        async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None, **kw):
            self.muted.append((chat_id, user_id))
            return True

        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            self.sent.append({"chat_id": chat_id, "text": text})
            return True

        async def delete_message(self, chat_id, message_id, **kw):
            return True

    saved_admins = config.ADMIN_IDS
    try:
        # Владелец нужен, чтобы письму было куда уйти: без получателей
        # notify_owners_ai_mute выходит молча, и проверять было бы нечего.
        config.ADMIN_IDS = (OWNER,)
        hist.dossier_add_message(FULL, username="c4nightmare", first_name="Максим")
        hist.dossier_add_message(NICK_ONLY, username="ghost", first_name="")

        for uid, want in ((FULL, "Максим (@c4nightmare)"),
                          (NICK_ONLY, "@ghost"),
                          (STRANGER, str(STRANGER))):
            bot = _Bot()
            asyncio.run(proactive._apply_mute(bot, CHAT, uid, 600, QUOTE))

            expect(f"мут {uid}: сам мут не выдан — проверять подпись не на чем",
                   bot.muted == [(CHAT, uid)])

            letters = [m["text"] for m in bot.sent if m["chat_id"] == OWNER]
            expect(f"мут {uid}: владельцу не ушло письмо о муте от бота", len(letters) == 1)
            if letters:
                first = letters[0].splitlines()[0]
                expect(f"мут {uid}: в письме «{first}», а ждали "
                       f"«🗣 Бот сам выдал мут: {want} на 10 мин 0 сек.»",
                       first == f"🗣 Бот сам выдал мут: {want} на 10 мин 0 сек.")
                expect(f"мут {uid}: в письме пропала цитата, за которую наказали",
                       QUOTE in letters[0])

            entry = next((e for e in hist.get_recent_moderation_actions(10)
                          if e["action"] == "mute_ai" and e["user_id"] == uid), None)
            expect(f"мут {uid}: в журнале наказаний нет записи «мут от бота»",
                   entry is not None)
            if entry:
                expect(f"мут {uid}: в журнале подпись «{entry['name']}», "
                       f"а ждали «{want}»", entry["name"] == want)

        # Отдельно: у известного человека номера в письме быть НЕ ДОЛЖНО —
        # ровно с этого началась правка.
        bot = _Bot()
        asyncio.run(proactive._apply_mute(bot, CHAT, FULL, 600, QUOTE))
        letter = next((m["text"] for m in bot.sent if m["chat_id"] == OWNER), "")
        expect("мут известного человека: в письме остался его номер вместо имени",
               str(FULL) not in letter)
    finally:
        config.ADMIN_IDS = saved_admins

    return problems, (f"{done} проверок: имя с ником, один ник без скобок, "
                      f"неизвестный остаётся номером; письмо и журнал наказаний")


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

        # ── Чужие группы: где работать боту, решает только владелец (15.09.2026) ──
        expect_press("владелец → «остаться в группе»", OWNER, "grp:stay:-1004332242579", True)
        expect_press("модератор с «мут» → «выйти из группы»",
                     MOD_MUTE, "grp:leave:-1004332242579", False)
        expect_press("модератор с «карточки» → «остаться в группе»",
                     MOD_CARDS, "grp:stay:-1004332242579", False)

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
        "qwen3.8-flash": {"cache_hit": 0.016, "cache_miss": 0.15, "output": 0.47},
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
    "deepseek-flash": {
        "peak":    {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
        "offpeak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
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

        # ── Фото в личке ради поиска по базе (07.09.2026) ──────────────
        #
        # ⚠️ ЭТОГО СЦЕНАРИЯ ЗДЕСЬ НЕ БЫЛО ВОВСЕ, а он единственный, где перебор
        # режется ПО ЧИСЛУ моделей, а не только по времени. Проверка заведена
        # в тот день, когда число подстраховок подняли с одной до трёх: без неё
        # откат к единице не заметила бы ни одна проверка проекта.
        run("фото в личке (разбор ради поиска по базе)",
            lambda: g._describe_image("QQ", g._SEARCH_PHOTO_CHAIN_LIMIT),
            g._MEDIA_CHAIN_BUDGET_SEC, g._describe_timeout(1), n_media)
        done += 1
        if len(calls) != g._SEARCH_PHOTO_CHAIN_LIMIT:
            problems.append(f"фото в личке: пробовано моделей {len(calls)}, а "
                            f"должно {g._SEARCH_PHOTO_CHAIN_LIMIT} — укорот цепочки не сработал")

        # Связь «поиск по базе → настройка» жива. Отдельной проверкой, потому
        # что предыдущая гоняет _describe_image НАПРЯМУЮ и слепа к тому, какое
        # число просит сам поиск: верни туда жёсткую единицу — и она смолчит.
        asked = []
        import services.rag as rag_module
        saved_describe, saved_rag_active = g._describe_image, rag_module.is_active
        try:
            rag_module.is_active = lambda: True
            g._describe_image = lambda img, chain_limit=0, **kw: (asked.append(chain_limit)
                                                                  or "описание картинки.")
            g._media_search_text(image_base64="QQ")
        finally:
            g._describe_image = saved_describe
            rag_module.is_active = saved_rag_active
        done += 1
        if asked != [g._SEARCH_PHOTO_CHAIN_LIMIT]:
            problems.append(f"поиск по базе просит у разбора фото {asked}, а "
                            f"должен [{g._SEARCH_PHOTO_CHAIN_LIMIT}] — "
                            f"настройка и вызов разъехались")

    finally:
        g.time, g._http = saved_time, saved_http
        g._notify_chain_dead = saved_notify
        g._quota_blocked.clear()
        g._quota_blocked.update(saved_blocked)

    return problems, (f"{done} проверок: личка, группа, фото ради поиска по базе, "
                      f"первая попытка не урезана")


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


def check_group_guard():
    """
    Заслон от чужих групп (15.09.2026, services/group_guard.py).

    ⚠️ РАДИ ЧЕГО. 12.09.2026 бота без ведома Максима добавили в чужую группу
    «Ветеэм»: в тот же вечер он ответил там 99 раз, сам влезал в разговор,
    потом три дня слал туда вопрос дня. Поломка заслона тихая В ОБЕ
    СТОРОНЫ: пропускает чужих — выглядит как обычная работа бота; не пускает
    своих — бот «просто молчит». Поэтому проверяются обе стороны.

    ⚠️ ГОНЯЕТСЯ НАСТОЯЩАЯ РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ (handlers.setup_handlers) в
    настоящем приложении Telegram — поддельные только бот и сеть. Проверять
    gate() отдельно значило бы не заметить, что его забыли зарегистрировать,
    поставили не первым или сделали неблокирующим, а это ровно те поломки,
    при которых заслон молча становится дыркой. Все остальные обработчики
    подменены записной книжкой «дошло»: ответы модели здесь не нужны, нужно
    знать, прошло ли обновление дальше заслона.
    """
    import asyncio

    import config
    from telegram import Update
    from telegram.error import Forbidden
    from telegram.ext import ApplicationBuilder

    from database import history as hist
    from handlers import setup_handlers
    from services import group_guard as gg
    from services import quiz_daily, roles

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    OWNER, STRANGER, BOT = 555000111, 555000222, 555000999
    OWN, OWN2, FOREIGN, UNKNOWN, JOIN_ONLY = (-1009990001, -1009990002, -1009990003,
                                              -1009990004, -1009990005)
    MINE, OLD_BASIC, NEW_SUPER, GONE, NETFAIL, FAILOPEN = (
        -1009990006, -9990007, -1009990008, -1009990009, -1009990010, -1009990011)
    # Переезд в супергруппу: ждущая решения (сначала сообщение о переезде /
    # сначала событие «бота добавили») и своя (сначала событие).
    P_OLD, P_NEW, Q_OLD, Q_NEW, M_OLD, M_NEW = (
        -9990012, -1009990013, -9990014, -1009990015, -9990016, -1009990017)
    LEFT_ME, NOT_MEMBER = -1009990018, -1009990019
    ALL_CHATS = (OWN, OWN2, FOREIGN, UNKNOWN, JOIN_ONLY, MINE, OLD_BASIC, NEW_SUPER,
                 GONE, NETFAIL, FAILOPEN, P_OLD, P_NEW, Q_OLD, Q_NEW, M_OLD, M_NEW,
                 LEFT_ME, NOT_MEMBER)

    # ── 0. Русское число в вопросе («4 человека») ──
    for n, want in ((1, "участник"), (2, "участника"), (4, "участника"),
                    (5, "участников"), (11, "участников"), (12, "участников"),
                    (21, "участник"), (22, "участника"), (111, "участников")):
        got = gg._plural(n, "участник", "участника", "участников")
        expect(f"число {n}: вышло «{n} {got}», а должно «{n} {want}»", got == want)

    class _User:
        def __init__(self, uid, first_name, username=None):
            self.id = uid
            self.first_name = first_name
            self.last_name = None
            self.username = username

    class _Member:
        def __init__(self, status, user=None):
            self.status = status
            self.user = user
            self.is_member = False

    class _Bot:
        """Telegram без сети: всё, что бот отправил, ложится в записную книжку."""
        id = BOT
        username = "C4_Max_bot"

        def __init__(self):
            self.sent = []
            self.left = []
            self.member_error = None
            self.retargeted = {}     # номер сообщения → callback_data новых кнопок
            self.retired = {}        # номер сообщения → текст, которым вопрос снят
            self._next_id = 1000

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
            self._next_id += 1
            self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup,
                              "message_id": self._next_id})

            class _Sent:
                message_id = self._next_id
            return _Sent()

        async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
            rows = getattr(reply_markup, "inline_keyboard", None) or ()
            self.retargeted[message_id] = [b.callback_data for row in rows for b in row]

        async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, **kw):
            self.retired[message_id] = text

        async def get_chat_member_count(self, chat_id):
            return 5            # Telegram считает и самого бота — людей четверо

        async def get_chat_administrators(self, chat_id):
            return [_Member("administrator", _User(STRANGER, "Чужой", "stranger")),
                    _Member("creator", _User(777, "KRUPP <босс>", "deKRUPPde"))]

        async def get_chat_member(self, chat_id, user_id):
            if self.member_error is not None:
                raise self.member_error
            return _Member("member")

        async def leave_chat(self, chat_id):
            self.left.append(chat_id)
            return True

    class _Clock:
        now = 1_800_000_000.0

        def time(self):
            return self.now

    query_ids = iter(range(5000, 6000))

    class _Query:
        def __init__(self, data):
            self.data = data
            self.alerts = []
            self.edited = []

            class _Msg:
                chat_id = OWNER
                message_id = next(query_ids)
            self.message = _Msg()

        async def answer(self, text="", show_alert=False):
            self.alerts.append(text)

        async def edit_message_text(self, text, parse_mode=None, **kw):
            self.edited.append(text)

    class _Ctx:
        def __init__(self, bot):
            self.bot = bot

    # Что поставлено на самоудаление: (чат, номер сообщения) → через сколько
    # секунд. Настоящий schedule_delete завёл бы задачу на пять минут ожидания.
    DONE_TTL = 5 * 60        # просьба Максима 15.09.2026 — числом, а не из модуля
    deleted = {}

    def _record_delete(bot, chat_id, message_id, delay=30):
        deleted[(chat_id, message_id)] = delay

    # ── строители обновлений в том виде, в каком их присылает Telegram ──
    ids = iter(range(1, 10_000))
    DATE = 1_700_000_000

    def person(uid, name, username=None, is_bot=False):
        data = {"id": uid, "is_bot": is_bot, "first_name": name}
        if username:
            data["username"] = username
        return data

    def group(cid, title, kind="supergroup"):
        return {"id": cid, "type": kind, "title": title}

    def msg(cid, title, text="привет", kind="supergroup", **extra):
        m = {"message_id": next(ids), "date": DATE, "chat": group(cid, title, kind),
             "from": person(STRANGER, "Чужой", "stranger")}
        if text is not None:
            m["text"] = text
        m.update(extra)
        return Update.de_json({"update_id": next(ids), "message": m}, None)

    def private_msg():
        return Update.de_json({"update_id": next(ids), "message": {
            "message_id": next(ids), "date": DATE, "text": "привет",
            "chat": {"id": STRANGER, "type": "private", "first_name": "Чужой"},
            "from": person(STRANGER, "Чужой", "stranger")}}, None)

    def poll_answer():
        # ⚠️ option_persistent_ids ОБЯЗАТЕЛЕН с библиотеки 22.8 (Bot API 9.6):
        # без него на сервере (там 22.8) проверка срывалась бы на сборке
        # обновления и откатывала выкатку, хотя дома, на 22.7, зеленела.
        return Update.de_json({"update_id": next(ids), "poll_answer": {
            "poll_id": "selftest-poll", "option_ids": [0], "option_persistent_ids": ["0"],
            "user": person(STRANGER, "Чужой", "stranger")}}, None)

    def button_in(cid, title):
        return Update.de_json({"update_id": next(ids), "callback_query": {
            "id": str(next(ids)), "chat_instance": "selftest", "data": "quiz_start",
            "from": person(STRANGER, "Чужой", "stranger"),
            "message": {"message_id": next(ids), "date": DATE, "text": "вопрос",
                        "chat": group(cid, title)}}}, None)

    def my_status(cid, title, actor, old, new):
        me = person(BOT, "C4", "C4_Max_bot", is_bot=True)
        return Update.de_json({"update_id": next(ids), "my_chat_member": {
            "chat": group(cid, title), "from": actor, "date": DATE,
            "old_chat_member": {"status": old, "user": me},
            "new_chat_member": {"status": new, "user": me}}}, None)

    async def scenario(clock):
        app = (ApplicationBuilder().token("123456789:AAEeTestTokenForSelftestChecksOnly")
               .updater(None).build())
        setup_handlers(app)
        bot = _Bot()
        app.bot = bot

        # ── 1. Как заслон зарегистрирован ──
        guard_groups = [g for g, hs in app.handlers.items()
                        if any(getattr(h, "callback", None) is gg.gate for h in hs)]
        expect("заслон group_guard.gate не зарегистрирован в handlers/__init__.py — "
               "чужие группы проходят без всякой проверки", len(guard_groups) == 1)
        if len(guard_groups) != 1:
            return
        guard = guard_groups[0]
        expect(f"заслон стоит в группе обработчиков {guard}, а раньше него есть другие — "
               f"они увидят сообщения чужих групп", guard == min(app.handlers))
        callbacks = [getattr(h, "callback", None) for h in app.handlers[guard]]
        expect("событие «сменился мой статус» не стоит ПЕРЕД заслоном в его группе — "
               "заслон съест событие «бота добавили», вопрос владельцу не уйдёт",
               gg.on_my_chat_member in callbacks
               and callbacks.index(gg.on_my_chat_member) < callbacks.index(gg.gate))
        expect("заслон зарегистрирован НЕБЛОКИРУЮЩИМ — в таком ApplicationHandlerStop "
               "не действует, и чужие сообщения проходят",
               all(h.block is not False for h in app.handlers[guard]))

        reached = []

        async def _reached(update, context):
            reached.append(update.update_id)

        for g, handlers in app.handlers.items():
            if g == guard:
                continue
            for h in handlers:
                h.callback = _reached
                h.block = True

        async def passes(update):
            before = len(reached)
            await app.process_update(update)
            return len(reached) > before

        await app.initialize()
        try:
            # ── 2. Своя группа, личка, обновление без чата — проходят ──
            hist.remember_chat(OWN, "Своя")
            hist.remember_chat(OWN2, "Своя вторая")
            expect("сообщение из СВОЕЙ группы не дошло до обработчиков — бот замолчит "
                   "в группах Максима", await passes(msg(OWN, "Своя")))
            expect("сообщение в ЛИЧКЕ не дошло до обработчиков", await passes(private_msg()))
            expect("ответ на опрос (у него нет чата) не дошёл до обработчиков — очки "
                   "викторины перестанут считаться", await passes(poll_answer()))
            expect("о своей группе спросили владельца", not bot.sent)

            # ── 3. Бота добавил чужой ──
            await app.process_update(my_status(FOREIGN, "Ветеэм <тест>",
                                               person(STRANGER, "Чужой", "stranger"),
                                               "left", "member"))
            expect("бота добавил ЧУЖОЙ, а группа сразу стала своей — ровно история "
                   "«Ветеэм» 12.09", not hist.is_known_chat(FOREIGN))
            asks = [s for s in bot.sent if s["chat_id"] == OWNER]
            expect(f"владельцу ушло вопросов: {len(asks)}, а должен ровно один", len(asks) == 1)
            if asks:
                text = asks[0]["text"]
                expect("в вопросе нет строки «Добавил:» с именем добавившего",
                       "Добавил: Чужой (@stranger)" in text)
                expect("название группы в вопросе не экранировано — «<» в названии "
                       "порвёт разметку, и вопрос не уйдёт вовсе",
                       "<тест>" not in text and "&lt;тест&gt;" in text)
                expect("в вопросе нет владельца группы или его имя не экранировано",
                       "Владелец: KRUPP &lt;босс&gt; (@deKRUPPde)" in text)
                expect("людей в группе посчитано вместе с ботом: Telegram отдал 5, "
                       "людей 4", "4 человека" in text)
                rows = getattr(asks[0]["markup"], "inline_keyboard", None) or ()
                datas = [b.callback_data for row in rows for b in row]
                expect(f"кнопки под вопросом не те: {datas}",
                       datas == [f"grp:stay:{FOREIGN}", f"grp:leave:{FOREIGN}"])
                expect("вопрос, ЖДУЩИЙ решения, поставлен на самоудаление — вместе с ним "
                       "пропали бы кнопки", (OWNER, asks[0]["message_id"]) not in deleted)
            expect("что владельца уже спросили, не записано в settings — после "
                   "перезапуска бота вопрос пришёл бы заново", str(FOREIGN) in gg._pending())

            # ── 4. Чужая группа пишет ──
            sent_before = len(bot.sent)
            expect("сообщение из ЧУЖОЙ группы дошло до обработчиков — бот отвечал бы "
                   "там, как 12.09 в «Ветеэм»", not await passes(msg(FOREIGN, "Ветеэм <тест>")))
            expect("кнопка под сообщением в чужой группе дошла до обработчиков",
                   not await passes(button_in(FOREIGN, "Ветеэм <тест>")))
            expect("чужая группа пишет — и владельцу вопрос на каждое сообщение, а не "
                   "раз в сутки", len(bot.sent) == sent_before)
            clock.now += gg.REASK_SEC + 1
            await app.process_update(msg(FOREIGN, "Ветеэм <тест>"))
            new = bot.sent[sent_before:]
            expect("чужая группа пишет через сутки после вопроса, а напоминания нет",
                   len(new) == 1)
            if new:
                expect("напоминание не помечено как напоминание — выглядит как вопрос "
                       "о новой группе", "Напоминаю" in new[0]["text"])
                expect("в напоминании потерялось, кто добавил бота, — хотя в первом "
                       "вопросе это было известно", "Добавил: Чужой (@stranger)" in new[0]["text"])

            # ── 5. О группе узнали по сообщению (событие о добавлении потерялось) ──
            sent_before = len(bot.sent)
            expect("сообщение из НЕЗНАКОМОЙ группы дошло до обработчиков",
                   not await passes(msg(UNKNOWN, "Незнакомая")))
            new = bot.sent[sent_before:]
            expect("о незнакомой группе не спросили владельца — бот молчал бы там без "
                   "конца, а Максим не знал бы почему",
                   len(new) == 1 and "Кто добавил — не знаю" in new[0]["text"])

            # ── 6. Служебное «добавил бота» — второго вопроса нет ──
            sent_before = len(bot.sent)
            joined = msg(JOIN_ONLY, "Только вступление", text=None,
                         new_chat_members=[person(BOT, "C4", "C4_Max_bot", is_bot=True)])
            expect("служебное «добавил бота» из чужой группы дошло до обработчиков",
                   not await passes(joined))
            expect("на служебное «добавил бота» ушёл вопрос «кто добавил — не знаю» — "
                   "рядом с событием о добавлении было бы два вопроса об одной группе",
                   len(bot.sent) == sent_before)

            # ── 7. Бота добавил владелец ──
            sent_before = len(bot.sent)
            await app.process_update(my_status(MINE, "Моя новая", person(OWNER, "Максим"),
                                               "left", "member"))
            expect("бота добавил ВЛАДЕЛЕЦ, а группа не стала своей — бот молчал бы в "
                   "группе Максима", hist.is_known_chat(MINE))
            expect("бота добавил владелец, а его всё равно спросили",
                   len(bot.sent) == sent_before)
            expect("сообщение из группы, куда бота добавил владелец, не дошло до обработчиков",
                   await passes(msg(MINE, "Моя новая")))

            # ── 8. Своя группа стала супергруппой ──
            hist.remember_chat(OLD_BASIC, "Переезжающая")
            sent_before = len(bot.sent)
            await app.process_update(msg(NEW_SUPER, "Переезжающая", text=None,
                                         migrate_from_chat_id=OLD_BASIC))
            expect("своя группа стала супергруппой, а новый номер не стал своим — бот "
                   "замолчал бы после переезда", hist.is_known_chat(NEW_SUPER))
            expect("старый номер переехавшей группы остался в списке — вопрос дня рвался "
                   "бы в мёртвую группу", not hist.is_known_chat(OLD_BASIC))
            await app.process_update(msg(OLD_BASIC, "Переезжающая", text=None, kind="group",
                                         migrate_to_chat_id=NEW_SUPER))
            # Telegram шлёт ещё и «бота добавили» в новый номер — от того, кто
            # вызвал переезд. Группа уже своя: спрашивать не о чем.
            await app.process_update(my_status(NEW_SUPER, "Переезжающая",
                                               person(STRANGER, "Чужой", "stranger"),
                                               "left", "member"))
            expect("о своей переехавшей группе спросили владельца",
                   len(bot.sent) == sent_before)

            # ── 8б. Переезд группы, ЖДУЩЕЙ решения (живой тест 15.09.2026) ──
            stranger = person(STRANGER, "Чужой", "stranger")
            # Сначала сообщение о переезде, потом «бота добавили» в новый номер.
            sent_before = len(bot.sent)
            await app.process_update(my_status(P_OLD, "Переезд до решения", stranger,
                                               "left", "member"))
            first = bot.sent[sent_before:]
            await app.process_update(msg(P_NEW, "Переезд до решения", text=None,
                                         migrate_from_chat_id=P_OLD))
            await app.process_update(my_status(P_NEW, "Переезд до решения", stranger,
                                               "left", "member"))
            await app.process_update(msg(P_OLD, "Переезд до решения", text=None, kind="group",
                                         migrate_to_chat_id=P_NEW))
            expect(f"группа сменила номер, пока ждала решения, — и владельцу ушло "
                   f"вопросов: {len(bot.sent) - sent_before}, а должен один",
                   len(bot.sent) - sent_before == 1)
            pending = gg._pending()
            expect("ожидание решения не переехало на новый номер группы — на её "
                   "сообщения вопрос пришёл бы заново",
                   str(P_NEW) in pending and str(P_OLD) not in pending)
            if first:
                expect("кнопки уже отправленного вопроса не переключились на новый номер — "
                       "«Выйти» ответил бы «меня уже нет», хотя бот в группе",
                       bot.retargeted.get(first[0]["message_id"])
                       == [f"grp:stay:{P_NEW}", f"grp:leave:{P_NEW}"])
                expect("вопрос с переключёнными кнопками поставлен на самоудаление — он "
                       "всё ещё ждёт решения", (OWNER, first[0]["message_id"]) not in deleted)

            # Сначала «бота добавили» в новый номер, потом сообщение о переезде.
            sent_before = len(bot.sent)
            await app.process_update(my_status(Q_OLD, "Переезд наоборот", stranger,
                                               "left", "member"))
            first = bot.sent[sent_before:]
            await app.process_update(my_status(Q_NEW, "Переезд наоборот", stranger,
                                               "left", "member"))
            await app.process_update(msg(Q_NEW, "Переезд наоборот", text=None,
                                         migrate_from_chat_id=Q_OLD))
            pending = gg._pending()
            expect("после переезда в ожидании остался старый номер группы или пропал новый",
                   str(Q_NEW) in pending and str(Q_OLD) not in pending)
            if first:
                retired = bot.retired.get(first[0]["message_id"], "")
                expect("второй вопрос о той же группе ушёл, а первый не снят — у владельца "
                       "два вопроса, и кнопки первого бьют мимо",
                       "стала супергруппой" in retired and "ниже" in retired)
                expect(f"снятый вопрос («вопрос о ней ниже») не исчезает через 5 минут: "
                       f"срок {deleted.get((OWNER, first[0]['message_id']))}",
                       deleted.get((OWNER, first[0]["message_id"])) == DONE_TTL)

            # Своя группа: «бота добавили» в новый номер пришло раньше переезда.
            hist.remember_chat(M_OLD, "Своя переезжающая")
            sent_before = len(bot.sent)
            await app.process_update(my_status(M_NEW, "Своя переезжающая", stranger,
                                               "left", "member"))
            asked = bot.sent[sent_before:]
            await app.process_update(msg(M_NEW, "Своя переезжающая", text=None,
                                         migrate_from_chat_id=M_OLD))
            expect("своя группа переехала (событие раньше сообщения), а новый номер не "
                   "стал своим", hist.is_known_chat(M_NEW) and not hist.is_known_chat(M_OLD))
            expect("вопрос о СВОЕЙ переехавшей группе остался ждать решения — нажатое "
                   "по ошибке «Выйти» вывело бы бота из группы Максима",
                   str(M_NEW) not in gg._pending())
            if asked:
                expect("вопрос о своей переехавшей группе не снят — у владельца висят "
                       "кнопки «Остаться/Выйти» про его же группу",
                       "ваша группа" in bot.retired.get(asked[0]["message_id"], ""))
                expect(f"снятый вопрос («это ваша группа») не исчезает через 5 минут: "
                       f"срок {deleted.get((OWNER, asked[0]['message_id']))}",
                       deleted.get((OWNER, asked[0]["message_id"])) == DONE_TTL)

            # ── 8в. Бот вышел — хвосты из покинутой группы вопросов не порождают ──
            sent_before = len(bot.sent)
            left_me = msg(LEFT_ME, "Покинутая", text=None,
                          left_chat_member=person(BOT, "C4", "C4_Max_bot", is_bot=True))
            expect("служебное «бот покинул группу» дошло до обработчиков",
                   not await passes(left_me))
            expect("бот вышел из группы — и на служебное «бот покинул группу» тут же ушёл "
                   "новый вопрос о ней (живой тест 15.09.2026)", len(bot.sent) == sent_before)
            bot.member_error = Forbidden("bot is not a member of the supergroup chat")
            try:
                await app.process_update(msg(NOT_MEMBER, "Где бота нет"))
            finally:
                bot.member_error = None
            expect("вопрос ушёл о группе, где бота уже нет", len(bot.sent) == sent_before)

            # ── 9. Бота удалили ──
            quiz_daily.remember(OWN, {"message_id": 1, "poll_id": "selftest-poll"})
            sent_before = len(bot.sent)
            await app.process_update(my_status(OWN, "Своя", person(STRANGER, "Чужой", "stranger"),
                                               "member", "left"))
            expect("бота удалили из своей группы, а она осталась в списке — вопрос дня "
                   "и дайджест шли бы туда дальше", not hist.is_known_chat(OWN))
            expect("бота удалили, а запись о вопросе дня той группы осталась висеть",
                   str(OWN) not in quiz_daily.active())
            new = bot.sent[sent_before:]
            expect("бота удалил из своей группы чужой, а владельцу не сообщили (или "
                   "сообщили без группы и того, кто удалил)",
                   len(new) == 1 and "Меня удалили из группы «Своя»" in new[0]["text"]
                   and "Удалил: Чужой (@stranger)" in new[0]["text"])
            if new:
                expect(f"«Меня удалили из группы» не исчезает через 5 минут: срок "
                       f"{deleted.get((OWNER, new[0]['message_id']))}",
                       deleted.get((OWNER, new[0]["message_id"])) == DONE_TTL)
            sent_before = len(bot.sent)
            await app.process_update(my_status(OWN2, "Своя вторая", person(OWNER, "Максим"),
                                               "member", "left"))
            expect("владелец сам удалил бота, а группа осталась в списке",
                   not hist.is_known_chat(OWN2))
            expect("владелец сам удалил бота — и получил об этом сообщение",
                   len(bot.sent) == sent_before)

            # ── 10. Сбой проверки пропускает, а не запирает ──
            real = gg.is_known_chat

            def _broken(chat_id):
                raise RuntimeError("база не ответила")

            gg.is_known_chat = _broken
            try:
                expect("проверка группы сломалась — и сообщение не прошло: сбой заслона "
                       "обязан пропускать, иначе бот замолчит во всех группах сразу",
                       await passes(msg(FAILOPEN, "Любая")))
            finally:
                gg.is_known_chat = real
        finally:
            await app.shutdown()

        # ── 11. Кнопки «✅ Остаться» и «🚪 Выйти» ──
        ctx = _Ctx(bot)
        q = _Query(f"grp:stay:{FOREIGN}")
        await gg.handle_group_callback(q, ctx, q.data, OWNER)
        expect("«✅ Остаться» не сделал группу своей", hist.is_known_chat(FOREIGN))
        expect("после «✅ Остаться» группа осталась ждать решения — напоминания шли бы дальше",
               str(FOREIGN) not in gg._pending())
        expect("после «✅ Остаться» вопрос не сменился итогом — кнопки остались бы висеть",
               bool(q.edited) and "Остаюсь" in q.edited[-1])
        expect(f"итог «✅ Остаюсь» не исчезает через 5 минут: срок "
               f"{deleted.get((OWNER, q.message.message_id))}",
               deleted.get((OWNER, q.message.message_id)) == DONE_TTL)

        q = _Query(f"grp:leave:{UNKNOWN}")
        await gg.handle_group_callback(q, ctx, q.data, OWNER)
        expect("«🚪 Выйти» не вывел бота из группы", bot.left == [UNKNOWN])
        expect("после «🚪 Выйти» группа в списке своих", not hist.is_known_chat(UNKNOWN))
        expect("после «🚪 Выйти» группа осталась ждать решения", str(UNKNOWN) not in gg._pending())
        expect("после «🚪 Выйти» вопрос не сменился итогом",
               bool(q.edited) and "Вышел из «Незнакомая»" in q.edited[-1])
        expect(f"итог «🚪 Вышел из…» не исчезает через 5 минут: срок "
               f"{deleted.get((OWNER, q.message.message_id))}",
               deleted.get((OWNER, q.message.message_id)) == DONE_TTL)

        # Бота удалили раньше, чем владелец нажал «Остаться».
        gg._mark_asked(GONE, "Ушедшая")
        bot.member_error = Forbidden("bot is not a member of the supergroup chat")
        q = _Query(f"grp:stay:{GONE}")
        await gg.handle_group_callback(q, ctx, q.data, OWNER)
        expect("«✅ Остаться» внёс в свои группу, откуда бота уже удалили — вопрос дня "
               "рвался бы туда", not hist.is_known_chat(GONE))
        expect("владельцу не сказали, что бота в той группе уже нет",
               bool(q.edited) and "уже нет" in q.edited[-1])
        expect(f"итог «🚪 Меня уже нет…» не исчезает через 5 минут: срок "
               f"{deleted.get((OWNER, q.message.message_id))}",
               deleted.get((OWNER, q.message.message_id)) == DONE_TTL)

        # Telegram не ответил — решение не должно ни засчитаться, ни пропасть.
        gg._mark_asked(NETFAIL, "Без связи")
        bot.member_error = RuntimeError("сеть")
        q = _Query(f"grp:stay:{NETFAIL}")
        await gg.handle_group_callback(q, ctx, q.data, OWNER)
        expect("Telegram не ответил на «Остаться», а группа всё равно стала своей",
               not hist.is_known_chat(NETFAIL))
        expect("Telegram не ответил на «Остаться», а группа пропала из ждущих решения — "
               "напоминаний о ней больше не будет, хотя решение не принято",
               str(NETFAIL) in gg._pending())
        expect("Telegram не ответил на «Остаться», а владельцу не сказали нажать ещё раз",
               bool(q.alerts) and "ещё раз" in q.alerts[-1])
        expect("Telegram не ответил на «Остаться», а вопрос поставлен на самоудаление — "
               "решение не принято, и кнопки пропали бы",
               (OWNER, q.message.message_id) not in deleted)
        bot.member_error = None

        with hist._lock:
            conn = hist._get_connection()
            details = [r[0] for r in conn.execute(
                "SELECT details FROM staff_log WHERE action = 'group' AND actor_id = ?",
                (OWNER,)).fetchall()]
        expect(f"в журнал персонала записано решений по группам: {len(details)}, а "
               f"принято два (остаться и выйти)",
               len(details) == 2 and any("остался" in d for d in details)
               and any("вышел" in d for d in details))

    saved_cfg_admins, saved_roles_admins = config.ADMIN_IDS, roles.ADMIN_IDS
    saved_time = gg.time
    saved_schedule_delete = gg.schedule_delete
    saved_settings = {key: hist.get_setting(key, None)
                      for key in (gg.PENDING_KEY, quiz_daily.ACTIVE_KEY)}
    config.ADMIN_IDS = [OWNER]
    roles.ADMIN_IDS = (OWNER,)
    clock = _Clock()
    gg.time = clock
    gg.schedule_delete = _record_delete
    try:
        asyncio.run(scenario(clock))
    finally:
        config.ADMIN_IDS, roles.ADMIN_IDS = saved_cfg_admins, saved_roles_admins
        gg.time = saved_time
        gg.schedule_delete = saved_schedule_delete
        with hist._lock:
            conn = hist._get_connection()
            conn.executemany("DELETE FROM known_chats WHERE chat_id = ?",
                             [(c,) for c in ALL_CHATS])
            conn.execute("DELETE FROM staff_log WHERE actor_id = ?", (OWNER,))
            for key, value in saved_settings.items():
                if value is None:
                    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
                else:
                    conn.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
            conn.commit()

    return problems, (f"{done} проверок: своя группа и личка проходят, чужая и "
                      f"незнакомая молчат, вопрос владельцу раз в сутки, добавил "
                      f"владелец, переезд в супергруппу, удаление, сбой пропускает, "
                      f"кнопки и журнал")


def check_kb_card():
    """
    Кнопки карточки статьи в панели базы знаний (17.09.2026).

    ⚠️ РАДИ ЧЕГО. Кнопка панели без своей ветки в обработчике нажимается и
    МОЛЧА НИЧЕГО НЕ ДЕЛАЕТ: `preflight` сверяет кнопку только с общей веткой
    роутера (приставка `kb_`), а что внутри панели — не видит никто.

    ⚠️ Вторая половина — про саму карточку: это сообщение С ФАЙЛОМ, и
    перерисовать его текстом, как остальные экраны панели, Telegram не даёт.
    Кнопки возврата обязаны ПРИСЫЛАТЬ панель заново; сделанная «как на
    соседних экранах» (kb_open), кнопка снова нажималась бы впустую.
    """
    import asyncio
    import shutil
    import tempfile

    from database import history as hist
    from handlers.admin import panel_rag as rag
    import services.knowledge_store as ks

    problems = []
    done = 0

    def expect(title, ok):
        nonlocal done
        done += 1
        if not ok:
            problems.append(title)

    OWNER = 555000333
    src = pathlib.Path(ROOT, "handlers", "admin", "panel_rag.py").read_text(encoding="utf-8")

    # ── 1. Каждая кнопка карточки разбирается обработчиком ──
    cards = {
        "статья в базе": rag._kb_card_keyboard("7", "approved"),
        "статья в очереди": rag._kb_card_keyboard("7", "pending"),
        "подтверждение удаления": rag._kb_card_keyboard("7", "approved", confirm_delete=True),
    }
    actions = {}
    for what, markup in cards.items():
        for row in markup.inline_keyboard:
            for button in row:
                actions.setdefault(button.callback_data.partition(":")[0], what)
    for action, what in sorted(actions.items()):
        expect(f"кнопка «{action}» ({what}) не разбирается обработчиком панели базы "
               f"знаний — нажатие молча ничего не сделает",
               f'action == "{action}"' in src)
    expect("в карточке статьи нет кнопки возврата к списку раздела",
           "kb_panel" in actions)
    expect("обработчик kb_sections есть, а кнопки «⬅️ К разделам» в карточке статьи "
           "нет — возвращаться к разделам нечем", "kb_sections" in actions)

    # ── 2. Поведение: что кнопки возврата делают на самом деле ──
    art_dir = tempfile.mkdtemp(prefix="c4max-selftest-kbcard-")
    saved_folders = dict(ks._FOLDERS)
    ks._FOLDERS["pending"] = os.path.join(art_dir, "pending")
    ks._FOLDERS["approved"] = os.path.join(art_dir, "approved")
    os.makedirs(ks._FOLDERS["pending"], exist_ok=True)
    os.makedirs(ks._FOLDERS["approved"], exist_ok=True)
    with open(os.path.join(ks._FOLDERS["approved"], "Проверочный_танк.md"), "w",
              encoding="utf-8") as f:
        f.write("# Проверочный танк\n\nПроверочный танк — советский основной боевой "
                "танк (ОБТ) X ранга в War Thunder Mobile. Статья для проверки.\n")

    class _Bot:
        """Telegram без сети: панель, которую бот прислал, ложится в записную книжку."""
        def __init__(self):
            self.sent = []
            self._next_id = 700

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
            self._next_id += 1
            self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})

            class _Sent:
                message_id = self._next_id
            return _Sent()

        async def delete_message(self, chat_id, message_id):
            return True

    class _App:
        def __init__(self):
            self.bot_data = {}

    class _Ctx:
        def __init__(self, bot):
            self.bot = bot
            self.user_data = {}
            self.application = _App()

    class _Query:
        def __init__(self):
            self.edits = 0
            self.answers = []

        async def answer(self, text="", show_alert=False):
            self.answers.append(text)

        async def edit_message_text(self, *a, **kw):
            self.edits += 1

        async def edit_message_reply_markup(self, *a, **kw):
            self.edits += 1

    async def press(data: str, screen: str):
        bot = _Bot()
        ctx = _Ctx(bot)
        ctx.user_data["kb_screen"] = screen
        ctx.user_data["kb_page"] = 3
        query = _Query()
        await rag._handle_kb_callback(query, ctx, data, OWNER, OWNER)
        return bot, ctx, query

    def datas(markup):
        rows = getattr(markup, "inline_keyboard", None) or ()
        return [b.callback_data for row in rows for b in row]

    try:
        bot, ctx, query = asyncio.run(press("kb_sections", "ground"))
        expect("«⬅️ К разделам» не открыл экран разделов — остался прежний раздел",
               ctx.user_data.get("kb_screen") == "")
        expect("«⬅️ К разделам» не сбросил номер страницы — на разделах он не нужен, "
               "а в следующем разделе откроется пустая страница",
               ctx.user_data.get("kb_page") == 0)
        expect("«⬅️ К разделам» не прислал панель новым сообщением",
               len(bot.sent) == 1)
        expect("«⬅️ К разделам» попытался ПЕРЕРИСОВАТЬ карточку: это сообщение с "
               "файлом, Telegram менять его текст не даёт — кнопка молча не сработает",
               query.edits == 0)
        if bot.sent:
            expect(f"после «⬅️ К разделам» пришёл не экран разделов: {datas(bot.sent[0]['markup'])}",
                   any(str(d).startswith("kb_open:") for d in datas(bot.sent[0]["markup"])))

        bot, ctx, query = asyncio.run(press("kb_panel", "ground"))
        expect("«⬅️ К списку» сменил раздел — он обязан вернуть в тот же, откуда "
               "открыли статью", ctx.user_data.get("kb_screen") == "ground")
        expect("«⬅️ К списку» не прислал панель новым сообщением", len(bot.sent) == 1)
    finally:
        ks._FOLDERS.clear()
        ks._FOLDERS.update(saved_folders)
        shutil.rmtree(art_dir, ignore_errors=True)
        with hist._lock:
            conn = hist._get_connection()
            conn.execute("DELETE FROM bot_sent_messages WHERE chat_id = ?", (OWNER,))
            conn.commit()

    return problems, (f"{done} проверок: у каждой кнопки карточки есть обработчик, "
                      f"«К разделам» открывает разделы и присылает панель заново, "
                      f"«К списку» остаётся в своём разделе")


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

    # Все места, где пишут в журнал: панели бота, действия сайта и службы.
    # ⚠️ Службы добавлены 15.09.2026: кнопки чужих групп пишут в журнал из
    # services/group_guard.py, и без этой строки их код проверка не видела бы
    # вовсе — пропала бы подпись, а проверка осталась бы зелёной.
    files = sorted(pathlib.Path(ROOT, "handlers", "admin").glob("*.py"))
    files += sorted(pathlib.Path(ROOT, "services").glob("*.py"))
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
    # ⚠️ Второй ответ НЕВЕРНЫЙ, и это не украшение: без промаха кнопка
    # «Обнулить промахи» на карточке не рисуется (её незачем показывать при
    # 100%), и сверка «кнопка ↔ обработчик» объявила бы обработчик сиротой.
    # Уберёшь эту строку — проверка покраснеет на ровном месте (04.09.2026).
    _hist.add_quiz_attempt(777000111, "проверка", False)
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

    # ── Остатки на счетах ВИДНЫ везде, даже там, где их не правят ──
    #
    # ⚠️ ЗАВЕДЕНО ПО СЛЕДАМ СВОЕЙ ЖЕ ОШИБКИ (14.09.2026). Убирая правку остатка
    # DeepSeek, я убрал со страницы сайта строку целиком — вместе с формой
    # пропал и САМ ПОКАЗ цифры, хотя её убирать не просили. В боте блок
    # остался, на сайте исчез; заметил это Максим, а не проверка.
    # Поэтому сверяется ПОКАЗ, а не правка: у каждого провайдера со счётом
    # цифра обязана быть на обоих экранах — и в панели бота, и на странице.
    from config import PROVIDERS as _PROV
    from handlers.admin.panel_balance import _build_balance_panel

    # ⚠️ ИСКАТЬ ОДНО ИМЯ ПРОВАЙДЕРА НЕДОСТАТОЧНО, и это выяснилось сразу же:
    # первая версия проверки искала слово «DeepSeek» на странице и НЕ ПОКРАСНЕЛА
    # на нарочно убранном остатке — слово осталось в соседней строке
    # «Потрачено: Deepseek». Поэтому ищем связку «остаток + имя», а в боте —
    # строку остатка ВНУТРИ блока этого провайдера.
    money_html = pages._money_block("csrf").lower()
    bot_text, _ = _build_balance_panel()
    bot_blocks = bot_text.split("───────────────────────────")
    for _pid, _meta in _PROV.items():
        if not _meta.get("balance_key"):
            continue
        done += 2
        if f"остаток на счету: {_meta['title']}".lower() not in money_html:
            problems.append(f"остаток {_meta['title']} пропал со страницы «Счета и квоты» "
                            f"на сайте — правку убрать можно, показ нельзя")
        block = next((b for b in bot_blocks if _meta["title"] in b), "")
        if "Остаток на счету" not in block:
            problems.append(f"остаток {_meta['title']} пропал с экрана «Счета и квоты» "
                            f"в боте — правку убрать можно, показ нельзя")

    return problems, (f"{done} проверок: порядок копий, ключи сборки вопросов, "
                      f"формы ↔ обработчики, разметка, дайджест, остатки видны везде")


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


def check_quiz_score():
    """
    Ручная правка счёта викторины из карточки участника (04.09.2026, просьба
    Максима — до неё счёт правился только руками в базе на сервере).

    ⚠️ Ради чего проверка существует. Тут три тихих способа соврать, и ни один
    не падает сам по себе:
      • пропустить «верных больше попыток» — /rank нарисует «110%», а полоска
        прогресса уедет за край экрана;
      • не пересчитать звание — карточка покажет новый счёт со старым званием;
      • забыть НЕДЕЛЬНЫЙ СНИМОК ДАЙДЖЕСТА. Он самый коварный: счёт правится
        в личке, а врёт потом дайджест В ГРУППЕ — «за неделю +115 ответов»,
        которых человек не давал, или «0 за неделю» неделями после понижения.

    Отдельно сверяется, что бот и сайт зовут ОДНИ правила: два набора запретов
    начнут принимать разные числа, и заметит это только тот, кому счёт
    перепишут не так.
    """
    import json as _json

    from config import QUIZ_RANKS
    from database import history as _hist
    from handlers.admin.panel_users import (_QUIZ_FIELDS, _QUIZ_SCORE_MAX,
                                            _set_quiz_score, fix_quiz_misses,
                                            quiz_score_summary)

    UID = 777000222
    problems = []
    done = 0

    def expect(title, got, want):
        nonlocal done
        done += 1
        if got != want:
            problems.append(f"{title}: ожидалось {want!r}, вышло {got!r}")

    def refuse(title, **kwargs):
        """Правка ОБЯЗАНА отказать — и словами, а не пустым исключением."""
        nonlocal done
        done += 1
        try:
            _set_quiz_score(UID, **kwargs)
        except ValueError as e:
            if not str(e).strip():
                problems.append(f"{title}: отказ без объяснения")
            return
        problems.append(f"{title}: правка ПРОШЛА — так нельзя")

    def score():
        st = _hist.get_user_stats(UID)
        return st["correct_answers"], st["total_attempts"]

    try:
        # ── 1. Три запрета ──
        _set_quiz_score(UID, correct=10, attempts=20)
        refuse("верных больше попыток", correct=21)
        refuse("отрицательные верные", correct=-1)
        refuse("отрицательные попытки", attempts=-5)
        refuse("выше потолка", attempts=_QUIZ_SCORE_MAX + 1)
        expect("после отказов счёт не поехал", score(), (10, 20))

        # Ровно на потолке — можно: запрет «больше», а не «столько».
        _set_quiz_score(UID, correct=_QUIZ_SCORE_MAX, attempts=_QUIZ_SCORE_MAX)
        expect("потолок берётся", score(), (_QUIZ_SCORE_MAX, _QUIZ_SCORE_MAX))

        # ── 2. «Обнулить промахи» ──
        _set_quiz_score(UID, correct=285, attempts=289)
        changed = fix_quiz_misses(UID)
        expect("промахи убраны", score(), (289, 289))
        expect("точность стала ровно 100", _hist.get_user_stats(UID)["success_rate"], 100.0)
        done += 1
        if not changed or quiz_score_summary(*changed) != "285 из 289 → 289 из 289":
            problems.append(f"строка «было → стало» разъехалась: "
                            f"{changed and quiz_score_summary(*changed)!r}")
        expect("повторное нажатие — уже нечего убирать", fix_quiz_misses(UID), None)

        # ── 3. Звание едет за счётом ──
        # Границы взяты из config.QUIZ_RANKS, а не переписаны сюда числами:
        # переставят лестницу — проверка поедет вместе с ней.
        ranks = {r["name"]: r for r in QUIZ_RANKS}
        maj = ranks["Майор"]
        _set_quiz_score(UID, correct=maj["min"] - 1, attempts=_QUIZ_SCORE_MAX)
        expect("на ступень ниже — не Майор",
               _hist.get_user_stats(UID)["rank"] == "Майор", False)
        _set_quiz_score(UID, correct=maj["min"])
        expect("нижняя граница — уже Майор", _hist.get_user_stats(UID)["rank"], "Майор")
        _set_quiz_score(UID, correct=maj["max"] + 1)
        expect("выше верхней границы — уже не Майор",
               _hist.get_user_stats(UID)["rank"] == "Майор", False)
        _set_quiz_score(UID, correct=0, attempts=0)
        expect("обнуление возвращает первое звание",
               _hist.get_user_stats(UID)["rank"], QUIZ_RANKS[0]["name"])

        # ── 4. Недельный снимок дайджеста едет вместе со счётом ──
        # Иначе правка счёта в личке превращается во вранье в ГРУППЕ.
        _hist.set_setting("group_digest_quiz", _json.dumps({str(UID): 100, "1": 7}))
        # Счёт дайджеста берём ДО и ПОСЛЕ правки: в базе проверок живут и
        # другие игроки, и их вклад в «за неделю» нам не интересен — важно,
        # что правка не добавила НИЧЕГО.
        week_before, _b, _n = _quiz_week_for_check()
        _set_quiz_score(UID, correct=140, attempts=200)
        snap = _json.loads(_hist.get_setting("group_digest_quiz", "{}"))
        expect("снимок недели не поехал за правкой", snap.get(str(UID)), 140)
        expect("правка одного человека тронула чужую строку снимка",
               snap.get("1"), 7)

        week_after, _b, _n = _quiz_week_for_check()
        expect("правка счёта засчиталась дайджесту как ответы за неделю",
               week_after, week_before)

        # Снимка нет вовсе — первый выпуск дайджеста намеренно считает всё
        # накопленное, и создавать снимок раньше времени нельзя.
        _hist.delete_setting("group_digest_quiz")
        _set_quiz_score(UID, correct=141)
        expect("снимок создан там, где его не должно быть",
               _hist.get_setting("group_digest_quiz", ""), "")

        # ── 5. Сайт правит счёт ТЕМИ ЖЕ правилами, что и бот ──
        # ⚠️ Проверяется ПОВЕДЕНИЕМ, а не поиском имени по файлу. Первая
        # редакция этой проверки искала «_set_quiz_score» в web/actions.py —
        # и проспала подделку, в которой имя осталось на месте, а считалось
        # по-своему (сломано нарочно 04.09.2026, проверка не покраснела).
        from web import actions as _wact
        _set_quiz_score(UID, correct=5, attempts=10)
        done += 1
        try:
            _wact.user_quiz_score(1, UID, correct=11)
            problems.append("сайт принял «верных больше попыток» — запреты "
                            "бота и сайта разъехались")
        except _wact.ActionError:
            pass
        expect("отказ сайта не должен менять счёт", score(), (5, 10))
        _wact.user_quiz_fix(1, UID)
        expect("«обнулить промахи» на сайте не сработало", score(), (10, 10))
    finally:
        with _hist._lock:
            _hist._get_connection().execute("DELETE FROM quiz_stats WHERE user_id=?", (UID,))
            _hist._get_connection().commit()
        _hist.delete_setting("group_digest_quiz")

    # ── 6. Кнопка в карточке есть, и видит её только владелец ──
    # ⚠️ Опять же ПОВЕДЕНИЕМ: поиск «usr:quiz:» по файлу эту кнопку не
    # сторожит — такая же строка стоит у «Отмены» на экране ввода, и убранная
    # из карточки кнопка проверку бы не уронила (сломано нарочно 04.09.2026).
    from handlers.admin.common import _filter_keyboard
    from handlers.admin.panel_users import _actions_keyboard
    from services import roles as _roles

    rows = _actions_keyboard(5)
    on_card = [b.callback_data for row in rows for b in row]
    expect("кнопки «Счёт викторины» в карточке участника больше нет",
           "usr:quiz:5" in on_card, True)

    for data in ("usr:quiz:5", "usr:quizfix:5", "usr:quizset:5:correct",
                 "usr:quizzero:5", "usr:quizzerogo:5"):
        expect(f"право на «{data}»", _roles.perm_for_callback(data), "owner")

    # Право в таблице — половина дела: кнопку с экрана убирает фильтр
    # клавиатуры, и молчащий фильтр показал бы её модератору как ни в чём
    # не бывало. Модератору с «⚙️ Правкой карточек» соседнее «Почётное
    # звание» остаться ОБЯЗАНО — иначе проверка поймала бы не то.
    MOD = 777000333
    try:
        _roles.make_moderator(MOD, 1)
        _roles.grant_perm(MOD, "cards_edit", True)
        seen = [b.callback_data for row in _filter_keyboard(rows, MOD) for b in row]
        expect("модератор видит кнопку счёта викторины", "usr:quiz:5" in seen, False)
        expect("у модератора заодно пропало почётное звание",
               "usr:rank:5" in seen, True)
    finally:
        _roles.unmake_moderator(MOD)

    # Поля правки и их ключи не разъехались с тем, что отдаёт get_user_stats.
    stats_keys = set(_hist.get_user_stats(1).keys())
    for code, (_title, key, _example) in _QUIZ_FIELDS.items():
        expect(f"поле «{code}» смотрит в несуществующий ключ счёта",
               key in stats_keys, True)

    return problems, (f"{done} проверок: три запрета, обнуление промахов, "
                      f"звание по границам лестницы, недельный снимок "
                      f"дайджеста, общие правила бота и сайта, права")


def _quiz_week_for_check() -> tuple:
    """Сколько дайджест засчитает «за неделю» прямо сейчас (без сохранения)."""
    from services.group_digest import _quiz_week
    return _quiz_week(save=False)


def check_rag_notice():
    """
    Уведомления о базе знаний: «✅ снова полная» исчезает само, а «⚠️ неполная»
    висит до конца инцидента и стирается отбоем (просьба Максима 04.09.2026).

    ⚠️ Ради чего проверка существует. Ежечасный цикл добора не видит НИ ОДНА
    другая проверка: preflight смотрит кнопки и импорты, остальные группы
    selftest сюда не заглядывают. Ошибка здесь всплывает не сразу, а через час
    или через сутки, и выглядит не как поломка, а как «бот замолчал» или
    «опять мусор в личке» — руками такое ловится случайно и поздно.

    ⚠️ Проверяется НЕ пара вспомогательных функций, а сам цикл: он запускается
    целиком, с поддельным Telegram и поддельным индексом. Проверить отдельно
    send_notice и drop_notices означало бы проверить механизм и не заметить,
    что его перестали звать из нужного места.

    Срок самоудаления берётся заведомо НЕ ТОТ, что стоит в config: с настоящими
    пятью минутами зашитое в код число прошло бы проверку насквозь.
    """
    import asyncio

    import config as cfg
    import utils as u
    import jobs.rag as jr
    from services import rag as rag_mod

    problems = []
    done = 0
    OWNER, SECOND = 4242, 4343
    TTL = 111  # нарочно не 300: зашитое в код число обязано провалиться

    class _Stop(Exception):
        """Останов сценария. Бросается из sleep — он стоит ВНЕ try цикла."""

    def run(script, admins=(OWNER,), fail_for=()):
        """
        Прогоняет цикл по сценарию. script — шаги вида
        (сколько статей без векторов, что вернёт синхронизация).
        Возвращает: что отправлено, что поставлено на самоудаление, что стёрто.
        """
        sent, deletes, dropped = [], [], []
        steps = iter(script)
        current = {}

        class _Msg:
            def __init__(self, mid):
                self.message_id = mid

        class _Bot:
            def __init__(self):
                self.n = 0

            async def send_message(self, chat_id=None, text=None, **kw):
                if chat_id in fail_for:
                    raise RuntimeError("админ заблокировал бота")
                self.n += 1
                sent.append((chat_id, text, self.n))
                return _Msg(self.n)

            async def delete_message(self, chat_id=None, message_id=None):
                dropped.append((chat_id, message_id))

        class _App:
            bot = _Bot()
            bot_data = {}

        class _Sleeper:
            """Подменяет модуль asyncio внутри jobs/rag.py: час ждать незачем."""
            calls = 0

            @staticmethod
            async def sleep(_):
                _Sleeper.calls += 1
                if _Sleeper.calls > len(script):
                    raise _Stop()

            get_running_loop = staticmethod(asyncio.get_running_loop)

        def _lag():
            current["step"] = next(steps)
            return current["step"][0]

        def _sync():
            return current["step"][1]

        saved = (cfg.RAG_ENABLED, cfg.ADMIN_IDS, cfg.RAG_NOTICE_TTL_SEC,
                 rag_mod.index_lag, rag_mod.sync_knowledge_base,
                 u.schedule_delete, jr.asyncio)
        try:
            cfg.RAG_ENABLED = True
            cfg.ADMIN_IDS = list(admins)
            cfg.RAG_NOTICE_TTL_SEC = TTL
            rag_mod.index_lag = _lag
            rag_mod.sync_knowledge_base = _sync
            u.schedule_delete = lambda bot, chat, mid, delay: deletes.append((chat, mid, delay))
            jr.asyncio = _Sleeper
            try:
                asyncio.run(jr.rag_catchup_loop(_App()))
            except _Stop:
                pass
        finally:
            (cfg.RAG_ENABLED, cfg.ADMIN_IDS, cfg.RAG_NOTICE_TTL_SEC,
             rag_mod.index_lag, rag_mod.sync_knowledge_base,
             u.schedule_delete, jr.asyncio) = saved
        return sent, deletes, dropped

    # ── 1. Рутина: поправили статью, цикл её дотянул ────────────────────────
    # Именно этот случай Максим и видит чаще всего — инцидента не было вовсе.
    sent, deletes, dropped = run([(1, (92, 92))])
    done += 1
    if len(sent) != 1 or "✅" not in sent[0][1]:
        problems.append(f"после успешного добора не пришло «✅ снова полная»: {sent!r}")
    else:
        done += 1
        if "92 из 92" not in sent[0][1]:
            problems.append(f"в отбое потерялись цифры: {sent[0][1]!r}")
    done += 1
    if not deletes:
        problems.append("«✅ снова полная» НЕ поставлено на самоудаление — "
                        "сообщение останется висеть в личке навсегда")
    else:
        chat, mid, delay = deletes[0]
        done += 3
        if chat != OWNER:
            problems.append(f"самоудаление назначено не в тот чат: {chat}")
        if sent and mid != sent[0][2]:
            problems.append(f"самоудаление назначено не тому сообщению: {mid}")
        if delay != TTL:
            problems.append(f"срок самоудаления {delay} не поехал за настройкой "
                            f"RAG_NOTICE_TTL_SEC ({TTL}) — похоже, число зашито в код")
    done += 1
    if dropped:
        problems.append(f"без инцидента цикл полез что-то удалять: {dropped!r}")

    # ── 2. Инцидент: тревога висит, отбой её уносит ─────────────────────────
    sent, deletes, dropped = run([(5, (80, 92)), (5, (85, 92)), (3, (92, 92))])
    done += 1
    if len(sent) != 2:
        problems.append(f"за инцидент ждали ровно два сообщения (тревога и отбой), "
                        f"пришло {len(sent)}: {[s[1][:40] for s in sent]!r}")
    else:
        warn, ok = sent
        done += 2
        if "⚠️" not in warn[1]:
            problems.append(f"первым сообщением инцидента пришла не тревога: {warn[1][:60]!r}")
        if "✅" not in ok[1]:
            problems.append(f"вторым сообщением инцидента пришёл не отбой: {ok[1][:60]!r}")
        done += 1
        if any(mid == warn[2] for _, mid, _ in deletes):
            problems.append("тревоге «⚠️ база неполная» назначено самоудаление — "
                            "она пропадёт посреди инцидента, пока база ещё сломана")
        done += 1
        if not any(mid == ok[2] and delay == TTL for _, mid, delay in deletes):
            problems.append(f"отбой не поставлен на самоудаление по сроку: {deletes!r}")
        done += 1
        if (OWNER, warn[2]) not in dropped:
            problems.append("отбой не стёр прежнюю тревогу — «⚠️ база неполная» "
                            f"останется висеть навсегда: стёрто {dropped!r}")

    # ── 3. Базу починили мимо цикла (кнопкой «Пересобрать RAG») ─────────────
    sent, deletes, dropped = run([(5, (80, 92)), (0, None)])
    done += 1
    if len(sent) != 2 or "✅" not in sent[-1][1]:
        problems.append(f"база стала полной мимо цикла, а обещанный отбой не пришёл: "
                        f"{[s[1][:40] for s in sent]!r}")
    done += 1
    if sent and (OWNER, sent[0][2]) not in dropped:
        problems.append("база починена мимо цикла, а тревога осталась висеть: "
                        f"стёрто {dropped!r}")

    # ── 4. Админов несколько, одному не доходит ────────────────────────────
    sent, deletes, dropped = run([(1, (92, 92))], admins=(OWNER, SECOND), fail_for=(OWNER,))
    done += 1
    if len(sent) != 1 or sent[0][0] != SECOND:
        problems.append(f"отказ отправки одному админу утянул за собой второго: {sent!r}")
    done += 1
    if len(deletes) != 1 or deletes[0][0] != SECOND:
        problems.append(f"самоудаление назначено не тому, кто получил сообщение: {deletes!r}")

    return problems, (f"{done} проверок: отбой уходит по сроку из настройки, "
                      f"тревога висит до конца инцидента и стирается отбоем, "
                      f"починка мимо цикла, отказ отправки одному из админов")


def check_photo_route():
    """
    Куда уходит ФОТО: к активной модели или в обход, по цепочке Gemini.

    ⚠️ Ради чего проверка существует. До 04.09.2026 в этот маршрут не смотрела
    НИ ОДНА проверка — ни preflight, ни selftest. А ошибиться тут можно тихо:
    DeepSeek на картинку НЕ РУГАЕТСЯ. Запрос проходит с кодом 200, картинка
    молча выбрасывается, и человек получает уверенный ответ вслепую вместо
    отказа (проверено живым запросом: 106 токенов входа с картинкой против 101
    без неё). То есть «слепая модель в цепочке фото» — не ошибка с сообщением,
    а враньё без единого следа в логе.

    ⚠️ Проверяется НЕ поле "vision" в конфиге, а КУДА РЕАЛЬНО УШЁЛ ЗАПРОС:
    запускается настоящий `_gemini_chat_request`, а провайдерские отправлялки
    подменены на записные книжки. Сверять флаг с флагом значило бы проверять,
    что конфиг равен сам себе.
    """
    import time as _time
    import types

    import config as cfg
    from database import history as hist
    from services import gemini as g

    problems = []
    done = 0
    MSG = [{"role": "user", "content": "неважно"}]

    def route(active: str, has_image: bool) -> list:
        """Возвращает список моделей, которых РЕАЛЬНО попробовали по порядку."""
        tried = []

        def _fake_provider(model_name, messages, thinking_override=None):
            tried.append(model_name)
            return None          # «не ответила» — цепочка идёт дальше

        class _Resp:
            status_code = 503

            @staticmethod
            def raise_for_status():
                raise RuntimeError("подставной отказ")

        class _Session:
            @staticmethod
            def post(url, json=None, **kw):
                tried.append((json or {}).get("model"))
                return _Resp()

        saved = (g._qwen_chat_request, g._deepseek_chat_request,
                 g._xiaomi_chat_request, g._http, g._notify_models_failed, g.time)
        try:
            g._qwen_chat_request = _fake_provider
            g._deepseek_chat_request = _fake_provider
            g._xiaomi_chat_request = _fake_provider
            g._http = lambda: _Session()
            g._notify_models_failed = lambda *a, **kw: None
            # Ждать по-настоящему нельзя: между попытками стоят паузы в секунды,
            # а проверка гоняется на каждой выкатке.
            g.time = types.SimpleNamespace(sleep=lambda *_: None,
                                           perf_counter=_time.perf_counter)
            hist.set_setting("active_model", active)
            g._gemini_chat_request(MSG, kind="проверка", has_image=has_image)
        finally:
            (g._qwen_chat_request, g._deepseek_chat_request,
             g._xiaomi_chat_request, g._http, g._notify_models_failed, g.time) = saved
        # Активную пробуют дважды — в списке она встретится подряд, схлопываем.
        out = []
        for m in tried:
            if not out or out[-1] != m:
                out.append(m)
        return out

    def blind(models) -> list:
        return [m for m in models
                if not cfg.AVAILABLE_MODELS.get(m, {}).get("vision", False)]

    saved_active = hist.get_setting("active_model", "")
    try:
        # ── 1. Активные ЗРЯЧИЕ не-Gemini: фото должна получить сама активная ──
        # ⚠️ Все перечислены поимённо НАМЕРЕННО. Каждая проверена живыми
        # запросами на игровых скриншотах и включена по прямой просьбе
        # Максима: две Qwen 04.09.2026 (обе переписали панель ТТХ и верно
        # назвали флаг страны), deepseek-flash — 10.09.2026 (12 значений из 12
        # за 13 с, страну назвала сама). Вернёт любая из них пометку «слепая» —
        # эта строка обязана покраснеть.
        # qwen3.8-flash добавлена 21.09.2026: две панели ТТХ переписаны без
        # ошибок за 10 с, флаг Китая назван верно (флаг Омана — нет, но решение
        # Максима «активная модель отвечает сама» от этого не меняется).
        for seeing in ("qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash", "deepseek-flash"):
            chain = route(seeing, has_image=True)
            done += 3
            if not chain or chain[0] != seeing:
                problems.append(f"фото при зрячей активной ушло мимо неё: первой пробовали "
                                f"{chain[0] if chain else '— никого —'}, ждали {seeing}")
            if blind(chain):
                problems.append(f"в цепочке фото ({seeing}) оказались СЛЕПЫЕ модели: "
                                f"{blind(chain)} — картинка уйдёт в пустоту, а человек "
                                f"получит ответ вслепую")
            if len(chain) < 2:
                problems.append(f"у фото не осталось подстраховки ({seeing}): цепочка {chain}")

        # ── 2. Активная СЛЕПАЯ: её не должны пробовать вовсе ──
        for model in ("qwen3.7-max", "mimo-v2.5-pro"):
            chain = route(model, has_image=True)
            done += 2
            if model in chain:
                problems.append(f"фото отправили СЛЕПОЙ активной модели {model} — "
                                f"обход (vision-reroute) не сработал")
            if blind(chain):
                problems.append(f"обход фото у {model} привёл к слепым моделям: {blind(chain)}")

        # ── 3. ТЕКСТ у слепой активной идёт ЕЙ, а не в обход ──
        # Иначе обход фото тихо утащил бы к Gemini всю переписку.
        chain = route("qwen3.7-max", has_image=False)
        done += 1
        if not chain or chain[0] != "qwen3.7-max":
            problems.append(f"текст у слепой активной ушёл мимо неё: {chain} — "
                            f"обход сработал там, где картинки нет вовсе")

        # ── 4. Активная Gemini: фото остаётся у неё ──
        chain = route(cfg.FALLBACK_MODEL, has_image=True)
        done += 2
        if not chain or chain[0] != cfg.FALLBACK_MODEL:
            problems.append(f"фото при активной Gemini ушло мимо неё: {chain}")
        if blind(chain):
            problems.append(f"в запасе у активной Gemini есть слепые: {blind(chain)}")
    finally:
        hist.set_setting("active_model", saved_active)

    return problems, (f"{done} проверок: фото идёт зрячей активной, слепую обходит, "
                      f"в цепочке нет слепых ни у кого, текст обходом не задет")


def check_stopwatch():
    """
    Время в строке «Ответ от …» и в журнале разговоров — время САМОЙ ответившей
    модели; после отказов весь перебор стоит рядом отдельным числом (15.09.2026).

    ⚠️ Ради чего проверка существует. Секундомер запускался один раз на всю
    очередь подстраховки и при переходе к запасной не сбрасывался: DeepSeek
    висела 90 с, Gemini 3.8 Flash отвечала за 7 — в логе стояло «Ответ от
    gemini-3.8-flash за 98.9 с». Ошибка жила в трёх очередях (текст, голосовое,
    видео) и в журнале разговоров. Живьём её не поймать — подстраховка
    включается только на отказе, — а по этим числам выбирали, кого ставить
    первым в очередь.

    ⚠️ Гоняются НАСТОЯЩИЕ очереди на поддельных часах: каждая «модель» двигает
    их ровно на своё время. Сверяются СТРОКИ, которые ушли бы в лог и в журнал,
    а не переменные внутри: число могло бы считаться верно, а в строку уходить
    другое. Письмо владельцу тоже двигает часы — попадёт его время в перебор,
    проверка это назовёт.

    ⚠️ Ожидаемый перебор — до конца последней попытки по поддельным часам, а НЕ
    сумма пауз, переписанная сюда: паузы между попытками — дело очереди, и
    проверка секундомера не должна краснеть от их смены.

    ⚠️ Поддельное письмо владельцу уходит ТОЛЬКО при отказах, как настоящее.
    Иначе проверка ловила бы «время письма» там, где письма в жизни нет, —
    и краснела бы на верных строках.
    """
    import requests
    import config as cfg
    from database import history as hist
    from services import chat_log
    from services import gemini as g

    problems = []
    done = 0
    NOTIFY_SEC = 3.0          # сколько «идёт» письмо владельцу на поддельных часах
    USER = -777031            # свой id: чужую переписку во временной базе не трогаем
    MSG = [{"role": "user", "content": "неважно"}]

    class Clock:
        def __init__(self): self.t = 1000.0
        def monotonic(self): return self.t
        def perf_counter(self): return self.t
        def sleep(self, s): self.t += s
        def time(self): return 1700000000.0

    clock = Clock()

    class Log:
        """Вместо логгера: строки ровно такими, какими ушли бы в лог."""
        def __init__(self): self.lines = []

        def _note(self, msg, *args, **kw):
            # Настоящий logging на кривом формате не падает — не падаем и мы,
            # но строку помечаем: разбор её не узнает и назовёт.
            try:
                self.lines.append(msg % args if args else msg)
            except Exception:
                self.lines.append(f"ОШИБКА ФОРМАТА СТРОКИ ЛОГА: {msg!r} % {args!r}")

        debug = info = warning = error = exception = _note

    log = Log()
    written = []              # куски журнала разговоров
    step_ends = []            # когда кончилась каждая попытка — по поддельным часам
    answered = []             # кто из «моделей» ответил

    # Поведение «моделей» по провайдерам: очередь исходов попыток (секунд,
    # "ok" | "503" | "timeout"); последний исход повторяется. Провайдер без
    # сценария отказывает сразу — неожиданный звонок не зависнет и не ответит.
    script = {}

    def _step(provider, model):
        queue = script.get(provider) or [(0.0, "503")]
        seconds, outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        clock.t += seconds
        step_ends.append(clock.t)
        if outcome == "timeout":
            raise requests.exceptions.Timeout(f"{model}: подставной таймаут")
        if outcome == "503":
            raise requests.exceptions.HTTPError(f"503 Server Error: подставной отказ {model}")
        answered.append(model)

    # Один ответ на оба формата: OpenAI-совместимый (choices) и родной Gemini
    # (candidates) — голосовое и видео читают второй.
    def _answer_json():
        return {
            "choices": [{"message": {"content": "подставной ответ"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "candidates": [{"content": {"parts": [{"text": "подставной ответ"}]}}],
            "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 15},
        }

    def _provider_request(provider):
        def fake(model_name, messages, thinking_override=None):
            _step(provider, model_name)
            return _answer_json()
        return fake

    class _Resp:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            return _answer_json()

    class _Session:
        @staticmethod
        def post(url, json=None, **kw):
            found = re.search(r"/models/([^:/]+):", url)
            _step("gemini", found.group(1) if found else (json or {}).get("model", "?"))
            return _Resp()

    def _notify(kind, failures, **kw):
        if failures:          # настоящее письмо без отказов не уходит
            clock.t += NOTIFY_SEC

    def run(fn):
        """Один сценарий с чистого листа. Возвращает момент его начала."""
        log.lines.clear()
        written.clear()
        step_ends.clear()
        answered.clear()
        t0 = clock.t
        fn()
        return t0

    answer_line = re.compile(r"Ответ от (\S+) за (\d+\.\d) с(?: \(([^)]*)\))?")
    seconds_in = re.compile(r"(\d+\.\d) с")

    def from_log():
        """(модель, своё время, весь перебор | None, скобки) из строки «Ответ от …»."""
        lines = [ln for ln in log.lines if "Ответ от " in ln]
        if len(lines) != 1:
            return None, f"строк «Ответ от …» в логе {len(lines)}, а должна быть одна"
        found = answer_line.search(lines[0])
        if not found:
            return None, f"строку лога не разобрать: {lines[0]!r}"
        notes = found.group(3) or ""
        total = None
        for part in notes.split(", "):
            sec = seconds_in.search(part)
            if sec:
                total = float(sec.group(1))
        return (found.group(1), float(found.group(2)), total, notes), ""

    def from_chat_log():
        """(модель, своё время | None, весь перебор | None) из «ВЕРНУЛА МОДЕЛЬ (…)»."""
        found = re.search(r"ВЕРНУЛА МОДЕЛЬ \((.*)\) ──", "".join(written))
        if not found:
            return None, "в журнале разговоров нет строки «ВЕРНУЛА МОДЕЛЬ»"
        model, *parts = found.group(1).split(", ")
        own = total = None
        for part in parts:
            sec = seconds_in.search(part)
            if not sec:
                continue
            if part.strip() == sec.group(0):      # голое «7.0 с» — время самой модели
                own = float(sec.group(1))
            else:                                 # с подписью — весь перебор
                total = float(sec.group(1))
        return (model, own, total, ""), ""

    def near(a, b):
        return a is not None and b is not None and abs(a - b) < 0.05

    def expect(title, parsed, err, model, own, t0=None):
        """own — сколько работала ответившая (None — не ответил никто).
        t0 — начало сценария, если по дороге были отказы: тогда в строке обязан
        стоять весь перебор; без t0 отказов не было и второго числа быть не должно."""
        nonlocal done
        done += 2
        if parsed is None:
            problems.append(f"{title}: {err}")
            return
        got_model, got_own, got_total, _ = parsed
        total = None
        if t0 is not None:
            if not step_ends:
                problems.append(f"{title}: ни одна модель не вызывалась — перебора не было")
                return
            total = step_ends[-1] - t0
        if model is not None and got_model != model:
            problems.append(f"{title}: в строке стоит {got_model}, а ответила {model}")
        if own is None:
            if got_own is not None:
                problems.append(f"{title}: не ответил никто, а записано время модели "
                                f"{got_own} с — будто она ответила")
        elif not near(got_own, own):
            hint = ""
            if total is not None and (near(got_own, total) or near(got_own, total + NOTIFY_SEC)):
                hint = " — это время ВСЕЙ очереди подстраховки под именем последней модели"
            problems.append(f"{title}: у ответившей модели записано "
                            f"{'ничего' if got_own is None else f'{got_own} с'}, "
                            f"а она работала {own:.1f} с{hint}")
        if total is None:
            if got_total is not None:
                problems.append(f"{title}: отказов не было, а в строке стоит общее время "
                                f"{got_total} с")
        elif got_total is None:
            problems.append(f"{title}: были отказы, а общего времени перебора в строке нет")
        elif not near(got_total, total):
            hint = " — в него попало письмо владельцу" if near(got_total, total + NOTIFY_SEC) else ""
            problems.append(f"{title}: весь перебор записан как {got_total} с, "
                            f"а занял {total:.1f} с{hint}")

    def expect_label(title, parsed, label):
        nonlocal done
        done += 1
        if parsed is not None and label not in parsed[3]:
            problems.append(f"{title}: из строки пропала пометка «{label}»: ({parsed[3]})")

    def model_of(provider):
        return next((m for m, info in cfg.AVAILABLE_MODELS.items()
                     if info.get("provider", "gemini") == provider), None)

    # Медленная активная — любая не-Gemini: у неё запасные из Gemini, как в жизни.
    slow = model_of("deepseek") or model_of("qwen") or model_of("xiaomi") or cfg.FALLBACK_MODEL
    slow_provider = g._provider_of(slow)

    def fallback_script(first_sec, first_outcome, answer_sec):
        """Первая попытка медленной активной — first_*, запасная Gemini отвечает за answer_sec."""
        script.clear()
        if slow_provider == "gemini":
            script["gemini"] = [(first_sec, first_outcome), (answer_sec, "ok")]
        else:
            script[slow_provider] = [(first_sec, first_outcome)]
            script["gemini"] = [(answer_sec, "ok")]

    saved = {name: getattr(g, name) for name in (
        "time", "logger", "_http", "_qwen_chat_request", "_deepseek_chat_request",
        "_xiaomi_chat_request", "_notify_models_failed", "_notify_chain_dead",
        "_build_proactive_parts", "RAG_ENABLED")}
    saved_hist = {name: getattr(hist, name) for name in (
        "register_api_call", "add_provider_cost", "spend_qwen_tokens", "add_messages")}
    saved_write = chat_log._write
    saved_active = hist.get_setting("active_model", "")
    try:
        g.time = clock
        g.logger = log
        g._http = lambda: _Session()
        g._qwen_chat_request = _provider_request("qwen")
        g._deepseek_chat_request = _provider_request("deepseek")
        g._xiaomi_chat_request = _provider_request("xiaomi")
        g._notify_models_failed = _notify
        g._notify_chain_dead = lambda *a, **kw: None
        g._build_proactive_parts = lambda *a, **kw: (["характер"], "стенограмма", ["запрос"])
        # Поиск по базе на голосовом и видео сам зовёт модели — здесь он лишний шум.
        g.RAG_ENABLED = False
        for name in saved_hist:
            setattr(hist, name, lambda *a, **kw: None)
        chat_log._write = written.append

        # ── 1. Ответила сама активная: время прежнее, скобок нет ──
        # Каждый провайдер пишет СВОЮ строку лога — проверяются все четыре.
        for provider in ("deepseek", "qwen", "xiaomi", "gemini"):
            model = cfg.FALLBACK_MODEL if provider == "gemini" else model_of(provider)
            if not model:
                continue
            hist.set_setting("active_model", model)
            script.clear()
            script[provider] = [(5.0, "ok")]
            run(lambda: g._gemini_chat_request(MSG, kind="проверка"))
            parsed, err = from_log()
            expect(f"текст, {model} ответила сама", parsed, err, model, 5.0)

        # ── 2. Активная зависла, ответила запасная ──
        hist.set_setting("active_model", slow)
        fallback_script(90.0, "timeout", 7.0)
        t0 = run(lambda: g._gemini_chat_request(MSG, kind="проверка"))
        parsed, err = from_log()
        expect("текст, запасная после зависшей активной", parsed, err,
               answered[-1] if answered else None, 7.0, t0)

        # ── 3. Первая попытка той же модели отказала, вторая ответила ──
        hist.set_setting("active_model", cfg.FALLBACK_MODEL)
        script.clear()
        script["gemini"] = [(2.0, "503"), (5.0, "ok")]
        t0 = run(lambda: g._gemini_chat_request(MSG, kind="проверка"))
        parsed, err = from_log()
        expect("текст, повтор той же модели после отказа", parsed, err,
               cfg.FALLBACK_MODEL, 5.0, t0)

        # ── 4–5. Голосовое и видео в личке ──
        hist.set_setting("active_model", slow)
        for label, ask, chain in (
                ("аудио", lambda: g.ask_gemini_audio(USER, USER, "QQ"),
                 [m for m in cfg.AUDIO_FALLBACK_CHAIN if m in cfg.AVAILABLE_MODELS]),
                ("видео", lambda: g.ask_gemini_video(USER, USER, "QQ"),
                 [m for m in cfg.VIDEO_FALLBACK_CHAIN
                  if cfg.AVAILABLE_MODELS.get(m, {}).get("video")])):
            script.clear()
            script["gemini"] = [(4.0, "ok")]
            run(ask)
            parsed, err = from_log()
            expect(f"{label}, ответила первая модель очереди", parsed, err,
                   answered[-1] if answered else None, 4.0)
            expect_label(f"{label}, ответила первая модель очереди", parsed, label)

            if len(chain) < 2:
                continue          # подстраховки нет — и перебора нет
            script.clear()
            script["gemini"] = [(30.0, "503"), (4.0, "ok")]
            t0 = run(ask)
            parsed, err = from_log()
            expect(f"{label}, запасная после отказа", parsed, err,
                   answered[-1] if answered else None, 4.0, t0)
            expect_label(f"{label}, запасная после отказа", parsed, label)

        # ── 6. Журнал разговоров «Сам в разговор» ──
        hist.set_setting("active_model", slow)
        fallback_script(90.0, "timeout", 7.0)
        t0 = run(lambda: g.ask_group_proactive(-777032, 1, "повод"))
        parsed, err = from_chat_log()
        expect("журнал разговоров, запасная после зависшей активной", parsed, err,
               answered[-1] if answered else None, 7.0, t0)

        script.clear()
        script[slow_provider] = [(5.0, "ok")]
        run(lambda: g.ask_group_proactive(-777032, 1, "повод"))
        parsed, err = from_chat_log()
        expect("журнал разговоров, ответила сама активная", parsed, err, slow, 5.0)

        script.clear()
        script[slow_provider] = [(90.0, "timeout")]
        script["gemini"] = [(2.0, "503")]
        t0 = run(lambda: g.ask_group_proactive(-777032, 1, "повод"))
        parsed, err = from_chat_log()
        expect("журнал разговоров, не ответил никто", parsed, err, slow, None, t0)
    finally:
        for name, value in saved.items():
            setattr(g, name, value)
        for name, value in saved_hist.items():
            setattr(hist, name, value)
        chat_log._write = saved_write
        hist.set_setting("active_model", saved_active)

    return problems, (f"{done} проверок: текст у каждого провайдера, запасная после "
                      f"зависания, повтор той же модели, голосовое, видео, журнал "
                      f"разговоров; письмо владельцу в перебор не входит")


# ───────────────────────────────────────────────
#  40. ГОЛОСОВОЕ И ВИДЕО ДЕЛАЮТ ОДНО И ТО ЖЕ
# ─────────────────────────────────────────────

def check_media_twins():
    """
    Голосовое и видео доходят до конца пути, и каждое — со своими параметрами.

    ⚠️ ЧТО ЗДЕСЬ ИЗМЕНИЛОСЬ 21.09.2026. Проверка заводилась сторожем над ДВУМЯ
    КОПИЯМИ одного кода (`ask_gemini_audio` и `ask_gemini_video`, 92 строки
    совпадали дословно) — ловить забытую во второй копии правку. В тот же день
    копии свели в один путь `_ask_native_media`, и сторожить стало нечего:
    рассинхрона больше не бывает по устройству. Проверка осталась, но отвечает
    теперь на ДРУГОЙ вопрос — доходят ли оба типа до конца общего пути и не
    перепутаны ли параметры, которыми они этот путь проходят.

    ⚠️ ПОЧЕМУ ЭТО НЕ ЛИШНЕЕ ПОСЛЕ ОБЪЕДИНЕНИЯ. Объединение УБРАЛО риск забытой
    копии и ЗАВЕЛО новый: то, что было зашито в теле каждой функции, стало
    параметром вызова, а параметр перепутать легче. Поймано нарочной поломкой в
    тот же день: голосовое, записанное в память бота как «[Видео]», проходило
    ВСЕ проверки проекта — отсюда шаг «своя подпись в памяти бота».

    ⚠️ ШАГИ ПЕРЕЧИСЛЕНЫ ОДИН РАЗ и гоняются по обоим типам одним циклом. Разошлись
    типы — проверка называет и шаг, и тип; не сделали оба — говорит об этом
    отдельно: это разные поломки с разной ценой.

    ⚠️ ТЕКСТЫ ЛОГА И ПОРЯДОК СТРОК ЗДЕСЬ НЕ СВЕРЯЮТСЯ, и это не забывчивость.
    Сличение текста краснеет от любой безобидной правки и быстро становится
    проверкой, которую перестают читать. Сверяется то, что имеет последствия:
    деньги, письмо владельцу, память бота, мысли, активная модель.
    Секундомер и потолки живут в своих группах (check_stopwatch,
    check_wait_budgets) — здесь они не дублируются.

    Сценарий один и тот же на оба типа: ПЕРВАЯ модель очереди отказывает,
    отвечает запасная. Сеть и часы подставные — ждать на выкатке нельзя.
    """
    import requests
    import config as cfg
    from database import history as hist
    from services import gemini as g

    problems = []
    done = 0

    USER = -777033            # свой id: чужую переписку во временной базе не трогаем
    THOUGHT = "сначала прикину, о чём просят"
    ANSWER = "вот ответ человеку"

    class Clock:
        def __init__(self): self.t = 1000.0
        def monotonic(self): return self.t
        def perf_counter(self): return self.t
        def sleep(self, s): self.t += s
        def time(self): return 1700000000.0

    clock = Clock()

    class Silent:
        """Вместо логгера: строки этой проверке не нужны, шум в выводе — нужен ещё меньше."""
        def _skip(self, *a, **kw): pass
        debug = info = warning = error = exception = _skip

    asked = []          # модели, к которым обратились, по порядку
    charged = []        # [модель] — учёт вызова в статистике
    remembered = []     # [(подпись, текст ответа, модель)] — запись в память бота
    told = []           # [(что, список отказавших)] — письмо владельцу об отказе
    dead = []           # письмо «вся очередь легла» — в этом сценарии его быть не должно

    class _Refused:
        @staticmethod
        def raise_for_status():
            raise requests.exceptions.HTTPError("503 Server Error: подставной отказ")

        @staticmethod
        def json():
            return {}

    class _Answered:
        @staticmethod
        def raise_for_status():
            pass

        @staticmethod
        def json():
            # Родной формат Gemini: мысли — ОТДЕЛЬНЫЕ части с флагом "thought".
            # Иначе проверять «мысли не попали в память» было бы не на чем.
            return {
                "candidates": [{"content": {"parts": [
                    {"text": THOUGHT, "thought": True},
                    {"text": ANSWER},
                ]}}],
                "usageMetadata": {"promptTokenCount": 10, "thoughtsTokenCount": 3,
                                  "totalTokenCount": 15},
            }

    class _Session:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None, **kw):
            found = re.search(r"/models/([^:/]+):", url)
            asked.append(found.group(1) if found else "?")
            clock.t += 1.0
            return _Refused if len(asked) == 1 else _Answered

    # Что обязан сделать КАЖДЫЙ из двух типов. Первым — название шага, вторым —
    # как о нём жаловаться человеку, третьим — сам вопрос.
    def all_steps(answer, active, note):
        want = asked[-1] if asked else None        # ответившая — последняя, к кому обратились
        first = asked[0] if asked else None        # отказавшая — первая
        failed = (told[0][1] if told else None) or []
        return (
            ("расход списан на ответившую",
             (f"расход записан на {charged} — а ответила {want}"
              if charged else "расход не списан вовсе"),
             len(charged) == 1 and charged[0] == want),

            ("владельцу сказано об отказавшей",
             ("письма об отказе не было" if not told
              else f"в письме владельцу нет отказавшей {first}: {failed}"),
             len(told) == 1 and any(m == first for m, _ in failed) and not dead),

            ("разговор записан в память бота",
             ("в память бота не записано ничего" if not remembered
              else f"в памяти записей {len(remembered)}, модель {remembered[0][2]} — "
                   f"а ответила {want}"),
             len(remembered) == 1 and remembered[0][2] == want),

            ("мыслей модели в памяти нет",
             "в память бота попали мысли модели — они уедут в следующий запрос",
             bool(remembered) and THOUGHT not in (remembered[0][1] or "")),

            # ⚠️ Заведено 21.09.2026, когда две копии свели в одну: подпись стала
            # ПАРАМЕТРОМ, а параметр перепутать легче, чем строку внутри функции.
            # Проверено нарочной поломкой: без этого шага голосовое, записанное
            # в память как «[Видео]», проходило ВСЕ проверки проекта.
            ("своя подпись в памяти бота",
             (f"в памяти стоит {remembered[0][0]!r}, а для этого типа ждём {note!r} — "
              f"похоже на перепутанный параметр" if remembered
              else "в памяти нет записи, подпись проверять не на чем"),
             bool(remembered) and (remembered[0][0] or "") == note),

            ("мысли ушли человеку",
             "человек не увидел мыслей модели — свёрнутой цитате взяться не из чего",
             THOUGHT in (answer or "")),

            ("человек получил ответ",
             "человеку ушла заглушка вместо ответа запасной модели",
             ANSWER in (answer or "")),

            ("активная модель не подменилась",
             (f"после подстраховки активной стала {hist.get_setting('active_model', '')}, "
              f"а была {active}"),
             hist.get_setting("active_model", "") == active),
        )

    saved = {name: getattr(g, name) for name in (
        "time", "logger", "_http", "_notify_models_failed", "_notify_chain_dead",
        "RAG_ENABLED")}
    saved_hist = {name: getattr(hist, name) for name in ("register_api_call", "add_messages")}
    saved_active = hist.get_setting("active_model", "")
    try:
        g.time = clock
        g.logger = Silent()
        g._http = lambda: _Session()
        g._notify_models_failed = lambda kind, failures, **kw: told.append((kind, failures))
        g._notify_chain_dead = lambda *a, **kw: dead.append(a)
        # Поиск по базе знаний сам зовёт модели — здесь он сбил бы счёт обращений.
        g.RAG_ENABLED = False
        hist.register_api_call = charged.append
        hist.add_messages = (lambda chat_id, user_id, user_text, answer,
                             prompt_tokens=0, model_name=None, total_tokens=0:
                             remembered.append((user_text, answer, model_name)))

        result = {}
        for label, ask, chain, note in (
                ("голосовое", lambda: g.ask_gemini_audio(USER, USER, "QQ"),
                 [m for m in cfg.AUDIO_FALLBACK_CHAIN if m in cfg.AVAILABLE_MODELS],
                 "[Голосовое сообщение]"),
                ("видео", lambda: g.ask_gemini_video(USER, USER, "QQ"),
                 [m for m in cfg.VIDEO_FALLBACK_CHAIN
                  if cfg.AVAILABLE_MODELS.get(m, {}).get("video")],
                 "[Видео]")):
            done += 1
            if len(chain) < 2:
                problems.append(f"{label}: в очереди меньше двух моделей — подстраховку "
                                f"проверить нечем, сценарий этой группы не отработал")
                continue
            for box in (asked, charged, remembered, told, dead):
                box.clear()
            g._quota_blocked.clear()
            hist.set_setting("active_model", chain[0])
            answer = ask()
            result[label] = all_steps(answer, chain[0], note)

        # ── Жалобы. Расхождение типов важнее самого шага: это перепутанный параметр ──
        if len(result) == 2:
            (one_label, one), (two_label, two) = result.items()
            for (name, why_one, ok_one), (_, why_two, ok_two) in zip(one, two):
                done += 1
                if ok_one and ok_two:
                    continue
                if ok_one != ok_two:
                    good, bad, why = ((one_label, two_label, why_two) if ok_one
                                      else (two_label, one_label, why_one))
                    problems.append(f"«{name}»: это делает {good}, а {bad} — нет: {why}. "
                                    f"Ищи в параметрах, которыми {bad} идёт "
                                    f"в общий путь _ask_native_media")
                else:
                    problems.append(f"«{name}»: этого не делает ни {one_label}, "
                                    f"ни {two_label} — {why_one}")
    finally:
        for name, value in saved.items():
            setattr(g, name, value)
        for name, value in saved_hist.items():
            setattr(hist, name, value)
        hist.set_setting("active_model", saved_active)

    return problems, (f"{done} проверок: на обоих типах — расход на ответившую, письмо "
                      f"владельцу, память бота без мыслей и со своей подписью, мысли "
                      f"человеку, активная модель на месте; расхождение типов названо отдельно")


CHECKS = (
    ("деньги — расчёт стоимости запросов", check_money),
    ("квота Qwen — оборванный ответ, срок и письма", check_qwen_quota),
    ("деньги — сам прайс не менялся", check_price_list),
    ("пометка мута — разбор ответа модели", check_mute_tag),
    ("мут от бота подписан человеком, а не номером", check_ai_mute_name),
    ("права доступа — кнопки и иерархия", check_permissions),
    ("размышления модели не утекают в чат", check_thoughts),
    ("длинные ответы — разметка не разъезжается", check_long_answers),
    ("потолки ожидания — перебор моделей", check_wait_budgets),
    ("антиспам — альбом не считается флудом", check_album_not_flood),
    ("копилка альбома — все кадры уходят модели", check_album_collect),
    ("фильтр ссылок — белый список и мут за повторы", check_link_filter),
    ("приветствие новичков и проверка «я не бот»", check_greeter),
    ("чужие группы — бот молчит, пока владелец не решит", check_group_guard),
    ("кнопки карточки статьи базы знаний", check_kb_card),
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
    ("ручная правка счёта викторины", check_quiz_score),
    ("уведомления о базе знаний живут по сроку", check_rag_notice),
    ("фото уходит зрячей модели, а не в пустоту", check_photo_route),
    ("секундомер — время ответившей модели, а не всей очереди", check_stopwatch),
    ("голосовое и видео доходят до конца пути", check_media_twins),
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
