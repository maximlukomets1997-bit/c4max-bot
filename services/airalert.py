# ───────────────────────────────────────────────
#  airalert.py — воздушная тревога в Днепре (2026-07-30, просьба Максима)
#
#  ЧТО ДЕЛАЕТ. Спрашивает у сервиса тревог, объявлена ли сейчас тревога у нас.
#  «У нас» — это область целиком ЛИБО отдельно Днепр (город, громада, район);
#  так решил Максим, потому что Днепропетровская область чаще всего объявляет
#  тревогу целиком, и ловить только город значило бы пропускать настоящие
#  тревоги.
#
#  ИСТОЧНИКА ДВА, ВЫБОР АВТОМАТИЧЕСКИЙ (см. блок в config.py):
#    • есть ALERTS_TOKEN → alerts.in.ua, точность до громады;
#    • токена нет        → ubilling.net.ua, БЕЗ КЛЮЧА, но ТОЛЬКО ОБЛАСТИ.
#  Кнопки переключения нет намеренно: точный источник строго лучше, выбирать
#  не из чего. Появился токен — бот переключится сам при перезапуске.
#  ⚠️ Запасной источник — временная мера, а не равноценная замена: на нём
#  тревога, объявленная по одной громаде, до владельца НЕ дойдёт.
#
#  КОМУ ЭТО ВИДНО. Никому, кроме владельца: сообщения шлёт jobs.py::air_alert_loop
#  строго в ADMIN_IDS. Подписки для участников нет и заводить её не надо —
#  это личное уведомление (решение Максима).
#
#  ⚠️ МОДУЛЬ «ТИХИЙ», но по-особенному: своих исключений наружу не бросает,
#  а вот РАЗНИЦУ между «тревоги нет» и «не смог узнать» отдаёт честно —
#  первое это False, второе None. Смешивать их нельзя ни в коем случае:
#  упавший сервис превратился бы в «отбой», и владелец получил бы ложное
#  «всё спокойно» посреди настоящей тревоги. Ради этого же различия цикл
#  в jobs.py умеет отдельно жаловаться на затянувшееся молчание сервиса.
#
#  ⚠️ ОПРОС ДЕШЁВЫЙ ЗА СЧЁТ If-Modified-Since. Сервис отдаёт заголовок
#  Last-Modified; мы возвращаем его в следующем запросе и почти всегда
#  получаем пустой ответ 304 «ничего не изменилось» — тела нет, разбирать
#  нечего, берём прошлый результат. Поэтому опрос раз в полминуты не
#  считается злоупотреблением. Уберёшь кэш — вырастет и трафик, и риск
#  упереться в лимит запросов (за него блокируют токен).
# ─────────────────────────────────────────────

import logging

from config import (AIRALERT_API_URL, AIRALERT_OBLAST_MARKER, AIRALERT_OBLAST_UID,
                    AIRALERT_POLL_SEC, AIRALERT_POLL_SEC_NOKEY, AIRALERT_TITLE_MARKERS,
                    AIRALERT_UBILLING_URL, ALERTS_TOKEN)

logger = logging.getLogger(__name__)

_ICON = "🚨"                # значок этой темы в логах
_TIMEOUT = 10               # сервис отвечает за доли секунды; ждать дольше незачем

# Кэш последнего удачного ответа — для If-Modified-Since (см. шапку файла).
_last_modified: str | None = None
_last_alerts: list | None = None

# Текст последней неудачи — его показывают владельцу, когда сервис молчит
# слишком долго. Без него «нет связи» одинаково выглядит и при упавшем
# сервисе, и при неверном токене, а это РАЗНЫЕ беды: первая пройдёт сама,
# вторая не пройдёт никогда и лечится правкой .env.
_last_error: str = ""


class RateLimited(Exception):
    """Сервис попросил сбавить темп (429). Цикл на это отвечает долгой паузой."""


def precise() -> bool:
    """Работаем ли на ТОЧНОМ источнике (есть токен). False — на запасном,
    который знает только области. От этого зависит и опрос, и то, что бот
    обещает человеку: на запасном тревогу по одной громаде он не увидит."""
    return bool(ALERTS_TOKEN)


def source_name() -> str:
    """Как назвать текущий источник человеку — в подсказке кнопки и в логе."""
    return ("alerts.in.ua (Днепр и область)" if precise()
            else "ubilling.net.ua (только область, без ключа)")


def poll_interval() -> int:
    """Пауза между опросами: у запасного источника она длиннее (см. config)."""
    return AIRALERT_POLL_SEC if precise() else AIRALERT_POLL_SEC_NOKEY


def last_error() -> str:
    """Из-за чего сорвался последний опрос. Пусто — последний опрос удался."""
    return _last_error


# ───────────────────────────────────────────────
#  Запрос к сервису
# ───────────────────────────────────────────────

def _fetch_alerts() -> list:
    """
    Список действующих тревог по всей стране (сырые словари от сервиса).
    Ошибки НЕ глушит: их обязан увидеть вызывающий, иначе он не отличит
    «тревог нет» от «не дозвонились».
    """
    global _last_modified, _last_alerts

    from services.http import session

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {ALERTS_TOKEN}",
    }
    if _last_modified and _last_alerts is not None:
        headers["If-Modified-Since"] = _last_modified

    response = session().get(AIRALERT_API_URL, headers=headers, timeout=_TIMEOUT)

    # Ничего не изменилось с прошлого раза — самый частый ответ.
    if response.status_code == 304 and _last_alerts is not None:
        return _last_alerts

    if response.status_code == 429:
        raise RateLimited("сервис просит сбавить темп опроса (429)")
    if response.status_code == 401:
        # Отдельно от прочих: тут виноват не сервис, а наш ключ, и лечится
        # это не ожиданием, а заменой токена в .env.
        raise RuntimeError("alerts.in.ua не принял токен (401) — проверь ALERTS_TOKEN в .env")

    response.raise_for_status()

    data = response.json()
    alerts = data.get("alerts") or []

    _last_alerts = alerts
    _last_modified = response.headers.get("Last-Modified") or _last_modified
    return alerts


def _fetch_ubilling() -> tuple[bool, list[str]]:
    """
    Запасной источник без ключа. Отдаёт словарь областей вида
    {"states": {"Дніпропетровська область": {"alertnow": true, ...}, ...}}.

    ⚠️ НАШЕЙ ОБЛАСТИ В ОТВЕТЕ НЕТ — это НЕ «тревоги нет», а «не смог узнать»:
    так выглядит и смена формата ответа, и обрезанный ответ. Молча считать
    это отбоем нельзя, поэтому здесь бросается исключение, а `check` превращает
    его в None. То же самое, если у найденной области не оказалось поля
    alertnow: раз сервис не сказал, мы не знаем.

    Ошибки НЕ глушит: их обязан увидеть вызывающий.
    """
    from services.http import session

    response = session().get(AIRALERT_UBILLING_URL, timeout=_TIMEOUT)
    response.raise_for_status()

    states = (response.json() or {}).get("states") or {}
    if not states:
        raise RuntimeError("ubilling вернул ответ без списка областей")

    for name, params in states.items():
        if AIRALERT_OBLAST_MARKER not in str(name).lower():
            continue
        if not isinstance(params, dict) or "alertnow" not in params:
            raise RuntimeError(f"ubilling не сказал про «{name}», есть ли тревога")
        return (bool(params["alertnow"]), [str(name)])

    # Число областей в ответе кладём в текст ошибки НАМЕРЕННО: оно сразу
    # отвечает, что случилось. Пришли все 25 областей, а нашей нет — сменилось
    # написание названия; пришло две-три — сервис отдаёт огрызок ответа.
    # Владелец увидит эту строку в предупреждении и поймёт, куда смотреть.
    raise RuntimeError(f"ubilling прислал {len(states)} обл., нашей среди них нет — "
                       "возможно, сменился формат ответа")


# ───────────────────────────────────────────────
#  Отбор «наших» тревог
# ───────────────────────────────────────────────

def _is_ours(alert: dict) -> bool:
    """
    Наша ли это тревога: область целиком или отдельно Днепр.

    ⚠️ Проверка области идёт ПЕРВОЙ и обязательна. Совпадение по названию без
    неё ловило бы «Дніпровський район» города Киева — это другой конец страны.
    """
    oblast_uid = str(alert.get("location_oblast_uid") or "")
    own_uid = str(alert.get("location_uid") or "")

    # Тревога по области целиком приходит записью самой области: у неё
    # location_uid равен UID области. Второе условие — на случай, если сервис
    # не проставит location_oblast_uid у записи верхнего уровня.
    if own_uid == AIRALERT_OBLAST_UID:
        return True
    if oblast_uid != AIRALERT_OBLAST_UID:
        return False

    title = (alert.get("location_title") or "").lower()
    return any(marker in title for marker in AIRALERT_TITLE_MARKERS)


def check() -> tuple[bool, list[str]] | None:
    """
    Главный вопрос модуля: «объявлена ли сейчас тревога у нас?»

    Возвращает:
      (True,  ["Дніпропетровська область", ...])  — тревога объявлена;
      (False, [])                                 — тревоги нет;
      None                                        — УЗНАТЬ НЕ УДАЛОСЬ.

    ⚠️ Третий случай — не то же самое, что второй, и обрабатывать их
    одинаково нельзя: молчащий сервис не означает отбоя.

    Спрашивает тот источник, который сейчас выбран (`precise()`): точный
    разбирает список тревог по местам, запасной отвечает сразу готовой парой,
    потому что кроме области ему сказать нечего.
    """
    global _last_error

    # ── Запасной источник: без ключа, только область ────────────────────
    if not precise():
        try:
            result = _fetch_ubilling()
        except Exception as e:
            _last_error = str(e)
            logger.warning("%s Не удалось узнать обстановку у ubilling: %s", _ICON, e)
            return None
        _last_error = ""
        return result

    # ── Точный источник ─────────────────────────────────────────────────
    try:
        alerts = _fetch_alerts()
    except RateLimited:
        raise                       # цикл ответит на это долгой паузой
    except Exception as e:
        _last_error = str(e)
        logger.warning("%s Не удалось узнать обстановку у alerts.in.ua: %s", _ICON, e)
        return None

    _last_error = ""

    ours = []
    for alert in alerts:
        try:
            # Интересует именно воздушная тревога. Прочие типы (артобстрел,
            # химическая, уличные бои) сервис отдаёт тем же списком — их
            # Максим не просил, и мешать их в одно сообщение нельзя:
            # «отбой» одной выглядел бы отбоем всего.
            if alert.get("alert_type") != "air_raid":
                continue
            if alert.get("finished_at"):
                continue
            if _is_ours(alert):
                ours.append(alert.get("location_title") or "Дніпропетровська область")
        except Exception as e:
            # Одна кривая запись не должна прятать остальные.
            logger.debug("%s Пропущена непонятная запись тревоги: %s", _ICON, e)

    return (bool(ours), ours)


# ───────────────────────────────────────────────
#  Мелочи для сообщений
# ───────────────────────────────────────────────

def human_duration(seconds: int) -> str:
    """«1 ч 12 мин», «43 мин», «меньше минуты» — длительность по-русски."""
    seconds = max(0, int(seconds))
    minutes = seconds // 60
    if minutes < 1:
        return "меньше минуты"
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"
