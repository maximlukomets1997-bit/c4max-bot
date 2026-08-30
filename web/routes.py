# ───────────────────────────────────────────────
#  web/routes.py — адреса сайта и их обработчики (30.08.2026, этап 0).
#
#  ⚠️ ROUTES — ЕДИНСТВЕННОЕ место, где перечислены адреса сайта. По этой
#  таблице собирается приложение И по ней же проверяет preflight.py::check_web:
#  адрес без обработчика или обработчик мимо таблицы роняют проверку. Ровно
#  та же связка, что «кнопка ↔ ветка роутера» у кнопок бота.
#
#  ⚠️ КОЛОНКА «вход» НЕ ДЕКОРАТИВНАЯ. "owner" означает, что обработчик обёрнут
#  в проверку входа автоматически, при сборке приложения, — руками её в
#  обработчиках не пишем и забыть негде. "open" разрешено только двум адресам:
#  самому входу и оформлению страницы.
# ───────────────────────────────────────────────

import logging
import os

from aiohttp import web as aioweb

from config import WEB_COOKIE_NAME, WEB_PUBLIC_URL, WEB_SESSION_TTL_SEC
from . import auth, pages

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Ставим ли куке пометку «только по HTTPS». Снаружи сайт всегда за HTTPS
# (его держит Caddy), а вот при местной проверке по http такая кука просто
# не сохранится — поэтому смотрим на настроенный адрес, а не пишем True.
_SECURE_COOKIE = WEB_PUBLIC_URL.startswith("https://")


# ─── обработчики ────────────────────────────────────────────────────

async def index(request):
    """Сводка. Собирается на каждый заход — кэша намеренно нет: цифры живые."""
    return aioweb.Response(text=pages.page_summary(), content_type="text/html")


async def enter(request):
    """
    Вход. Два способа, оба заканчиваются одной и той же кукой:
      • GET  /enter?t=…  — одноразовая ссылка, которую бот прислал в личку;
      • POST /enter      — подписанные данные мини-приложения (страница входа
        забирает их из адресной строки и отправляет сюда сама).
    """
    if request.method == "POST":
        form = await request.post()
        user_id = auth.check_webapp(str(form.get("tgWebAppData", "")))
    else:
        user_id = auth.read_login_token(request.query.get("t"))

    if not auth.is_allowed(user_id):
        # Отказ и «подпись не сошлась» отвечают ОДИНАКОВО и намеренно: иначе
        # по разнице ответов можно перебором выяснять, чей id пускают.
        logger.warning("🌐 Отказ во входе на сайт (%s)", request.method)
        return aioweb.Response(text=pages.page_denied(), content_type="text/html",
                               status=403)

    logger.info("🌐 Вход в админку: %s", user_id)
    response = aioweb.HTTPSeeOther("/")
    response.set_cookie(WEB_COOKIE_NAME, auth.make_session(user_id),
                        max_age=WEB_SESSION_TTL_SEC, httponly=True,
                        secure=_SECURE_COOKIE, samesite="Lax", path="/")
    return response


async def exit_(request):
    """Выход: стираем куку. Ничего не хранится на сервере, стирать больше нечего."""
    response = aioweb.HTTPSeeOther("/")
    response.del_cookie(WEB_COOKIE_NAME, path="/")
    return response


async def health(request):
    """Ответ для проверки «сайт поднялся». Ничего о боте не сообщает."""
    return aioweb.Response(text="ok")


# ─── таблица адресов ────────────────────────────────────────────────
#
#  (метод, адрес, обработчик, вход): вход "owner" — только владелец,
#  "open" — без входа.

ROUTES = (
    ("GET",  "/",       index,  "owner"),
    ("GET",  "/enter",  enter,  "open"),
    ("POST", "/enter",  enter,  "open"),
    ("GET",  "/exit",   exit_,  "owner"),
    ("GET",  "/health", health, "open"),
)


# ─── сборка приложения ──────────────────────────────────────────────

def _guard(handler):
    """
    Обёртка «только для владельца». Не пустили — показываем страницу входа,
    а не ошибку: чаще всего это просто истёкшая неделя, а не чужой человек.
    """
    async def wrapped(request):
        if auth.current_user(request) is None:
            return aioweb.Response(text=pages.page_login(),
                                   content_type="text/html", status=401)
        return await handler(request)
    return wrapped


def build_app():
    """
    Собирает aiohttp-приложение по ROUTES. Отдельной функцией — чтобы то же
    самое мог собрать preflight, не поднимая сервер.
    """
    app = aioweb.Application()
    for method, path, handler, access in ROUTES:
        app.router.add_route(method, path,
                             _guard(handler) if access == "owner" else handler)
    app.router.add_static("/static/", _STATIC_DIR, name="static")
    return app
