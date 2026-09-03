# ───────────────────────────────────────────────
#  database/money.py — деньги и счётчики обращений к моделям (02.09.2026).
#
#  Шаг 10 разреза history.py. Всё, что копит цифры расхода:
#
#    add_provider_cost   — прибавить стоимость запроса к копилке провайдера и
#                          на ту же сумму уменьшить остаток на его счету
#    spend_qwen_tokens / get_qwen_tokens — отдельный учёт БЕСПЛАТНОЙ КВОТЫ
#                          Qwen: она считается не в деньгах, а в токенах на
#                          каждую модель
#    register_api_call   — +1 обращение к модели (таблица api_calls)
#    clear_api_calls / clear_user_token_usage — месячный сброс
#    register_image_call / unregister_image_call / get_remaining_image_calls —
#                          суточный лимит картинок на человека
#
#  ⚠️ ОДНА ФУНКЦИЯ НА ВСЕХ ПРОВАЙДЕРОВ (2026-08-03). Раньше их было пять —
#  add_deepseek_cost, add_xiaomi_cost, add_qwen_cost и прочие, — и различались
#  они только именами ключей. Теперь имена берутся из реестра
#  `config.PROVIDERS` (`cost_key`, `balance_key`): новый провайдер добавляется
#  в реестр, а не сюда.
#
#  ⚠️ СУТКИ ЛИМИТА КАРТИНОК СЧИТАЮТСЯ ПО КИЕВУ, а `called_at` хранится в UTC:
#  берём киевскую полночь и переводим её в UTC для сравнения. Есть запасная
#  ветка на случай, если база часовых поясов недоступна, — она считает по
#  фиксированному летнему смещению и потому чуть врёт зимой. Так и было.
#
#  ⚠️ ПОПЫТКА КАРТИНКИ СПИСЫВАЕТСЯ АВАНСОМ, ДО генерации, и возвращается
#  через unregister_image_call, если картинка не вышла. Иначе залпом команд
#  лимит обходится: обработчик /imagine не блокирует очередь обновлений.
#
#  ⚠️ ЗДЕСЬ ТОЛЬКО КОПИЛКИ, А НЕ АРИФМЕТИКА ЦЕНЫ. Сколько стоит конкретный
#  запрос, считает services/gemini.py по прайсам из config, и вот ЭТО как раз
#  покрыто selftest (группы «деньги — расчёт стоимости» и «деньги — сам прайс
#  не менялся»). До самих копилок проверки НЕ ДОХОДЯТ — ни одного вызова
#  функций этого файла в selftest нет. Правка здесь проверяется руками.
# ───────────────────────────────────────────────

import logging

from config import PROVIDERS

from ._core import _lock, _get_connection

logger = logging.getLogger(__name__)

def add_provider_cost(provider: str, delta_usd: float):
    """
    Прибавляет стоимость одного запроса к копилке расхода провайдера и на ту же
    сумму уменьшает остаток на его счету. ОДНА функция на всех (2026-08-03):
    раньше их было пять — add_deepseek_cost / add_xiaomi_cost /
    add_openrouter_cost / add_qwen_cost / add_image_cost, — и различались они
    только именами ключей.

    Имена ключей берутся из реестра `config.PROVIDERS`: `cost_key` (копилка,
    есть у всех, кто тратит деньги) и `balance_key` (остаток, есть не у всех —
    у Qwen вместо денег бесплатная квота в токенах, см. spend_qwen_tokens).
    Провайдер без копилки — молча ничего не делаем: у Gemini бесплатный ключ,
    и считать там нечего.

    Оба действия выполняются прямо в SQL — атомарно, без гонки
    «прочитал-прибавил-записал» между worker-потоками; settings хранит строки,
    CAST приводит к числу. Остаток не заведён — уменьшать нечего, UPDATE молча
    не найдёт строку, а копилка расхода при этом работает как обычно.
    ⚠️ На бесплатных вариантах моделей сюда приходят НУЛИ, и это правильно:
    копилка заведётся на нуле и будет видна в панели — значит, счёт ведётся.
    """
    meta = PROVIDERS.get(provider) or {}
    cost_key = meta.get("cost_key")
    if not cost_key:
        return
    balance_key = meta.get("balance_key")
    with _lock:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS REAL) + CAST(excluded.value AS REAL)",
            (cost_key, str(delta_usd)),
        )
        if balance_key:
            conn.execute(
                "UPDATE settings SET value = CAST(value AS REAL) - CAST(? AS REAL) "
                "WHERE key = ?",
                (str(delta_usd), balance_key),
            )
        conn.commit()


def spend_qwen_tokens(model_name: str, tokens: int):
    """Вычитает израсходованные токены из ОСТАТКА бесплатной квоты Alibaba
    по КАЖДОЙ модели Qwen отдельно (ключ settings qwen_tokens_<имя модели>) —
    в панели «📡 Настройки API» это число «осталось».
    Устроено как остаток баланса DeepSeek: квота заводится в settings РУКАМИ
    (при смене ключа или рабочего пространства), дальше тает сама. Ключа
    в settings нет — вычитать нечего, UPDATE молча не найдёт строку.
    Остаток ВЕЧНЫЙ: месячный сброс (_monthly_stats_reset) его не трогает —
    квота Alibaba даётся не помесячно, а объёмом токенов на модель.
    Число берётся из usage.total_tokens — это «всего» из строки лога
    (вход + ответ + размышления), сверено с консолью Model Studio 2026-07-05.
    Уходит в МИНУС при исчерпанной квоте — честный сигнал, что расход пошёл
    за деньги; не «чинить» ограничением снизу.
    Вычитание атомарно в SQL — без гонки между потоками."""
    if not model_name or not tokens or tokens <= 0:
        return
    with _lock:
        conn = _get_connection()
        conn.execute(
            "UPDATE settings SET value = CAST(value AS INTEGER) - ? WHERE key = ?",
            (int(tokens), f"qwen_tokens_{model_name}"),
        )
        conn.commit()


def get_qwen_tokens() -> dict:
    """ОСТАТОК бесплатной квоты по каждой модели Qwen: {имя модели: токенов}.
    Читает все ключи settings вида qwen_tokens_<модель> (их уменьшает
    spend_qwen_tokens). Показывается в панели «📡 Настройки API».
    Значение может быть отрицательным — квота исчерпана, пошёл платный расход."""
    prefix = "qwen_tokens_"
    with _lock:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
    out = {}
    for row in rows:
        try:
            out[row["key"][len(prefix):]] = int(float(row["value"] or 0))
        except (TypeError, ValueError):
            out[row["key"][len(prefix):]] = 0
    return out


def register_api_call(model_name: str):
    """Регистрирует успешный вызов API модели."""
    with _lock:
        conn = _get_connection()
        conn.execute("INSERT INTO api_calls (model_name) VALUES (?)", (model_name,))
        conn.commit()
    # Лог убран: дублировал строку «✅ Ответила …» (модель видна там). Запись в БД остаётся.


def clear_api_calls() -> int:
    """Полная очистка счётчиков вызовов API — месячный сброс статистики
    (jobs/cleanup.py::_monthly_stats_reset, первое число месяца). Возвращает число
    удалённых записей. Обмены «вопрос-ответ» (user_token_usage) не трогает."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute("DELETE FROM api_calls")
        deleted = cur.rowcount
        conn.commit()
    return deleted


def clear_user_token_usage() -> int:
    """Месячный сброс обменов «вопрос-ответ»: обнуляет счётчики токенов и
    запросов всех пользователей (jobs/cleanup.py::_monthly_stats_reset).
    ВАЖНО: именно UPDATE до нулей, а НЕ DELETE — init_db при полностью пустой
    таблице заново заполнил бы её старыми числами из устаревшей user_context
    (одноразовый перенос в init_db выше). Возвращает число затронутых строк."""
    with _lock:
        conn = _get_connection()
        cur = conn.execute(
            "UPDATE user_token_usage SET total_tokens = 0, total_requests = 0"
        )
        affected = cur.rowcount
        conn.commit()
    return affected


def register_image_call(user_id: int):
    """Списывает одну генерацию картинки из суточного лимита пользователя.

    С 2026-07-13 вызывается ДО генерации (аванс): обработчик /imagine не
    блокирует очередь апдейтов (block=False), и списание после генерации
    позволило бы залпом команд обойти лимит. Если генерация не удалась,
    попытка возвращается через unregister_image_call."""
    with _lock:
        conn = _get_connection()
        conn.execute("INSERT INTO user_image_calls (user_id) VALUES (?)", (user_id,))
        conn.commit()
    logger.info("🎨 Генерация картинки учтена в лимите (пользователь %s)", user_id)


def unregister_image_call(user_id: int):
    """Возвращает пользователю попытку генерации, списанную авансом
    (register_image_call), если картинка не получилась: удаляет самую
    свежую запись этого пользователя из user_image_calls."""
    with _lock:
        conn = _get_connection()
        conn.execute(
            "DELETE FROM user_image_calls WHERE id = ("
            "SELECT id FROM user_image_calls WHERE user_id=? ORDER BY id DESC LIMIT 1)",
            (user_id,),
        )
        conn.commit()
    logger.info("🎨 Попытка генерации возвращена в лимит (пользователь %s)", user_id)


def get_remaining_image_calls(user_id: int, default_limit: int) -> int:
    """Возвращает количество оставшихся генераций картинок для пользователя
    на текущие сутки.

    Сутки отсчитываются по Киеву (Europe/Kyiv): лимит сбрасывается в 00:00 по
    киевскому времени, перевод часов зима/лето учитывается автоматически (через
    базу часовых поясов из пакета tzdata). called_at в БД хранится в UTC, поэтому
    мы считаем момент киевской полуночи и переводим его в UTC для сравнения.
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        kyiv = ZoneInfo("Europe/Kyiv")
    except Exception:
        kyiv = None

    cutoff = None
    if kyiv is not None:
        now_kyiv = datetime.now(kyiv)
        midnight_kyiv = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = midnight_kyiv.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with _lock:
        conn = _get_connection()
        if cutoff is not None:
            # called_at и cutoff — оба в формате UTC "ГГГГ-ММ-ДД ЧЧ:ММ:СС",
            # строковое сравнение для него совпадает с хронологическим.
            cnt_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM user_image_calls WHERE user_id=? AND called_at >= ?",
                (user_id, cutoff),
            ).fetchone()
        else:
            # Подстраховка, если база часовых поясов недоступна (нет tzdata):
            # фиксированное смещение Киева летом (UTC+3), без учёта перевода часов.
            cnt_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM user_image_calls WHERE user_id=? AND date(called_at, '+3 hours') = date('now', '+3 hours')",
                (user_id,),
            ).fetchone()
        calls_today = cnt_row["cnt"] if cnt_row else 0
        remaining = default_limit - calls_today
    return max(0, remaining)
