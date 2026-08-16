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
import threading
import time
import json
import base64
import requests

from config import (
    PROACTIVE_MEDIA_CHAIN,
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
# Единственная «витрина» среза мыслей — своей второй копии не заводить.
# utils_format ничего из services не импортирует, кольца зависимостей нет.
from utils_format import strip_thoughts

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
# ⚠️ Замок к метке времени: запросы к моделям идут из РАЗНЫХ рабочих потоков
# (run_in_executor), и без него два одновременно отказавших запроса успевали
# оба пройти проверку «прошёл ли час» до того, как первый обновит метку —
# в личку прилетала пара одинаковых уведомлений вместо одного.
_fallback_notify_lock = threading.Lock()


def _notify_admins_fallback(active_model: str, used_model: str) -> None:
    """Шлёт админам в личку «активная модель не ответила, работаю на запасной»."""
    global _last_fallback_notify
    now = time.monotonic()
    # Проверка «прошёл ли час» и обновление метки — одной неделимой операцией.
    with _fallback_notify_lock:
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
    """Какому сервису принадлежит модель: 'gemini' (по умолчанию), 'qwen',
    'deepseek' или 'xiaomi'."""
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
#
#  ⚠️ ВМЕСТЕ С МЕДИА МОДЕЛИ НЕ УХОДИТ НИ ОДНОГО СЛОВА — только сам файл.
#  Зашитых фраз здесь быть не должно ни одной: сочинять формулировки от имени
#  бота запрещено решением Максима (2026-07-24). Настраиваемый промпт разбора
#  тут был с 2026-08-05 по 2026-08-11 и убран по его же решению — если слова
#  к медиа понадобятся снова, это отдельная просьба, а не «мелкая правка».
#
#  ⚠️ 2026-08-10, две просьбы Максима разом:
#   1. ЦЕПОЧКА ПОДСТРАХОВКИ вместо одной зашитой модели — `PROACTIVE_MEDIA_CHAIN`.
#      Раньше единственная flash-lite не ответила → описания нет → для фото и
#      видео без подписи бот молча пропускал сообщение.
#   2. МАКСИМАЛЬНОЕ МЫШЛЕНИЕ у разборщиков (`thinking_budget: -1` — динамический
#      максимум, тот же, что у обычных ответов бота).
#
#  ⚠️ МЫСЛИ ЗАПРАШИВАЮТСЯ, НО НЕ ПОКАЗЫВАЮТСЯ (`include_thoughts: False`), и
#  это не мелочь: результат разбора идёт В СТЕНОГРАММУ как реплика участника.
#  Придут мысли — активная модель прочтёт «размышляю: кажется, это танк…» как
#  слова человека. Плюс `_native_text_only` ниже на всякий случай выбрасывает
#  части с флагом thought: нативный разбор раньше брал parts[0], а первой
#  частью у думающей модели приходит как раз мысль.


def _native_text_only(data: dict) -> str:
    """
    Текст ответа нативного API БЕЗ мыслей: части с флагом "thought" отброшены.

    Отдельная от `_native_answer_with_thoughts` намеренно: та собирает ответ
    ДЛЯ ЧЕЛОВЕКА и заворачивает мысли в <thought>, чтобы Телеграм показал их
    свёрнутой цитатой. Здесь ответ идёт в стенограмму для другой модели —
    мысли в нём не нужны ни в каком виде.
    """
    parts = data["candidates"][0]["content"]["parts"]
    texts = [p.get("text", "") for p in parts if not p.get("thought")]
    return "".join(texts).strip()


#  ⚠️ РАЗБОРУ МЫШЛЕНИЕ НЕ НУЖНО (2026-08-11, решение Максима). Сутки до этого
#  здесь стоял «максимум» — и он стоил 9 секунд на КАЖДОЕ фото: разбор не
#  рассуждает, он описывает, что видит. Из-за этих секунд проактивная проверка
#  растягивалась за полминуты, а всё, что люди писали в чат за это время,
#  отсеивалось защёлкой `_in_flight` — бот пропустил прямое оскорбление,
#  потому что «был занят» разбором картинки.
#
#  ⚠️ Уровень называется `thinkingLevel` (minimal / low / medium / high) — это
#  параметр ПОКОЛЕНИЯ GEMINI 3. Прежний `thinkingBudget: -1` — из 2.5-й серии;
#  он работает (живой замер 11.08: с ним мысли есть, с `minimal` их ровно 0),
#  но означает «думай сколько сочтёшь нужным», а не максимум. Мышление ОТВЕТА
#  (`ask_group_proactive_media`) при этом не трогаем — там оно по делу.
_MEDIA_THINKING_LEVEL = "minimal"

# ⚠️ У ВИДЕО СВОЙ УРОВЕНЬ — «medium» (2026-08-11, решение Максима). Ролик это
# не картинка: там важна последовательность событий — кто кого подбил, чем
# кончился бой, — а на «minimal» разбор выходит поверхностным («игрок едет по
# карте»), и в стенограмму попадает пустышка. «medium» — это уровень Google
# ПО УМОЛЧАНИЮ, то есть середина, а не крайность; выбран сознательно, чтобы
# двигаться ступенями и смотреть на живых роликах.
#
# ⚠️ ЗАМЕР 11.08 на боевом ключе (gemini-3.1-flash-lite, один вопрос):
# minimal — 0 токенов мыслей, low — 114, medium — 437, high — 807. Все четыре
# уровня API принимает; шкала ровная, так что при желании поднять до «high»
# правится ровно эта константа.
#
# ⚠️ ЦЕНА: разбор видео и так самый долгий (таймаут 180 с), а пока идёт
# проверка, сообщения чата отсеиваются защёлкой `_in_flight`. Станет заметно
# мешать — сначала думать про уровень, а не про потолок размера.
_VIDEO_THINKING_LEVEL = "medium"


def _media_thinking_native(level: str = _MEDIA_THINKING_LEVEL) -> dict:
    """Настройка мышления для разбора медиа (аудио, видео) — нативный формат."""
    return {"thinkingConfig": {"includeThoughts": False, "thinkingLevel": level}}


def _media_answer_thinking() -> dict:
    """
    Мышление для ОТВЕТА на медиа в группе — верхняя ступень (2026-08-11,
    решение Максима). Здесь модель не описывает картинку, а решает, вступать
    ли в чужой разговор, и пишет реплику от лица бота: думать есть о чём.

    ⚠️ Отдельно от `_native_thinking_config`, которой пользуются обычные ответы
    на аудио и видео: там до сих пор `thinkingBudget: -1` — параметр 2.5-й
    серии, означающий «думай сколько сочтёшь нужным». Здесь явный
    `thinkingLevel: "high"` — по живому замеру 11.08 это ~855 токенов мыслей
    против ~672 у динамического (gemini-3.6-flash, 3 прогона).

    ⚠️ Мысли ЗАПРАШИВАЮТСЯ (в отличие от разбора): реплика уходит человеку
    через send_formatted, который показывает их свёрнутой цитатой.

    ⚠️ ЦЕНА: ответ думает дольше, а пока идёт проверка, сообщения чата
    отсеиваются защёлкой `_in_flight` — они не пропадают (счётчик растёт, и
    следующая проверка увидит их в стенограмме), но реакция запаздывает.
    Догоняющую проверку под это заводили 2026-08-11 и в тот же день убрали по
    решению Максима — заново не заводить без его просьбы.
    """
    return {"thinkingConfig": {"includeThoughts": True, "thinkingLevel": "high"}}


def _media_thinking_openai() -> dict:
    """То же для OpenAI-совместимого пути (фото): формат у Google другой."""
    return {"google": {"thinking_config": {"include_thoughts": False,
                                           "thinking_level": _MEDIA_THINKING_LEVEL}}}


# ── Модель, исчерпавшая квоту, уходит на скамейку ────────────────────────
#
#  ⚠️ 2026-08-11 (решение Максима). У `gemini-3.5-flash` кончилась квота, и
#  бот стучался в неё на КАЖДОМ медиа: сначала в разборе, потом в ответе.
#  Каждая попытка — впустую потраченное время в чужом чате, где счёт идёт на
#  секунды. Теперь модель, вернувшая 429, пропускается следующие
#  `_QUOTA_COOLDOWN_SEC`, и цепочка сразу идёт к живой.
#
#  ⚠️ В памяти процесса, как счётчики антиспама: перезапуск обнуляет — и это
#  правильно, квоты Google к тому времени могли и восстановиться.
#  ⚠️ Касается ТОЛЬКО цепочки разбора и проактивного ответа на медиа. Обычные
#  пути (ask_gemini / аудио / видео при прямом обращении) не тронуты: там свои
#  фолбэки и уведомления админам, и лезть туда этой правкой не просили.
_QUOTA_COOLDOWN_SEC = 600
_quota_blocked: dict[str, float] = {}


def _quota_blocked_now(model_name: str) -> bool:
    """Стоит ли пропустить модель: она недавно ответила «квота исчерпана»."""
    return _quota_blocked.get(model_name, 0.0) > time.monotonic()


def _note_quota_error(model_name: str, err: Exception) -> bool:
    """
    True, если ошибка — это 429 (квота). Тогда модель отправляется на скамейку.
    Проверяем по тексту ошибки: requests отдаёт HTTPError со строкой вида
    «429 Client Error: Too Many Requests for url: …».
    """
    if "429" not in str(err):
        return False
    _quota_blocked[model_name] = time.monotonic() + _QUOTA_COOLDOWN_SEC
    logger.warning("🤖 %s исчерпала квоту — не трогаем её %d минут",
                   model_name, _QUOTA_COOLDOWN_SEC // 60)
    return True


def _media_chain(chain_limit: int = 0) -> list:
    """
    Живые звенья цепочки разбора медиа `PROACTIVE_MEDIA_CHAIN`: без тех, что
    недавно вернули 429 и сидят на скамейке (_quota_blocked_now).

    chain_limit > 0 — взять только первые N живых. Так ходит РАЗБОР РАДИ
    ПОИСКА по базе знаний (16.08.2026): там человек ждёт ответа на своё
    сообщение, и перебор всей цепочки по таймауту стоил бы ему минут — у
    видео таймаут 90 секунд на модель, четыре звена дают до шести минут.
    Проактивному режиму ограничение не нужно: он работает фоном, и там
    ценность полного перебора выше цены ожидания — поэтому 0 по умолчанию.
    """
    live = [m for m in PROACTIVE_MEDIA_CHAIN if not _quota_blocked_now(m)]
    return live[:chain_limit] if chain_limit else live


def _describe_image(image_base64: str, chain_limit: int = 0) -> str:
    """
    Описывает фото: нестриминговый запрос к Gemini ПО ЦЕПОЧКЕ
    `PROACTIVE_MEDIA_CHAIN` — первая ответившая модель и выигрывает.
    Без истории, системного промпта и RAG — только описание картинки.
    Мышление на максимуме, мысли в ответ не запрашиваются (см. шапку блока).

    Модели уходит ГОЛЫЙ файл, без единого слова — что с ним делать, она решает
    сама (см. шапку блока: слов к медиа бот от себя не добавляет).

    Зовут её ДВОЕ (с 16.08.2026): режим «Сам в разговор» (описание идёт в
    стенограмму как речь участника) и поиск по базе знаний в ask_gemini
    (описание идёт только в поисковый запрос, модели НЕ показывается).
    Поэтому в имени и в логах больше нет слова «proactive» — оно врало бы
    половину времени. chain_limit — см. _media_chain.

    Возвращает описание или пустую строку, если НИ ОДНА модель цепочки не
    ответила. Провал разбора не ломает ни того, ни другого вызывающего:
    у проактивного триггером останется подпись, у поиска — тоже.
    """
    content = [{"type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}]

    for model_name in _media_chain(chain_limit):
        try:
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
                "extra_body": _media_thinking_openai(),
            }
            logger.info("🤖 Запрос к модели %s (описание фото)", model_name)
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
            text = (data["choices"][0]["message"]["content"] or "").strip()
            if not text:
                # Пустой ответ — тоже отказ: идём к следующей модели, иначе в
                # стенограмму уйдут пустые скобки вместо разбора.
                logger.warning("🤖 %s вернула пустое описание фото — пробую следующую", model_name)
                continue
            logger.info("🤖 Ответ от %s за %.1f с (описание фото)", model_name, elapsed)
            return text
        except Exception as e:
            # ⚠️ warning, а не debug (2026-08-10): молчаливый сбой разбора
            # выглядит как «бот проигнорировал сообщение» и не находится в логе.
            if not _note_quota_error(model_name, e):
                logger.warning("🤖 %s не описала фото: %s", model_name, e)
    logger.error("⚠️ 🤖 Фото не описала НИ ОДНА модель цепочки")
    return ""


def _transcribe_audio(audio_base64: str, chain_limit: int = 0) -> str:
    """
    Расшифровывает голосовое: лёгкий запрос к native Gemini generateContent.
    Без истории и системного промпта — модели уходит только сам файл, без
    единого слова от бота.

    Идёт ПО ЦЕПОЧКЕ `PROACTIVE_MEDIA_CHAIN`, мышление на максимуме, мысли в
    ответ не запрашиваются и на всякий случай отбрасываются (`_native_text_only`).

    Зовут её двое (с 16.08.2026): режим «Сам в разговор» и поиск по базе
    знаний в ask_gemini_audio — там расшифровка идёт ТОЛЬКО в поисковый
    запрос, а само голосовое основная модель слушает сама. chain_limit —
    см. _media_chain.

    Возвращает текст расшифровки или пустую строку, если не ответила ни одна
    модель цепочки. Провал ничего не ломает: проактивный просто не отреагирует
    на голосовое, а поиск по базе будет пропущен.
    """
    parts = [{"inlineData": {"mimeType": "audio/ogg", "data": audio_base64}}]

    for model_name in _media_chain(chain_limit):
        try:
            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": _media_thinking_native(),
            }
            logger.info("🤖 Запрос к модели %s (расшифровка аудио)", model_name)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
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
            text = _native_text_only(data)
            if not text:
                logger.warning("🤖 %s вернула пустую расшифровку — пробую следующую", model_name)
                continue
            logger.info("🤖 Ответ от %s за %.1f с (расшифровка аудио)", model_name, elapsed)
            return text
        except Exception as e:
            if not _note_quota_error(model_name, e):
                logger.warning("🤖 %s не расшифровала голосовое: %s", model_name, e)
    logger.error("⚠️ 🤖 Голосовое не расшифровала НИ ОДНА модель цепочки")
    return ""


def _describe_video(video_base64: str, mime_type: str = "video/mp4",
                    chain_limit: int = 0) -> str:
    """
    Описывает ВИДЕО (2026-07-24) — устроено как _describe_image/_transcribe_audio:
    лёгкий запрос к gemini-3.1-flash-lite без истории и системного промпта.

    Модели уходит голый файл, без единого слова от бота: насколько кратко
    описывать, она решает сама. Таймаут больше, чем у фото и аудио: разбор
    видео дольше.

    Идёт ПО ЦЕПОЧКЕ `PROACTIVE_MEDIA_CHAIN`, мышление на максимуме, мысли не
    показываются и отбрасываются (`_native_text_only`).

    Зовут её двое (с 16.08.2026): режим «Сам в разговор» и поиск по базе
    знаний в ask_gemini_video — там описание идёт ТОЛЬКО в поисковый запрос,
    а сам ролик основная модель смотрит сама.

    Возвращает описание или пустую строку, если не ответила ни одна модель —
    сбой не ломает ни проактивную проверку, ни поиск по базе.

    ⚠️ ХУДШИЙ СЛУЧАЙ ПО ВРЕМЕНИ ЗДЕСЬ САМЫЙ ДОЛГИЙ: таймаут 90 секунд на
    модель, в цепочке их четыре — при полном отказе Google перебор займёт до
    шести минут. Проактивной проверке это не страшно (фоновая задача), а вот
    ПРЯМОМУ ответу страшно: там ждёт живой человек. Поэтому ask_gemini_video
    зовёт этот разбор с chain_limit=1 — одна живая модель, не больше полутора
    минут сверху. Резать сам таймаут по-прежнему нельзя: 90 секунд ролику
    нужны честно.
    """
    parts = [{"inlineData": {"mimeType": mime_type, "data": video_base64}}]

    for model_name in _media_chain(chain_limit):
        try:
            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": _media_thinking_native(_VIDEO_THINKING_LEVEL),
            }
            logger.info("🤖 Запрос к модели %s (описание видео)", model_name)
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
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
            text = _native_text_only(data)
            if not text:
                logger.warning("🤖 %s вернула пустое описание видео — пробую следующую", model_name)
                continue
            logger.info("🤖 Ответ от %s за %.1f с (описание видео)", model_name, elapsed)
            return text
        except Exception as e:
            if not _note_quota_error(model_name, e):
                logger.warning("🤖 %s не описала видео: %s", model_name, e)
    logger.error("⚠️ 🤖 Видео не описала НИ ОДНА модель цепочки")
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
                # Мысли называются по-разному: Qwen, DeepSeek и Xiaomi шлют их
                # в reasoning_content. Второе поле, reasoning, оставлено
                # НАМЕРЕННО, хотя сейчас его никто не шлёт: так звали мысли
                # у OpenRouter (провайдер убран 2026-08-04), и любой следующий
                # OpenAI-совместимый провайдер может назвать их так же. Стоит
                # это одного `or`, а его отсутствие даёт ответ БЕЗ мыслей —
                # молча, без ошибки.
                rc = delta.get("reasoning_content") or delta.get("reasoning")
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
    ({"type": "enabled"/"disabled", "reasoning_effort": "max"} — формат DeepSeek V4).

    ⚠️ reasoning_effort="max" — ВЕРХНЯЯ ступень (2026-08-10, решение Максима
    «менять для обеих моделей»). До этого здесь стоял "xhigh" с подписью
    «экстремальная глубина», и это оказалось неправдой: у DeepSeek V4 шкала
    low / high / max, а "xhigh" — устаревшее значение, которое приводится к
    "high", то есть к уровню ПО УМОЛЧАНИЮ. Бот честно думал средне, а комментарий
    обещал максимум. Скорее всего, когда это писалось, "xhigh" и был верхом —
    шкалу DeepSeek с тех пор переделали (у них и цены дважды менялись за месяц).

    ⚠️ Значение применяется к ОБЕИМ моделям (v4-flash и v4-pro) — ветка тут
    одна. У v4-pro на 2026-08-10 работают только "high" и "max" ("low" тоже
    подтягивается до "high"); три полноценные ступени обещаны в начале августа.

    ⚠️ ЦЕНА: рассуждения тарифицируются как обычный ответ (v4-pro $0.87, v4-flash
    $0.28 за млн токенов) — на "max" их заметно больше, и ответы дольше.
    Сверять при правках цен в config.DEEPSEEK_PRICES.
    """
    if _is_thinking(model_name, thinking_override):
        extra = {"thinking": {"type": "enabled", "reasoning_effort": "max"}}
    else:
        extra = {"thinking": {"type": "disabled"}}
    return _openai_stream_request(
        model_name, messages, DEEPSEEK_API_URL, DEEPSEEK_API_KEY, extra,
    )


def _xiaomi_chat_request(model_name: str, messages: list, thinking_override: bool | None = None):
    """Запрос к Xiaomi MiMo (2026-07-25). Формат управления рассуждениями —
    ТОТ ЖЕ, что у DeepSeek: {"thinking": {"type": "enabled"/"disabled"}}.
    Проверено живыми запросами: с "disabled" размышлений ноль, с
    reasoning_effort="xhigh" их вдвое больше (решение Максима о глубине).

    ⚠️ ЗДЕСЬ "xhigh" ОСТАВЛЕН НАМЕРЕННО и «как у DeepSeek» больше НЕ читать:
    2026-08-10 у DeepSeek перешли на "max", потому что там "xhigh" оказался
    устаревшим синонимом уровня по умолчанию. У Xiaomi шкала СВОЯ и в их
    документации не описана — "xhigh" выбран не по бумаге, а по живому замеру
    (размышлений вдвое больше). Менять на "max" вслепую нельзя: неизвестное
    значение MiMo может молча проглотить и думать как обычно. Нужен такой же
    живой замер — сравнить длину reasoning_content на "xhigh" и "max"."""
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


def _err_short(exc) -> str:
    """
    Короткий текст ошибки для лога.

    У requests сообщение начинается с того же кода, который в строке лога уже
    напечатан рядом («код 503: 503 Server Error: …»), а заканчивается полным
    URL, где ЕЩЁ РАЗ повторяется имя модели — тоже уже напечатанное. Остаётся
    голое дублирование, за которым теряется суть. Убираем оба хвоста и
    оставляем причину: «Service Unavailable».

    Тело ответа сервера этим НЕ трогается — оно приходит отдельно, через
    _err_body, и там самое ценное для разбора (см. его докстринг).
    """
    text = str(exc).strip()
    text = re.sub(r'^\d{3}\s+(?:Client|Server)\s+Error:\s*', '', text)
    text = re.sub(r'\s+for url:\s*\S+$', '', text)
    return text or exc.__class__.__name__


def _native_thinking_config(model_name: str) -> dict:
    """
    Кусок generationConfig для НАТИВНЫХ запросов (аудио, видео): просим модель
    вернуть сводку рассуждений.

    ⚠️ Формат отличается от текстового пути. Там запрос идёт через
    OpenAI-совместимый эндпоинт и настройка едет в extra_body["google"]
    (см. _gemini_chat_request); здесь — обычным полем generationConfig.
    Из-за этого различия аудио и видео до 2026-07-27 отвечали БЕЗ мыслей:
    настройку туда просто не передавали.

    thinkingBudget: -1 — динамический максимум, как в текстовых запросах.
    Гемма шлёт <thought> сама (native_thinking) — её не просим.
    """
    info = AVAILABLE_MODELS.get(model_name, {})
    if _is_thinking(model_name) and not info.get("native_thinking"):
        return {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": -1}}
    return {}


def _native_answer_with_thoughts(data: dict) -> str:
    """
    Собирает ответ нативного API (аудио, видео) из частей.

    Мысли приходят ОТДЕЛЬНЫМИ частями с флагом "thought": true. Старый разбор
    брал parts[0] — то есть при включённых размышлениях вернул бы первую мысль
    вместо ответа. Здесь части раскладываются по флагу, мысли заворачиваются
    в <thought>…</thought> и ставятся ПЕРЕД ответом — ровно как в текстовом
    пути (_openai_stream_request), чтобы send_formatted показал их свёрнутой
    цитатой «Мысли».

    Бросает KeyError/IndexError при неожиданном формате — вызывающий их ловит.
    """
    parts = data["candidates"][0]["content"]["parts"]

    thoughts, answer = [], []
    for part in parts:
        text = part.get("text")
        if not text:
            continue
        (thoughts if part.get("thought") else answer).append(text)

    body = "".join(answer).strip()
    reasoning = "".join(thoughts).strip()
    if not body and not reasoning:
        raise KeyError("в ответе нет ни текста, ни мыслей")

    return f"<thought>{reasoning}</thought>\n{body}" if reasoning else body


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
    GEMINI_API_KEY (нестриминговый JSON), модели Qwen, DeepSeek, Xiaomi и
    в _openai_stream_request).
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
                    model_name, attempt + 1, attempts, _err_code(e), _err_short(e), _err_body(e),
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
        #   • активная Qwen/DeepSeek/Xiaomi → сразу бесплатные Gemini-lite
        #     (другой провайдер).
        # Модели Xiaomi MiMo сами в подстраховку НЕ ставятся (решение Максима
        # 2026-07-25 — сначала посмотреть их в деле), но подстраховываются как все.
        # (429) — цепочка сама уведёт запрос на Gemini, человек этого не заметит.
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
            # ⚠️ Учёт вызова — под своим try, как соседние записи расхода ниже.
            # Модель уже ответила, токены потрачены: сорвавшаяся запись в
            # статистику не вправе уронить функцию и отобрать у человека
            # ответ, за который заплачено.
            try:
                hist.register_api_call(model_name)
            except Exception as e:
                logger.warning("⚠️ Не удалось учесть вызов модели в статистике: %s", e)
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
                        hist.add_provider_cost("deepseek", _cost)
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
                        hist.add_provider_cost("xiaomi", _cost)
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
                        hist.add_provider_cost("qwen", _cost)
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
#  База знаний, когда прислали не текст (16.08.2026, просьба Максима)
#
#  Поиск по базе — ТЕКСТОВЫЙ: он сравнивает слова и смысл запроса со статьями.
#  Картинку, голосовое и ролик он не видит, поэтому раньше на медиа искал по
#  одной подписи, а без подписи не искал вовсе — база молчала ровно там, где
#  человек показывал технику и спрашивал про неё.
#
#  Теперь вложение сначала разбирается лёгкой моделью (те же помощники, что у
#  режима «Сам в разговор»), и найденные слова уходят В ПОИСКОВЫЙ ЗАПРОС.
#
#  ⚠️ РАЗБОР МОДЕЛИ НЕ ПОКАЗЫВАЕТСЯ — ни человеку, ни основной модели. Она
#  смотрит на сам файл своими глазами; подсунуть ей чужой пересказ вместо
#  файла означало бы ухудшить разбор ради поиска. В промпт уходят только
#  НАЙДЕННЫЕ СТАТЬИ, ровно как при текстовом вопросе.
# ───────────────────────────────────────────────

# Сколько первых фраз разбора уходит в поиск (решение Максима 16.08.2026).
# Модель почти всегда начинает с главного — «На изображении Leopard 2A7…», —
# а дальше идут ангар, тени и погода. Для поиска это шум: чем длиннее запрос,
# тем меньше весит бонус за буквальное совпадение слов (в services/rag.py он
# делится на число значимых слов), и прозвище «умка» в простыне текста тонет.
_MEDIA_SEARCH_SENTENCES = 2
# Страховка на случай разбора без единой точки: обрезаем по длине.
_MEDIA_SEARCH_LIMIT = 400

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def _first_sentences(text: str, count: int = _MEDIA_SEARCH_SENTENCES) -> str:
    """Первые `count` фраз текста, но не длиннее _MEDIA_SEARCH_LIMIT символов."""
    text = " ".join((text or "").split())          # переводы строк — в пробелы
    if not text:
        return ""
    head = " ".join(_SENTENCE_SPLIT_RE.split(text)[:count]).strip()
    return head[:_MEDIA_SEARCH_LIMIT]


def _media_search_text(caption: str = "", *, image_base64: str = "",
                       audio_base64: str = "", video_base64: str = "",
                       video_mime: str = "video/mp4") -> str:
    """
    Текст, которым ищем статьи базы знаний, когда пришло вложение:
    подпись пользователя ПЛЮС первые фразы разбора файла.

    Подпись остаётся в запросе (решение Максима 16.08.2026): в ней часто вся
    суть вопроса — «а броня у него какая?», — и выкидывать её значит терять то,
    ради чего человек прислал файл.

    Разбор стоит отдельного похода к модели, поэтому сначала спрашиваем
    rag.is_active(): база погашена — платить не за что, возвращаем одну
    подпись. Разбор не удался (модели молчат) — тоже возвращаем подпись, то
    есть поведение ровно как до этой правки.

    Цепочка моделей ограничена ОДНИМ живым звеном (chain_limit=1): здесь, в
    отличие от проактивного режима, человек ждёт ответа — см. _media_chain.
    """
    caption = (caption or "").strip()
    try:
        import services.rag as rag_module
        if not rag_module.is_active():
            return caption
    except Exception as e:
        logger.error("⚠️ Не удалось проверить состояние базы знаний: %s", e)
        return caption

    if image_base64:
        kind, described = "фото", _describe_image(image_base64, chain_limit=1)
    elif audio_base64:
        kind, described = "голосовое", _transcribe_audio(audio_base64, chain_limit=1)
    elif video_base64:
        kind, described = "видео", _describe_video(video_base64, video_mime, chain_limit=1)
    else:
        return caption

    if not described:
        return caption

    head = _first_sentences(described)
    if not head:
        return caption
    # ⚠️ ТЕКСТА РАЗБОРА В ЛОГЕ НЕТ — общее правило Максима (11.08.2026) про
    # полные тексты ответов моделей. Остаётся факт и длина: по ним видно, что
    # шаг сработал, и во что обошёлся.
    logger.info("%s Вложение разобрано для поиска по базе (%s, %d символов)",
                RAG_ICON, kind, len(head))
    return f"{caption} {head}".strip() if caption else head


def _rag_block(query_text: str, *, remember_query: bool = True) -> str:
    """
    Готовый кусок системного промпта: «шапка»-инструкция (панель /prompt,
    /rag_prompt_set) плюс найденные статьи. Пусто — если база ничего не нашла.

    Ошибки наружу не выпускает: сбой базы знаний не должен ломать ответ.
    ЕДИНАЯ сборка для всех путей — текста, фото, голосового и видео: иначе
    четыре места разъедутся, как только у блока поменяется вид.
    """
    if not RAG_ENABLED or not (query_text or "").strip():
        return ""
    try:
        import services.rag as rag_module
        rag_context = rag_module.retrieve_relevant_context(
            query_text, remember_query=remember_query)
        if not rag_context:
            return ""
        # Шапки может не быть вовсе (заводская опустошена 2026-08-16, своя не
        # задана) — тогда статьи уходят одни, без пары пустых строк в начале.
        instruction = hist.get_rag_instruction()
        return f"{instruction}\n\n{rag_context}" if instruction else rag_context
    except Exception as rag_err:
        logger.error("⚠️ Не удалось добавить контекст RAG: %s", rag_err)
        return ""


# ───────────────────────────────────────────────
#  Голосовые сообщения (native generateContent)
# ───────────────────────────────────────────────

def ask_gemini_audio(chat_id: int, user_id: int, audio_base64: str) -> str:
    """
    Отправляет голосовое сообщение пользователя в Gemini API (native generateContent).
    Стратегия устойчивости: 2 попытки на активной модели, затем фолбэк на FALLBACK_MODEL.
    Технические ошибки наружу не отдаются — при полном провале возвращается мягкое сообщение.

    С 16.08.2026 подмешивает базу знаний: голосовое сначала расшифровывается
    лёгкой моделью, и по расшифровке ищутся статьи. Сама расшифровка модели НЕ
    показывается — она слушает файл своими ушами (см. блок помощников выше).
    """
    history = hist.get_history(user_id)

    is_admin = (user_id in ADMIN_IDS)
    bypass_prompt = is_admin and (hist.get_setting(f"admin_no_prompt_{user_id}", "0") == "1")

    if bypass_prompt:
        current_system_prompt = ""
    else:
        current_system_prompt, _, _ = hist.get_active_system_prompt()

    # База знаний по расшифровке. Как и в ask_gemini, работает независимо от
    # админского тумблера «PROMPT ВЫКЛ»: статьи — это факты, а не характер.
    if RAG_ENABLED:
        block = _rag_block(_media_search_text(audio_base64=audio_base64),
                           remember_query=False)
        if block:
            current_system_prompt = (
                f"{current_system_prompt}\n\n{block}" if current_system_prompt else block
            )

    # Своя последняя публикация — сразу за базой знаний, как в ask_gemini.
    news_block = _last_news_block()
    if news_block:
        current_system_prompt = (
            f"{current_system_prompt}\n\n{news_block}" if current_system_prompt else news_block
        )

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
        # Просьба о размышлениях зависит от МОДЕЛИ, а payload общий на всю
        # цепочку — поэтому конфиг добавляется в копию, на каждое звено своё.
        req = dict(payload)
        thinking = _native_thinking_config(model_name)
        if thinking:
            req["generationConfig"] = thinking
        for attempt in range(attempts):
            try:
                response = _http().post(url, json=req, headers=headers, timeout=GEMINI_TIMEOUT)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(
                    "⚠️ Модель %s не ответила (попытка %s из %s, %s): %s%s",
                    model_name, attempt + 1, attempts, _err_code(e), _err_short(e), _err_body(e),
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

    # Учёт вызова — под своим try: ответ уже получен и оплачен (см. текстовый путь).
    try:
        hist.register_api_call(used_model)
    except Exception as e:
        logger.warning("⚠️ Не удалось учесть вызов модели в статистике: %s", e)
    # Ответила запасная модель вместо активной — сообщаем админам (не чаще раза
    # в час). Если активная модель аудио вообще не принимает, обход цепочки —
    # штатный режим, а не сбой, поэтому уведомления нет.
    if active_model in AUDIO_FALLBACK_CHAIN and used_model != active_model:
        _notify_admins_fallback(active_model, used_model)
    try:
        raw_answer = _native_answer_with_thoughts(data)
    except (KeyError, IndexError):
        logger.error("⚠️ Неожиданный формат аудио-ответа Gemini API: %s", str(data)[:300])
        return SOFT_FAIL_MESSAGE

    answer = compress_newlines(raw_answer)
    usage = data.get("usageMetadata", {})
    prompt_tokens = usage.get("promptTokenCount", 0)
    total_tokens = usage.get("totalTokenCount", 0)
    thought_tokens = usage.get("thoughtsTokenCount", 0)
    # Модель + время ответа + токены в одной строке.
    logger.info("%s Ответ от %s за %.1f с (аудио) | контекст=%s | размышления=%s | всего=%s",
                _icon_of(used_model), used_model, elapsed, prompt_tokens, thought_tokens, total_tokens)

    # В историю — без блока мыслей: он нужен только на экране, а в контексте
    # диалога занимал бы место (тот же порядок, что в ask_gemini).
    db_answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL | re.IGNORECASE).strip()
    hist.add_messages(chat_id, user_id, "[Голосовое сообщение]", db_answer, prompt_tokens, used_model, total_tokens)
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

    С 16.08.2026 подмешивает базу знаний: ролик сначала описывает лёгкая
    модель, и по подписи вместе с описанием ищутся статьи. Описание самой
    модели НЕ показывается — ролик она смотрит сама (см. блок помощников выше).
    """
    history = hist.get_history(user_id)

    is_admin = (user_id in ADMIN_IDS)
    bypass_prompt = is_admin and (hist.get_setting(f"admin_no_prompt_{user_id}", "0") == "1")

    if bypass_prompt:
        current_system_prompt = ""
    else:
        current_system_prompt, _, _ = hist.get_active_system_prompt()

    # База знаний по подписи + описанию ролика. Разбор идёт по ОДНОМУ живому
    # звену цепочки (chain_limit=1 внутри _media_search_text): у видео таймаут
    # 90 секунд на модель, и полный перебор заставил бы человека ждать минуты.
    if RAG_ENABLED:
        block = _rag_block(
            _media_search_text(user_text, video_base64=video_base64, video_mime=mime_type),
            remember_query=False)
        if block:
            current_system_prompt = (
                f"{current_system_prompt}\n\n{block}" if current_system_prompt else block
            )

    # Своя последняя публикация — сразу за базой знаний, как в ask_gemini.
    news_block = _last_news_block()
    if news_block:
        current_system_prompt = (
            f"{current_system_prompt}\n\n{news_block}" if current_system_prompt else news_block
        )

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
        # Конфиг размышлений — свой на каждое звено цепочки (см. _try_audio).
        req = dict(payload)
        thinking = _native_thinking_config(model_name)
        if thinking:
            req["generationConfig"] = thinking
        for attempt in range(attempts):
            try:
                response = _http().post(url, json=req, headers=headers, timeout=VIDEO_TIMEOUT)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                logger.warning(
                    "⚠️ Модель %s не ответила на видео (попытка %s из %s, %s): %s%s",
                    model_name, attempt + 1, attempts, _err_code(e), _err_short(e), _err_body(e),
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

    # Учёт вызова — под своим try: ответ уже получен и оплачен (см. текстовый путь).
    try:
        hist.register_api_call(used_model)
    except Exception as e:
        logger.warning("⚠️ Не удалось учесть вызов модели в статистике: %s", e)
    # Ответила запасная вместо активной — уведомляем админов (не чаще раза в час).
    # Если активная видео вообще не принимает, обход цепочки — штатный режим
    # (как у аудио и у vision-reroute), поэтому уведомления нет.
    if _supports_video(active_model) and used_model != active_model:
        _notify_admins_fallback(active_model, used_model)

    try:
        raw_answer = _native_answer_with_thoughts(data)
    except (KeyError, IndexError):
        logger.error("⚠️ Неожиданный формат видео-ответа Gemini API: %s", str(data)[:300])
        return SOFT_FAIL_MESSAGE

    answer = compress_newlines(raw_answer)
    usage = data.get("usageMetadata", {})
    prompt_tokens = usage.get("promptTokenCount", 0)
    total_tokens = usage.get("totalTokenCount", 0)
    thought_tokens = usage.get("thoughtsTokenCount", 0)
    logger.info("%s Ответ от %s за %.1f с (видео) | контекст=%s | размышления=%s | всего=%s",
                _icon_of(used_model), used_model, elapsed, prompt_tokens, thought_tokens, total_tokens)

    # В контекст диалога пишем пометку с подписью пользователя, если она была, —
    # иначе в истории останется голое «[Видео]» без вопроса, к которому был ответ.
    # Блок мыслей в историю не идёт — он только для экрана (как в ask_gemini).
    context_note = f"[Видео] {caption}".strip() if caption else "[Видео]"
    db_answer = re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL | re.IGNORECASE).strip()
    hist.add_messages(chat_id, user_id, context_note, db_answer, prompt_tokens, used_model, total_tokens)
    return answer




# ───────────────────────────────────────────────
#  Основной запрос к модели (текст / Vision)
# ───────────────────────────────────────────────

def ask_gemini(chat_id: int, user_id: int, user_text: str, image_base64: str = None,
               reply_context: str = "") -> str:
    """
    Отправляет сообщение пользователя в Gemini API вместе с объединённым
    контекстным окном (личка + группы одного пользователя).

    Контекст берётся по user_id (см. history.get_history). Сжатия контекста и
    лимита по токенам больше нет — размер окна задаётся MAX_CONTEXT_MESSAGES.

    Использует реальный подсчёт токенов из usage в ответе и сохраняет сообщения
    в БД только после успешного ответа. Поддерживает изображения (Vision).
    Технические ошибки наружу не отдаются (2 попытки + фолбэк на FALLBACK_MODEL).

    :param reply_context: готовый блок «на какое сообщение отвечают» — его
        собирает handlers/messages.py::_reply_context_block, когда человек
        отвечает Reply. Пустая строка — обычное сообщение, блок не добавляется.
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
    # Общий тумблер базы знаний («📖 База знаний» в панели /rag) проверяется
    # ВНУТРИ retrieve_relevant_context — там единственная точка входа поиска,
    # и её слушаются все пути сразу. Здесь его дублировать не надо.
    if RAG_ENABLED:
        # Фото ищет статьи по подписи ВМЕСТЕ с разбором картинки (16.08.2026):
        # до этого без подписи поиск пропускался совсем. Разбор в промпт не
        # уходит — модель смотрит на файл сама (см. блок помощников выше).
        # Запрос с разбором в кэш не кладём: он уникален почти всегда и только
        # вытеснял бы оттуда настоящие вопросы людей.
        search_text = (_media_search_text(user_text, image_base64=image_base64)
                       if image_base64 else user_text)
        # «Шапка»-инструкция настраивается из Телеграма (панель /prompt,
        # /rag_prompt_set); по умолчанию — заводской RAG_INSTRUCTION.
        # Сами статьи всегда подставляются под ней — сборка в _rag_block.
        block = _rag_block(search_text, remember_query=not image_base64)
        if block:
            current_system_prompt = (
                f"{current_system_prompt}\n\n{block}" if current_system_prompt else block
            )
            # Состав подсказки печатает сам поиск (services/rag.py) — там
            # известны названия статей. Вторая строка «контекст добавлен»
            # только путала: непонятно, добавили в базу или в промпт.

    # ── Своя последняя публикация (2026-08-16) ──
    # Идёт СРАЗУ ЗА базой знаний и до справки об авторе — тот же порядок, что
    # в _build_proactive_parts, чтобы два пути не разъезжались. Добавляется и
    # в личке тоже (решение Максима «везде»): рассылка не оседает ни в личной
    # переписке, ни в архиве групп дольше окна стенограммы, и без этого блока
    # бот не знал бы о собственной новости нигде, кроме свежего разговора.
    try:
        news_block = _last_news_block()
        if news_block:
            current_system_prompt = (
                f"{current_system_prompt}\n\n{news_block}" if current_system_prompt else news_block
            )
    except Exception as news_err:
        logger.debug("📰 Не удалось добавить последнюю новость в запрос: %s", news_err)

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

    # ── На какое сообщение человек отвечает (2026-08-16) ──
    # Reply Telegram присылает отдельным полем, в тексте сообщения его нет
    # вовсе: без этого блока бот получал голое «а это точно?» и не знал, о чём
    # речь. Работает и в группе, и в личке. Стоит ПОСЛЕДНИМ — это самая
    # свежая и самая узкая справка, ближе всего к самому вопросу.
    if reply_context:
        current_system_prompt = (
            f"{current_system_prompt}\n\n{reply_context}" if current_system_prompt else reply_context
        )

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
            answer = data["choices"][0]["message"]["content"]
            # ⚠️ СРЕЗАЕМ РАЗМЫШЛЕНИЯ. Думающая модель возвращает ответ вместе с
            # блоком <thought>…</thought> впереди (его ставит _openai_stream_request
            # у Qwen/DeepSeek/Xiaomi и просит include_thoughts у Gemini). Дальше
            # новость уходит подписчикам, и короткая — через convert_md, который
            # мыслей НЕ разбирает: без этой строки в рассылку уезжали бы черновые
            # рассуждения модели прямо со служебными тегами. Тем же текстом
            # новость пишется в архив групп (jobs/news.py) — то есть мысли
            # попадали бы ещё и в стенограмму режима «Сам в разговор».
            # ⚠️ Лечить это выключением размышлений (thinking_override=False)
            # НЕЛЬЗЯ — думающая модель должна думать, устойчивым обязан быть разбор.
            return compress_newlines(strip_thoughts(answer))
        except (KeyError, IndexError):
            logger.error("⚠️ Неожиданный формат ответа при форматировании новости")

    # Резервный вариант (API недоступен) — сырой текст статьи без доп. надписей
    return content


# ───────────────────────────────────────────────
#  Проактивное участие в разговоре групп («Сам в разговор»)
# ───────────────────────────────────────────────

# Максимальная длина одной строки стенограммы (защита от «простыней» в промпте)
_PROACTIVE_LINE_MAX = 300

# Свои реплики бот режет по отдельному, БОЛЬШЕМУ потолку (2026-08-16, решение
# Максима). Под общие 300 знаков попадала прежде всего РАССЫЛКА НОВОСТИ: она
# пишется в архив групп как реплика бота (jobs/news.py) и обрывалась на первом
# же абзаце — люди обсуждали новость, а бот видел её начало и многоточие.
# Полностью снимать предел нельзя: сюда же попадают его собственные длинные
# ответы (handlers/messages.py::_archive_bot_group_reply), и без потолка
# стенограмма распухла бы на каждой проверке.
_PROACTIVE_BOT_LINE_MAX = 2000

# ⚠️ ПАМЯТИ ОБ ИСХОДЕ ПРОШЛОЙ ПРОВЕРКИ ЗДЕСЬ БОЛЬШЕ НЕТ (заведена и убрана
# 2026-08-11 по решению Максима). Она нужна была ровно для строки «Ты: уже
# видел, решил промолчать» в стенограмме — та строка выглядела репликой бота
# и путала модель. Вместе со строкой убран и флаг: держать состояние, которое
# никто не читает, — верный способ однажды на нём запутаться.


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

    Вступление справки с 2026-08-16 правится из Телеграма как обычный промпт —
    /author_prompt_set, ключ settings 'author_brief_instruction'; заводского
    текста у него нет, не задано — уходит одна строка данных. Сами данные
    участника собираются здесь и вшиты в код: их состав настройками не меняется.

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

        # Вступление справки правится из Телеграма (/author_prompt_set, ключ
        # settings 'author_brief_instruction'). Данные участника подставляются
        # под ним — как статьи базы знаний под RAG-инструкцией.
        # ⚠️ Заводского вступления больше нет (2026-08-16): не задано — уходит
        # ОДНА строка данных, без пустой строки в начале. Модель при этом не
        # знает, что справка служебная, и может зачитать её вслух.
        instruction = hist.get_author_brief_instruction()
        return f"{instruction}\n{who}." if instruction else f"{who}."
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


def _last_news_block() -> str:
    """
    Справка о ПОСЛЕДНЕЙ новости, которую бот разослал сам (2026-08-16,
    решение Максима «бот должен знать о своей рассылке везде»). Подставляется
    во ВСЕ пути ответа: текст и фото (ask_gemini), голосовые
    (ask_gemini_audio), видео (ask_gemini_video) и режим «Сам в разговор»
    (_build_proactive_parts, а через него оба его пути — текстовый и медийный).
    Везде идёт сразу за базой знаний: и то и другое — факты, а не характер.

    Зачем: рассылку бот отправляет фоновой задачей (jobs/news.py), апдейтом
    она не приходит и в личную переписку (таблица messages) не попадает
    вовсе — на вопрос «что ты только что прислал?» бот честно не знал ответа.
    В группах новость видна ещё и в стенограмме, но лишь пока не уедет из
    окна последних сообщений; эта справка живёт до следующей новости.

    ⚠️ ТЕКСТ ИДЁТ ЦЕЛИКОМ И В КАЖДЫЙ ЗАПРОС (решение Максима: «полный текст
    сводки», «только последнюю»). Сводка бывает в пару тысяч символов —
    столько и уходит, пока не придёт новая новость. Станет дорого — резать
    надо здесь и осознанно, а не подкручивать где-то ещё.

    Возвращает готовый блок или "" (бот ещё ничего не рассылал / любая
    ошибка — справка не должна ронять ответ).
    """
    try:
        news = hist.get_last_news()
    except Exception as e:
        logger.debug("📰 Не удалось прочитать последнюю новость: %s", e)
        return ""
    if not news:
        return ""

    lines = [
        "[Последняя новость, которую ты разослал]",
        "Это твоя собственная публикация: ты сам разослал её в чат с сайта wtmobile.com. "
        "Спросят о ней — отвечай как о своей новости, ссылку дать можно. "
        "Сам разговор с неё не начинай, если о ней не спрашивают.",
    ]
    if news.get("title"):
        lines.append(f"Заголовок: {news['title']}")
    if news.get("url"):
        lines.append(f"Ссылка: {news['url']}")
    lines.append(f"Текст, который увидели люди:\n{news.get('text', '')}")
    return "\n".join(lines)


def last_news_brief() -> str:
    """ПУБЛИЧНОЕ имя справки о последней разосланной новости — тот же текст,
    что уходит модели (2026-08-16, для показа в панели промптов).

    Заведена по образцу `author_brief`: панель не должна звать приватную
    `_last_news_block` напрямую, иначе показанное владельцу и отправленное
    модели однажды разъедутся. Источник один — меняешь состав блока, панель
    подхватывает сама.
    """
    return _last_news_block()


def _build_proactive_parts(chat_id: int, bot_id: int, trigger_text: str,
                           trigger_user_id: int | None) -> tuple:
    """
    Подготовка контекста для проактивного режима: характер, RAG, последняя
    разосланная новость, справка об авторе, инструкция участия, стенограмма
    чата одним блоком.

    Порядок частей важен: характер и должностная инструкция → факты из базы →
    своя последняя публикация → справка об авторе → правила участия →
    стенограмма.
    Инструкция участия идёт ПОСЛЕ системного промпта, поэтому её правила
    (например «в реплике никакой разметки») перекрывают общие правила
    оформления — так и задумано.

    Возвращает ДВА списка кусков системного промпта: (что уходит модели, что
    писать в лог разговора). Второй отличается только тем, что заданные тексты
    промптов в нём заменены строкой с длиной — см. комментарий ниже по коду.
    Оба — None, если говорить не о чем (пустая стенограмма, например сразу
    после кнопки «Очистить РАЗГОВОРЫ»).
    """
    # Размер стенограммы настраивается из панели (регулятор «контекст»);
    # settings хранит строки — приводим к int с фолбэком на дефолт конфига.
    try:
        context_msgs = int(hist.get_setting("proactive_context_msgs", str(PROACTIVE_CONTEXT_MSGS)))
    except (TypeError, ValueError):
        context_msgs = PROACTIVE_CONTEXT_MSGS

    rows = hist.get_recent_group_messages(chat_id, context_msgs)
    if not rows:
        return None, None

    # ── Стенограмма беседы: «Имя: текст», свои реплики бота — «Ты: …» ──
    lines = []
    # Попала ли в стенограмму строка САМОГО ПОСЛЕДНЕГО сообщения. Нужно
    # подстановке triggerText ниже: с 2026-08-10 строка может и не попасть
    # (видео без разбора, команда боту), и тогда lines[-1] — ЧУЖАЯ реплика,
    # которую подстановка молча затёрла бы разбором чужого медиа.
    last_row_added = False
    for i, r in enumerate(rows):
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
        # ⚠️ ПОМЕТОК «[голосовое]» / «[фото]» / «[видео]» БОЛЬШЕ НЕТ (2026-08-10,
        # решение Максима: «убрать пометки там, где разбора нет — ей не нужен
        # этот бред»). В стенограмму попадает ТОЛЬКО то, что бот действительно
        # понял: расшифровка голосового, разбор картинки или ролика (их кладёт
        # в архив services/proactive.py) и подпись автора.
        #
        # Медиа, которое разобрать не удалось или которое бот скачать не может
        # (ролики тяжелее 20 МБ — предел Telegram), не оставляет в стенограмме
        # НИЧЕГО: строка без единого слова о содержимом модели не помогает, а
        # сбивает — она видит «участник что-то прислал» и пытается это
        # обсуждать вслепую. Пустая строка отсеивается ниже.
        if not text:
            continue
        # ⚠️ СТРОКИ С МЕДИА НЕ ОБРЕЗАЮТСЯ (2026-08-10, решение Максима «разбор —
        # целиком»). Предел в 300 символов защищает промпт от простыней в
        # обычной переписке, но у медиа весь смысл строки и есть разбор:
        # обрезанный на середине, он превращается в «на видео игрок управляет
        # колесной бронетехникой, машина начинает движение в парковой зоне
        # среди деревьев сакуры, лавочек и…» — модель не узнаёт ни исхода боя,
        # ни техники противника.
        #
        # ⚠️ ДО 2026-08-10 разбор и так уходил целиком: он подставлялся в
        # стенограмму МИМО этой обрезки. Под предел он попал в тот день, когда
        # разбор начали сохранять в архив и строка стала браться из базы —
        # побочный эффект, который заметил Максим («присылается полный текст
        # или обрезанный?»). Это возврат прежнего поведения, а не новое.
        #
        # ⚠️ Цена: длинный разбор видео (бывает 1500+ символов) уходит в промпт
        # на КАЖДОЙ проверке, пока сообщение живёт в окне стенограммы. Станет
        # дорого — резать надо промптом разбора («опиши одной фразой»), а не
        # обрезкой: обрезка рвёт текст на полуслове, промпт делает его коротким
        # осмысленно.
        #
        # ⚠️ У СВОИХ РЕПЛИК ПОТОЛОК СВОЙ, БОЛЬШЕ (2026-08-16): под общие 300
        # знаков попадала рассылка новости — она пишется в архив групп как
        # реплика бота и обрывалась на первом абзаце.
        is_media = r["has_photo"] or r["has_voice"] or r.get("has_video")
        line_max = _PROACTIVE_BOT_LINE_MAX if r["user_id"] == bot_id else _PROACTIVE_LINE_MAX
        if not is_media and len(text) > line_max:
            text = text[:line_max] + "…"

        # ⚠️ КТО ГОВОРИТ, А КТО ПРИСЛАЛ КАРТИНКУ (2026-08-11, решение Максима
        # после живого случая). Разбор фото хранится под ником автора, и на
        # скриншотах с текстом он неотличим от речи: модель прочитала надпись
        # «Поздравляю! Вы получили VT-4» и в стенограмме это выглядело так,
        # будто Максим сам это написал. Одних скобок мало — нужна пометка
        # СНАРУЖИ, в самой строке.
        #
        # ⚠️ ГОЛОСОВЫЕ ПОМЕТКИ НЕ ПОЛУЧАЮТ: там расшифровка и ЕСТЬ слова
        # человека — «Вася прислал голосовое: привет» было бы неправдой, он
        # именно сказал «привет». Та же логика, по которой голосовые не
        # оборачиваются в скобки.
        if r["has_photo"]:
            lines.append(f"{name} прислал фото: {text}")
        elif r.get("has_video"):
            lines.append(f"{name} прислал видео: {text}")
        else:
            lines.append(f"{name}: {text}")
        last_row_added = (i == len(rows) - 1)
    if not lines:
        return None, None

    # Если триггер — медиа с готовым результатом анализа (описание фото /
    # расшифровка голосового / описание видео), подменяем пометку
    # [фото]/[голосовое]/[видео] на реальный текст. В БД у таких сообщений text
    # пустой, а trigger_text уже содержит результат работы
    # _describe_image / _transcribe_audio / _describe_video.
    #
    # ⚠️ ЗДЕСЬ ТЕКСТ СТАНОВИТСЯ ПРЯМОЙ РЕЧЬЮ УЧАСТНИКА («Вася: <текст>»), и это
    # верно только для голосовых: расшифровка и есть слова человека. Описание
    # картинки или ролика словами участника НЕ является, поэтому с 2026-08-10
    # оно приходит сюда уже обёрнутым в КВАДРАТНЫЕ СКОБКИ — «Имя: [текст
    # модели]» (обёртку ставит services/proactive.py, там же написано почему: без неё
    # бот принимал машинный разбор за речь человека и отвечал шуткой про
    # робота, чем раздражал людей). Снимешь обёртку там — вернёшь и шутки.
    #
    # ⚠️ С 2026-08-10 подстановка нужна РЕДКО: разбор оседает в архиве сразу
    # после анализа (proactive.py), поэтому текст обычно уже приходит из БД.
    # Осталась как страховка на случай, когда записать в архив не удалось.
    # `last_row_added` обязателен: без него подстановка затирала бы ЧУЖУЮ
    # последнюю строку, если строка самого триггера в стенограмму не попала.
    if trigger_text and rows and last_row_added:
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

    # ⚠️ ВТОРОЙ СПИСОК — ТО ЖЕ САМОЕ ДЛЯ ЛОГА РАЗГОВОРА (2026-08-16, решение
    # Максима). В файл не пишутся: характер (SYSTEM PROMPT), RAG-инструкция
    # ВМЕСТЕ С НАЙДЕННЫМИ СТАТЬЯМИ и PROMPT участия в разговоре. Вместо текста
    # остаётся строка с длиной — видно, что кусок уходил, и какой величины.
    # Причина: всё это Максим читает в панели промптов и в базе знаний, а в
    # записи оно занимало больше места, чем сам разговор.
    # ⚠️ Статьи базы знаний убраны ВТОРЫМ заходом (17.08): сначала их оставили
    # как «живые данные», но на деле они и раздували запись сильнее всего.
    # В логе остаются: последняя новость, справка об авторе и стенограмма.
    log_parts = []

    def _part(text: str, for_log: str | None = None) -> None:
        """Добавить кусок промпта: в запрос — всегда, в лог — то, что можно."""
        system_parts.append(text)
        log_parts.append(text if for_log is None else for_log)

    persona, _, _ = hist.get_active_system_prompt()
    if persona:
        _part(persona, f"[SYSTEM PROMPT — {len(persona)} симв., в лог не пишется]")

    # RAG по последнему сообщению (триггеру) — как в ask_gemini: если чат
    # обсуждает игру, бот подтянет факты из базы знаний. Общий тумблер базы
    # знаний действует и здесь — он проверяется внутри поиска (services/rag.py).
    if RAG_ENABLED and trigger_text:
        try:
            import services.rag as rag_module
            rag_context = rag_module.retrieve_relevant_context(trigger_text)
            if rag_context:
                # Шапки может не быть вовсе (заводская опустошена 2026-08-16) —
                # тогда статьи уходят одни, без пустых строк в начале.
                rag_instruction = hist.get_rag_instruction()
                if rag_instruction:
                    _part(f"{rag_instruction}\n\n{rag_context}",
                          f"[RAG-PROMPT + статьи базы знаний — "
                          f"{len(rag_instruction)} + {len(rag_context)} симв., "
                          f"в лог не пишутся]")
                else:
                    _part(rag_context,
                          f"[Статьи базы знаний — {len(rag_context)} симв., "
                          f"в лог не пишутся]")
                logger.info("%s Контекст RAG добавлен в проактивную проверку", RAG_ICON)
        except Exception as rag_err:
            logger.error("⚠️ Не удалось добавить контекст RAG в проактивную проверку: %s", rag_err)

    # Своя последняя публикация — такой же «факт», как статья базы знаний,
    # поэтому стоит рядом с RAG и ДО справки об авторе (2026-08-16).
    news_block = _last_news_block()
    if news_block:
        _part(news_block)

    who = _who_is_talking(trigger_user_id)
    if who:
        _part(who)

    # Инструкции участия может не быть вовсе (заводская опустошена 2026-08-16,
    # своя не задана) — пустой кусок в промпт не кладём, иначе между справкой
    # об авторе и стенограммой встанет дыра из пустых строк. Правила участия
    # тогда модели никто не объясняет, но слово ПРОПУСК ей всё равно называет
    # сам запрос (ask_group_proactive) — молчать она сможет.
    proactive_instruction = hist.get_proactive_instruction()
    if proactive_instruction:
        _part(proactive_instruction,
              f"[PROMPT участия в разговоре — {len(proactive_instruction)} симв., "
              f"в лог не пишется]")

    # ОДИН БЛОК СО ВСЕЙ СТЕНОГРАММОЙ (2026-08-16, решение Максима: «чистое
    # слияние без пометки последней строки»). Последнее сообщение ничем не
    # выделено — оно просто последняя строка списка.
    #
    # ⚠️ ЭТО ВОЗВРАТ К ФОРМАТУ, КОТОРЫЙ УЖЕ ЛОМАЛСЯ. С 11.08 по 16.08 блока
    # было два — «[Контекст сообщений чата]» и «[Последнее сообщение]»; их
    # развели как раз потому, что в одном списке все строки выглядят одинаково
    # свежими, модель не понимает, ради чего её позвали, и отвечает на разбор
    # фото минутной давности вместо нового сообщения («как будто я ему фото
    # прислал»). Максим предупреждён и выбрал слияние. Вернётся то же
    # поведение — причина здесь, а не в промптах.
    #
    # ⚠️ СТРОКИ «Ты: уже видел, решил промолчать» ЗДЕСЬ НЕТ (заведена и убрана
    # 2026-08-11 по решению Максима). Она подписывалась как «Ты:», то есть
    # выглядела РЕПЛИКОЙ БОТА рядом с его настоящими репликами — модель читала
    # обе одинаково. Понадобится вернуть — только служебной строкой БЕЗ имени,
    # вида «(сообщения выше ты уже видел)».
    _part("[Контекст сообщений всех участников чата]\n" + "\n".join(lines))
    return system_parts, log_parts


def ask_group_proactive_media(chat_id: int, bot_id: int, trigger_text: str,
                              trigger_user_id: int | None,
                              media_b64: str, mime_type: str, kind: str) -> str | None:
    """
    ⚡ ПРОАКТИВНЫЙ ОТВЕТ НА МЕДИА — отвечает та же модель, что СМОТРИТ файл
    (2026-08-11, решение Максима: «чтобы отвечала модель-разбиратель, а не
    активная»). Возвращает текст реплики (с блоком <thought>) или None.

    Зачем: активная модель (сейчас qwen3.7-plus) картинок не видит вовсе, и до
    этой правки она отвечала ПО ПЕРЕСКАЗУ — по строке разбора в стенограмме.
    Пересказ и породил всю возню 10 августа: шутки про ИИ над живыми людьми,
    скобки в стенограмме, споры про обрезку. Теперь на медиа отвечает Gemini
    из цепочки `PROACTIVE_MEDIA_CHAIN`, получая САМ ФАЙЛ плюс ровно ту же
    системную часть, что и активная модель: характер, RAG, справку об авторе,
    инструкцию участия и стенограмму.

    ⚠️ РАЗБОР МЕДИА ПРИ ЭТОМ НЕ ОТМЕНЯЕТСЯ (services/proactive.py). Он нужен не
    для ответа, а для ПАМЯТИ: через несколько сообщений файла нет ни у кого, а
    стенограмма живёт — без разбора в истории будет дыра. Итого на медиа два
    запроса: разбор (для архива) и этот (для реплики).

    ⚠️ ЗАПРОС НАТИВНЫЙ (contents/parts/systemInstruction), а не через
    OpenAI-совместимый эндпоинт: так фото, голосовое и видео идут ОДНИМ путём,
    а мысли приходят отдельными частями и собираются готовым
    `_native_answer_with_thoughts` — тем же, что у обычных ответов на аудио и
    видео. Мысли здесь ЗАПРАШИВАЮТСЯ (в отличие от разбора): реплика уходит
    человеку через send_formatted, который показывает их свёрнутой цитатой.

    ⚠️ Модели цепочки, не умеющей нужный вид медиа, запрос не отправляем:
    у видео проверяем поле "video" (_supports_video), у фото — "vision".

    ЧТО ВОЗВРАЩАЕТ — три разных исхода, не перепутать:
      • текст реплики (с <thought>) — модель решила вступить;
      • ПУСТАЯ СТРОКА — модель решила промолчать (штатный и самый частый исход);
      • None — не ответила НИ ОДНА модель цепочки, и вызывающий уходит на
        обычный `ask_group_proactive` (активная модель по стенограмме), чтобы
        бот не онемел из-за отказа Google.
    ⚠️ Вернуть None вместо пустой строки на «промолчать» = заставить бота
    переспросить активную модель и заговорить там, где Gemini смолчала.

    Синхронная (requests) — звать через run_in_executor.
    """
    system_parts, log_parts = _build_proactive_parts(chat_id, bot_id, trigger_text,
                                                     trigger_user_id)
    if system_parts is None:
        return None

    task = (
        "Твоё решение: вступить в разговор (напиши только текст реплики) "
        f"или промолчать (ответь ровно одним словом: {PROACTIVE_SKIP_MARKER})."
    )

    # Файл впереди задания: модель сначала смотрит, потом читает, что от неё хотят.
    parts = [
        {"inlineData": {"mimeType": mime_type, "data": media_b64}},
        {"text": task},
    ]
    base = {
        "contents": [{"role": "user", "parts": parts}],
        "systemInstruction": {"parts": [{"text": "\n\n".join(system_parts)}]},
    }
    # Видео разбирается дольше всего — ему свой таймаут, как в ask_gemini_video.
    timeout = VIDEO_TIMEOUT if kind == "видео" else 90

    # Дословный лог разговора (2026-08-16) — см. services/chat_log.py.
    # Запрос пишем ОДИН раз до перебора цепочки: он у всех моделей один и тот
    # же, а на три неудачные попытки легли бы три одинаковые простыни.
    from services import chat_log
    chat_log.note_request(f"{kind} + цепочка {', '.join(PROACTIVE_MEDIA_CHAIN)}",
                          "\n\n".join(log_parts), task)

    for model_name in PROACTIVE_MEDIA_CHAIN:
        if _quota_blocked_now(model_name):
            continue          # недавно вернула 429 — не тратим время
        if kind == "видео" and not _supports_video(model_name):
            continue
        if kind == "фото" and not _supports_vision(model_name):
            continue
        try:
            req = dict(base)
            req["generationConfig"] = _media_answer_thinking()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            logger.info("🤖 Запрос к модели %s (проактивный ответ на %s)", model_name, kind)
            start = time.perf_counter()
            response = _http().post(
                url, json=req,
                headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            elapsed = time.perf_counter() - start
            answer = _native_answer_with_thoughts(data)
            if not (answer or "").strip():
                logger.warning("🤖 %s вернула пустой проактивный ответ на %s — пробую следующую",
                               model_name, kind)
                continue
            hist.register_api_call(model_name)
            logger.info("🤖 Ответ от %s за %.1f с (проактивный ответ на %s)",
                        model_name, elapsed, kind)
            chat_log.note_answer(model_name, elapsed, answer)
            answer = compress_newlines(answer)
            # ⚠️ РЕШЕНИЕ «ПРОМОЛЧАТЬ» ОТЛИЧАЕТСЯ ОТ ОТКАЗА, и разница здесь
            # принципиальная. Маркер ищем в ВИДИМОЙ части (без <thought>) —
            # рассуждая, модель поминает его как вариант, это не ответ.
            # Промолчала → ПУСТАЯ СТРОКА: вызывающий увидит «не None» и НЕ
            # пойдёт переспрашивать активную модель. Вернуть тут None значило
            # бы «Gemini отказала» — и бот полез бы за репликой к активной,
            # то есть заговорил бы там, где Gemini решила молчать.
            body = re.sub(r'<thought>.*?</thought>', '', answer,
                          flags=re.DOTALL | re.IGNORECASE).strip()
            if _is_proactive_skip(body):
                logger.info("🤖 %s решила промолчать (%s)", model_name, kind)
                return ""
            return answer
        except Exception as e:
            if not _note_quota_error(model_name, e):
                logger.warning("🤖 %s не ответила на %s в проактивной проверке: %s",
                               model_name, kind, e)
    logger.error("⚠️ 🤖 Проактивный ответ на %s не дала НИ ОДНА модель цепочки — "
                 "уходим на активную модель по стенограмме", kind)
    chat_log.note_answer("—", 0,
                         "(ни одна модель цепочки не ответила — спрашиваем "
                         "активную модель по стенограмме)")
    return None


def ask_group_proactive(chat_id: int, bot_id: int, trigger_text: str,
                        trigger_user_id: int | None = None) -> str | None:
    """
    Проактивное участие в разговоре группы: запрос к АКТИВНОЙ думающей модели,
    которая И решает «вступать или промолчать», И пишет реплику (одношаговая
    схема). Судья удалён 2026-07-20 — быстрые модели без мышления плохо
    следуют инструкциям.

    В модель уходит: характер и должностная инструкция (системный промпт) +
    RAG-контекст по последнему сообщению + справка об авторе + инструкция
    участия (get_proactive_instruction; блок рук в ней только если владелец
    вписал его сам) + стенограмма чата одним блоком.

    Возвращает текст реплики (С блоком <thought> — send_formatted покажет его
    свёрнутыми «Мыслями», как в обычных ответах) или None (решение промолчать /
    любая ошибка). Личная память диалогов (add_messages) НЕ трогается —
    проактивные реплики живут только в архиве групп (туда — без мыслей).
    Синхронная (requests) — вызывать через run_in_executor, как ask_gemini.
    """
    # ⚠️ ДОСЛОВНО ЗАПРОС И ОТВЕТ ПИШУТСЯ В ОТДЕЛЬНЫЙ ФАЙЛ (2026-08-16, просьба
    # Максима) — logs/chat, см. services/chat_log.py. В ОБЩИЙ лог их не
    # возвращать: ровно за это отладочную строку и убрали 11.08 (см. ниже).
    from services import chat_log

    system_parts, log_parts = _build_proactive_parts(chat_id, bot_id, trigger_text,
                                                     trigger_user_id)
    if system_parts is None:
        # Единственный путь, на котором модели не было вовсе. В логе разговора
        # это надо сказать словами: иначе запись выглядит как «бот подумал и
        # промолчал», а он не думал. У медиа-запроса такой же строки НЕТ
        # намеренно — после его отказа управление приходит сюда, и строка
        # написалась бы дважды.
        chat_log.note_answer("—", 0, "(модель не спрашивали: стенограмма пуста — "
                                     "например, сразу после очистки разговоров)")
        return None

    task = (
        "Твоё решение: вступить в разговор (напиши только текст реплики) "
        f"или промолчать (ответь ровно одним словом: {PROACTIVE_SKIP_MARKER})."
    )

    messages = [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": task},
    ]

    # ⚠️ ОТЛАДОЧНЫЙ ЛОГ «🧪 ЧТО УХОДИТ МОДЕЛИ» УДАЛЁН 2026-08-11 (решение
    # Максима). Он печатал ЦЕЛИКОМ весь запрос — характер, справку об авторе,
    # инструкцию участия и всю стенограмму чата — на КАЖДУЮ проверку в КАЖДОЙ
    # группе. Заводился 10.08 на время теста, чтобы своими глазами увидеть
    # строки с разбором фото. Не возвращать без прямой просьбы: это десятки
    # строк на сообщение, чужая переписка в логе и распухший архив.
    # Что уходит модели, показывает панель промптов (блок «📦 ЧТО УХОДИТ
    # МОДЕЛИ В РЕЖИМЕ „САМ В РАЗГОВОР“») — там же и живые размеры, а дословный
    # текст — в отдельном файле logs/chat (см. импорт chat_log выше).
    started_at = time.perf_counter()
    data, used_model = _gemini_chat_request(messages, kind="группа (сам)")
    elapsed = time.perf_counter() - started_at
    # Пишем ПОСЛЕ запроса, а не до: имя ответившей модели известно только
    # теперь — цепочка подстраховки могла увести запрос на запасную.
    chat_log.note_request(used_model or "—", "\n\n".join(log_parts), task)
    if data is None:
        # Все модели цепочки недоступны — молчим (тишина = штатный исход,
        # никакого SOFT_FAIL_MESSAGE в чат).
        chat_log.note_answer(used_model or "—", elapsed,
                             "(ни одна модель цепочки не ответила)")
        return None

    try:
        raw_answer = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        logger.error("⚠️ Неожиданный формат ответа при проактивной проверке: %s", str(data)[:300])
        chat_log.note_answer(used_model or "—", elapsed, "(неожиданный формат ответа)")
        return None

    chat_log.note_answer(used_model or "—", elapsed, raw_answer)

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
                hist.add_provider_cost("image", cost)
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
