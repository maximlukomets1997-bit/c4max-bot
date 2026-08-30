# ───────────────────────────────────────────────
#  web/routes.py — адреса сайта и их обработчики (30.08.2026, этапы 0–1).
#
#  ⚠️ ROUTES — ЕДИНСТВЕННОЕ место, где перечислены адреса сайта. По этой
#  таблице собирается приложение И по ней же проверяет preflight.py::check_web:
#  адрес без обработчика или обработчик мимо таблицы роняют проверку. Ровно
#  та же связка, что «кнопка ↔ ветка роутера» у кнопок бота.
#
#  ⚠️ КОЛОНКА «вход» НЕ ДЕКОРАТИВНАЯ. "owner" означает, что обработчик обёрнут
#  в проверку входа автоматически, при сборке приложения, — руками её в
#  обработчиках не пишем и забыть негде. "open" разрешено только двум адресам:
#  самому входу и ответу «сайт поднялся».
# ───────────────────────────────────────────────

import logging
import os

from aiohttp import web as aioweb

from config import WEB_COOKIE_NAME, WEB_PUBLIC_URL, WEB_SESSION_TTL_SEC
from . import actions, auth, pages

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Ставим ли куке пометку «только по HTTPS». Снаружи сайт всегда за HTTPS
# (его держит Caddy), а вот при местной проверке по http такая кука просто
# не сохранится — поэтому смотрим на настроенный адрес, а не пишем True.
_SECURE_COOKIE = WEB_PUBLIC_URL.startswith("https://")


# ─── обработчики ────────────────────────────────────────────────────

async def index(request):
    """Сводка и органы управления. Собирается на каждый заход — кэша
    намеренно нет: и цифры, и положения тумблеров должны быть живыми."""
    csrf = auth.csrf_for(request.cookies.get(WEB_COOKIE_NAME))
    return aioweb.Response(text=pages.page_summary(csrf), content_type="text/html")


async def apply(request):
    """
    Приём правки. Поля формы: csrf, what, key, value (у тумблера value нет).

    Отвечает по-разному и намеренно: сценарию страницы — JSON с новым
    значением (чтобы обновить один орган на месте), обычной форме без
    сценария — переброс на главную (чтобы страница перерисовалась целиком).
    """
    form = await request.post()
    if not auth.csrf_ok(request, str(form.get("csrf", ""))):
        # Форма пришла не с нашей страницы (или вход сменили в другой вкладке).
        # Молча ничего не меняем.
        logger.warning("🌐 Правка отклонена: подпись формы не сошлась")
        return _answer(request, {"ok": False, "error": "устаревшая страница"}, 403)

    user_id = auth.current_user(request)
    what = str(form.get("what", ""))
    key = str(form.get("key", ""))
    raw = form.get("value")

    try:
        if what == "setting":
            shown = await actions.apply_setting(user_id, key, raw,
                                                request.app.get("tg"))
            return _answer(request, _setting_answer(key, shown))
        if what == "model":
            actions.apply_model(user_id, key)
        elif what == "image":
            actions.apply_image_model(user_id, key)
        elif what == "think":
            provider, _, code = key.partition(":")
            actions.apply_thinking(user_id, provider, code)
        else:
            raise actions.ActionError(f"неизвестное действие «{what}»")
    except actions.ActionError as e:
        logger.warning("🌐 Правка не принята: %s", e)
        return _answer(request, {"ok": False, "error": str(e)}, 400)

    # Выбор из ряда кнопок меняет подсветку соседей — проще перерисовать всё.
    return _answer(request, {"ok": True, "reload": True})


def _setting_answer(key: str, shown: str) -> dict:
    """
    Ответ сценарию страницы: новое значение и заново посчитанные СОСЕДНИЕ
    значения для ➖/➕. Без них после правки кнопки остались бы с прежними
    числами в формах и на второе нажатие вернули бы настройку назад.
    """
    from services import settings_spec as sspec
    item = sspec.SPEC[key]
    answer = {"ok": True, "shown": shown}
    if item["kind"] == "toggle":
        answer["on"] = sspec.read(key)
    else:
        answer["raw"] = sspec.read(key)
        answer["neighbours"] = [pages._neighbour(key, -1), pages._neighbour(key, +1)]
    return answer


def _answer(request, payload: dict, status: int = 200):
    """JSON — сценарию страницы, переброс на главную — обычной форме."""
    if "application/json" in request.headers.get("Accept", ""):
        return aioweb.json_response(payload, status=status)
    return aioweb.HTTPSeeOther("/")


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
    ("POST", "/set",    apply,  "owner"),
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


def build_app(application=None):
    """
    Собирает aiohttp-приложение по ROUTES. Отдельной функцией — чтобы то же
    самое мог собрать preflight, не поднимая сервер.

    application — приложение Telegram. Нужно ровно одному действию: объявлению
    группам при выключении «Сам в разговор». Без него (в проверках) сайт
    работает, объявление молча пропускается с записью в лог.
    """
    app = aioweb.Application()
    app["tg"] = application
    for method, path, handler, access in ROUTES:
        app.router.add_route(method, path,
                             _guard(handler) if access == "owner" else handler)
    app.router.add_static("/static/", _STATIC_DIR, name="static")
    return app
