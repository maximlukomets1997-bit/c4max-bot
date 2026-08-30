# ───────────────────────────────────────────────
#  web/pages.py — сборка страниц веб-админки (30.08.2026, этапы 0–1).
#
#  ⚠️ Ни одного адреса со стороны: ни шрифтов, ни картинок, ни библиотек.
#  Дело не в скорости — каждый чужой адрес на странице сообщал бы чужому
#  серверу, когда владелец открывает свою админку. График рисуется прямым
#  SVG, а не библиотекой, по той же причине.
#
#  ⚠️ КАЖДЫЙ ОРГАН УПРАВЛЕНИЯ — ЭТО ФОРМА, а не кнопка со сценарием. Страница
#  обязана полностью работать без JavaScript: сломается сценарий, отключится
#  он в браузере — админка остаётся рабочей, просто перезагружается целиком.
#  Сценарий внизу файла — только ускорение: он перехватывает отправку и
#  обновляет один орган на месте. Убери его, и ничего не сломается.
#
#  ⚠️ Любое значение, пришедшее не из наших констант, проходит через esc().
#  Названия моделей и версия — наши, но правило держим общим: место, где
#  однажды подставят чужой текст, находится само.
# ───────────────────────────────────────────────

import html
import logging

from config import (AUTO_UPDATE_ENABLED_DEFAULT, AVAILABLE_IMAGE_MODELS,
                    AVAILABLE_MODELS, BOT_VERSION, BOT_VERSION_URL,
                    GEMINI_MODEL, PROVIDERS, THINKING_LEVELS, THINKING_PHASES)
from services import settings_spec as spec

logger = logging.getLogger(__name__)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def _shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"ru\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex, nofollow\">"
        f"<title>{esc(title)}</title>"
        "<link rel=\"stylesheet\" href=\"/static/style.css\">"
        "</head><body>" + body + "</body></html>"
    )


# ─── вход и отказ ───────────────────────────────────────────────────

# Вход из самого Telegram: открывая мини-приложение, Telegram дописывает
# подписанные данные о человеке в АДРЕСНУЮ СТРОКУ, после решётки. Забираем их
# оттуда и отправляем на проверку. Так обходимся без стороннего сценария с
# telegram.org — страница остаётся полностью своей.
_WEBAPP_JS = """
<script>
(function () {
  var m = (location.hash || "").match(/tgWebAppData=([^&]*)/);
  if (!m) return;
  var f = document.createElement("form");
  f.method = "POST"; f.action = "/enter";
  var i = document.createElement("input");
  i.type = "hidden"; i.name = "tgWebAppData"; i.value = decodeURIComponent(m[1]);
  f.appendChild(i); document.body.appendChild(f); f.submit();
})();
</script>
"""


def page_login() -> str:
    """Страница «войдите». Открыта из Telegram — сама себя отправит дальше."""
    body = (
        "<div class=\"wrap center\">"
        "<div class=\"big\">🔒</div>"
        "<h1>Админка C4_Max</h1>"
        "<p>Вход только через Telegram. Откройте админку кнопкой в боте — "
        "или попросите у бота одноразовую ссылку для браузера.</p>"
        "</div>" + _WEBAPP_JS
    )
    return _shell("Вход — C4_Max", body)


def page_denied() -> str:
    """Пришёл кто-то не тот. Ничего лишнего не сообщаем."""
    body = (
        "<div class=\"wrap center\">"
        "<div class=\"big\">⛔</div>"
        "<h1>Доступа нет</h1>"
        "<p>Эта страница только для владельца бота.</p>"
        "</div>"
    )
    return _shell("Нет доступа", body)


# ─── органы управления ──────────────────────────────────────────────
#
#  Все они — формы к POST /set. Поля:
#    csrf  — подпись сессии (без неё правка не принимается);
#    what  — «setting» | «model» | «image» | «think»;
#    key   — ключ настройки, модели или провайдера;
#    value — значение; у тумблера его нет вовсе (это «переключи»).

def _form(csrf: str, what: str, key: str, value=None, cls: str = "",
          inner: str = "") -> str:
    hidden = (f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
              f'<input type="hidden" name="what" value="{esc(what)}">'
              f'<input type="hidden" name="key" value="{esc(key)}">')
    if value is not None:
        hidden += f'<input type="hidden" name="value" value="{esc(value)}">'
    return (f'<form method="post" action="/set" class="ctl {cls}" '
            f'data-key="{esc(key)}">{hidden}{inner}</form>')


def _switch(csrf: str, key: str) -> str:
    """Тумблер. Без сценария — обычная кнопка формы, с ним — переключается на месте."""
    on = spec.read(key)
    state = "on" if on else "off"
    word = "включено" if on else "выключено"
    inner = (f'<button type="submit" class="switch {state}" '
             f'aria-pressed="{"true" if on else "false"}">'
             f'<span class="knob"></span><span class="lbl">{word}</span></button>')
    return _form(csrf, "setting", key, None, "sw", inner)


def _stepper(csrf: str, key: str) -> str:
    """
    Регулятор числа: ➖ значение ➕ плюс ползунок.

    ⚠️ Ползунок — НАДСТРОЙКА: без сценария он ничего не отправляет, и остаются
    рабочими ➖/➕. Поэтому шкала и пределы у них общие (settings_spec), а не
    заданы здесь.
    """
    item = spec.SPEC[key]
    value = spec.read(key)
    shown = spec.display(key)
    minus = _form(csrf, "setting", key, _neighbour(key, -1), "step",
                  '<button type="submit" class="rnd" aria-label="меньше">−</button>')
    plus = _form(csrf, "setting", key, _neighbour(key, +1), "step",
                 '<button type="submit" class="rnd" aria-label="больше">+</button>')
    slider = (f'<input type="range" class="slider" data-key="{esc(key)}" '
              f'min="{item["min"]}" max="{item["max"]}" step="{item["step"]}" '
              f'value="{value}">')
    return (f'<div class="num" data-key="{esc(key)}">'
            f'{minus}<span class="val">{esc(shown)}</span>{plus}</div>'
            f'{slider}')


def _neighbour(key: str, steps: int):
    """
    Соседнее по шкале значение — его подставляем в форму ➖/➕.

    ⚠️ Считаем ЗДЕСЬ, а не «шагни от текущего» на сервере, намеренно: форма
    несёт конкретное значение, поэтому двойное нажатие по залипшей кнопке или
    повторная отправка страницы не уводят настройку дальше, чем видел человек.
    """
    item = spec.SPEC[key]
    value = spec.read(key) + item["step"] * steps
    value = max(item["min"], min(item["max"], value))
    if item["kind"] == "float":
        return f"{round(value, item.get('digits', 2)):.{item.get('digits', 2)}f}"
    return int(value)


def _chips(csrf: str, what: str, options: list, current) -> str:
    """Ряд взаимоисключающих кнопок: активная подсвечена и не нажимается."""
    out = []
    for key, label in options:
        if key == current:
            out.append(f'<span class="chip on">{esc(label)}</span>')
        else:
            out.append(_form(csrf, what, key, "1", "chipform",
                             f'<button type="submit" class="chip">{esc(label)}</button>'))
    return '<div class="chips">' + "".join(out) + "</div>"


def _rows(items) -> str:
    """items — список (название, подпись, html органа управления)."""
    out = []
    for name, hint, control in items:
        sub = f'<div class="note">{esc(hint)}</div>' if hint else ""
        out.append(f'<div class="row"><div class="name">{esc(name)}{sub}</div>'
                   f'<div class="ctlbox">{control}</div></div>')
    return '<div class="rows">' + "".join(out) + "</div>"


# ─── блоки страницы ─────────────────────────────────────────────────

def _models_block(csrf: str) -> str:
    import database.history as hist
    active = hist.get_setting("active_model", GEMINI_MODEL)
    active_img = hist.get_setting("active_image_model", "")

    text_options = [
        (key, f'{PROVIDERS.get(info.get("provider", ""), {}).get("icon", "")} {info["name"]}'.strip())
        for key, info in AVAILABLE_MODELS.items()
    ]
    img_options = [(key, info["name"]) for key, info in AVAILABLE_IMAGE_MODELS.items()]

    return ("<h2>Модель</h2>"
            + _chips(csrf, "model", text_options, active)
            + "<h2>Модель картинок</h2>"
            + _chips(csrf, "image", img_options, active_img))


def _thinking_block(csrf: str) -> str:
    """
    Глубина раздумий. На кнопке в Telegram видно одно положение и она листает
    по кругу; здесь помещается вся шкала, поэтому ступень выбирается сразу.
    Значение спрашиваем у services.gemini.thinking_level — там же живёт откат
    на начальное положение при мусоре в базе.
    """
    from services.gemini import thinking_level

    items = []
    for provider, levels in THINKING_LEVELS.items():
        meta = PROVIDERS.get(provider, {})
        codes = [code for code, _ in levels]
        cur = thinking_level(provider)
        if cur not in codes:
            cur = codes[0]
        phases = THINKING_PHASES.get(len(codes))
        options = []
        for pos, (code, label) in enumerate(levels):
            phase = phases[pos] if phases else ("🌑" if pos == 0 else "🌕")
            options.append((f"{provider}:{code}", f"{phase} {label}"))
        title = f'{meta.get("icon", "")} {meta.get("title", provider)}'.strip()
        items.append((title, "", _chips(csrf, "think", options, f"{provider}:{cur}")))
    return "<h2>Глубина раздумий</h2>" + _rows(items)


def _spec_blocks(csrf: str) -> str:
    """Разделы простых настроек — порядок и состав берутся из settings_spec."""
    out = []
    for code, title in spec.SECTIONS:
        items = []
        for key in spec.keys_of(code):
            item = spec.SPEC[key]
            control = (_switch(csrf, key) if item["kind"] == "toggle"
                       else _stepper(csrf, key))
            items.append((item["title"], item.get("hint", ""), control))
        if items:
            out.append(f"<h2>{esc(title)}</h2>" + _rows(items))
    return "".join(out)


def _chart(pairs: list, limit: int = 8) -> str:
    """
    Столбики «вызовов по моделям». Рисуем прямым SVG: библиотеке графиков
    здесь нечего делать, а тянуть её со стороны нельзя (см. шапку файла).
    """
    data = [(name, cnt) for name, cnt in pairs if cnt][:limit]
    if not data:
        return '<div class="rows"><div class="empty">вызовов ещё не было</div></div>'

    top = max(cnt for _, cnt in data)
    row_h, gap, pad_l, pad_r = 30, 8, 178, 56
    height = len(data) * (row_h + gap) - gap
    width = 700
    bar_max = width - pad_l - pad_r

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" '
             f'role="img" aria-label="Вызовы по моделям" '
             f'preserveAspectRatio="xMinYMin meet">']
    for i, (name, cnt) in enumerate(data):
        y = i * (row_h + gap)
        w = max(2, round(bar_max * cnt / top))
        label = AVAILABLE_MODELS.get(name, {}).get("name", name)
        cls = "bar" if i == 0 else "bar-2"
        parts.append(
            f'<text x="{pad_l - 10}" y="{y + row_h / 2 + 4}" text-anchor="end">{esc(label)}</text>'
            f'<rect class="{cls}" x="{pad_l}" y="{y}" width="{w}" height="{row_h}" rx="5"/>'
            f'<text class="val" x="{pad_l + w + 9}" y="{y + row_h / 2 + 4}">{esc(cnt)}</text>'
        )
    parts.append("</svg>")
    return '<div class="card wide">' + "".join(parts) + "</div>"


def _tiles() -> str:
    import database.history as hist
    from services import daily_report

    stats = hist.get_bot_stats()
    # ⚠️ На чистой базе get_bot_stats отдаёт строку "unknown", а не пустоту —
    # проверка «или» её не ловит, и в плитке было бы написано «unknown».
    active = stats.get("active_model")
    if active not in AVAILABLE_MODELS:
        active = GEMINI_MODEL
    active_title = AVAILABLE_MODELS.get(active, {}).get("name", active)
    icon = PROVIDERS.get(AVAILABLE_MODELS.get(active, {}).get("provider", ""), {}).get("icon", "")

    img = hist.get_setting("active_image_model", "")
    img_title = AVAILABLE_IMAGE_MODELS.get(img, {}).get("name", img or "—")

    # Расход с последнего снимка (обычно с полуночи). Считается на лету и
    # снимок НЕ трогает — та же функция, что у кнопки в панели статистики.
    calls_today, money_today, _ = daily_report._open_day_totals()

    return (
        "<div class=\"grid\">"
        f"<div class=\"card\"><div class=\"k\">Активная модель</div>"
        f"<div class=\"v\">{esc(icon)} {esc(active_title)}</div>"
        f"<div class=\"sub\">картинки: {esc(img_title)}</div></div>"

        f"<div class=\"card\"><div class=\"k\">Сегодня</div>"
        f"<div class=\"v\">{esc(calls_today)}</div>"
        f"<div class=\"sub\">вызовов · ≈${money_today:.4f}</div></div>"

        f"<div class=\"card\"><div class=\"k\">Всего вызовов</div>"
        f"<div class=\"v\">{esc(stats.get('api_calls_total', 0))}</div>"
        f"<div class=\"sub\">обменов за месяц: {esc(stats.get('lifetime_requests', 0))}</div></div>"

        f"<div class=\"card\"><div class=\"k\">Подписки на новости</div>"
        f"<div class=\"v\">{esc(stats.get('subscriptions', 0))}</div>"
        f"<div class=\"sub\">сообщений групп в архиве: {esc(stats.get('group_msg_count', 0))}</div></div>"
        "</div>"
    ), stats


# Ускорение: перехватываем отправку формы, шлём её тем же адресом и обновляем
# только этот орган. Страница целиком не перезагружается — на телефоне это
# разница между «работает» и «мучение».
# ⚠️ Ничего сверх этого сценарий не делает. Отключи его — останутся обычные
# формы, и админка будет работать так же, только с перезагрузкой.
_CONTROLS_JS = """
<script>
(function () {
  function post(form, extraValue) {
    var body = new FormData(form);
    if (extraValue !== undefined) body.set("value", extraValue);
    return fetch("/set", {method: "POST", body: body, headers: {"Accept": "application/json"}})
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); });
  }
  function flash(node, bad) {
    node.classList.add(bad ? "bad" : "ok");
    setTimeout(function () { node.classList.remove("ok", "bad"); }, 700);
  }
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form.ctl");
    if (!form) return;
    e.preventDefault();
    var box = form.closest(".row") || form;
    post(form).then(function (data) {
      render(form.dataset.key, data);
      flash(box, false);
    }).catch(function () { flash(box, true); location.reload(); });
  });
  document.addEventListener("change", function (e) {
    var slider = e.target.closest("input.slider");
    if (!slider) return;
    var row = slider.closest(".row");
    var form = row.querySelector("form.ctl");
    post(form, slider.value).then(function (data) {
      render(slider.dataset.key, data);
      flash(row, false);
    }).catch(function () { location.reload(); });
  });
  // Пока ползунок тянут — показываем число, но на сервер ничего не шлём.
  document.addEventListener("input", function (e) {
    var slider = e.target.closest("input.slider");
    if (!slider) return;
    var val = slider.closest(".row").querySelector(".val");
    if (val) val.textContent = slider.value;
  });
  function render(key, data) {
    if (!data || !data.ok) { location.reload(); return; }
    if (data.reload) { location.reload(); return; }
    var row = document.querySelector('[data-key="' + key + '"]');
    row = row && row.closest(".row");
    if (!row) { location.reload(); return; }
    var val = row.querySelector(".val");
    if (val) val.textContent = data.shown;
    var sw = row.querySelector(".switch");
    if (sw) {
      sw.classList.toggle("on", data.on);
      sw.classList.toggle("off", !data.on);
      sw.setAttribute("aria-pressed", data.on ? "true" : "false");
      sw.querySelector(".lbl").textContent = data.shown;
    }
    // У ➖/➕ в формах записаны СОСЕДНИЕ значения — после правки они устарели.
    if (data.neighbours) {
      var forms = row.querySelectorAll("form.step");
      if (forms.length === 2) {
        forms[0].querySelector('[name="value"]').value = data.neighbours[0];
        forms[1].querySelector('[name="value"]').value = data.neighbours[1];
      }
      var slider = row.querySelector("input.slider");
      if (slider && data.raw !== undefined) slider.value = data.raw;
    }
  }
})();
</script>
"""


# ─── люди: список и карточка (этап 3) ───────────────────────────────
#
#  ⚠️ Всё, что показывает карточка, читается ТЕМИ ЖЕ функциями, что и карточка
#  в боте (handlers/admin/panel_users.py). Своих запросов к базе здесь нет:
#  две карточки, считающие одно и то же по-разному, рано или поздно начнут
#  показывать разное, и понять, какая права, будет нечем.
#
#  ⚠️ Имя, ник и название группы — ЧУЖОЙ ТЕКСТ. Через esc() проходит всё
#  без исключений: «<» в нике сломал бы страницу так же, как ломал панель.

def _uform(csrf: str, target_id: int, fields: dict, inner: str,
           cls: str = "ctl") -> str:
    """Форма действия над участником. Все они шлют POST на /users/<id>."""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    for name, value in fields.items():
        hidden += f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
    return (f'<form method="post" action="/users/{int(target_id)}" '
            f'class="{cls}">{hidden}{inner}</form>')


def _btn(text: str, kind: str = "") -> str:
    return f'<button type="submit" class="btn {kind}">{esc(text)}</button>'


def page_users(csrf: str = "") -> str:
    """Список всех, кого знает бот."""
    from database.history import list_known_users
    from handlers.admin.panel_users import _display_name, _USERS_LIST_LIMIT
    from services import roles

    users = list_known_users(_USERS_LIST_LIMIT)

    rows = []
    for u in users:
        uid = u["user_id"]
        role = roles.role_of(uid)
        badge = {"owner": '<span class="tag owner">👑 владелец</span>',
                 "moderator": '<span class="tag mod">🛡 модератор</span>'}.get(role, "")
        marks = [f'сообщений {u.get("msg_count", 0)}']
        if u.get("mute_count"):
            marks.append(f'мутов {u["mute_count"]}')
        if u.get("link_count"):
            marks.append(f'ссылок {u["link_count"]}')
        rows.append(
            f'<a class="urow" href="/users/{uid}">'
            f'<div class="uname">{esc(_display_name(u))} {badge}</div>'
            f'<div class="note">id {uid} · {esc(" · ".join(marks))}</div>'
            f'</a>'
        )

    head = ("<header><h1>Пользователи</h1>"
            "<div class=\"ver\"><a href=\"/\">← к сводке</a></div></header>")
    body = ("<div class=\"wrap\">" + head
            + f'<div class="note" style="margin-bottom:14px">Бот знает '
              f'{len(users)} человек. Нажмите на строку — откроется карточка.</div>'
            + '<div class="ulist">' + "".join(rows) + '</div>'
            + '<footer><span><a href="/">← к сводке</a></span></footer></div>')
    return _shell("Пользователи — C4_Max", body)


def _user_facts(bot_membership: list, target_id: int) -> str:
    """Верх карточки: кто это, стаж, нарушения, звание, общение с ботом."""
    from config import IMAGE_DAILY_LIMIT, MAX_CONTEXT_MESSAGES, ADMIN_IDS, QUIZ_RANKS
    from database.history import (get_history_length, get_remaining_image_calls,
                                  get_user_stats, get_user_usage)
    from services.antispam import trust_info
    from services.user_settings import honorary_rank

    ti = trust_info(target_id)
    stats = get_user_stats(target_id)
    usage = get_user_usage(target_id)
    ctx_len = get_history_length(target_id)
    img_left = get_remaining_image_calls(target_id, IMAGE_DAILY_LIMIT)
    img_line = ("∞ (админ бота)" if target_id in ADMIN_IDS
                else f"осталось {img_left} из {IMAGE_DAILY_LIMIT}")

    hon = honorary_rank(target_id)
    if hon:
        found = next((r for r in QUIZ_RANKS if r["name"] == hon), None)
        rank = f'{found["icon"] if found else "🏅"} {hon} (почётное)'
    else:
        rank = f'{stats["rank_icon"]} {stats["rank"]}'

    quiz = ""
    if stats["total_attempts"]:
        rate = f' · {stats["success_rate"]}%'
        quiz = (f'{stats["correct_answers"]} верных из '
                f'{stats["total_attempts"]}{rate}')

    tiles = [
        ("Служба в гарнизоне", f'{ti["days"]} дн.',
         f'сообщений в группах: {ti["msgs"]}'),
        ("Нарушения", f'{ti["mutes"]} мутов',
         f'удалённых ссылок: {ti["links"]}'),
        ("Звание", rank, quiz or "в викторину не играл"),
        ("Общение с ботом", f'{usage["total_requests"]} запросов',
         f'контекст {ctx_len} из {MAX_CONTEXT_MESSAGES} · картинки: {img_line}'),
    ]
    cards = "".join(
        f'<div class="card"><div class="k">{esc(k)}</div>'
        f'<div class="v">{esc(v)}</div><div class="sub">{esc(sub)}</div></div>'
        for k, v, sub in tiles
    )
    block = f'<div class="grid">{cards}</div>'

    if bot_membership:
        lines = "".join(f'<div class="row"><div class="name">{esc(line)}</div></div>'
                        for line in bot_membership)
        block += f'<h2>Группы</h2><div class="rows">{lines}</div>'
    return block


def _user_settings_block(csrf: str, target_id: int) -> str:
    """Персональные настройки: четыре регулятора и три тумблера."""
    from handlers.admin.panel_users import (_USER_LIMITS, _USER_TOGGLES,
                                            _base_value)
    from services.user_settings import get as us_get

    titles = {
        "count":  ("Порог флуда", "сообщений за окно"),
        "window": ("Окно засчёта", "секунд"),
        "mute":   ("Длительность мута", "секунд"),
        "img":    ("Лимит картинок", "в сутки"),
    }
    personal = us_get(target_id)
    items = []
    for code, (title, unit) in titles.items():
        field = _USER_LIMITS[code]["field"]
        own = personal.get(field)
        if own is None:
            shown = f'{_base_value(code)} {unit} · общая'
        else:
            shown = f'{own} {unit} · своё'
        minus = _uform(csrf, target_id, {"do": "set", "code": code, "delta": "-1"},
                       '<button type="submit" class="rnd" aria-label="меньше">−</button>',
                       "ctl step")
        plus = _uform(csrf, target_id, {"do": "set", "code": code, "delta": "1"},
                      '<button type="submit" class="rnd" aria-label="больше">+</button>',
                      "ctl step")
        control = (f'<div class="num">{minus}'
                   f'<span class="val wide">{esc(shown)}</span>{plus}</div>')
        items.append((title, "ниже минимума — вернётся общая настройка бота", control))

    from web.actions import USER_TOGGLE_WORDS
    for code, field in _USER_TOGGLES.items():
        title, words = USER_TOGGLE_WORDS[code]
        value = 1 if personal.get(field) else 0
        state = "on" if value else "off"
        inner = (f'<button type="submit" class="switch {state}">'
                 f'<span class="knob"></span><span class="lbl">'
                 f'{esc(words[value])}</span></button>')
        items.append((title, "", _uform(csrf, target_id,
                                        {"do": "tog", "code": code}, inner, "ctl sw")))
    return "<h2>Персональные настройки</h2>" + _rows(items)


def _user_role_block(csrf: str, target_id: int) -> str:
    """Роль и галочки прав модератора."""
    from services import roles

    role = roles.role_of(target_id)
    if role == "owner":
        return ('<h2>Роль</h2><div class="rows"><div class="row">'
                '<div class="name">👑 Владелец<div class="note">задаётся в '
                'config.py — из админки не меняется</div></div></div></div>')

    items = []
    if role == "moderator":
        items.append(("🛡 Модератор", "снятие уберёт все галочки прав разом",
                      _uform(csrf, target_id, {"do": "role", "make": "0"},
                             _btn("Снять модератора", "danger"))))
        perms = roles.perms_of(target_id)
        for code, meta in roles.PERMS.items():
            on = perms.get(code)
            state = "on" if on else "off"
            inner = (f'<button type="submit" class="switch {state}">'
                     f'<span class="knob"></span><span class="lbl">'
                     f'{"выдано" if on else "нет"}</span></button>')
            items.append((meta["title"], meta["hint"],
                          _uform(csrf, target_id, {"do": "perm", "code": code},
                                 inner, "ctl sw")))
    else:
        items.append(("Обычный участник", "модератору права выдаются галочками",
                      _uform(csrf, target_id, {"do": "role", "make": "1"},
                             _btn("Назначить модератором"))))
    return "<h2>Роль и права</h2>" + _rows(items)


def _user_moderation_block(csrf: str, target_id: int) -> str:
    """Ручные меры по каждой известной группе."""
    from database.history import get_known_chats
    from handlers.admin.panel_users import _MUTE_PRESETS, _chat_title

    chats = get_known_chats() or []
    if not chats:
        return ('<h2>Меры</h2><div class="rows">'
                '<div class="empty">бот не знает ни одной группы</div></div>')

    blocks = []
    for chat in chats:
        chat_id = chat["chat_id"] if isinstance(chat, dict) else chat
        title = _chat_title(chat_id)
        mutes = "".join(
            _uform(csrf, target_id,
                   {"do": "mod", "act": "mute", "chat": chat_id, "sec": sec},
                   f'<button type="submit" class="chip">{esc(lbl)}</button>',
                   "ctl chipform")
            for sec, lbl in _MUTE_PRESETS)
        others = "".join(
            _uform(csrf, target_id, {"do": "mod", "act": act, "chat": chat_id},
                   f'<button type="submit" class="chip">{esc(lbl)}</button>',
                   "ctl chipform")
            for act, lbl in (("unmute", "🔓 Размут"), ("kick", "👢 Кик"),
                             ("ban", "⛔ Бан"), ("unban", "🔙 Разбан")))
        blocks.append(
            f'<div class="modchat"><div class="name">{esc(title)}</div>'
            f'<div class="note">🔇 мут на срок:</div>'
            f'<div class="chips">{mutes}</div>'
            f'<div class="chips">{others}</div></div>'
        )
    return ("<h2>Меры</h2>"
            '<div class="warnline">Кик и бан применяются СРАЗУ, без второго '
            'вопроса — кнопка одна. Бан снимается только «Разбаном».</div>'
            + "".join(blocks))


def _user_danger_block(csrf: str, target_id: int, confirm: str) -> str:
    """Три действия, которые стирают данные, — каждое со своим вопросом."""
    jobs = [
        ("viol", "Обнулить нарушения",
         "счётчики мутов и удалённых ссылок обнулятся, взыскание снимется "
         "досрочно. Стаж и число сообщений останутся"),
        ("clr", "Очистить диалог с ИИ",
         "бот забудет разговор — то же самое, что команда /clear от самого "
         "человека"),
        ("reset", "Сбросить персональные настройки",
         "человек вернётся на общие правила бота по всем полям сразу"),
    ]
    items = []
    for code, title, hint in jobs:
        if confirm == code:
            control = (
                '<div class="warn-btns">'
                + _uform(csrf, target_id, {"do": code, "confirm": "1"},
                         _btn("Да, выполнить", "danger"))
                + f'<a class="btn" href="/users/{int(target_id)}">Отмена</a>'
                + '</div>')
        else:
            control = _uform(csrf, target_id, {"do": code}, _btn(title))
        items.append((title, hint, control))
    return "<h2>Очистка</h2>" + _rows(items)


def _user_rank_block(csrf: str, target_id: int) -> str:
    """Почётное звание: все звания списком, текущее подсвечено."""
    from config import QUIZ_RANKS
    from services.user_settings import honorary_rank

    cur = honorary_rank(target_id)
    chips = []
    if cur:
        chips.append(_uform(csrf, target_id, {"do": "rank", "idx": "-1"},
                            '<button type="submit" class="chip">🚫 убрать</button>',
                            "ctl chipform"))
    for idx, r in enumerate(QUIZ_RANKS):
        label = f'{r["icon"]} {r["name"]}'
        if r["name"] == cur:
            chips.append(f'<span class="chip on">{esc(label)}</span>')
        else:
            chips.append(_uform(csrf, target_id, {"do": "rank", "idx": idx},
                                f'<button type="submit" class="chip">{esc(label)}</button>',
                                "ctl chipform"))
    return ("<h2>Почётное звание</h2>"
            '<div class="note" style="margin-bottom:8px">Почётное звание '
            'перекрывает заработанное в викторине.</div>'
            f'<div class="chips">{"".join(chips)}</div>')


async def page_user_card(bot, target_id: int, csrf: str = "",
                         confirm: str = "", message: str = "",
                         bad: bool = False) -> str:
    """
    Карточка участника.

    confirm — код действия, для которого показать вопрос «точно?».
    message — что сказать о только что выполненном действии.
    """
    from database.history import list_known_users
    from handlers.admin.panel_users import (_chat_membership, _display_name)
    from services import roles

    info = next((u for u in list_known_users(1000) if u["user_id"] == target_id),
                {"user_id": target_id, "username": "", "first_name": "",
                 "quiz_name": "", "msg_count": 0, "mute_count": 0, "link_count": 0})
    name = _display_name(info)
    nick = f' (@{info["username"]})' if info.get("username") else ""
    role = roles.role_of(target_id)
    badge = {"owner": '<span class="tag owner">👑 владелец</span>',
             "moderator": '<span class="tag mod">🛡 модератор</span>'}.get(role, "")

    membership = []
    if bot is not None:
        try:
            membership = await _chat_membership(bot, target_id)
        except Exception as e:
            logger.debug("🌐 Не удалось узнать группы участника %s: %s", target_id, e)

    head = (f'<header><h1>{esc(name)}{esc(nick)} {badge}</h1>'
            f'<div class="ver">id {int(target_id)} · '
            f'<a href="/users">← к списку</a></div></header>')

    note = ""
    if message:
        note = (f'<div class="{"warn-box" if bad else "ok-box"}">'
                f'{esc(message)}</div>')

    body = ("<div class=\"wrap\">" + head + note
            + _user_facts(membership, target_id)
            + _user_settings_block(csrf, target_id)
            + _user_rank_block(csrf, target_id)
            + _user_role_block(csrf, target_id)
            + _user_moderation_block(csrf, target_id)
            + _user_danger_block(csrf, target_id, confirm)
            + '<footer><span><a href="/users">← к списку</a></span>'
              '<span><a href="/">к сводке</a></span></footer></div>')
    return _shell(f"{name} — C4_Max", body)


# ─── страница промптов (этап 2) ─────────────────────────────────────
#
#  Отдельной страницей, а не блоком на главной: промпты — длинные тексты, и
#  рядом со сводкой и тумблерами они превратили бы её в простыню.
#
#  ⚠️ ЗДЕСЬ НЕТ НИ СТРОЧКИ JavaScript, и это осознанно. На главной сценарий
#  оправдан — там десятки мелких правок подряд. Здесь правка одна и редкая:
#  вставил текст, нажал «Сохранить». Обычная форма надёжнее и понятнее.

def page_prompts(csrf: str = "", confirm: str = "", saved: str = "") -> str:
    """
    Страница промптов.

    confirm — ключ промпта, для которого показать вопрос «точно стереть?»
    (человек отправил пустое поле поверх непустого текста).
    saved   — ключ промпта, который только что сохранён (подсветить карточку).
    """
    from services import prompts_spec

    assembled, assembled_len = prompts_spec.assembled_system_prompt()

    cards = []
    for item in prompts_spec.PROMPTS:
        key = item["key"]
        text = prompts_spec.read(key)
        marks = []
        if text:
            marks.append(f"{len(text)} символов")
        else:
            marks.append("пусто — бот работает без этого куска")
        # У основного промпта показываем, что уйдёт модели ЦЕЛИКОМ: он
        # склеивается с дополнениями, и по одному полю этого не видно.
        if key == "custom_system_prompt" and assembled_len != len(text):
            marks.append(f"вместе с дополнениями модель получит {assembled_len}")

        if key == confirm:
            body = (
                '<div class="warn-box">'
                '<b>Стереть этот промпт?</b><br>'
                'Поле пустое, а текст в нём есть. Заводского текста у промптов '
                'нет — восстановить будет нечем.'
                '<div class="warn-btns">'
                + f'<form method="post" action="/prompts">'
                  f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                  f'<input type="hidden" name="key" value="{esc(key)}">'
                  f'<input type="hidden" name="text" value="">'
                  f'<input type="hidden" name="confirm" value="1">'
                  f'<button type="submit" class="btn danger">Да, стереть</button>'
                  f'</form>'
                + '<a class="btn" href="/prompts">Отмена</a>'
                + '</div></div>'
            )
        else:
            body = (
                f'<form method="post" action="/prompts">'
                f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                f'<input type="hidden" name="key" value="{esc(key)}">'
                f'<textarea name="text" rows="10" spellcheck="false" '
                f'placeholder="Пусто. Бот работает без этого текста.">{esc(text)}</textarea>'
                f'<div class="pbtns">'
                f'<button type="submit" class="btn primary">Сохранить</button>'
                f'<span class="note">чтобы стереть — очистите поле и сохраните</span>'
                f'</div></form>'
            )

        cls = "pcard saved" if key == saved else "pcard"
        cards.append(
            f'<div class="{cls}">'
            f'<div class="phead"><h3>{esc(item["title"])}</h3>'
            f'<span class="pmeta">{esc(" · ".join(marks))}</span></div>'
            f'<div class="note">{esc(item["hint"])}</div>'
            f'{body}</div>'
        )

    head = ("<header>"
            "<h1>Промпты</h1>"
            "<div class=\"ver\"><a href=\"/\">← к сводке</a></div>"
            "</header>")

    intro = ('<div class="card wide"><div class="note">'
             'Промпты — это инструкции модели. Заводских текстов у них нет: '
             'пустое поле означает «работать без этого куска», а не «взять '
             'значение по умолчанию». Правка применяется сразу, следующий же '
             'ответ бота пойдёт по новому тексту.'
             '</div></div>')

    foot = ('<footer><span><a href="/">← к сводке</a></span>'
            '<span><a href="/exit">выйти</a></span></footer>')

    body = "<div class=\"wrap\">" + head + intro + "".join(cards) + foot + "</div>"
    return _shell("Промпты — C4_Max", body)


def page_summary(csrf: str = "") -> str:
    """Главная страница: сводка сверху, управление ниже."""
    tiles, stats = _tiles()

    version = (f'<a href="{esc(BOT_VERSION_URL)}" target="_blank" '
               f'rel="noopener noreferrer">v{esc(BOT_VERSION)}</a>')
    head = ("<header>"
            "<h1>Админка C4_Max</h1>"
            f"<div class=\"ver\">{version}</div>"
            "<div class=\"pill\"><span class=\"dot\"></span>работает</div>"
            "</header>")

    foot = ('<footer><span>Правки применяются сразу — бот видит их без '
            'перезапуска.</span><span><a href="/prompts">промпты</a></span>'
            '<span><a href="/users">пользователи</a></span>'
            '<span><a href="/">обновить</a></span>'
            '<span><a href="/exit">выйти</a></span></footer>')

    nav = ('<div class="nav">'
           '<a href="/prompts">⚙️ Промпты</a>'
           '<a href="/users">👥 Пользователи</a>'
           '</div>')

    body = ("<div class=\"wrap\">" + head + tiles + nav
            + _models_block(csrf)
            + _thinking_block(csrf)
            + _spec_blocks(csrf)
            + "<h2>Вызовы по моделям</h2>"
            + _chart(stats.get("api_calls_by_model", []))
            + foot + "</div>" + _CONTROLS_JS)
    return _shell("Админка C4_Max", body)
