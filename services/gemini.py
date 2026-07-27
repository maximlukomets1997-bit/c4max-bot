# ───────────────────────────────────────────────
#  gemini.py — запросы к Google Gemini API
#
#  Содержит:
#    _gemini_chat_request()    — устойчивый запрос: 2 попытки + авто-фолбэк
#    ask_gemini()              — основной запрос с историей (текст/Vision)
#    ask_gemini_audio()        — обработка голосовых (native generateContent)
#    format_news_as_colonel()  — форматирование новостей в стиле C4_Max
#    generate_image()          — генерация картинок (Nano Banana, gemini-*-image)
#
#  УСТОЙЧИВОСТЬ К ОШИБКАМ:
#    Пользователь НИКОГДА не видит технических ошибок Gemini API.
#    Любой запрос делается до 2 раз на активной модели; если ответа нет —
#    бот автоматически переключается на FALLBACK_MODEL (gemini-3.1-flash-lite)
#    и пробует её. Только если недоступно вообще всё — отдаётся мягкое
#    сообщение (SOFT_FAIL_MESSAGE), без «падения» и без деталей ошибки.
#
#  АРХИТЕКТУРНОЕ ЗАМЕЧАНИЕ (ПОЧЕМУ НЕ ИСПОЛЬЗУЕМ АСИНХРОННЫЙ SDK GOOGLE):
#    Мы осознанно используем прямые REST API запросы (библиотеку requests)
#    и отказываемся от официального асинхронного SDK (google-genai).
#    Причины:
#      1. Избежание конфликтов Event Loop (особенно ProactorEventLoop на Windows).
#      2. Отсутствие "тяжелых" зависимостей (gRPC, protobuf).
#      3. Полный контроль над таймаутами, форматом JSON и историей.
#      4. Стабильная работа внутри python-telegram-bot (через run_in_executor).
# ─────────────────────────────────────────────

import logging
import re
import time
import json
import base64
import requests

from config import (
    GEMINI_API_URL,
    GEMINI_MODEL,
    GEMINI_TIMEOUT,
    GEMINI_STREAM_DEADLINE,
    GEMINI_API_KEY,
    GEMINI_IMAGEN_API_KEY,
    QWEN_API_URL,
    QWEN_API_KEY,
    DEEPSEEK_API_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_PRICES,
    XIAOMI_API_URL,
    XIAOMI_API_KEY,
    XIAOMI_PRICES,
    QWEN_PRICES,
    IMAGE_PRICES,
    FALLBACK_MODEL,
    AUDIO_FALLBACK_CHAIN,
    VIDEO_FALLBACK_CHAIN,
    VIDEO_TIMEOUT,
    AVAILABLE_MODELS,
    RAG_ENABLED,
    ADMIN_IDS,
    TELEGRAM_TOKEN,
    PROACTIVE_CONTEXT_MSGS,
    PROACTIVE_SKIP_MARKER,
    PROVIDER_ICONS,
    PROVIDER_ICON_FALLBACK,
    RAG_ICON,
)
import database.history as hist
# Соединения к моделям и сайтам переиспользуются (2026-07-27): «рукопожатие»
# TLS с сервером стоит 130–200 мс и раньше платилось на КАЖДЫЙ запрос.
# Подробности и запрет на повторы — в services/http.py.
from services.http import session as _http

logger = logging.getLogger(__name__)

# Мягкое сообщение на случай, когда недоступны ВСЕ модели (без технических деталей)
SOFT_FAIL_MESSAGE = "📡 Связь временно нестабильна — повтори запрос через минуту, я уже на связи."


def compress_newlines(text: str) -> str:
    """
    Убирает избыточные пустые строки из ответов.
    Заменяет последовательности пустых строк на один перевод строки \n,
    чтобы сделать текст максимально компактным в Telegram.
    """
    if not text:
        return ""
    # Нормализуем переводы строк к \n
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Удаляем пробельные символы на пустых строках (например, "  \n" -> "\n")
    text = re.sub(r'\n\s*\n', '\n\n', text)
    # Сжимаем все двойные и более переводы строк до одного \n
    text = re.sub(r'\n{2,}', '\n', text)
    return text.strip()


# ───────────────────────────────────────────────
#  Уведомление админов о работе на запасной модели
# ───────────────────────────────────────────────

# Когда запрос обслужила НЕ активная модель (сработала цепочка фолбэка),
# админам уходит сообщение в личку — сигнал, что активная модель сбоит.
# Не чаще раза в час (_FALLBACK_NOTIFY_COOLDOWN), чтобы при длительном сбое
# не засыпать личку. Отправка идёт напрямую через Bot API (requests): этот
# код выполняется в синхронном потоке (run_in_executor), где объекта бота
# python-telegram-bot нет. Любая ошибка отправки глушится — уведомление
# не должно ломать ответ пользователю.
_FALLBACK_NOTIFY_COOLDOWN = 3600  # секунд (1 час)
_last_fallback_notify = 0.0


def _notify_admins_fallback(active_model: str, used_model: str) -> None:
    """Шлёт админам в личку «активная модель не ответила, работаю на запасной»."""
    global _last_fallback_notify
    now = time.monotonic()
    if _last_fallback_notify and now - _last_fallback_notify < _FALLBACK_NOTIFY_COOLDOWN:
        return
    _last_fallback_notify = now

    active_name = AVAILABLE_MODELS.get(active_model, {}).get("name", active_model)
    used_name = AVAILABLE_MODELS.get(used_model, {}).get("name", used_model)
    text = (
        f"⚠️ Активная модель {active_name} не ответила — запрос обслужила запасная {used_name}.\n\n"
        f"Активная модель не менялась: следующий запрос снова пойдёт на {active_name}. "
        f"Если сбои продолжатся, это уведомление повторится не раньше чем через час."
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for admin_id in ADMIN_IDS:
        try:
            _http().post(url, json={"chat_id": admin_id, "text": text}, timeout=10)
        except Exception as e:
            logger.warning("⚠️ Не удалось уведомить админа %s о запасной модели: %s", admin_id, e)


# ───────────────────────────────────────────────
#  Устойчивый запрос к OpenAI-совместимому эндпоинту
# ───────────────────────────────────────────────

def _provider_of(model_name: str) -> str:
    """Какому сервису принадлежит модель: 'gemini' (по умолчанию), 'qwen' или 'deepseek'."""
    return AVAILABLE_MODELS.get(model_name, {}).get("provider", "gemini")


def _icon_of(model_name: str) -> str:
    """
    Смысловой значок для строк лога: ♊️ Gemini/Gemma, 🐪 Qwen, 🐋 DeepSeek
    (список — PROVIDER_ICONS в config.py). Неизвестная модель (нет в конфиге) —
    запасной 🤖, как было до 2026-07-19 у всех нейросетей разом.

    ⚠️ Значок берётся по КОНКРЕТНОЙ модели строки (chain[0], model_name), а не
    по активной: при уходе на запасную в логе должен стоять значок той, кто
    реально ответил, иначе подмена провайдера в фолбэке становится незаметной.

    Модель ищем в AVAILABLE_MODELS напрямую, а НЕ через _provider_of: тот для
    незнакомого имени отвечает «gemini» (разумно для запроса — но для значка
    выдало бы ♊️ чужой модели).
    """
    info = AVAILABLE_MODELS.get(model_name)
    if not info:
        return PROVIDER_ICON_FALLBACK
    return PROVIDER_ICONS.get(info.get("provider"), PROVIDER_ICON_FALLBACK)


def _is_thinking(model_name: str, override: bool | None = None) -> bool:
    """
    True, если модель должна отдавать цепочку рассуждений.

    override=None — как записано в AVAILABLE_MODELS (обычное поведение).
    override=False — размышления ПРИНУДИТЕЛЬНО выключены.
    ⚠️ У моделей с native_thinking (Gemma) параметр не помогает — они шлют
    <thought> сами.
    """
    if override is not None:
        return override
    return bool(AVAILABLE_MODELS.get(model_name, {}).get("thinking", False))


def _supports_vision(model_name: str) -> bool:
    """True, если модель принимает картинки (поле vision в AVAILABLE_MODELS).
    У DeepSeek и текстовых Qwen поля нет — их API принимает только текст."""
    return bool(AVAILABLE_MODELS.get(model_name, {}).get("vision", False))


def _supports_video(model_name: str) -> bool:
    """True, если модель принимает ВИДЕО (поле video в AVAILABLE_MODELS, 2026-07-24).
    Стоит у всех Gemini; у Qwen и DeepSeek поля нет — видео им не отправляем."""
    return bool(AVAILABLE_MODELS.get(model_name, {}).get("video", False))


# ── Лёгкие хелперы для проактивного режима (без истории, без RAG) ──

def _proactive_describe_image(image_base64: str) -> str:
    """
    Описывает фото для проактивного триггера: лёгкий нестриминговый запрос
    к Gemini (gemini-3.1-flash-lite — самая быстрая и дешёвая vision-модель).
    Без истории, системного промпта и RAG — только описание картинки.

    Возвращает описание (1–2 предложения) или пустую строку при любой ошибке.
    Ошибка описания НЕ должна ломать проактивную проверку — тогда триггером
    останется оригинальная подпись (возможно, пустая).
    """
    try:
        payload = {
            "model": "gemini-3.1-flash-lite",
            "messages": [
                # 2026-07-24 (решение Максима): зашитая фраза-инструкция УДАЛЕНА —
                # модели уходит только сам файл. Он не хочет, чтобы бот подсказывал
                # моделям формулировки от себя. Не возвращать без его просьбы.
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                ]}
            ],
            "stream": False,
        }
        start = time.perf_counter()
        response = _http().post(
            GEMINI_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        elapsed = time.perf_counter() - start
        text = data["choices"][0]["message"]["content"].strip()
        logger.info("🤖 Ответ от gemini-3.1-flash-lite за %.1f с (описание фото для proactive)", elapsed)
        return text
    except Exception as e:
        logger.debug("🤖 Не удалось описать фото для проактивной проверки: %s", e)
        return ""


def _proactive_transcribe_audio(audio_base64: str) -> str:
    """
    Расшифровывает голосовое для проактивного триггера: лёгкий запрос к native
    Gemini generateContent (gemini-3.1-flash-lite). Без истории и системного
    промпта — только расшифровка аудиодорожки.

    Возвращает текст расшифровки или пустую строку при любой ошибке.
    Ошибка расшифровки НЕ ломает проактивную проверку — тогда бот просто
    не отреагирует на голосовое (как и раньше).
    """
    try:
        payload = {
            "contents": [
                # 2026-07-24 (решение Максима): зашитая фраза-инструкция УДАЛЕНА,
                # уходит только аудиофайл. Не возвращать без его просьбы.
                {"role": "user", "parts": [
                    {"inlineData": {"mimeType": "audio/ogg", "data": audio_base64}},
                ]}
            ],
        }
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        start = time.perf_counter()
        response = _http().post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        elapsed = time.perf_counter() - start
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        logger.info("🤖 Ответ от gemini-3.1-flash-lite за %.1f с (расшифровка аудио для proactive)", elapsed)
        return text
    except Exception as e:
        logger.debug("🤖 Не удалось расшифровать голосовое для проактивной проверки: %s", e)
        return ""


def _proactive_describe_video(video_base64: str, mime_type: str = "video/mp4") -> str:
    """
    Коротко описывает ВИДЕО для проактивного триггера (2026-07-24) — устроено
    как _proactive_describe_image/_proactive_transcribe_audio: лёгкий запрос
    к gemini-3.1-flash-lite без истории и системного промпта.

    Просим КРАТКО: описание идёт в стенограмму беседы, а не пользователю, —
    развёрнутый пересказ ролика только зашумил бы контекст.
    Таймаут больше, чем у фото и аудио: разбор видео дольше.

    Возвращает описание или пустую строку при любой ошибке — сбой разбора
    НЕ ломает проактивную проверку, бот просто не отреагирует на видео.
    """
    try:
        payload = {
            "contents": [
                # 2026-07-24 (решение Максима): зашитая фраза-инструкция УДАЛЕНА,
                # уходит только видеофайл. Не возвращать без его просьбы.
                {"role": "user", "parts": [
                    {"inlineData": {"mimeType": mime_type, "data": video_base64}},
                ]}
            ],
        }
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        start = time.perf_counter()
        response = _http().post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        elapsed = time.perf_counter() - start
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        logger.info("🤖 Ответ от gemini-3.1-flash-lite за %.1f с (описание видео для proactive)", elapsed)
        return text
    except Exception as e:
        logger.debug("🤖 Не удалось описать видео для проактивной проверки: %s", e)
        return ""


def _openai_stream_request(model_name: str, messages: list, api_url: str,
                           api_key: str, extra_payload: dict):
    """
    Общий потоковый запрос к OpenAI-совместимому эндпоинту (Qwen, DeepSeek).

    Рассуждающие модели этих провайдеров работают в потоковом режиме: ответ
    приходит кусочками. «Мысли» приходят в отдельном поле reasoning_content,
    финальный текст — в content. Мы собираем оба и НОРМАЛИЗУЕМ результат к тому
    же виду, что отдаёт OpenAI-совместимый Gemini:
        {"choices": [{"message": {"content": ...}}], "usage": {...}}
    Благодаря этому остальной код (ask_gemini, utils_format) не меняется.

    Рассуждения заворачиваем в <thought>…</thought> и ставим ПЕРЕД ответом —
    их развернёт utils_format как сворачиваемую цитату (тот же механизм, что и
    у Gemini). В БД <thought> вырезается выше по стеку, в ask_gemini.

    extra_payload — специфичные для провайдера параметры (включение рассуждений
    у каждого называется по-своему, см. _qwen_chat_request/_deepseek_chat_request).

    Возвращает dict (как у Gemini) или None при пустом/неуспешном ответе.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        **extra_payload,
    }

    reasoning_parts, answer_parts = [], []
    usage = {}

    # Момент старта — для общего предела на весь ответ (GEMINI_STREAM_DEADLINE).
    start = time.monotonic()
    response = _http().post(
        api_url, json=payload, headers=headers,
        timeout=GEMINI_TIMEOUT, stream=True,
    )
    response.raise_for_status()
    try:
        for raw in response.iter_lines():
            # Жёсткий общий предел: GEMINI_TIMEOUT ловит только паузу между
            # кусочками, а этот предел обрывает «висяк», когда сервер потихоньку
            # шлёт данные, но не заканчивает ответ. Timeout → _try_model уйдёт
            # к следующей модели цепочки (как на обычном таймауте).
            if time.monotonic() - start > GEMINI_STREAM_DEADLINE:
                raise requests.exceptions.Timeout(
                    f"потоковый ответ {model_name} превысил общий предел {GEMINI_STREAM_DEADLINE}с"
                )
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if chunk == "[DONE]":
                break
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for choice in (obj.get("choices") or []):
                delta = choice.get("delta") or {}
                rc = delta.get("reasoning_content")
                if rc:
                    reasoning_parts.append(rc)
                c = delta.get("content")
                if c:
                    answer_parts.append(c)
    finally:
        response.close()

    answer = "".join(answer_parts).strip()
    reasoning = "".join(reasoning_parts).strip()
    if not answer and not reasoning:
        return None  # пусто — считаем неудачей, цепочка фолбэка пойдёт дальше

    content = f"<thought>{reasoning}</thought>\n{answer}" if reasoning else answer
    return {"choices": [{"message": {"content": content}}], "usage": usage}


def _qwen_chat_request(model_name: str, messages: list, thinking_override: bool | None = None):
    """Запрос к Qwen (Alibaba Cloud Model Studio).
    enable_thinking — нестандартный параметр Qwen: включает цепочку рассуждений."""
    return _openai_stream_request(
        model_name, messages, QWEN_API_URL, QWEN_API_KEY,
        {"enable_thinking": _is_thinking(model_name, thinking_override)},
    )


def _deepseek_chat_request(model_name: str, messages: list, thinking_override: bool | None = None):
    """Запрос к DeepSeek. Рассуждения включаются параметром thinking
    ({"type": "enabled"/"disabled", "reasoning_effort": "xhigh"} — формат DeepSeek V4).
    reasoning_effort="xhigh" — экстремальная глубина размышлений."""
    if _is_thinking(model_name, thinking_override):
        extra = {"thinking": {"type": "enabled", "reasoning_effort": "xhigh"}}
    else:
        extra = {"thinking": {"type": "disabled"}}
    return _openai_stream_request(
        model_name, messages, DEEPSEEK_API_URL, DEEPSEEK_API_KEY, extra,
    )


def _xiaomi_chat_request(model_name: str, messages: list, thinking_override: bool | None = None):
    """Запрос к Xiaomi MiMo (2026-07-25). Формат управления рассуждениями —
    ТОТ ЖЕ, что у DeepSeek: {"thinking": {"type": "enabled"/"disabled"}}.
    Проверено живыми запросами: с "disabled" размышлений ноль, с
    reasoning_effort="xhigh" их вдвое больше — как у DeepSeek, максимум
    (решение Максима о глубине мышления)."""
    if _is_thinking(model_name, thinking_override):
        extra = {"thinking": {"type": "enabled", "reasoning_effort": "xhigh"}}
    else:
        extra = {"thinking": {"type": "disabled"}}
    return _openai_stream_request(
        model_name, messages, XIAOMI_API_URL, XIAOMI_API_KEY, extra,
    )


def _xiaomi_cost(model_name: str, usage: dict):
    """Точная стоимость запроса Xiaomi MiMo по ценам XIAOMI_PRICES.
    Кэшированный вход приходит в prompt_tokens_details.cached_tokens (как у
    Qwen — проверено живым запросом: cached_tokens=192). Рассуждения уже входят
    в completion_tokens и оплачиваются как выход. Кэш-поле не пришло — весь вход
    по полной цене (оценка сверху, расход не занижаем).
    None — модели нет в таблице цен."""
    prices = XIAOMI_PRICES.get(model_name)
    if not prices:
        return None
    total_in = usage.get("prompt_tokens", 0) or 0
    hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    hit = min(hit, total_in)
    miss = max(0, total_in - hit)
    out = usage.get("completion_tokens", 0) or 0
    return (hit * prices["cache_hit"] + miss * prices["cache_miss"] + out * prices["output"]) / 1_000_000


def _deepseek_cost(model_name: str, usage: dict):
    """Точная стоимость запроса DeepSeek в долларах по официальным ценам
    (DEEPSEEK_PRICES). Все токены присылает сам API: вход из кэша, вход без
    кэша, ответ (рассуждения уже входят в completion_tokens и оплачиваются
    как ответ). Если кэш-поля не пришли, весь вход считается «без кэша» —
    оценка сверху, чтобы не занизить расход. None — модели нет в таблице цен."""
    prices = DEEPSEEK_PRICES.get(model_name)
    if not prices:
        return None
    hit = usage.get("prompt_cache_hit_tokens", 0) or 0
    miss = usage.get("prompt_cache_miss_tokens", 0) or 0
    if not hit and not miss:
        miss = usage.get("prompt_tokens", 0) or 0
    out = usage.get("completion_tokens", 0) or 0
    return (hit * prices["cache_hit"] + miss * prices["cache_miss"] + out * prices["output"]) / 1_000_000


def _qwen_cost(model_name: str, usage: dict):
    """Точная стоимость запроса Qwen в долларах по ценам QWEN_PRICES.
    Кэшированный вход приходит в prompt_tokens_details.cached_tokens (неявный
    кэш Alibaba; на практике часто 0 — кэш у них «без гарантий»). Рассуждения
    уже входят в completion_tokens и оплачиваются как выход. Если кэш-поле
    не пришло — весь вход по полной цене (оценка сверху, расход не занижаем).
    None — модели нет в таблице цен."""
    prices = QWEN_PRICES.get(model_name)
    if not prices:
        return None
    pt = usage.get("prompt_tokens", 0) or 0
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    cached = min(cached, pt)
    miss = pt - cached
    out = usage.get("completion_tokens", 0) or 0
    return (cached * prices["cache_hit"] + miss * prices["cache_miss"] + out * prices["output"]) / 1_000_000


def _err_code(exc) -> str:
    """Код HTTP-ошибки из исключения requests (если есть) — для логов."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return f"код {code}" if code else "без HTTP-кода"


def _err_body(exc) -> str:
    """
    Тело ответа сервера при HTTP-ошибке — САМОЕ ценное для разбора сбоя:
    код 400 сам по себе не говорит ничего, а причина («фильтр контента»,
    «квота исчерпана», «неверный параметр») приходит именно в теле.
    Наступили 2026-07-19: три отказа qwen3.7-max с кодом 400, причину
    установить по логу не удалось — тело выбрасывалось.

    Обрезаем до 500 символов (тело бывает многословным). Пустая строка,
    если ответа нет (таймаут, обрыв сети) или прочитать не удалось —
    сам разбор ошибки не должен бросать новую ошибку. Чтение тела заодно
    освобождает соединение потокового запроса, оборванного на raise_for_status.
    """
    try:
        body = (getattr(exc, "response", None) or "") and exc.response.text
        body = (body or "").strip()
        return f" | ответ сервера: {body[:500]}" if body else ""
    except Exception:
        return ""


def _gemini_chat_request(messages: list, kind: str = "текст", has_image: bool = False,
                         thinking_override: bool | None = None,
                         chain_override: list[str] | None = None):
    """
    Запрос к моделям с устойчивой стратегией (цепочка, схема B):
      1. Активная модель: до 2 попыток.
      2. При неудаче — 1–2 ЗАПАСНЫЕ модели (перекрёстная подстраховка:
         другой провайдер / быстрая бесплатная Gemini-lite), а НЕ перебор всех.
    Активная модель пользователя при этом НЕ меняется (фолбэк временный, на запрос).

    thinking_override (2026-07-20) — принудительно включить/выключить цепочку
    рассуждений у ВСЕХ моделей запроса (см. _is_thinking).

    chain_override (2026-07-20) — своя цепочка моделей вместо расчёта от активной.
    При заданном chain_override уведомление админам «ответила запасная» НЕ
    шлётся — активная модель тут ни при чём.

    ⚠️ Оба параметра появились для судьи проактивного режима; судья удалён
    2026-07-20, и сейчас их НИКТО не передаёт — механизм живой, но спит
    (оставлен на будущее: пригодится любой служебной проверке без размышлений).

    Возвращает кортеж (data | None, used_model).
    data is None означает, что недоступны все варианты — решение, что показать
    пользователю, принимает вызывающий код (мягкое сообщение, без деталей ошибки).

    Маршрутизация по провайдеру: модели Gemini идут на GEMINI_API_URL с ключом
    GEMINI_API_KEY (нестриминговый JSON), модели Qwen, DeepSeek и Xiaomi — каждая
    на свой адрес со своим ключом (потоковый разбор в _openai_stream_request).
    Цепочка фолбэка может смешивать провайдеров: каждая модель сама знает свой
    адрес и ключ.

    ВАЖНО: temperature/top_p/top_k не задаются намеренно — для моделей Gemini 3.x
    Google рекомендует использовать значения по умолчанию (их сэмплинг оптимизирован
    под дефолт), поэтому мы не передаём эти параметры.
    """

    def _try_model(model_name: str, attempts: int = 2):
        provider = _provider_of(model_name)
        for attempt in range(attempts):
            try:
                if provider == "qwen":
                    data = _qwen_chat_request(model_name, messages, thinking_override)
                    if data is None:
                        raise ValueError("пустой ответ Qwen")
                    return data
                if provider == "deepseek":
                    data = _deepseek_chat_request(model_name, messages, thinking_override)
                    if data is None:
                        raise ValueError("пустой ответ DeepSeek")
                    return data
                if provider == "xiaomi":
                    data = _xiaomi_chat_request(model_name, messages, thinking_override)
                    if data is None:
                        raise ValueError("пустой ответ Xiaomi")
                    return data
                # provider == "gemini"
                payload = {
                    "model": model_name,
                    "messages": messages,
                    "stream": False,
                }
                # Думающие модели Gemini (кроме Gemma — она шлёт <thought> сама):
                # просим вернуть сводку рассуждений. Мысли приходят в <thought>…</thought>
                # прямо в тексте — тот же формат, что у Gemma, разбор НЕ меняется.
                # thinking_budget: -1 = динамический максимум (думает по сложности задачи).
                info = AVAILABLE_MODELS.get(model_name, {})
                if _is_thinking(model_name, thinking_override) and not info.get("native_thinking"):
                    payload["extra_body"] = {
                        "google": {"thinking_config": {"include_thoughts": True, "thinking_budget": -1}}
                    }
                response = _http().post(
                    GEMINI_API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {GEMINI_API_KEY}"},
                    timeout=GEMINI_TIMEOUT,
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(
                    "⚠️ Модель %s не ответила (попытка %s из %s, %s): %s%s",
                    model_name, attempt + 1, attempts, _err_code(e), e, _err_body(e),
                )
                # Таймаут (модель «зависла» на GEMINI_TIMEOUT сек): вторая попытка
                # почти всегда тоже упрётся в таймаут — не тратим ещё столько же
                # времени, сразу отдаём управление цепочке фолбэка. На быстрых
                # ошибках (503/500) повтор осмыслен — там поведение прежнее.
                if isinstance(e, requests.exceptions.Timeout):
                    logger.warning("⚠️ Модель %s: таймаут — пропускаю остальные попытки, иду к запасной", model_name)
                    break
                if attempt < attempts - 1:
                    time.sleep((attempt + 1) * 2)
        return None

    active_model = hist.get_setting("active_model", GEMINI_MODEL)

    # ── Фото у «слепой» модели: сразу цепочка Gemini (vision-reroute) ──
    # Официальный API DeepSeek и текстовые модели Qwen картинки не принимают
    # (content — только строка; сверено с документацией 2026-07-10). Попытка
    # была бы заведомо провальной, а админам уходила бы ложная тревога
    # «модель не ответила». Поэтому фото сразу анализирует цепочка Gemini —
    # как голосовые (AUDIO_FALLBACK_CHAIN). active_model НЕ меняется,
    # уведомления админам нет: это штатный режим, а не сбой.
    vision_reroute = has_image and not _supports_vision(active_model)
    if chain_override:
        # Своя цепочка (быстрый судья проактивного режима): активная модель
        # и vision-reroute тут ни при чём — идём строго туда, куда сказали.
        chain = [m for m in chain_override if m in AVAILABLE_MODELS]
        if not chain:
            # Кривая настройка (модель удалили из конфига) — не молчим,
            # а работаем как обычно, от активной модели.
            logger.warning("⚠️ Цепочка судьи пуста (%s) — иду обычным путём", chain_override)
            chain = [active_model]
            chain_override = None
        vision_reroute = False
    elif vision_reroute:
        # Порядок задан Максимом 2026-07-24 и совпадает с цепочками аудио/видео
        # (AUDIO_FALLBACK_CHAIN / VIDEO_FALLBACK_CHAIN в config.py): 3.5 Flash →
        # 3.6 Flash → 3.1 Flash-Lite. Меняешь там — поправь и здесь.
        chain = []
        for cand in ("gemini-3.5-flash", FALLBACK_MODEL, "gemini-3.1-flash-lite"):
            if cand in AVAILABLE_MODELS and _supports_vision(cand) and cand not in chain:
                chain.append(cand)
        if not chain:
            # Страховка от кривого конфига (у запасных нет vision) —
            # ведём себя как раньше: пробуем активную, дальше обычный фолбэк.
            chain = [active_model]
        logger.info("%s Модель %s не видит картинки — фото анализирует цепочка Gemini",
                    _icon_of(active_model), active_model)
    else:
        # ── Цепочка переключения (схема B — «перекрёстная подстраховка») ──
        # Активная модель первой (2 попытки), затем 1–2 ЗАПАСНЫЕ — а НЕ перебор всех.
        # Принцип: запас уходит на ДРУГОГО провайдера / на быструю бесплатную Gemini-lite,
        # чтобы пережить сбой целого сервиса и не жечь зря токены думающих моделей.
        #   • активная Gemini        → Gemini-lite, ещё одна Gemini-lite, и в самом
        #     конце Qwen (последний рубеж на случай падения всего Google);
        #   • активная Qwen/DeepSeek/Xiaomi → сразу бесплатные Gemini-lite (другой провайдер).
        # Модели Xiaomi MiMo сами в подстраховку НЕ ставятся (решение Максима
        # 2026-07-25 — сначала посмотреть их в деле), но подстраховываются как все.
        # active_model НЕ меняется — фолбэк временный, на один запрос.
        # 2026-07-24: вторым звеном была gemini-2.5-flash-lite — удалена вместе
        # со всей 2.5-серией; её место заняла gemini-3.1-flash-lite (решение
        # Максима: «3.1 Flash-Lite оставить в цепочке следующей за 3.6 Flash»).
        if _provider_of(active_model) == "gemini":
            fallback_candidates = [FALLBACK_MODEL, "gemini-3.1-flash-lite", "qwen3.7-plus"]
        else:  # активная — Qwen, DeepSeek или Xiaomi
            fallback_candidates = [FALLBACK_MODEL, "gemini-3.1-flash-lite"]

        chain = [active_model]
        for cand in fallback_candidates:
            if cand != active_model and cand in AVAILABLE_MODELS and cand not in chain:
                chain.append(cand)

    logger.info("%s Запрос к модели %s", _icon_of(chain[0]), chain[0])

    start = time.perf_counter()
    for i, model_name in enumerate(chain):
        data = _try_model(model_name, attempts=(2 if model_name == active_model else 1))
        if data is not None:
            hist.register_api_call(model_name)
            # Ответила запасная модель — сообщаем админам (не чаще раза в час).
            # При vision-reroute НЕ сообщаем: обход «слепой» модели на фото —
            # штатный режим (как у голосовых), а не сбой активной модели.
            if model_name != active_model and not vision_reroute and not chain_override:
                _notify_admins_fallback(active_model, model_name)
            elapsed = time.perf_counter() - start
            # Модель + время ответа + токены в одной строке.
            # контекст=вход, ответ=видимый текст, размышления=скрытые токены «думающих»
            # моделей, всего=полный расход. Размышления считаются двумя способами:
            #   • Gemini/Gemma: их нет в completion_tokens → вычитание (всего − контекст − ответ);
            #   • Qwen: они УЖЕ включены в completion_tokens (вычитание даёт 0), зато
            #     настоящее число приходит во вложенном поле
            #     completion_tokens_details.reasoning_tokens — берём его и вычитаем
            #     из «ответа», чтобы графы значили то же самое для всех провайдеров.
            _u = (data or {}).get("usage", {}) or {}
            _pt = _u.get("prompt_tokens", 0) or 0
            _ct = _u.get("completion_tokens", 0) or 0
            _tt = _u.get("total_tokens", 0) or 0
            _think = max(0, _tt - _pt - _ct)
            if not _think:
                _details = _u.get("completion_tokens_details") or {}
                _rt = _details.get("reasoning_tokens", 0) or 0
                if _rt:
                    _think = _rt
                    _ct = max(0, _ct - _rt)
            if _provider_of(model_name) == "deepseek":
                # DeepSeek: считаем точную стоимость (цены DEEPSEEK_PRICES,
                # кэш-поля из usage) и копим сумму в settings для панели /stats.
                _cost = _deepseek_cost(model_name, _u)
                if _cost is not None:
                    try:
                        hist.add_deepseek_cost(_cost)
                    except Exception as e:
                        logger.warning("⚠️ Не удалось записать расход DeepSeek в БД: %s", e)
                _hit = _u.get("prompt_cache_hit_tokens", 0) or 0
                logger.info("%s Ответ от %s за %.1f с | контекст=%s (из кэша %s) | ответ=%s | размышления=%s | всего=%s | ≈$%.6f",
                            _icon_of(model_name), model_name, elapsed, _pt, _hit, _ct, _think, _tt, _cost or 0.0)
            elif _provider_of(model_name) == "xiaomi":
                # Xiaomi MiMo: точная стоимость по XIAOMI_PRICES (кэш из
                # prompt_tokens_details, рассуждения оплачиваются как выход) —
                # копим в settings и вычитаем из остатка счёта, как у DeepSeek.
                # Для стоимости берём СЫРОЙ completion_tokens из usage: в графе
                # «ответ» ниже размышления уже вычтены, а биллятся они вместе.
                _cost = _xiaomi_cost(model_name, _u)
                if _cost is not None:
                    try:
                        hist.add_xiaomi_cost(_cost)
                    except Exception as e:
                        logger.warning("⚠️ Не удалось записать расход Xiaomi в БД: %s", e)
                _cached = (_u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                logger.info("%s Ответ от %s за %.1f с | контекст=%s (из кэша %s) | ответ=%s | размышления=%s | всего=%s | ≈$%.6f",
                            _icon_of(model_name), model_name, elapsed, _pt, _cached, _ct, _think, _tt, _cost or 0.0)
            elif _provider_of(model_name) == "qwen":
                # Qwen: точная стоимость по QWEN_PRICES (кэш из prompt_tokens_details,
                # рассуждения оплачиваются как выход) — копим в settings для панели API.
                # Для стоимости берём СЫРОЙ completion_tokens из usage: в графе «ответ»
                # выше рассуждения уже вычтены, а Alibaba биллит их в составе выхода.
                _cost = _qwen_cost(model_name, _u)
                if _cost is not None:
                    try:
                        hist.add_qwen_cost(_cost)
                    except Exception as e:
                        logger.warning("⚠️ Не удалось записать расход Qwen в БД: %s", e)
                # Израсходованные токены вычитаем из ОСТАТКА бесплатной квоты
                # Alibaba по КАЖДОЙ модели отдельно (вечный ключ settings
                # qwen_tokens_<модель>) — в панели API это число «осталось».
                # Берём «всего» (_tt): вход, ответ и размышления, ровно как
                # в строке лога ниже.
                try:
                    hist.spend_qwen_tokens(model_name, _tt)
                except Exception as e:
                    logger.warning("⚠️ Не удалось списать токены Qwen в БД: %s", e)
                _cached = (_u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                logger.info("%s Ответ от %s за %.1f с | контекст=%s (из кэша %s) | ответ=%s | размышления=%s | всего=%s | ≈$%.6f",
                            _icon_of(model_name), model_name, elapsed, _pt, _cached, _ct, _think, _tt, _cost or 0.0)
            else:
                logger.info("%s Ответ от %s за %.1f с | контекст=%s | ответ=%s | размышления=%s | всего=%s",
                            _icon_of(model_name), model_name, elapsed, _pt, _ct, _think, _tt)
            return data, model_name
        if i < len(chain) - 1:
            logger.warning("⚠️ Переключаюсь на запасную модель %s (активная %s не меняется)", chain[i + 1], active_model)
            time.sleep(1.5)

    return None, active_model


# ───────────────────────────────────────────────
#  Голосовые сообщения (native generateContent)
# ───────────────────────────────────────────────

def ask_gemini_audio(chat_id: int, user_id: int, audio_base64: str) -> str:
    """
    Отправляет голосовое сообщение пользователя в Gemini API (native generateContent).
    Стратегия устойчивости: 2 попытки на активной модели, затем фолбэк на FALLBACK_MODEL.
    Технические ошибки наружу не отдаются — при полном провале возвращается мягкое сообщение.
    """
    history = hist.get_history(user_id)

    is_admin = (user_id in ADMIN_IDS)
    bypass_prompt = is_admin and (hist.get_setting(f"admin_no_prompt_{user_id}", "0") == "1")

    if bypass_prompt:
        current_system_prompt = ""
    else:
        current_system_prompt, _, _ = hist.get_active_system_prompt()

    native_history = []
    for msg in history:
        content = (msg.get("content") or "").strip()
        if not content:
            continue  # пустые сообщения native API не принимает
        role = "user" if msg["role"] == "user" else "model"
        native_history.append({
            "role": role,
            "parts": [{"text": content}]
        })

    # native generateContent требует, чтобы беседа начиналась с роли user
    while native_history and native_history[0]["role"] != "user":
        native_history.pop(0)

    # 2026-07-24 (решение Максима): зашитая фраза «Ответь на это голосовое
    # сообщение пользователя.» УДАЛЕНА — модели уходит только сам файл, а как
    # на него отвечать, ей объясняет характер бота (systemInstruction ниже).
    # Не возвращать без его просьбы.
    native_history.append({
        "role": "user",
        "parts": [
            {"inlineData": {"mimeType": "audio/ogg", "data": audio_base64}}
        ]
    })

    payload = {
        "contents": native_history,
    }
    # Персонаж/системный промпт для native API передаётся через systemInstruction
    if current_system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": current_system_prompt}]}

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    def _try_audio(model_name: str, attempts: int = 2):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        for attempt in range(attempts):
            try:
                response = _http().post(url, json=payload, headers=headers, timeout=GEMINI_TIMEOUT)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(
                    "⚠️ Модель %s не ответила (попытка %s из %s, %s): %s%s",
                    model_name, attempt + 1, attempts, _err_code(e), e, _err_body(e),
                )
                if attempt < attempts - 1:
                    time.sleep((attempt + 1) * 2)
        return None

    # Цепочка аудио-моделей: активная первой (если она поддерживает аудио),
    # затем остальные совместимые. При сбое сразу пробуем СЛЕДУЮЩУЮ модель
    # (повтор той же модели на ошибке 500 почти бесполезен). Активная модель
    # при этом НЕ меняется. Gemma в цепочку не входит — она аудио не принимает.
    active_model = hist.get_setting("active_model", GEMINI_MODEL)
    chain = [active_model] if active_model in AUDIO_FALLBACK_CHAIN else []
    for m in AUDIO_FALLBACK_CHAIN:
        if m not in chain:
            chain.append(m)
    # идём по ВСЕМ аудио-совместимым моделям (Gemma в список не входит — аудио не принимает)

    if active_model not in AUDIO_FALLBACK_CHAIN:
        logger.info("%s Модель %s не поддерживает аудио — иду по цепочке аудио-моделей",
                    _icon_of(active_model), active_model)

    _first = chain[0] if chain else "—"
    logger.info("%s Запрос к модели %s (аудио)", _icon_of(_first), _first)

    data = None
    used_model = active_model
    elapsed = 0.0
    start = time.perf_counter()
    for i, model_name in enumerate(chain):
        data = _try_audio(model_name, attempts=1)
        if data is not None:
            used_model = model_name
            elapsed = time.perf_counter() - start
            break
        if i < len(chain) - 1:
            logger.warning("⚠️ Переключаюсь на запасную модель %s (активная %s не меняется)", chain[i + 1], active_model)
            time.sleep(1.5)

    if data is None:
        logger.error("⚠️ Не удалось получить аудио-ответ — недоступны все модели цепочки (пользователь %s)", user_id)
        return SOFT_FAIL_MESSAGE

    hist.register_api_call(used_model)
    # Ответила запасная модель вместо активной — сообщаем админам (не чаще раза
    # в час). Если активная модель аудио вообще не принимает, обход цепочки —
    # штатный режим, а не сбой, поэтому уведомления нет.
    if active_model in AUDIO_FALLBACK_CHAIN and used_model != active_model:
        _notify_admins_fallback(active_model, used_model)
    try:
        raw_answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        logger.error("⚠️ Неожиданный формат аудио-ответа Gemini API: %s", str(data)[:300])
        return SOFT_FAIL_MESSAGE

    answer = compress_newlines(raw_answer)
    usage = data.get("usageMetadata", {})
    prompt_tokens = usage.get("promptTokenCount", 0)
    total_tokens = usage.get("totalTokenCount", 0)
    # Модель + время ответа + токены в одной строке (у аудио только контекст и всего).
    logger.info("%s Ответ от %s за %.1f с | контекст=%s | всего=%s",
                _icon_of(used_model), used_model, elapsed, prompt_tokens, total_tokens)

    hist.add_messages(chat_id, user_id, "[Голосовое сообщение]", answer, prompt_tokens, used_model, total_tokens)
    return answer


# ───────────────────────────────────────────────
#  Видео (native generateContent)
# ───────────────────────────────────────────────

def ask_gemini_video(chat_id: int, user_id: int, video_base64: str,
                     user_text: str = "", mime_type: str = "video/mp4") -> str:
    """
    Отправляет ВИДЕО пользователя в Gemini API (native generateContent) — устроено
    как ask_gemini_audio, отличия по существу три:
      • своя цепочка моделей VIDEO_FALLBACK_CHAIN (видео принимают только Gemini,
        поле "video" в AVAILABLE_MODELS; у Qwen и DeepSeek его нет);
      • свой таймаут VIDEO_TIMEOUT — разбор ролика дольше расшифровки речи;
      • у видео БЫВАЕТ подпись (caption), у голосового её не бывает, — поэтому
        текст пользователя передаётся отдельным параметром и идёт в тот же запрос.
    Размер здесь НЕ проверяется: файл не пройдёт дальше 20 МБ ещё в обработчике
    (Telegram столько и не отдаст, см. VIDEO_MAX_BYTES).
    Технические ошибки наружу не отдаются — при полном провале SOFT_FAIL_MESSAGE.
    """
    history = hist.get_history(user_id)

    is_admin = (user_id in ADMIN_IDS)
    bypass_prompt = is_admin and (hist.get_setting(f"admin_no_prompt_{user_id}", "0") == "1")

    if bypass_prompt:
        current_system_prompt = ""
    else:
        current_system_prompt, _, _ = hist.get_active_system_prompt()

    native_history = []
    for msg in history:
        content = (msg.get("content") or "").strip()
        if not content:
            continue  # пустые сообщения native API не принимает
        role = "user" if msg["role"] == "user" else "model"
        native_history.append({
            "role": role,
            "parts": [{"text": content}]
        })

    # native generateContent требует, чтобы беседа начиналась с роли user
    while native_history and native_history[0]["role"] != "user":
        native_history.pop(0)

    # Модели уходит подпись пользователя, если она есть, и БОЛЬШЕ НИЧЕГО:
    # зашитая заготовка на случай «видео без подписи» удалена 2026-07-24 по
    # решению Максима — бот не должен подсказывать модели формулировки от себя.
    # Без подписи текстовой части в запросе просто нет, остаётся сам файл;
    # что с ним делать, модели объясняет характер бота (systemInstruction ниже).
    # Не возвращать заготовку без просьбы Максима.
    caption = (user_text or "").strip()
    video_parts = []
    if caption:
        video_parts.append({"text": caption})
    video_parts.append({"inlineData": {"mimeType": mime_type, "data": video_base64}})
    native_history.append({"role": "user", "parts": video_parts})

    payload = {"contents": native_history}
    if current_system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": current_system_prompt}]}

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    def _try_video(model_name: str, attempts: int = 1):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        for attempt in range(attempts):
            try:
                response = _http().post(url, json=payload, headers=headers, timeout=VIDEO_TIMEOUT)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(
                    "⚠️ Модель %s не ответила на видео (попытка %s из %s, %s): %s%s",
                    model_name, attempt + 1, attempts, _err_code(e), e, _err_body(e),
                )
                if attempt < attempts - 1:
                    time.sleep((attempt + 1) * 2)
        return None

    # Цепочка: активная первой (если она видео принимает), затем остальные.
    # Активная модель при этом НЕ меняется — подмена временная, на один запрос.
    # Порядок задаёт VIDEO_FALLBACK_CHAIN, но каждое звено ещё и проверяется
    # полем "video" — защита от кривого конфига: модель, попавшую в цепочку
    # по ошибке (или потерявшую поддержку видео), в запрос не пустим.
    active_model = hist.get_setting("active_model", GEMINI_MODEL)
    chain = [active_model] if _supports_video(active_model) else []
    for m in VIDEO_FALLBACK_CHAIN:
        if m not in chain and _supports_video(m):
            chain.append(m)

    if not _supports_video(active_model):
        logger.info("%s Модель %s не принимает видео — иду по цепочке видео-моделей",
                    _icon_of(active_model), active_model)

    _first = chain[0] if chain else "—"
    logger.info("%s Запрос к модели %s (видео)", _icon_of(_first), _first)

    data = None
    used_model = active_model
    elapsed = 0.0
    start = time.perf_counter()
    for i, model_name in enumerate(chain):
        data = _try_video(model_name, attempts=1)
        if data is not None:
            used_model = model_name
            elapsed = time.perf_counter() - start
            break
        if i < len(chain) - 1:
            logger.warning("⚠️ Переключаюсь на запасную модель %s (активная %s не меняется)",
                           chain[i + 1], active_model)
            time.sleep(1.5)

    if data is None:
        logger.error("⚠️ Не удалось разобрать видео — недоступны все модели цепочки (пользователь %s)", user_id)
        return SOFT_FAIL_MESSAGE

    hist.register_api_call(used_model)
    # Ответила запасная вместо активной — уведомляем админов (не чаще раза в час).
    # Если активная видео вообще не принимает, обход цепочки — штатный режим
    # (как у аудио и у vision-reroute), поэтому уведомления нет.
    if _supports_video(active_model) and used_model != active_model:
        _notify_admins_fallback(active_model, used_model)

    try:
        raw_answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        logger.error("⚠️ Неожиданный формат видео-ответа Gemini API: %s", str(data)[:300])
        return SOFT_FAIL_MESSAGE

    answer = compress_newlines(raw_answer)
    usage = data.get("usageMetadata", {})
    prompt_tokens = usage.get("promptTokenCount", 0)
    total_tokens = usage.get("totalTokenCount", 0)
    logger.info("%s Ответ от %s за %.1f с (видео) | контекст=%s | всего=%s",
                _icon_of(used_model), used_model, elapsed, prompt_tokens, total_tokens)

    # В контекст диалога пишем пометку с подписью пользователя, если она была, —
    # иначе в истории останется голое «[Видео]» без вопроса, к которому был ответ.
    context_note = f"[Видео] {caption}".strip() if caption else "[Видео]"
    hist.add_messages(chat_id, user_id, context_note, answer, prompt_tokens, used_model, total_tokens)
    return answer


# ───────────────────────────────────────────────
#  Основной запрос к модели (текст / Vision)
# ───────────────────────────────────────────────

def ask_gemini(chat_id: int, user_id: int, user_text: str, image_base64: str = None) -> str:
    """
    Отправляет сообщение пользователя в Gemini API вместе с объединённым
    контекстным окном (личка + группы одного пользователя).

    Контекст берётся по user_id (см. history.get_history). Сжатия контекста и
    лимита по токенам больше нет — размер окна задаётся MAX_CONTEXT_MESSAGES.

    Использует реальный подсчёт токенов из usage в ответе и сохраняет сообщения
    в БД только после успешного ответа. Поддерживает изображения (Vision).
    Технические ошибки наружу не отдаются (2 попытки + фолбэк на FALLBACK_MODEL).
    """
    # ── Формируем системный промпт (+ RAG при необходимости) ──
    history = hist.get_history(user_id)

    is_admin = (user_id in ADMIN_IDS)
    bypass_prompt = is_admin and (hist.get_setting(f"admin_no_prompt_{user_id}", "0") == "1")

    if bypass_prompt:
        current_system_prompt = ""
    else:
        current_system_prompt, _, _ = hist.get_active_system_prompt()

    # RAG работает независимо от тумблера «PROMPT ВЫКЛ» админа: база знаний —
    # источник фактов, а не часть «личности» бота (решение 2026-07-05).
    # Но у админа есть СВОЙ тумблер в панели /rag («RAG в моей личке»):
    # он глушит базу только в личке админа; группы и других пользователей
    # не затрагивает. В личке chat_id == user_id — по этому и отличаем.
    rag_muted_for_admin = (
        is_admin and chat_id == user_id
        and hist.get_setting(f"admin_no_rag_{user_id}", "0") == "1"
    )
    if RAG_ENABLED and not rag_muted_for_admin:
        try:
            import services.rag as rag_module
            rag_context = rag_module.retrieve_relevant_context(user_text)
            if rag_context:
                # «Шапка»-инструкция настраивается из Телеграма (панель /prompt,
                # /rag_prompt_set); по умолчанию — заводской RAG_INSTRUCTION.
                # Сами статьи всегда подставляются под ней.
                rag_instruction = hist.get_rag_instruction()
                rag_block = f"{rag_instruction}\n\n{rag_context}"
                current_system_prompt = (
                    f"{current_system_prompt}\n\n{rag_block}" if current_system_prompt else rag_block
                )
                logger.info("%s Контекст RAG добавлен в системный промпт", RAG_ICON)
        except Exception as rag_err:
            logger.error("⚠️ Не удалось добавить контекст RAG: %s", rag_err)

    # ── Справка об авторе при ПРЯМОМ обращении в группе (2026-07-26) ──
    # Решение Максима: когда бота зовут через @ или отвечают на его сообщение,
    # он должен знать собеседника так же, как в режиме «Сам в разговор» —
    # иначе на вопрос «ты знаешь, кто я?» бот честно отвечал «нет».
    # ТОЛЬКО группы: в личке chat_id == user_id, там собеседник один и
    # представлять его незачем. Идёт ПОСЛЕ базы знаний — тот же порядок, что
    # в _build_proactive_parts, чтобы два пути не разъезжались.
    if chat_id != user_id:
        try:
            who = _who_is_talking(user_id)
            if who:
                current_system_prompt = (
                    f"{current_system_prompt}\n\n{who}" if current_system_prompt else who
                )
        except Exception as who_err:
            logger.debug("🤖 Не удалось добавить справку об авторе в запрос: %s", who_err)

    if image_base64:
        user_message_content = [
            {"type": "text", "text": user_text if user_text else ""},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]
    else:
        user_message_content = user_text

    if current_system_prompt:
        messages = (
            [{"role": "system", "content": current_system_prompt}]
            + history
            + [{"role": "user", "content": user_message_content}]
        )
    else:
        messages = (
            history
            + [{"role": "user", "content": user_message_content}]
        )

    # ── Отправляем (2 попытки + авто-фолбэк на FALLBACK_MODEL) ──
    # has_image включает vision-reroute: фото у «слепой» модели (DeepSeek/Qwen)
    # сразу анализирует цепочка Gemini, без провальной попытки и ложной тревоги.
    data, used_model = _gemini_chat_request(
        messages,
        kind="фото на анализ" if image_base64 else "текст",
        has_image=bool(image_base64),
    )

    if data is None:
        logger.error("⚠️ Не удалось получить ответ — недоступны все модели цепочки (пользователь %s)", user_id)
        return SOFT_FAIL_MESSAGE

    try:
        raw_answer = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        logger.error("⚠️ Неожиданный формат ответа Gemini API: %s", str(data)[:300])
        return SOFT_FAIL_MESSAGE

    answer = compress_newlines(raw_answer)

    # Реальный подсчёт токенов от Gemini API (нужен для сохранения в БД).
    # Отдельная строка лога убрана — токены теперь в строке «✅ Ответила …».
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)

    # ── Сохраняем в БД только после успешного ответа ──
    db_text = user_text if user_text else "[Фотография]"
    if image_base64 and user_text:
        db_text = f"[Фотография] {user_text}"

    # Очищаем ответ от блока мыслей <thought> перед сохранением (экономия контекста)
    db_answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL | re.IGNORECASE).strip()

    hist.add_messages(chat_id, user_id, db_text, db_answer, prompt_tokens, used_model, total_tokens)
    return answer


# ───────────────────────────────────────────────
#  Форматирование новостей
# ───────────────────────────────────────────────

def format_news_as_colonel(title: str, description: str, tag: str, article_text: str = "") -> str:
    """
    Готовит текст новости к рассылке.

    Логика:
      • Промпт новостей задан (ключ 'news_system_prompt', /news_prompt_set):
        в модель уходит ТОЛЬКО текст статьи (как user-сообщение) + промпт (как system).
        Никаких служебных надписей. Возвращается ответ модели.
      • Промпт не задан: модель НЕ вызывается. Возвращается текст статьи как есть
        (далее он форматируется telegramify и рассылается подписчикам).
      • Если API недоступен (при заданном промпте) — возвращается сырой текст
        статьи без каких-либо дополнительных надписей.

    :param article_text: текст статьи из scraper.fetch_article. Если пуст (парсер
                         не смог скачать статью) — как страховка берётся короткий анонс.
    """
    news_sys_prompt = hist.get_news_system_prompt()
    content = article_text or description  # текст с сайта; анонс — только как страховка

    # Промпт не задан → модель не трогаем, отдаём текст как есть
    if not news_sys_prompt:
        return content

    # Промпт задан → в модель уходит только текст статьи + системный промпт
    messages = [
        {"role": "system", "content": news_sys_prompt},
        {"role": "user", "content": content},
    ]

    data, _ = _gemini_chat_request(messages)
    if data is not None:
        try:
            return compress_newlines(data["choices"][0]["message"]["content"].strip())
        except (KeyError, IndexError):
            logger.error("⚠️ Неожиданный формат ответа при форматировании новости")

    # Резервный вариант (API недоступен) — сырой текст статьи без доп. надписей
    return content


# ───────────────────────────────────────────────
#  Проактивное участие в разговоре групп («Сам в разговор»)
# ───────────────────────────────────────────────

# Максимальная длина одной строки стенограммы (защита от «простыней» в промпте)
_PROACTIVE_LINE_MAX = 300


def _is_proactive_skip(body: str) -> bool:
    """
    True, если ответ модели — решение промолчать.
    Модель просят ответить ровно словом-маркером (PROACTIVE_SKIP_MARKER),
    но на практике вокруг бывают кавычки/точки/переносы — чистим и сверяем.
    Перекос намеренно в сторону «промолчать»: лучше потерять редкую реплику,
    начинающуюся со слова «пропуск», чем отправить маркер в чат.
    """
    if not body:
        return True
    cleaned = body.strip().strip('«»"\'`*_.,!?:;()[]— \n\t')
    if len(cleaned) < 2:
        return True
    return cleaned.upper().startswith(PROACTIVE_SKIP_MARKER)


def _who_is_talking(user_id: int | None) -> str:
    """
    Справка об авторе сообщения (2026-07-20; состав пересмотрен 2026-07-26).

    Модель должна понимать, С КЕМ говорит — чтобы обратиться по имени и не
    хамить персоналу. Состав СОЗНАТЕЛЬНО короткий: имя с ником, роль
    (владелец/модератор) и почётное звание.

    ⚠️ Что и почему УБРАНО 2026-07-26 (решения Максима, не возвращать без его
    просьбы): стаж в группах, число сообщений, счётчики мутов и удалённых
    ссылок, статус «проверенный/обычный». Довод Максима: цифры моделью
    никак не используются — «модель просто будет знать и всё», а история
    нарушений не должна влиять на разговор. Поэтому здесь больше НЕТ вызова
    antispam.trust_info: судить о поведении бот должен по тому, что видит
    в чате сейчас.

    Уходит модели в ДВУХ местах: режим «Сам в разговор»
    (_build_proactive_parts) и прямое обращение в ГРУППЕ (ask_gemini).
    В личке НЕ добавляется — там собеседник и так один.

    Возвращает готовый блок текста или "" (ни имени, ни ника / любая ошибка —
    модуль не должен ронять проверку из-за справки).
    """
    if not user_id:
        return ""
    try:
        # Локальные импорты: user_settings и roles тянут за собой БД,
        # а gemini.py грузится раньше них — на верхнем уровне вышло бы кольцо.
        from services.user_settings import honorary_rank
        from services import roles

        d = hist.get_dossier(user_id) or {}
        uname = (d.get("username") or "").strip()
        fname = (d.get("first_name") or "").strip()
        # Ни имени, ни ника — представлять некого, справку не собираем вовсе.
        if not fname and not uname:
            return ""

        who = fname or f"@{uname}"
        if fname and uname:
            who += f" (@{uname})"

        # Роль: владельца модель должна узнавать сразу (решение Максима
        # 2026-07-23), модератора — тоже: иначе бот грозит мутом персоналу,
        # которого всё равно не выдаст (защита в antispam._manual_guard).
        if user_id in ADMIN_IDS:
            # Просто «Владелец», без слова «бота» — решение Максима 2026-07-26
            # (тем же словом он подписан и в карточке). Не «уточнять» обратно.
            who += " — Владелец"
        elif roles.is_moderator(user_id):
            who += " — модератор"

        rank = honorary_rank(user_id)
        if rank:
            who += f"; звание: {rank}"

        return (
            "[С кем ты говоришь]\n"
            "Справка на автора последнего сообщения (тебе, не для пересказа вслух):\n"
            f"{who}."
        )
    except Exception as e:
        logger.debug("🤖 Не удалось собрать справку об авторе %s: %s", user_id, e)
        return ""


def author_brief(user_id: int | None) -> str:
    """ПУБЛИЧНОЕ имя справки об авторе — тот же текст, что уходит модели
    в режиме «Сам в разговор» (2026-07-26, для показа в панели промптов).

    Нужна, чтобы панель не звала приватную `_who_is_talking` напрямую и чтобы
    показанное владельцу и отправленное модели ГАРАНТИРОВАННО не разъехались:
    источник один. Меняешь состав справки — правишь `_who_is_talking`, панель
    подхватит сама.
    """
    return _who_is_talking(user_id)


def _build_proactive_parts(chat_id: int, bot_id: int, trigger_text: str,
                           trigger_user_id: int | None) -> list[str] | None:
    """
    Подготовка контекста для проактивного режима: характер, RAG, справка об
    авторе, инструкция участия (с блоком рук внутри), стенограмма чата.

    Порядок частей важен: характер и должностная инструкция → факты из базы →
    справка об авторе → правила участия (включая блок рук) → стенограмма.
    Инструкция участия идёт ПОСЛЕ системного промпта, поэтому её правила
    (например «в реплике никакой разметки») перекрывают общие правила
    оформления — так и задумано.

    Возвращает список кусков системного промпта или None, если говорить не о чем
    (пустая стенограмма — например, сразу после кнопки «Очистить РАЗГОВОРЫ»).
    """
    # Размер стенограммы настраивается из панели (регулятор «контекст»);
    # settings хранит строки — приводим к int с фолбэком на дефолт конфига.
    try:
        context_msgs = int(hist.get_setting("proactive_context_msgs", str(PROACTIVE_CONTEXT_MSGS)))
    except (TypeError, ValueError):
        context_msgs = PROACTIVE_CONTEXT_MSGS

    rows = hist.get_recent_group_messages(chat_id, context_msgs)
    if not rows:
        return None

    # ── Стенограмма беседы: «Имя: текст», свои реплики бота — «Ты: …» ──
    lines = []
    for r in rows:
        if r["user_id"] == bot_id:
            name = "Ты"
        else:
            # Подпись у ВСЕХ одинаковая — по имени (2026-07-26, решение
            # Максима). Прежнее исключение «владельца подписывать ником»
            # (2026-07-23) ОТМЕНЕНО им же: он хочет, чтобы в стенограмме он
            # выглядел как остальные участники. Не возвращать без его просьбы —
            # то, что он владелец, модель узнаёт из справки об авторе.
            name = r["first_name"] or (f"@{r['username']}" if r["username"] else f"Участник {r['user_id']}")
        text = (r["text"] or "").strip()
        # Команды боту (/clear, /rank, /help, /imagine …) — не часть беседы:
        # они адресованы механизму, а не собеседникам, и в стенограмме только
        # шум. Особенно /clear: он стирает личный контекст человека, само
        # сообщение Telegram сразу удаляет — но в архив групп оно попасть
        # успевает, и бот видел стёртое (решение Максима 2026-07-24).
        # Фильтр стоит ЗДЕСЬ, а не в архиваторе: архив нужен целым для счётчика
        # /stats и запасного источника имён в /users.
        if text.startswith("/"):
            continue
        if r["has_voice"]:
            text = f"[голосовое] {text}".strip()
        elif r["has_photo"]:
            text = f"[фото] {text}".strip()
        elif r.get("has_video"):
            text = f"[видео] {text}".strip()
        if not text:
            continue
        if len(text) > _PROACTIVE_LINE_MAX:
            text = text[:_PROACTIVE_LINE_MAX] + "…"
        lines.append(f"{name}: {text}")
    if not lines:
        return None

    # Если триггер — медиа с готовым результатом анализа (описание фото /
    # расшифровка голосового / описание видео), подменяем пометку
    # [фото]/[голосовое]/[видео] на реальный текст. В БД у таких сообщений text
    # пустой, а trigger_text уже содержит результат работы
    # _proactive_describe_image / _proactive_transcribe_audio / _proactive_describe_video.
    if trigger_text and rows:
        last_row = rows[-1]
        if (last_row.get("has_voice") or last_row.get("has_photo")
                or last_row.get("has_video")) and not (last_row.get("text") or "").strip():
            name_part = lines[-1].split(":", 1)[0]
            lines[-1] = f"{name_part}: {trigger_text}"

    transcript = "\n".join(lines)

    # ── Характер / должностная инструкция ──
    # Персональный тумблер «PROMPT ВЫКЛ» админа тут не действует: это групповой
    # контекст, бот говорит своим обычным характером.
    system_parts = []
    persona, _, _ = hist.get_active_system_prompt()
    if persona:
        system_parts.append(persona)

    # RAG по последнему сообщению (триггеру) — как в ask_gemini: если чат
    # обсуждает игру, бот подтянет факты из базы знаний. Персональный тумблер
    # «RAG в моей личке» не действует — он только для лички админа.
    if RAG_ENABLED and trigger_text:
        try:
            import services.rag as rag_module
            rag_context = rag_module.retrieve_relevant_context(trigger_text)
            if rag_context:
                rag_instruction = hist.get_rag_instruction()
                system_parts.append(f"{rag_instruction}\n\n{rag_context}")
                logger.info("%s Контекст RAG добавлен в проактивную проверку", RAG_ICON)
        except Exception as rag_err:
            logger.error("⚠️ Не удалось добавить контекст RAG в проактивную проверку: %s", rag_err)

    who = _who_is_talking(trigger_user_id)
    if who:
        system_parts.append(who)

    system_parts.append(hist.get_proactive_instruction())

    system_parts.append(f"[Последние сообщения чата]\n{transcript}")
    return system_parts


def ask_group_proactive(chat_id: int, bot_id: int, trigger_text: str,
                        trigger_user_id: int | None = None) -> str | None:
    """
    Проактивное участие в разговоре группы: запрос к АКТИВНОЙ думающей модели,
    которая И решает «вступать или промолчать», И пишет реплику (одношаговая
    схема). Судья удалён 2026-07-20 — быстрые модели без мышления плохо
    следуют инструкциям.

    В модель уходит: характер и должностная инструкция (системный промпт) +
    RAG-контекст по последнему сообщению + справка об авторе + инструкция
    участия (get_proactive_instruction, включает блок рук) + стенограмма чата.

    Возвращает текст реплики (С блоком <thought> — send_formatted покажет его
    свёрнутыми «Мыслями», как в обычных ответах) или None (решение промолчать /
    любая ошибка). Личная память диалогов (add_messages) НЕ трогается —
    проактивные реплики живут только в архиве групп (туда — без мыслей).
    Синхронная (requests) — вызывать через run_in_executor, как ask_gemini.
    """
    system_parts = _build_proactive_parts(chat_id, bot_id, trigger_text, trigger_user_id)
    if system_parts is None:
        return None

    task = (
        "Твоё решение: вступить в разговор (напиши только текст реплики) "
        f"или промолчать (ответь ровно одним словом: {PROACTIVE_SKIP_MARKER})."
    )

    messages = [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": task},
    ]

    data, used_model = _gemini_chat_request(messages, kind="группа (сам)")
    if data is None:
        # Все модели цепочки недоступны — молчим (тишина = штатный исход,
        # никакого SOFT_FAIL_MESSAGE в чат).
        return None

    try:
        raw_answer = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        logger.error("⚠️ Неожиданный формат ответа при проактивной проверке: %s", str(data)[:300])
        return None

    answer = compress_newlines(raw_answer)

    # Маркер «промолчать» ищем в ВИДИМОЙ части ответа (без блока <thought> —
    # думающая модель может прислать «<thought>…</thought>ПРОПУСК»).
    body = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL | re.IGNORECASE).strip()
    if _is_proactive_skip(body):
        return None
    # Возвращаем ответ ЦЕЛИКОМ, с мыслями: send_formatted покажет их свёрнутым
    # блоком «Мысли», как в обычных ответах (решение Максима 2026-07-16).
    return answer


# ───────────────────────────────────────────────
#  Генерация изображений (Nano Banana, gemini-*-image)
# ───────────────────────────────────────────────

def _image_cost(model_name: str, usage: dict):
    """Точная стоимость генерации картинки в долларах по ценам IMAGE_PRICES.
    Google тарифицирует по токенам: текстовый вход (промпт) + графический выход
    (картинка 1K = 1120 output-токенов). Токены берём из usageMetadata по
    модальностям. None — модели нет в таблице цен."""
    prices = IMAGE_PRICES.get(model_name)
    if not prices:
        return None
    text_in = 0
    for d in usage.get("promptTokensDetails", []):
        if d.get("modality") == "TEXT":
            text_in += d.get("tokenCount", 0) or 0
    if not text_in:
        text_in = usage.get("promptTokenCount", 0) or 0
    img_out = text_out = 0
    for d in usage.get("candidatesTokensDetails", []):
        if d.get("modality") == "IMAGE":
            img_out += d.get("tokenCount", 0) or 0
        elif d.get("modality") == "TEXT":
            text_out += d.get("tokenCount", 0) or 0
    return (text_in * prices["in"] + img_out * prices["img_out"] + text_out * prices["txt_out"]) / 1_000_000


def generate_image(prompt: str) -> bytes:
    """
    Генерирует изображение моделями Google «Nano Banana» (gemini-*-image)
    через generateContent. Imagen (эндпоинт predict) отключён Google 17.08.2026,
    поэтому формат другой: картинка возвращается как inlineData внутри parts
    ответа (у Nano Banana в parts рядом бывает thoughtSignature — берём именно
    часть с данными). Возвращает байты картинки или None при ошибке.
    """
    active_image_model = hist.get_setting("active_image_model", "gemini-3.1-flash-image")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{active_image_model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_IMAGEN_API_KEY
    }
    # responseModalities=["IMAGE"] — просим вернуть именно картинку (подтверждено
    # живым тестом 2026-07-05). Разрешение по умолчанию 1K (общее для обеих моделей).
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }

    logger.info("🎨 Запрос на генерацию картинки (модель %s)", active_image_model)

    start = time.perf_counter()
    try:
        response = _http().post(url, headers=headers, json=payload, timeout=90)
        response.raise_for_status()
        data = response.json()

        img_b64 = None
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    img_b64 = inline["data"]
                    break
            if img_b64:
                break

        if not img_b64:
            # data обрезаем: в ответе могут быть мегабайты base64 — не заливаем лог.
            logger.error("⚠️ Не удалось извлечь картинку из ответа модели %s: %s", active_image_model, str(data)[:300])
            return None

        elapsed = time.perf_counter() - start
        # Учитываем вызов в общей статистике (раньше картинки нигде не считались)
        # и копим точную стоимость по токенам из usageMetadata (цены IMAGE_PRICES).
        try:
            hist.register_api_call(active_image_model)
        except Exception as e:
            logger.warning("⚠️ Не удалось учесть вызов картинки в статистике: %s", e)
        cost = _image_cost(active_image_model, data.get("usageMetadata", {}))
        if cost is not None:
            try:
                hist.add_image_cost(cost)
            except Exception as e:
                logger.warning("⚠️ Не удалось записать расход картинок в БД: %s", e)
            logger.info("🎨 Картинка сгенерирована за %.1f с (модель %s) | ≈$%.6f",
                        elapsed, active_image_model, cost)
        else:
            logger.info("🎨 Картинка сгенерирована за %.1f с (модель %s)", elapsed, active_image_model)
        return base64.b64decode(img_b64)
    except requests.exceptions.HTTPError as http_err:
        err_text = http_err.response.text if http_err.response else str(http_err)
        logger.error("⚠️ Не удалось сгенерировать картинку (модель %s, %s): %s", active_image_model, _err_code(http_err), err_text[:500])
        return None
    except Exception as e:
        logger.error("⚠️ Не удалось сгенерировать картинку (модель %s): %s", active_image_model, e)
        return None
