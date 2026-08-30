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
        elif what == "theme":
            actions.apply_theme(user_id, key)
        elif what == "think":
            provider, _, code = key.partition(":")
            actions.apply_thinking(user_id, provider, code)
        else:
            raise actions.ActionError(f"неизвестное действие «{what}»")
    except actions.ActionError as e:
        logger.warning("🌐 Правка не принята: %s", e)
        return _answer(request, {"ok": False, "error": str(e)}, 400)

    # Выбор из ряда кнопок меняет подсветку соседей — проще перерисовать всё,
    # вернувшись на ТУ ЖЕ страницу (поле back верхней полосы).
    return _answer(request, {"ok": True, "reload": True},
                   back=form.get("back"))


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


def _safe_back(raw) -> str:
    """
    Куда вернуть человека после правки. Пускаем ТОЛЬКО свой путь.

    ⚠️ Без этой проверки поле формы стало бы дырой: адрес вида
    `back=https://чужой.сайт` увёл бы владельца с админки на чужую страницу,
    и выглядело бы это как обычное нажатие своей же кнопки. Отсюда три
    условия: путь начинается с одной косой черты (не с двух — «//чужой.сайт»
    браузер считает чужим адресом) и без двоеточия, чтобы не проехало
    «javascript:».
    """
    path = str(raw or "")
    if path.startswith("/") and not path.startswith("//") and ":" not in path:
        return path
    return "/"


def _answer(request, payload: dict, status: int = 200, back=None):
    """
    JSON — сценарию страницы, переброс — обычной форме.

    back нужен кнопкам, которые стоят НЕ на сводке (выбор темы в верхней
    полосе): без него смена темы со страницы промптов выбрасывала бы оттуда
    на главную.
    """
    if "application/json" in request.headers.get("Accept", ""):
        return aioweb.json_response(payload, status=status)
    return aioweb.HTTPSeeOther(_safe_back(back))


async def prompts(request):
    """
    Страница промптов. GET показывает пять текстов, POST сохраняет один.

    ⚠️ Пустое поле поверх непустого текста НЕ сохраняется сразу: страница
    возвращается с вопросом «точно стереть?». Заводских текстов у промптов
    нет, и случайно очищенное поле означало бы потерю текста насовсем — в
    боте на это же место поставлено подтверждение кнопкой.
    """
    csrf = auth.csrf_for(request.cookies.get(WEB_COOKIE_NAME))
    if request.method == "GET":
        return aioweb.Response(text=pages.page_prompts(csrf),
                               content_type="text/html")

    form = await request.post()
    if not auth.csrf_ok(request, str(form.get("csrf", ""))):
        logger.warning("🌐 Правка промпта отклонена: подпись формы не сошлась")
        return aioweb.Response(text=pages.page_prompts(csrf),
                               content_type="text/html", status=403)

    user_id = auth.current_user(request)
    key = str(form.get("key", ""))
    text = str(form.get("text", ""))

    from services import prompts_spec
    if key not in prompts_spec.BY_KEY:
        logger.warning("🌐 Правка промпта не принята: неизвестный ключ «%s»", key)
        return aioweb.Response(text=pages.page_prompts(csrf),
                               content_type="text/html", status=400)

    stirring = not text.strip() and prompts_spec.read(key)
    if stirring and str(form.get("confirm", "")) != "1":
        return aioweb.Response(text=pages.page_prompts(csrf, confirm=key),
                               content_type="text/html")

    try:
        actions.apply_prompt(user_id, key, text)
    except actions.ActionError as e:
        logger.warning("🌐 Правка промпта не принята: %s", e)
        return aioweb.Response(text=pages.page_prompts(csrf),
                               content_type="text/html", status=400)

    return aioweb.Response(text=pages.page_prompts(csrf, saved=key),
                           content_type="text/html")


async def system(request):
    """
    Обслуживание: деньги, отчёты, логи, обновления, дайджест, копия базы,
    очистка разговоров, перезапуск.

    ⚠️ Три действия спрашивают подтверждение: отправка дайджеста В ГРУППУ
    (её увидят все участники), очистка разговоров (общая на все чаты) и
    перезапуск (он гасит сам сайт).
    """
    csrf = auth.csrf_for(request.cookies.get(WEB_COOKIE_NAME))
    application = request.app.get("tg")

    if request.method == "GET":
        return aioweb.Response(text=pages.page_system(application, csrf),
                               content_type="text/html")

    form = await request.post()
    if not auth.csrf_ok(request, str(form.get("csrf", ""))):
        logger.warning("🌐 Действие обслуживания отклонено: подпись формы не сошлась")
        return aioweb.Response(text=pages.page_system(application, csrf),
                               content_type="text/html", status=403)

    actor_id = auth.current_user(request)
    do = str(form.get("do", ""))
    confirmed = str(form.get("confirm", "")) == "1"

    # Что требует подтверждения. Дайджест — по каждому чату отдельно.
    if do in ("wipe", "restart") and not confirmed:
        return aioweb.Response(text=pages.page_system(application, csrf, confirm=do),
                               content_type="text/html")
    if do == "digest_send" and not confirmed:
        # Показываем ТОТ ЖЕ текст рядом с вопросом — человек подтверждает не
        # «отправить дайджест вообще», а именно эти строки.
        try:
            chat_id = int(form.get("chat", ""))
        except (TypeError, ValueError):
            chat_id = 0
        page = pages.page_system(application, csrf,
                                 confirm=f"digest:{chat_id}",
                                 digest_chat=chat_id,
                                 digest_body=str(form.get("text", "")))
        return aioweb.Response(text=page, content_type="text/html")

    message, bad = "", False
    kwargs = {}
    try:
        if do == "money":
            message = actions.balance_set(actor_id, str(form.get("field", "")),
                                          str(form.get("value", "")))
        elif do == "report":
            kind = str(form.get("kind", "day"))
            kwargs = {"report": kind, "report_text": actions.report_text(kind)}
        elif do == "digest_toggle":
            on = actions.digest_toggle(actor_id)
            message = f'📊 Еженедельный дайджест {"включён" if on else "выключен"}.'
        elif do == "digest_show":
            chat_id = int(form.get("chat", ""))
            # Сбор дайджеста читает архив группы — уводим в рабочий поток.
            import asyncio
            text, _title = await asyncio.get_running_loop().run_in_executor(
                None, actions.digest_text, chat_id)
            kwargs = {"digest_chat": chat_id, "digest_body": text}
        elif do == "digest_send":
            message = await actions.digest_send(actor_id, int(form.get("chat", "")),
                                                str(form.get("text", "")),
                                                application)
        elif do == "backup":
            path, size = actions.make_backup(actor_id)
            from services import backup as bk
            message = (f"💾 Копия снята: {os.path.basename(path)}, "
                       f"{bk.human_size(size)}. Ссылка на скачивание — ниже.")
            kwargs = {}
            return aioweb.HTTPSeeOther("/download?what=backup")
        elif do == "wipe":
            message = actions.wipe_conversations(actor_id)
        elif do == "restart":
            message = await actions.restart_bot(actor_id, application)
        else:
            raise actions.ActionError(f"неизвестное действие «{do}»")
    except actions.ActionError as e:
        message, bad = f"Не вышло: {e}", True
        logger.warning("🌐 Обслуживание, действие «%s» не принято: %s", do, e)
    except Exception as e:
        message, bad = f"Не вышло: {e}", True
        logger.error("🌐 Обслуживание, действие «%s» сорвалось: %s", do, e)

    page = pages.page_system(application, csrf, message=message, bad=bad, **kwargs)
    return aioweb.Response(text=page, content_type="text/html",
                           status=400 if bad else 200)


# Что можно скачать и откуда это берётся. ⚠️ Список ЗАКРЫТЫЙ и путь наружу
# не принимается: иначе адрес вида ?what=../../.env отдал бы ключи.
_DOWNLOADS = ("log", "archive", "chatlog", "backup")


async def download(request):
    """Отдаёт файл: лог, архив логов, запись разговора или копию базы."""
    what = request.query.get("what", "")
    if what not in _DOWNLOADS:
        raise aioweb.HTTPNotFound()

    from handlers.admin import common as adm_common

    if what == "log":
        path, raw = adm_common._read_current_log()
    elif what == "archive":
        path, raw = adm_common._read_archive_log()
    elif what == "chatlog":
        from services import chat_log
        path, raw = adm_common._read_file_bytes(chat_log.current_path())
    else:
        from services import backup
        copies = backup.list_backups()
        if not copies:
            raise aioweb.HTTPNotFound()
        # ⚠️ list_backups отдаёт копии ОТ СТАРЫХ К СВЕЖИМ (так написано в её
        # докстринге — она делалась для отчётов). Самая свежая — ПОСЛЕДНЯЯ.
        # Здесь стояло copies[0], и «Снять и скачать» отдавало копию недельной
        # давности: файл скачивался, выглядел настоящим, и понять подмену было
        # нечем. Поймано разбором ошибок 30.08.2026.
        newest = os.path.join(backup.backup_dir(), copies[-1][0])
        path, raw = adm_common._read_file_bytes(newest)

    if not raw:
        return aioweb.Response(text="Файла нет или он пуст.",
                               content_type="text/plain", status=404)

    name = os.path.basename(path or what)
    logger.info("🌐 Сайт: скачан файл %s (%d байт)", name, len(raw))
    return aioweb.Response(
        body=raw,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
        content_type="application/octet-stream")


async def kb(request):
    """
    База знаний: GET показывает, POST выполняет одно действие.

    ⚠️ Пересборка указателя уходит в фон под ОБЩЕЙ с ботом защёлкой — два
    прогона по одним файлам дали бы половину указателя.
    """
    csrf = auth.csrf_for(request.cookies.get(WEB_COOKIE_NAME))
    application = request.app.get("tg")

    if request.method == "GET":
        page = pages.page_kb(application, csrf,
                             section=request.query.get("section", ""),
                             open_article=request.query.get("open", ""))
        return aioweb.Response(text=page, content_type="text/html")

    form = await request.post()
    if not auth.csrf_ok(request, str(form.get("csrf", ""))):
        logger.warning("🌐 Действие с базой знаний отклонено: подпись формы не сошлась")
        return aioweb.Response(text=pages.page_kb(application, csrf),
                               content_type="text/html", status=403)

    actor_id = auth.current_user(request)
    do = str(form.get("do", ""))
    section = str(form.get("section", ""))
    open_article, confirm, search, report, message = "", "", "", None, ""
    bad = False

    try:
        if do == "add":
            field = form.get("file")
            if field is None or not getattr(field, "filename", ""):
                raise actions.ActionError("файл не выбран")
            raw = field.file.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise actions.ActionError("файл не в кодировке UTF-8")
            fname = actions.kb_add(actor_id, field.filename, text)
            # ⚠️ add_article кладёт файл СРАЗУ В ОДОБРЕННЫЕ (так же, как
            # кнопка «➕ Добавить RAG» в боте): файл прислал сам владелец,
            # ждать одобрения не у кого. В очереди ждут только новости,
            # которые бот принёс с сайта сам.
            message = (f"➕ Статья добавлена в базу: {fname}. "
                       f"Чтобы она заработала в поиске — пересоберите указатель.")
            section, open_article = "", f"approved/{fname}"

        elif do == "approve":
            fname = str(form.get("fname", ""))
            actions.kb_approve(actor_id, fname)
            message = f"✅ Статья одобрена: {fname}"

        elif do == "delete":
            folder = str(form.get("folder", ""))
            fname = str(form.get("fname", ""))
            if str(form.get("confirm", "")) != "1":
                # Файл стирается насовсем — спрашиваем, как и кнопка в боте.
                page = pages.page_kb(application, csrf, section=section,
                                     open_article=f"{folder}/{fname}",
                                     confirm=f"{folder}/{fname}")
                return aioweb.Response(text=page, content_type="text/html")
            actions.kb_delete(actor_id, folder, fname)
            message = f"🗑 Статья удалена: {fname}"

        elif do == "replace":
            folder = str(form.get("folder", ""))
            fname = str(form.get("fname", ""))
            actions.kb_replace(actor_id, folder, fname, str(form.get("text", "")))
            message = (f"📝 Текст статьи сохранён: {fname}. "
                       f"Чтобы правка попала в поиск — пересоберите указатель.")
            open_article = f"{folder}/{fname}"

        elif do == "rebuild":
            message = actions.kb_rebuild(actor_id, application)

        elif do == "search":
            search = str(form.get("q", "")).strip()
            if not search:
                raise actions.ActionError("вопрос пустой")
            # Эмбеддинг запроса — сетевой вызов, уводим в рабочий поток:
            # в цикле бота он заморозил бы ответы всем.
            import asyncio
            report = await asyncio.get_running_loop().run_in_executor(
                None, actions.kb_test_search, search)
            open_article = ""

        elif do == "clearlog":
            if str(form.get("confirm", "")) != "1":
                page = pages.page_kb(application, csrf, section=section,
                                     confirm="clearlog")
                return aioweb.Response(text=page, content_type="text/html")
            deleted = actions.kb_clear_log(actor_id)
            message = f"🧹 Журнал очищен: {deleted} записей."

        else:
            raise actions.ActionError(f"неизвестное действие «{do}»")

    except actions.ActionError as e:
        message, bad = f"Не вышло: {e}", True
        logger.warning("🌐 База знаний, действие «%s» не принято: %s", do, e)
    except Exception as e:
        message, bad = f"Не вышло: {e}", True
        logger.error("🌐 База знаний, действие «%s» сорвалось: %s", do, e)

    page = pages.page_kb(application, csrf, section=section,
                         open_article=open_article, confirm=confirm,
                         search=search, report=report,
                         message="" if bad else message)
    if bad:
        page = page.replace('<div class="wrap">',
                            f'<div class="wrap"><div class="warn-box">'
                            f'{pages.esc(message)}</div>', 1)
    return aioweb.Response(text=page, content_type="text/html",
                           status=400 if bad else 200)


# Действия викторины, которые стирают данные и потому спрашивают подтверждение.
_QUIZ_CONFIRM = ("wipe", "nuke", "zero")


async def quiz(request):
    """Викторина: GET показывает, POST выполняет одно действие."""
    csrf = auth.csrf_for(request.cookies.get(WEB_COOKIE_NAME))
    application = request.app.get("tg")

    if request.method == "GET":
        page = pages.page_quiz(application, csrf,
                               mode=request.query.get("mode", "draft"))
        return aioweb.Response(text=page, content_type="text/html")

    form = await request.post()
    if not auth.csrf_ok(request, str(form.get("csrf", ""))):
        logger.warning("🌐 Действие с викториной отклонено: подпись формы не сошлась")
        return aioweb.Response(text=pages.page_quiz(application, csrf),
                               content_type="text/html", status=403)

    actor_id = auth.current_user(request)
    do = str(form.get("do", ""))
    mode = str(form.get("mode", "draft")) or "draft"

    if do in _QUIZ_CONFIRM and str(form.get("confirm", "")) != "1":
        page = pages.page_quiz(application, csrf, mode=mode, confirm=do)
        return aioweb.Response(text=page, content_type="text/html")

    message, bad = "", False
    try:
        message = _run_quiz_action(actor_id, do, form, application)
    except actions.ActionError as e:
        message, bad = f"Не вышло: {e}", True
        logger.warning("🌐 Викторина, действие «%s» не принято: %s", do, e)
    except Exception as e:
        message, bad = f"Не вышло: {e}", True
        logger.error("🌐 Викторина, действие «%s» сорвалось: %s", do, e)

    page = pages.page_quiz(application, csrf, mode=mode,
                           message="" if bad else message)
    if bad:
        page = page.replace('<div class="wrap">',
                            f'<div class="wrap"><div class="warn-box">'
                            f'{pages.esc(message)}</div>', 1)
    return aioweb.Response(text=page, content_type="text/html",
                           status=400 if bad else 200)


def _run_quiz_action(actor_id: int, do: str, form, application) -> str:
    """Одно действие викторины. Возвращает строку о том, что вышло."""
    if do == "auto":
        on = actions.quiz_auto_toggle(actor_id)
        return f'🕛 Вопрос дня {"включён" if on else "выключен"}.'
    if do == "gen":
        return actions.quiz_generate(actor_id, application)
    if do == "retry":
        return actions.quiz_generate(actor_id, application, retry=True)
    if do == "forget":
        return f"🗑 Список неудачных очищен: {actions.quiz_forget_fails(actor_id)}."
    if do == "seed":
        result = actions.quiz_seed(actor_id)
        return (f'📥 Загружено в черновики: {result.get("added", 0)}, '
                f'пропущено повторов: {result.get("skipped", 0)}.')
    if do in ("ok", "del"):
        try:
            qid = int(form.get("qid", ""))
        except (TypeError, ValueError):
            raise actions.ActionError("не понял, какой вопрос")
        if do == "ok":
            actions.quiz_approve(actor_id, qid)
            return f"✅ Вопрос #{qid} ушёл в игру."
        actions.quiz_delete(actor_id, qid)
        return f"🗑 Вопрос #{qid} удалён."
    if do == "wipe":
        return f"🧹 Черновиков стёрто: {actions.quiz_wipe_drafts(actor_id)}."
    if do == "nuke":
        return f"🗑 Стёрто вопросов: {actions.quiz_nuke(actor_id)}."
    if do == "zero":
        return f"🧹 Обнулено игроков: {actions.quiz_zero(actor_id)}."
    raise actions.ActionError(f"неизвестное действие «{do}»")


async def users(request):
    """Список всех, кого знает бот."""
    csrf = auth.csrf_for(request.cookies.get(WEB_COOKIE_NAME))
    return aioweb.Response(text=pages.page_users(csrf), content_type="text/html")


# Действия карточки, которые СТИРАЮТ данные и потому спрашивают подтверждение.
# ⚠️ Кик и бан сюда НЕ входят намеренно: они не стирают ничего, их видно в
# журнале, и они отменяются «Разбаном». Спрашивать о них дважды на странице,
# где кнопка и так одна, значит приучить нажимать «да» не глядя.
_USER_CONFIRM = ("viol", "clr", "reset")


async def user_card(request):
    """
    Карточка участника: GET показывает, POST выполняет одно действие.

    ⚠️ Каждое действие зовёт web/actions.py, а тот — те же функции, что кнопки
    карточки в боте, вместе с записью в журнал персонала тем же кодом.
    """
    csrf = auth.csrf_for(request.cookies.get(WEB_COOKIE_NAME))
    application = request.app.get("tg")
    bot = application.bot if application is not None else None

    try:
        target_id = int(request.match_info["uid"])
    except (KeyError, ValueError):
        raise aioweb.HTTPNotFound()

    if request.method == "GET":
        page = await pages.page_user_card(bot, target_id, csrf)
        return aioweb.Response(text=page, content_type="text/html")

    form = await request.post()
    if not auth.csrf_ok(request, str(form.get("csrf", ""))):
        logger.warning("🌐 Действие над участником отклонено: подпись формы не сошлась")
        page = await pages.page_user_card(bot, target_id, csrf)
        return aioweb.Response(text=page, content_type="text/html", status=403)

    actor_id = auth.current_user(request)
    do = str(form.get("do", ""))

    if do in _USER_CONFIRM and str(form.get("confirm", "")) != "1":
        page = await pages.page_user_card(bot, target_id, csrf, confirm=do)
        return aioweb.Response(text=page, content_type="text/html")

    message, bad = "", False
    try:
        message = await _run_user_action(actor_id, target_id, do, form, application)
    except actions.ActionError as e:
        message, bad = f"Не вышло: {e}", True
        logger.warning("🌐 Действие «%s» над %s не принято: %s", do, target_id, e)
    except Exception as e:
        message, bad = f"Не вышло: {e}", True
        logger.error("🌐 Действие «%s» над %s сорвалось: %s", do, target_id, e)

    page = await pages.page_user_card(bot, target_id, csrf,
                                      message=message, bad=bad)
    return aioweb.Response(text=page, content_type="text/html",
                           status=400 if bad else 200)


async def _run_user_action(actor_id: int, target_id: int, do: str, form,
                           application) -> str:
    """Одно действие карточки. Возвращает строку о том, что вышло."""
    if do == "set":
        code = str(form.get("code", ""))
        delta = 1 if str(form.get("delta", "1")) == "1" else -1
        new_val, changed = actions.user_adjust(actor_id, target_id, code, delta)
        if not changed:
            return "Дальше некуда — значение на границе."
        return ("↩️ Вернул общую настройку бота." if new_val is None
                else f"Новое значение: {new_val}")

    if do == "tog":
        code = str(form.get("code", ""))
        new_val = actions.user_toggle(actor_id, target_id, code)
        title, words = actions.USER_TOGGLE_WORDS[code]
        return f"{title}: {words[new_val]}"

    if do == "viol":
        actions.user_reset_violations(actor_id, target_id)
        return "🧾 Нарушения обнулены."

    if do == "clr":
        actions.user_clear_history(actor_id, target_id)
        return "🧹 История диалога очищена."

    if do == "reset":
        actions.user_reset_settings(actor_id, target_id)
        return "↩️ Персональные настройки сброшены — действуют общие."

    if do == "rank":
        try:
            idx = int(form.get("idx", ""))
        except (TypeError, ValueError):
            raise actions.ActionError("не понял, какое звание")
        name = actions.user_rank(actor_id, target_id, idx)
        return f"🎖️ Присвоено звание: {name}" if name else "🚫 Почётное звание убрано."

    if do == "role":
        make = str(form.get("make", "")) == "1"
        await actions.user_role(actor_id, target_id, make, application)
        return ("🛡 Назначен модератором. Права выдайте галочками ниже."
                if make else "🚫 Модератор снят вместе со всеми правами.")

    if do == "perm":
        code = str(form.get("code", ""))
        on = actions.user_perm(actor_id, target_id, code)
        from services import roles
        return f'{roles.PERMS[code]["title"]}: {"✅ выдано" if on else "⬜ снято"}'

    if do == "mod":
        act = str(form.get("act", ""))
        try:
            chat_id = int(form.get("chat", ""))
            seconds = int(form.get("sec", "0") or 0)
        except (TypeError, ValueError):
            raise actions.ActionError("не понял, в какой группе и на какой срок")
        return await actions.user_moderate(actor_id, target_id, act, chat_id,
                                           seconds, application)

    raise actions.ActionError(f"неизвестное действие «{do}»")


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
    ("GET",  "/",        index,   "owner"),
    ("POST", "/set",     apply,   "owner"),
    ("GET",  "/prompts", prompts, "owner"),
    ("POST", "/prompts", prompts, "owner"),
    ("GET",  "/system",  system,  "owner"),
    ("POST", "/system",  system,  "owner"),
    ("GET",  "/download", download, "owner"),
    ("GET",  "/kb",      kb,      "owner"),
    ("POST", "/kb",      kb,      "owner"),
    ("GET",  "/quiz",    quiz,    "owner"),
    ("POST", "/quiz",    quiz,    "owner"),
    ("GET",  "/users",   users,   "owner"),
    ("GET",  "/users/{uid}", user_card, "owner"),
    ("POST", "/users/{uid}", user_card, "owner"),
    ("GET",  "/enter",   enter,   "open"),
    ("POST", "/enter",   enter,   "open"),
    ("GET",  "/exit",    exit_,   "owner"),
    ("GET",  "/health",  health,  "open"),
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
