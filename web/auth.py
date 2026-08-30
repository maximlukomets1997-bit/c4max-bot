# ───────────────────────────────────────────────
#  web/auth.py — «кто пришёл на сайт» (30.08.2026).
#
#  Паролей нет НИ ОДНОГО и заводить их нельзя. Личность подтверждает сам
#  Telegram: он подписывает данные о человеке ключом, выведенным из токена
#  бота. Токен знают только Telegram и наш сервер — значит, подделать подпись
#  снаружи нельзя, а нам не приходится ничего хранить.
#
#  ДВА входа, и подписи у них считаются ПО-РАЗНОМУ (это не небрежность
#  Telegram, а два разных механизма):
#    • кнопка «Открыть админку» в самом боте (Mini App) — ключ подписи
#      HMAC(токен, ключ="WebAppData"), вход происходит молча;
#    • браузер, кнопка «Войти через Telegram» (Login Widget) — ключ подписи
#      SHA256(токен).
#  Перепутаешь ключи — обе проверки просто всегда будут говорить «подделка».
#
#  ⚠️ Проверка подписи отвечает только на вопрос «данные правда от Telegram».
#  На вопрос «а этому человеку сюда можно» отвечает список ADMIN_IDS, и это
#  ОТДЕЛЬНАЯ проверка: подпись Telegram выдаст кому угодно, кто нажал кнопку.
# ───────────────────────────────────────────────

import hashlib
import hmac
import logging
import time
from urllib.parse import parse_qsl

from config import (ADMIN_IDS, TELEGRAM_TOKEN, WEB_AUTH_MAX_AGE_SEC,
                    WEB_COOKIE_NAME, WEB_SESSION_TTL_SEC)

logger = logging.getLogger(__name__)


def _token_bytes() -> bytes:
    return TELEGRAM_TOKEN.encode()


# ─── подпись Telegram ───────────────────────────────────────────────

def _check_string(pairs: dict) -> str:
    """
    Строка, которую подписывает Telegram: все поля, кроме самой подписи,
    в виде «ключ=значение», отсортированные по ключу и склеенные переводом
    строки. Порядок обязателен — Telegram считает подпись именно так.
    """
    return "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs) if k != "hash")


def _verify(pairs: dict, secret: bytes) -> bool:
    """Совпала ли подпись. Сравнение через compare_digest — обычное «==»
    отвечает тем быстрее, чем раньше строки разошлись, и по времени ответа
    подпись можно подобрать посимвольно."""
    given = pairs.get("hash", "")
    if not given or not TELEGRAM_TOKEN:
        return False
    mine = hmac.new(secret, _check_string(pairs).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, given)


def _fresh(pairs: dict) -> bool:
    """
    Не протухла ли подпись. Telegram кладёт в неё время (auth_date), и старше
    WEB_AUTH_MAX_AGE_SEC мы её не принимаем: перехваченная ссылка входа
    перестаёт работать сама, без нашего участия.
    """
    try:
        issued = int(pairs.get("auth_date", "0"))
    except (TypeError, ValueError):
        return False
    return 0 < (time.time() - issued) < WEB_AUTH_MAX_AGE_SEC


def _user_id(pairs: dict) -> int | None:
    """id человека из подписанных данных. У двух входов он лежит в разных
    местах: в Mini App — внутри поля user (JSON), в Login Widget — прямо
    полем id."""
    raw = pairs.get("id")
    if raw is None and pairs.get("user"):
        import json
        try:
            raw = json.loads(pairs["user"]).get("id")
        except Exception:
            return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def check_webapp(init_data: str) -> int | None:
    """
    Вход из самого Telegram (кнопка «Открыть админку»). На вход — строка
    initData, которую Telegram отдаёт странице. Возвращает id человека или
    None, если подпись не сошлась либо протухла.
    """
    if not init_data or not TELEGRAM_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    secret = hmac.new(b"WebAppData", _token_bytes(), hashlib.sha256).digest()
    if not _verify(pairs, secret) or not _fresh(pairs):
        return None
    return _user_id(pairs)


def check_widget(pairs: dict) -> int | None:
    """
    Вход из браузера (кнопка «Войти через Telegram»). На вход — поля из
    адресной строки. Возвращает id человека или None.
    """
    if not pairs or not TELEGRAM_TOKEN:
        return None
    secret = hashlib.sha256(_token_bytes()).digest()
    if not _verify(pairs, secret) or not _fresh(pairs):
        return None
    return _user_id(pairs)


# ─── кто вообще имеет право войти ───────────────────────────────────

def is_allowed(user_id: int | None) -> bool:
    """
    Пускать ли на сайт. Этап 0 (решение Максима 30.08.2026): ТОЛЬКО владельцы
    из config.ADMIN_IDS. Модераторов на сайте нет вовсе — не «нет прав», а
    нет входа: так на страницах не нужно ни одной проверки прав, и забыть её
    негде.
    ⚠️ Появятся модераторы — эта функция станет недостаточной: понадобится
    services/roles.py, как в панелях.
    """
    return user_id is not None and user_id in ADMIN_IDS


# ─── наша собственная кука со входом ────────────────────────────────
#
#  После удачного входа мы выдаём свою куку, чтобы не гонять человека через
#  Telegram на каждую страницу. Кука ПОДПИСАНА тем же токеном: «id.срок.подпись».
#  Ничего секретного внутри нет, подделать нельзя, на сервере хранить нечего —
#  список выданных входов не ведётся намеренно.

def make_session(user_id: int) -> str:
    """Значение куки для этого человека."""
    expires = int(time.time()) + WEB_SESSION_TTL_SEC
    body = f"{user_id}.{expires}"
    sign = hmac.new(_token_bytes(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sign}"


def read_session(cookie: str | None) -> int | None:
    """id из куки, если подпись цела и срок не вышел. Иначе None."""
    if not cookie or not TELEGRAM_TOKEN:
        return None
    parts = cookie.split(".")
    if len(parts) != 3:
        return None
    body = f"{parts[0]}.{parts[1]}"
    mine = hmac.new(_token_bytes(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, parts[2]):
        return None
    try:
        user_id, expires = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if expires < time.time():
        return None
    return user_id


# ─── одноразовая ссылка «открыть в браузере» ────────────────────────
#
#  Мини-приложение открывается ВНУТРИ Telegram, и там вход происходит молча.
#  А чтобы открыть админку в обычном браузере, бот присылает в личку ссылку,
#  подписанную тем же токеном и живущую пять минут. Так не нужен ни сторонний
#  сценарий входа с telegram.org, ни привязка домена в BotFather.
#
#  ⚠️ Ссылка — это ключ от админки на пять минут. Она уходит в ЛИЧКУ владельца
#  и больше никуда; пересылать её нельзя.

LOGIN_LINK_TTL_SEC = 300


def make_login_token(user_id: int) -> str:
    """Подписанный кусок для ссылки входа. Живёт LOGIN_LINK_TTL_SEC секунд."""
    expires = int(time.time()) + LOGIN_LINK_TTL_SEC
    body = f"{user_id}.{expires}"
    sign = hmac.new(_token_bytes(), f"login:{body}".encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sign}"


def read_login_token(token: str | None) -> int | None:
    """id из ссылки входа, если подпись цела и пять минут не вышли."""
    if not token or not TELEGRAM_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    body = f"{parts[0]}.{parts[1]}"
    mine = hmac.new(_token_bytes(), f"login:{body}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, parts[2]):
        return None
    try:
        user_id, expires = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if expires < time.time():
        return None
    return user_id


def make_login_url(user_id: int) -> str:
    """Полный адрес входа для этого человека. Пусто, если сайт не настроен."""
    from config import WEB_PUBLIC_URL
    if not WEB_PUBLIC_URL:
        return ""
    return f"{WEB_PUBLIC_URL}/enter?t={make_login_token(user_id)}"


def current_user(request) -> int | None:
    """
    Кто сейчас на странице: id владельца или None. Единственная точка, где
    страницы узнают посетителя — мимо неё в обработчиках ходить нельзя.
    """
    user_id = read_session(request.cookies.get(WEB_COOKIE_NAME))
    return user_id if is_allowed(user_id) else None
