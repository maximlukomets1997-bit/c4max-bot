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

import hashlib
import html
import logging
import os

from config import (AUTO_UPDATE_ENABLED_DEFAULT, AVAILABLE_IMAGE_MODELS,
                    AVAILABLE_MODELS, BOT_VERSION, BOT_VERSION_URL,
                    GEMINI_MODEL, PROVIDERS, THINKING_LEVELS, THINKING_PHASES)
from services import settings_spec as spec

logger = logging.getLogger(__name__)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


# ─── версия файла оформления ────────────────────────────────────────
#
#  ⚠️ ЗАЧЕМ ЭТО ЕСТЬ. 30.08.2026 Максим сообщил: выбрана тёмная тема, а сайт
#  показывает светлую. Сервер при этом отдавал всё верно — в настройке стояло
#  «dark», на странице не было пометки светлой темы, файл оформления был
#  свежий. Показывал светлую БРАУЗЕР: у него лежала утренняя копия
#  style.css — та, что ещё подстраивалась под систему. Заголовка про кэш у
#  файла не было вовсе, адрес не менялся, и браузер имел полное право держать
#  старую копию сколько угодно.
#
#  Лечение из двух частей, обе нужны:
#   • в адресе файла стоит его ОТПЕЧАТОК — поменялось содержимое, поменялся
#     адрес, и старая копия просто не подходит;
#   • ответы на /static/ помечены «перепроверяй» (см. web/routes.py).
#
#  Отпечаток пересчитывается, когда у файла меняется время правки или размер:
#  один stat() на отрисовку страницы, зато правка оформления видна сразу и
#  без перезапуска бота.

_CSS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "static", "style.css")
_css_stamp = {"key": None, "version": "0"}


def css_version() -> str:
    """Короткий отпечаток файла оформления — им помечается его адрес."""
    try:
        info = os.stat(_CSS_PATH)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        return _css_stamp["version"]
    if key != _css_stamp["key"]:
        try:
            with open(_CSS_PATH, "rb") as f:
                data = f.read()
        except OSError:
            return _css_stamp["version"]
        _css_stamp["key"] = key
        _css_stamp["version"] = hashlib.md5(data).hexdigest()[:10]
    return _css_stamp["version"]


def plain(text: str) -> str:
    """
    Текст, собранный ДЛЯ TELEGRAM, приведённый к обычному виду.

    ⚠️ Нужны ОБА шага. Панели бота отдают строки с разметкой и с уже
    заэкранированными символами: срежешь только теги — на странице останется
    «&lt;0.01», уберёшь только экранирование — на экран попадёт «<b>».
    Оба огреха были на странице обслуживания: незаданный остаток показывался
    как «<i>не задан</i>» (поймано разбором ошибок 30.08.2026).
    """
    import re as _re
    return html.unescape(_re.sub(r"</?[a-zA-Z][^>]*>", "", text or ""))


# Две темы на выбор. Ключ настройки и цвет полосы браузера для каждой.
# ⚠️ Значения ("dark"/"light") попадают в разметку страницы и в базу — менять
# их нельзя, не поменяв и то, и другое.
THEME_SETTING_KEY = "web_theme"
THEMES = (
    ("dark",  "🌑 Тёмная",  "#000000"),
    ("light", "☀️ Светлая", "#f4f6f9"),
)
THEME_DEFAULT = "dark"


def current_theme() -> str:
    """
    Выбранная тема. Мусор в настройке или недоступная база = тёмная:
    страница обязана собраться в любом случае, оформление — не тот повод,
    чтобы админка не открылась.
    """
    try:
        import database.history as hist
        value = hist.get_setting(THEME_SETTING_KEY, THEME_DEFAULT)
    except Exception:
        return THEME_DEFAULT
    return value if value in {code for code, _, _ in THEMES} else THEME_DEFAULT


def _shell(title: str, body: str) -> str:
    theme = current_theme()
    bar = next(color for code, _, color in THEMES if code == theme)
    # Пометку вешаем ТОЛЬКО у светлой: тёмная — это и есть :root по умолчанию.
    mark = ' data-theme="light"' if theme == "light" else ""
    return (
        f"<!doctype html><html lang=\"ru\"{mark}><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex, nofollow\">"
        # ⚠️ Обе строки — про тему, и обе нужны.
        # color-scheme говорит браузеру, какими рисовать поля ввода, ползунки
        # и полосу прокрутки ДО того, как загрузится оформление: без неё
        # тёмная страница на светлом компьютере успевает мигнуть белым.
        # theme-color красит полосы браузера на телефоне — иначе чёрная
        # страница окажется в белой рамке.
        f"<meta name=\"color-scheme\" content=\"{theme}\">"
        f"<meta name=\"theme-color\" content=\"{bar}\">"
        f"<title>{esc(title)}</title>"
        f"<link rel=\"stylesheet\" href=\"/static/style.css?v={css_version()}\">"
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


def _chips(csrf: str, what: str, options: list, current, back: str = "") -> str:
    """
    Ряд взаимоисключающих кнопок: активная подсвечена и не нажимается.

    back — куда вернуть человека после нажатия. Нужен кнопкам, которые стоят
    НЕ на главной: без него `/set` уводит на сводку, и смена темы со страницы
    промптов выбрасывала бы оттуда.
    """
    out = []
    for key, label in options:
        if key == current:
            out.append(f'<span class="chip on">{esc(label)}</span>')
        else:
            extra = (f'<input type="hidden" name="back" value="{esc(back)}">'
                     if back else "")
            out.append(_form(csrf, what, key, "1", "chipform",
                             extra +
                             f'<button type="submit" class="chip">{esc(label)}</button>'))
    return '<div class="chips">' + "".join(out) + "</div>"


# ─── верхняя полоса: разделы и вид ──────────────────────────────────
#
#  ⚠️ ОДИН СПИСОК РАЗДЕЛОВ НА ВЕСЬ САЙТ. Раньше кнопки разделов стояли только
#  на сводке и только посреди страницы: с «Промптов» на «Пользователей» было
#  не перейти, не вернувшись назад. Теперь полоса рисуется на КАЖДОЙ странице
#  (просьба Максима 30.08.2026). Новый раздел дописывать сюда — и он появится
#  сразу везде.

NAV = (
    ("/",        "📊 Сводка"),
    ("/prompts", "⚙️ Промпты"),
    ("/users",   "👥 Пользователи"),
    ("/kb",      "📚 База знаний"),
    ("/quiz",    "🎮 Викторина"),
    ("/journal", "📋 Журналы"),
    ("/system",  "🛠 Обслуживание"),
)


def _topbar(csrf: str, active: str = "") -> str:
    """
    Полоса под заголовком: кнопки разделов слева, выбор темы справа.
    active — адрес текущей страницы, её кнопка подсвечена и не нажимается.
    """
    links = "".join(
        (f'<span class="navlink on">{esc(label)}</span>' if href == active
         else f'<a class="navlink" href="{href}">{esc(label)}</a>')
        for href, label in NAV)
    theme = _chips(csrf, "theme", [(code, label) for code, label, _ in THEMES],
                   current_theme(), back=active or "/")
    return (f'<div class="topbar"><nav class="nav">{links}</nav>'
            f'<div class="themepick"><span class="note">Вид</span>{theme}</div>'
            f'</div>')


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


# ─── деньги, отчёты, обслуживание (этап 5) ──────────────────────────

def _sysform(csrf: str, fields: dict, inner: str, cls: str = "ctl") -> str:
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    for name, value in fields.items():
        hidden += f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
    return f'<form method="post" action="/system" class="{cls}">{hidden}{inner}</form>'


def _money_block(csrf: str) -> str:
    """
    Счета, счётчики «потрачено» и квоты токенов — каждое поле своим окошком.

    ⚠️ Поля и разбор числа берутся из панели бота
    (`panel_balance._balance_field`): второй разборщик «5,32» и «1 000 000»
    разъехался бы с первым.
    """
    from handlers.admin.panel_balance import (_BALANCE_FIELDS, _COST_FIELDS,
                                              _balance_field, _qwen_model_keys,
                                              _value_str)

    ids = [pid for pid in _BALANCE_FIELDS]
    ids += [f"cost:{pid}" for pid in _COST_FIELDS]
    ids += [f"qwen:{m}" for m in _qwen_model_keys()]

    items = []
    for field_id in ids:
        info = _balance_field(field_id)
        if not info:
            continue
        # Разметку Telegram из значения убираем — здесь она не к месту.
        now = plain(_value_str(info["key"], info["kind"], info["absent"]))
        control = _sysform(
            csrf, {"do": "money", "field": field_id},
            f'<input type="text" name="value" class="qinput short" '
            f'placeholder="{esc(info["example"])}">' + _btn("Записать"),
            "ctl pbtns")
        items.append((info["title"].title(), f'сейчас: {now} · «−» — {info["clear"]}',
                      control))
    return "<h2>💰 Счета и квоты</h2>" + _rows(items)


def page_system(application, csrf: str = "", confirm: str = "",
                report: str = "", report_text: str = "",
                digest_chat: int = 0, digest_body: str = "",
                message: str = "", bad: bool = False, upd_page: int = 0) -> str:
    """Деньги, отчёты, логи, обновления, дайджест, копия базы, перезапуск."""
    from database.history import get_known_chats
    from services import backup, group_digest, update_log
    from handlers.admin import common as adm_common
    from handlers.admin.panel_users import _chat_title

    note = ""
    if message:
        note = f'<div class="{"warn-box" if bad else "ok-box"}">{esc(message)}</div>'

    # ── отчёты ──
    rep_items = [
        ("📊 Отчёт за вчера", "последний суточный отчёт о расходах",
         _sysform(csrf, {"do": "report", "kind": "day"}, _btn("Показать"))),
        ("📅 Отчёт за неделю", "последний недельный отчёт",
         _sysform(csrf, {"do": "report", "kind": "week"}, _btn("Показать"))),
    ]
    rep_html = "<h2>📊 Отчёты</h2>" + _rows(rep_items)
    if report_text:
        rep_html += f'<div class="pcard"><pre class="article">{esc(report_text)}</pre></div>'
    elif report:
        rep_html += ('<div class="rows"><div class="empty">отчёта ещё нет — '
                     'он появится после первой полуночи</div></div>')

    # ── логи ──
    _cur_path, cur_raw = adm_common._read_current_log()
    _arc_path, arc_raw = adm_common._read_archive_log()
    log_items = [
        ("📜 Лог этого запуска", f'{backup.human_size(len(cur_raw))}',
         f'<a class="btn" href="/download?what=log">Скачать</a>'),
        ("🗄 Архив прошлых запусков",
         f'{backup.human_size(len(arc_raw))} · '
         f'запусков: {adm_common._count_archive_sessions(arc_raw)}',
         f'<a class="btn" href="/download?what=archive">Скачать</a>'),
    ]
    log_html = "<h2>📜 Логи</h2>" + _rows(log_items)
    if cur_raw:
        tail = cur_raw.decode("utf-8", errors="replace").splitlines()[-40:]
        log_html += (f'<div class="pcard"><div class="note">Последние 40 строк:'
                     f'</div><pre class="article">{esc(chr(10).join(tail))}</pre></div>')

    # ── обновления ──
    upd_html = "<h2>⬇️ Обновления</h2>"
    try:
        items = update_log.recent() if update_log.available() else []
    except Exception as e:
        logger.debug("🌐 Не удалось прочитать историю обновлений: %s", e)
        items = []

    # Листаем страницами по столько же, сколько в боте (`panel_updates`):
    # до 01.09.2026 сайт показывал 15 последних и дальше пути не было.
    # ⚠️ Размер страницы берётся ОТТУДА, а не своим числом — иначе «Раньше»
    # на сайте и в боте пролистывали бы разное количество.
    from handlers.admin.panel_updates import _PAGE_SIZE as _UPD_PAGE
    upd_pages = max(1, (len(items) + _UPD_PAGE - 1) // _UPD_PAGE)
    upd_page = max(0, min(upd_page, upd_pages - 1))
    items = items[upd_page * _UPD_PAGE:(upd_page + 1) * _UPD_PAGE]
    if items:
        # ⚠️ Надпись собирает та же функция, что и кнопки в боте
        # (`panel_updates._label`): она умеет не повторять номер версии дважды
        # и подставлять её из git там, где в названии её нет. Своя сборка
        # разъехалась бы с кнопками на первой же особенности.
        from handlers.admin.panel_updates import _label
        rows = "".join(
            f'<div class="row"><div class="name">'
            f'<a href="{esc(u.get("url", ""))}" target="_blank" '
            f'rel="noopener noreferrer">{esc(_label(u))}</a></div></div>'
            for u in items)
        upd_html += f'<div class="rows">{rows}</div>'
        # Листалка появляется, только когда листать есть что. Ссылками, а не
        # формой: переход по страницам — чтение, менять им нечего.
        if upd_pages > 1:
            back = (f'<a class="btn" href="/system?upd={upd_page + 1}">← Раньше</a>'
                    if upd_page + 1 < upd_pages else "")
            fwd = (f'<a class="btn" href="/system?upd={upd_page - 1}">Позже →</a>'
                   if upd_page > 0 else "")
            upd_html += (f'<div class="warn-btns">{back}'
                         f'<span class="note">страница {esc(upd_page + 1)} '
                         f'из {esc(upd_pages)}</span>{fwd}</div>')
    else:
        upd_html += ('<div class="rows"><div class="empty">история недоступна — '
                     'git не отвечает или это не рабочая копия</div></div>')

    # ── дайджест ──
    dig_on = group_digest.is_enabled()
    dig_items = [("📊 Еженедельный дайджест", "приходит владельцу в личку по понедельникам",
                  _sysform(csrf, {"do": "digest_toggle"},
                           f'<button type="submit" class="switch '
                           f'{"on" if dig_on else "off"}"><span class="knob"></span>'
                           f'<span class="lbl">{"включён" if dig_on else "выключен"}'
                           f'</span></button>', "ctl sw"))]
    chats = get_known_chats() or []
    for chat in chats:
        cid = chat["chat_id"]
        controls = _sysform(csrf, {"do": "digest_show", "chat": cid}, _btn("Показать"))
        # ⚠️ КНОПКА ОТПРАВКИ ЕСТЬ ТОЛЬКО У ПОКАЗАННОГО ДАЙДЖЕСТА, и она несёт
        # с собой ИМЕННО ТОТ текст, который сейчас на экране. Неделя скользящая:
        # пересчёт в момент отправки дал бы другие цифры, и в группу ушло бы не
        # то, что владелец видел (то же решение — в кнопке бота).
        shown = digest_body if digest_chat == cid else ""
        if shown:
            if confirm == f"digest:{cid}":
                controls += ('<div class="warn-btns">'
                             + _sysform(csrf, {"do": "digest_send", "chat": cid,
                                               "text": shown, "confirm": "1"},
                                        _btn("Да, отправить в группу", "danger"))
                             + '<a class="btn" href="/system">Отмена</a></div>')
            else:
                controls += _sysform(csrf, {"do": "digest_send", "chat": cid,
                                            "text": shown}, _btn("📤 В группу"))
        hint = ("отправку увидят ВСЕ участники чата" if shown
                else "нажмите «Показать» — тогда появится отправка в группу")
        dig_items.append((_chat_title(cid), hint, controls))
    dig_html = "<h2>📊 Дайджест недели</h2>" + _rows(dig_items)
    if digest_body:
        dig_html += (f'<div class="pcard"><div class="phead"><h3>'
                     f'{esc(_chat_title(digest_chat))}</h3></div>'
                     f'<pre class="article">{esc(digest_body)}</pre></div>')

    # ── копия базы ──
    copies = backup.list_backups()
    copy_html = ("<h2>💾 Копия базы</h2>" + _rows([
        ("Снять копию сейчас",
         f'ночных копий на сервере: {len(copies)}',
         _sysform(csrf, {"do": "backup"}, _btn("Снять и скачать", "primary"))),
    ]))

    # ── опасное ──
    danger = []
    for code, title, hint, label in (
        ("wipe", "🧹 Очистить РАЗГОВОРЫ",
         "бот забудет переписку во ВСЕХ группах сразу", "Очистить"),
        ("restart", "🔄 Перезапустить бота",
         "сайт живёт внутри бота и пропадёт на несколько секунд — "
         "поднимется сам", "Перезапустить"),
    ):
        if confirm == code:
            control = ('<div class="warn-btns">'
                       + _sysform(csrf, {"do": code, "confirm": "1"},
                                  _btn("Да, выполнить", "danger"))
                       + '<a class="btn" href="/system">Отмена</a></div>')
        else:
            control = _sysform(csrf, {"do": code}, _btn(label))
        danger.append((title, hint, control))
    danger_html = "<h2>⚠️ Опасное</h2>" + _rows(danger)

    head = ('<header><h1>Обслуживание</h1>'
            '<div class="ver"><a href="/">← к сводке</a></div></header>')
    body = ("<div class=\"wrap\">" + head + _topbar(csrf, "/system") + note
            + _money_block(csrf) + rep_html + log_html + upd_html
            + dig_html + copy_html + danger_html
            + '<footer><span><a href="/">← к сводке</a></span></footer></div>')
    return _shell("Обслуживание — C4_Max", body)


# ─── журналы: модерация и персонал (этап 6, 01.09.2026) ─────────────
#
#  ⚠️ ЗАЧЕМ ЭТА СТРАНИЦА. До неё сайт ПИСАЛ в журнал персонала на каждом
#  действии, а прочитать его можно было только из Телеграма. Журнал модерации
#  и улики (тексты удалённых сообщений) на сайте отсутствовали вовсе.
#
#  ⚠️ ЗДЕСЬ ЛЕЖИТ ЧУЖАЯ ПЕРЕПИСКА. Улики — это сообщения живых людей, удалённые
#  ботом. Круг зрителей не расширился (на сайт пускает только владельца,
#  модераторов здесь нет вовсе), но цена утечки ссылки входа выросла: по ней
#  теперь открываются не только настройки. Любая новая строка на этой странице
#  проверяется вопросом «что будет, если этот экран однажды увидит чужой».
#
#  ⚠️ ИМЕНА И ТЕКСТЫ ЗДЕСЬ — ЧУЖОЙ ВВОД. Экранировать обязательно: символ «<»
#  в имени участника ломает разметку и страница перестаёт открываться. Ровно
#  на этом уже наступали в панели бота.

# Сколько строк журнала модерации показывает СТРАНИЦА. В боте их пять
# (`panel_mod`) — но там потолок сообщения Telegram в 4096 знаков, а здесь его
# нет. Переносить вместе с возможностью её ограничение незачем; панель бота
# при этом не тронута, там по-прежнему пять.
_MOD_LOG_LIMIT = 25


def _jform(csrf: str, fields: dict, inner: str, cls: str = "ctl") -> str:
    """Форма, отправляющая действие на страницу журналов."""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    for name, value in fields.items():
        hidden += f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
    return f'<form method="post" action="/journal" class="{cls}">{hidden}{inner}</form>'


def _evidence_block(log_id: int) -> str:
    """Развёрнутые улики одной записи: тексты удалённых сообщений."""
    from services.antispam import get_evidence, MOD_STATS_DAYS

    saved = get_evidence(log_id)
    if saved:
        items = []
        for i, m in enumerate(saved, 1):
            text = (m.get("text") or "").strip()
            if not text:
                # Пустой текст — не поломка: мутят и за поток картинок.
                text = ("🖼 [фото/медиа — без текста]" if m.get("has_photo")
                        else "[пусто]")
            items.append(f'<li>{esc(text)}</li>')
        inner = f'<ol class="opts">{"".join(items)}</ol>'
    else:
        inner = ('<div class="empty">тексты не сохранились — сообщения были '
                 'без текста или запись уже очищена</div>')
    return (f'<div class="pcard"><div class="phead">'
            f'<h3>📜 Удалённые сообщения</h3>'
            f'<span class="pmeta">хранятся {esc(MOD_STATS_DAYS)} дней · '
            f'<a href="/journal">закрыть</a></span></div>{inner}</div>')


def page_journal(csrf: str = "", confirm: str = "", evidence: int = 0,
                 message: str = "") -> str:
    """Журналы: модерация с уликами и персонал, обе очистки."""
    from config import STAFF_LOG_DAYS
    from database.history import (count_staff_actions, get_recent_moderation_actions,
                                  get_recent_staff_actions)
    from handlers.admin.common import _fmt_mod_time, _known_names, _staff_name
    from handlers.admin.panel_mod import MOD_ACTION_TITLES, MOD_ACTIONS_WITH_EVIDENCE
    from handlers.admin.panel_users import _ACTION_TITLES, _STAFF_LOG_LIMIT
    from services.antispam import get_mute_stats, MOD_STATS_DAYS

    # Имена собираем ОДНИМ проходом на всю страницу — как в панели бота:
    # иначе на каждую из 25 строк шёл бы отдельный скан таблиц.
    names = _known_names()

    # ── журнал модерации ──
    mod_rows = []
    for a in get_recent_moderation_actions(_MOD_LOG_LIMIT):
        icon, verb = MOD_ACTION_TITLES.get(a["action"], ("❔", a["action"]))
        who = (a.get("name") or str(a.get("user_id") or ""))[:40]
        note = ""
        admin = a.get("admin_name")
        if admin:
            # У автоматики графа пуста; слово подбирается по виду действия,
            # как в боте: размут и разбан «снял», остальное «админ».
            word = "снял" if a["action"] in ("unmute", "unban") else "админ"
            note = f'<div class="note">{esc(word)}: {esc(admin[:40])}</div>'
        link = ""
        if a["action"] in MOD_ACTIONS_WITH_EVIDENCE and a.get("id"):
            link = (f'<a class="btn" href="/journal?evidence={esc(a["id"])}">'
                    f'📜 улики</a>')
        mod_rows.append(
            f'<div class="row"><div class="name">'
            f'{esc(icon)} {esc(_fmt_mod_time(a["ts"]))} {esc(verb)}: {esc(who)}'
            f'{note}</div><div class="ctlbox">{link}</div></div>')
        # Улики разворачиваются ПОД своей строкой, а не отдельной страницей:
        # человек видит, к какой именно записи они относятся.
        if evidence and a.get("id") == evidence:
            mod_rows.append(_evidence_block(evidence))

    stats = get_mute_stats(MOD_STATS_DAYS) or {}
    mod_head = (f'<h2>🛡 Модерация</h2>'
                f'<div class="note">за {esc(MOD_STATS_DAYS)} дней: мутов '
                f'{esc(stats.get("mutes", 0))} · банов {esc(stats.get("bans", 0))} · '
                f'показаны последние {esc(_MOD_LOG_LIMIT)}</div>')
    mod_html = mod_head + (f'<div class="rows">{"".join(mod_rows)}</div>' if mod_rows
                           else '<div class="rows"><div class="empty">'
                                'пока пусто — бот никого не наказывал</div></div>')

    # ── журнал персонала ──
    staff_rows = []
    for r in get_recent_staff_actions(_STAFF_LOG_LIMIT):
        icon, verb = _ACTION_TITLES.get(r["action"], ("❔", r["action"]))
        actor = (r.get("actor_name") or str(r["actor_id"]))[:40]
        line = f'{esc(icon)} {esc(_fmt_mod_time(r["ts"]))} {esc(actor)} — {esc(verb)}'
        if r.get("target_id"):
            line += f' → {esc(_staff_name(r["target_id"], names)[:30])}'
        note = (f'<div class="note">{esc(r["details"][:120])}</div>'
                if r.get("details") else "")
        staff_rows.append(f'<div class="row"><div class="name">{line}{note}'
                          f'</div></div>')

    staff_head = (f'<h2>📋 Персонал</h2>'
                  f'<div class="note">действий за 7 дней: '
                  f'{esc(count_staff_actions(7))} · хранится '
                  f'{esc(STAFF_LOG_DAYS)} дней · показаны последние '
                  f'{esc(_STAFF_LOG_LIMIT)}</div>')
    staff_html = staff_head + (f'<div class="rows">{"".join(staff_rows)}</div>'
                               if staff_rows else
                               '<div class="rows"><div class="empty">'
                               'пока пусто — персонал ничего не делал</div></div>')

    # ── опасное: обе очистки, каждая со своим вопросом ──
    danger = []
    for code, title, hint in (
        ("modclear", "🧹 Очистить журнал модерации",
         "улики стираются вместе с ним — восстановить нечем"),
        ("staffclear", "🧹 Очистить журнал персонала",
         "записи о действиях админов пропадут навсегда"),
    ):
        if confirm == code:
            control = ('<div class="warn-btns">'
                       + _jform(csrf, {"do": code, "confirm": "1"},
                                _btn("Да, выполнить", "danger"))
                       + '<a class="btn" href="/journal">Отмена</a></div>')
        else:
            control = _jform(csrf, {"do": code}, _btn("Очистить"))
        danger.append((title, hint, control))

    note_html = f'<div class="ok-box">{esc(message)}</div>' if message else ""
    head = ('<header><h1>Журналы</h1>'
            '<div class="ver"><a href="/">← к сводке</a></div></header>')
    body = ('<div class="wrap">' + head + _topbar(csrf, "/journal") + note_html
            + mod_html + staff_html
            + "<h2>⚠️ Опасное</h2>" + _rows(danger)
            + '<footer><span><a href="/">← к сводке</a></span></footer></div>')
    return _shell("Журналы — C4_Max", body)


# ─── база знаний и викторина (этап 4) ───────────────────────────────
#
#  ⚠️ Обе страницы САМИ ПЕРЕЧИТЫВАЮТСЯ, пока идёт долгая работа (пересборка
#  указателя, сборка вопросов): иначе итог пришлось бы ловить вручную. Как
#  только работа кончилась, обновление прекращается — незачем дёргать базу.

def _kbform(csrf: str, fields: dict, inner: str, cls: str = "ctl",
            action: str = "/kb", multipart: bool = False) -> str:
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    for name, value in fields.items():
        hidden += f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
    enc = ' enctype="multipart/form-data"' if multipart else ""
    return (f'<form method="post" action="{action}" class="{cls}"{enc}>'
            f'{hidden}{inner}</form>')


def _job_box(application, latch: str, what: str) -> tuple:
    """Полоска состояния долгой работы. Возвращает (html, идёт ли сейчас)."""
    from . import longjobs
    if longjobs.is_running(application, latch):
        return (f'<div class="warnline">⏳ {esc(what)} идёт прямо сейчас. '
                f'Страница обновляется сама — итог появится здесь.</div>', True)
    result = longjobs.last_result(application, latch)
    if result:
        return f'<div class="ok-box">{esc(result)}</div>', False
    return "", False


def page_kb(application, csrf: str = "", section: str = "",
            open_article: str = "", confirm: str = "",
            search: str = "", report=None, message: str = "") -> str:
    """
    База знаний: разделы, статьи, загрузка, пересборка указателя, проверка
    поиска, журнал действий.
    """
    from services.knowledge_store import ARTICLE_KINDS, list_articles, read_article
    from database.history import get_recent_kb_actions
    from . import actions as web_actions

    articles = list_articles()
    pending = [a for a in articles if a["folder"] == "pending"]
    approved = [a for a in articles if a["folder"] == "approved"]

    # ── разделы со счётчиками ──
    tabs = [("pending", f"🕐 Ждут одобрения ({len(pending)})")]
    for kind, meta in sorted(ARTICLE_KINDS.items(), key=lambda kv: kv[1]["order"]):
        count = sum(1 for a in approved if a["kind"] == kind)
        if count:
            tabs.append((kind, f'{meta["icon"]} {meta["name"]} ({count})'))
    if not section:
        section = "pending" if pending else (tabs[1][0] if len(tabs) > 1 else "pending")
    tab_html = "".join(
        (f'<span class="chip on">{esc(label)}</span>' if code == section
         else f'<a class="chip" href="/kb?section={esc(code)}">{esc(label)}</a>')
        for code, label in tabs)

    # ── список статей раздела ──
    if section == "pending":
        shown = pending
    else:
        shown = [a for a in approved if a["kind"] == section]
    rows = "".join(
        f'<a class="urow" href="/kb?section={esc(section)}'
        f'&open={esc(a["folder"])}/{esc(a["fname"])}">'
        f'<div class="uname">{esc(a["title"])}</div>'
        f'<div class="note">{esc(a["fname"])}</div></a>'
        for a in shown)
    list_html = (f'<div class="ulist">{rows}</div>' if rows
                 else '<div class="rows"><div class="empty">в этом разделе пусто</div></div>')

    # ── открытая статья ──
    article_html = ""
    if open_article and "/" in open_article:
        folder, _, fname = open_article.partition("/")
        # ⚠️ Текст статьи читаем ОТДЕЛЬНО от кнопок. Файла может не быть
        # (удалили в другой вкладке, ошиблись папкой) — но вопрос «точно
        # удалить?» и кнопка «Отмена» обязаны показаться всё равно, иначе
        # страница молча превращается в тупик.
        try:
            _path, text = read_article(folder, fname)
            # ⚠️ Текст ПРАВИТСЯ прямо здесь — это та же «📝 Заменить», что в
            # боте. Без этого поля кнопка бота не имела на сайте пары, а
            # написанный для неё web/actions.kb_replace никем не звался.
            body_html = (
                f'<form method="post" action="/kb" class="ctl">'
                f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                f'<input type="hidden" name="do" value="replace">'
                f'<input type="hidden" name="folder" value="{esc(folder)}">'
                f'<input type="hidden" name="fname" value="{esc(fname)}">'
                f'<input type="hidden" name="section" value="{esc(section)}">'
                f'<textarea name="text" rows="16" spellcheck="false">'
                f'{esc(text)}</textarea>'
                f'<div class="pbtns">{_btn("Сохранить текст", "primary")}'
                f'<span class="note">после правки пересоберите указатель</span>'
                f'</div></form>')
            meta = f'{len(text)} символов · {"ждёт одобрения" if folder == "pending" else "в базе"}'
        except Exception as e:
            body_html = (f'<div class="warnline">Текст не открывается: '
                         f'{esc(e)}</div>')
            meta = "файла нет на месте"
        if True:
            buttons = []
            if folder == "pending":
                buttons.append(_kbform(csrf, {"do": "approve", "fname": fname,
                                              "section": section},
                                       _btn("✅ Одобрить", "primary")))
            if confirm == open_article:
                buttons.append(_kbform(csrf, {"do": "delete", "folder": folder,
                                              "fname": fname, "section": section,
                                              "confirm": "1"},
                                       _btn("Да, удалить насовсем", "danger")))
                buttons.append(f'<a class="btn" href="/kb?section={esc(section)}'
                               f'&open={esc(open_article)}">Отмена</a>')
            else:
                buttons.append(_kbform(csrf, {"do": "delete", "folder": folder,
                                              "fname": fname, "section": section},
                                       _btn("🗑 Удалить")))
            article_html = (
                f'<div class="pcard"><div class="phead">'
                f'<h3>{esc(fname)}</h3>'
                f'<span class="pmeta">{esc(meta)}</span></div>'
                f'{body_html}'
                f'<div class="pbtns">{"".join(buttons)}</div></div>'
            )

    # ── загрузка, пересборка, проверка поиска ──
    upload = _kbform(csrf, {"do": "add", "section": section},
                     '<input type="file" name="file" accept=".md,.txt" required>'
                     + _btn("Загрузить статью", "primary"),
                     "ctl pbtns", multipart=True)

    job_html, job_running = _job_box(application,
                                     web_actions.KB_REBUILD_LATCH,
                                     "Пересборка указателя")
    rebuild = _kbform(csrf, {"do": "rebuild", "section": section},
                      _btn("🔄 Пересобрать указатель"))

    search_form = _kbform(
        csrf, {"do": "search", "section": section},
        f'<input type="text" name="q" class="qinput" placeholder="Вопрос, '
        f'как его задал бы человек" value="{esc(search)}">' + _btn("Найти"),
        "ctl pbtns")
    search_html = ""
    if report is not None:
        if report.get("error"):
            search_html = f'<div class="warn-box">{esc(report["error"])}</div>'
        else:
            lines = []
            for r in report["results"]:
                mark = "✅" if r["passes"] else "▫️"
                lex = f' (слова +{r["lex"] * 100:.0f}%)' if r.get("lex") else ""
                why = "" if r["passes"] else f' — {r.get("reason", "")}'
                # ⚠️ Округляем, а не обрезаем строку: обрезка «первых четырёх
                # символов» на ровной сотне давала бы «100.%».
                lines.append(
                    f'<div class="row"><div class="name">{mark} '
                    f'{r["similarity"] * 100:.1f}%{esc(lex)} — '
                    f'{esc(r["title"])}{esc(why)}</div></div>')
            head = (f'Порог {int(round(report["threshold"] * 100))}% · '
                    f'статей в ответ {report["top_k"]} · '
                    f'полка {int(round(report.get("baseline", 0) * 100))}%')
            tail = ("✅ — эти статьи уйдут модели вместе с вопросом."
                    if any(r["passes"] for r in report["results"])
                    else "⚠️ Ничего не прошло отбор — модель ответит без базы знаний.")
            search_html = (f'<div class="note">{esc(head)}</div>'
                           f'<div class="rows">{"".join(lines)}</div>'
                           f'<div class="note">{esc(tail)}</div>')

    # ── журнал ──
    log_rows = "".join(
        f'<div class="row"><div class="name">{esc(a["action"])}: '
        f'{esc((a.get("article") or "")[:60])}</div></div>'
        for a in get_recent_kb_actions(10))
    log_html = (f'<div class="rows">{log_rows}</div>' if log_rows
                else '<div class="rows"><div class="empty">журнал пуст</div></div>')
    # Очистка журнала — та же кнопка, что «🧹 Очистить журнал» в панели бота.
    # Обработчик для неё был написан сразу, а кнопку я забыл: ветка висела
    # недостижимой (поймано разбором ошибок 30.08.2026).
    if confirm == "clearlog":
        log_html += ('<div class="warn-btns">'
                     + _kbform(csrf, {"do": "clearlog", "section": section,
                                      "confirm": "1"},
                               _btn("Да, очистить журнал", "danger"))
                     + f'<a class="btn" href="/kb?section={esc(section)}">Отмена</a>'
                     + '</div>')
    else:
        log_html += ('<div class="pbtns">'
                     + _kbform(csrf, {"do": "clearlog", "section": section},
                               _btn("🧹 Очистить журнал")) + '</div>')

    note = f'<div class="ok-box">{esc(message)}</div>' if message else ""
    head = ('<header><h1>База знаний</h1>'
            '<div class="ver"><a href="/">← к сводке</a></div></header>')
    refresh = ('<meta http-equiv="refresh" content="10">' if job_running else "")

    body = ("<div class=\"wrap\">" + head + _topbar(csrf, "/kb") + note + job_html
            + f'<div class="chips">{tab_html}</div>'
            + list_html + article_html
            + "<h2>Добавить статью</h2>"
            + '<div class="pcard"><div class="note">Файл .md или .txt. '
              'Загруженная вручную статья попадает СРАЗУ В БАЗУ, минуя очередь '
              '— в очереди ждут только новости, которые бот принёс сам. '
              'Чтобы она заработала в поиске, пересоберите указатель.'
              '</div>' + upload + '</div>'
            + "<h2>Поисковый указатель</h2>"
            + '<div class="pcard"><div class="note">Пересборка считает вектора '
              'заново по всем статьям. Идёт минутами, бот при этом работает.'
              '</div><div class="pbtns">' + rebuild + '</div></div>'
            + "<h2>Проверить поиск</h2>"
            + '<div class="pcard"><div class="note">Что нашлось бы по такому '
              'вопросу. Ничего не меняет.</div>' + search_form + search_html + '</div>'
            + "<h2>Последние действия</h2>" + log_html
            + '<footer><span><a href="/">← к сводке</a></span></footer></div>')
    return _shell("База знаний — C4_Max", body).replace(
        "</head>", refresh + "</head>")


def page_quiz(application, csrf: str = "", mode: str = "draft",
              confirm: str = "", message: str = "") -> str:
    """Викторина: сводка, сборка вопросов, разбор черновиков, очистка."""
    from database.history import (get_quiz_bank_counts, list_quiz_questions,
                                  list_quiz_failures, get_all_quiz_stats,
                                  get_setting)
    from services import quiz_bank, quiz_daily
    from . import actions as web_actions

    counts = get_quiz_bank_counts()
    kb = quiz_bank.stats()
    seed = quiz_bank.seed_stats()
    auto_on = get_setting(quiz_daily.ENABLED_KEY, "0") == "1"
    players = len(get_all_quiz_stats() or {})

    # 📄 Сверка эталонного файла с банком (2026-09-01) — та же, что в панели
    # бота. ⚠️ Кнопка загрузки пропускает знакомый вопрос целиком, поэтому
    # правка разбора или вариантов в файле сама собой в игру не доезжает;
    # увидеть это можно только здесь.
    diff = quiz_bank.seed_diff() if seed["questions"] else None
    seed_tile = ""
    if diff and diff["file_ok"]:
        marks = []
        if diff["changed"]:
            marks.append(f'⚠️ разошлись {diff["changed"]}')
        if diff["missing"]:
            marks.append(f'не залито {diff["missing"]}')
        seed_tile = (f'<div class="card"><div class="k">Файл вопросов</div>'
                     f'<div class="v">{esc(diff["total"])}</div>'
                     f'<div class="sub">{esc(" · ".join(marks)) if marks else "всё сошлось"}'
                     f'</div></div>')

    tiles = (
        '<div class="grid">'
        f'<div class="card"><div class="k">Черновики</div>'
        f'<div class="v">{esc(counts["drafts"])}</div>'
        f'<div class="sub">ждут одобрения</div></div>'
        f'<div class="card"><div class="k">В игре</div>'
        f'<div class="v">{esc(counts["approved"])}</div>'
        f'<div class="sub">эти вопросы задаются людям</div></div>'
        f'<div class="card"><div class="k">Статьи без вопросов</div>'
        f'<div class="v">{esc(kb["articles_left"])}</div>'
        f'<div class="sub">всего статей в базе: {esc(kb["articles_total"])}</div></div>'
        f'<div class="card"><div class="k">Не вышло</div>'
        f'<div class="v">{esc(kb["failed"])}</div>'
        f'<div class="sub">статей, где сборка сорвалась</div></div>'
        + seed_tile
        + '</div>'
    )

    job_html, job_running = _job_box(application, web_actions.QUIZ_GEN_LATCH,
                                     "Сборка вопросов")

    auto_state = "on" if auto_on else "off"
    auto = _kbform(csrf, {"do": "auto"},
                   f'<button type="submit" class="switch {auto_state}">'
                   f'<span class="knob"></span><span class="lbl">'
                   f'{"включён" if auto_on else "выключен"}</span></button>',
                   "ctl sw", action="/quiz")
    controls = [(f'🕛 Вопрос дня {quiz_daily.hours_label()}',
                 "бот сам задаёт вопрос в группе по расписанию", auto)]
    controls.append(("🧠 Собрать вопросы",
                     "по статьям, у которых вопросов ещё нет; идёт минутами",
                     _kbform(csrf, {"do": "gen"}, _btn("Собрать", "primary"),
                             "ctl", action="/quiz")))
    if kb["failed"]:
        controls.append(("🔁 Повторить неудачные",
                         f'статей в очереди: {kb["failed"]}',
                         _kbform(csrf, {"do": "retry"}, _btn("Повторить"),
                                 "ctl", action="/quiz")
                         + _kbform(csrf, {"do": "forget"}, _btn("Забыть список"),
                                   "ctl", action="/quiz")))
    if seed["questions"]:
        controls.append(("📥 Мои вопросы в черновики",
                         f'написаны вручную, в файле их {seed["questions"]}',
                         _kbform(csrf, {"do": "seed"}, _btn("Загрузить"),
                                 "ctl", action="/quiz")))
    # ♻️ Показывается ТОЛЬКО когда есть расхождения: кнопка без работы врала бы
    # о том, что банк отстал от файла.
    if diff and diff["changed"]:
        controls.append(("♻️ Обновить из файла",
                         f'в банке отстали варианты, верный ответ или разбор '
                         f'у {diff["changed"]} вопросов — загрузка их пропускает',
                         _kbform(csrf, {"do": "reseed"}, _btn("Обновить", "primary"),
                                 "ctl", action="/quiz")))

    # ── опасные кнопки: каждая со своим вопросом ──
    danger = []
    for code, title, hint, cond in (
        ("wipe", "Очистить черновики", "неодобренные вопросы будут стёрты",
         counts["drafts"]),
        ("nuke", "Стереть ВСЕ вопросы", "и черновики, и те, что в игре",
         counts["approved"] or counts["drafts"]),
        ("zero", "Обнулить статистику игроков",
         "вопросы не тронет — стираются заслуги людей", players),
    ):
        if not cond:
            continue
        if confirm == code:
            control = ('<div class="warn-btns">'
                       + _kbform(csrf, {"do": code, "confirm": "1"},
                                 _btn("Да, выполнить", "danger"), "ctl",
                                 action="/quiz")
                       + '<a class="btn" href="/quiz">Отмена</a></div>')
        else:
            control = _kbform(csrf, {"do": code}, _btn(title), "ctl", action="/quiz")
        danger.append((title, hint, control))

    # ── список вопросов ──
    tabs = (f'<a class="chip{" on" if mode == "draft" else ""}" href="/quiz?mode=draft">'
            f'📝 Черновики ({counts["drafts"]})</a>'
            f'<a class="chip{" on" if mode == "live" else ""}" href="/quiz?mode=live">'
            f'✅ В игре ({counts["approved"]})</a>')
    questions = list_quiz_questions(approved=(mode == "live"), limit=50)
    cards = []
    for q in questions:
        options = q.get("options") or []
        # ⚠️ Ключ именно correct_idx (database/history.py::_row_to_question).
        # С «correct» подсветка молча не работала бы — вопросы одобрялись бы
        # вслепую, не видя, какой ответ считается верным.
        opts = "".join(
            f'<li class="{"right" if i == q.get("correct_idx") else ""}">{esc(o)}</li>'
            for i, o in enumerate(options))
        buttons = []
        if mode == "draft":
            buttons.append(_kbform(csrf, {"do": "ok", "qid": q["id"], "mode": mode},
                                   _btn("✅ В игру", "primary"), "ctl", action="/quiz"))
        buttons.append(_kbform(csrf, {"do": "del", "qid": q["id"], "mode": mode},
                               _btn("🗑 Удалить"), "ctl", action="/quiz"))
        cards.append(
            f'<div class="pcard"><div class="phead"><h3>{esc(q["question"])}</h3>'
            f'<span class="pmeta">#{esc(q["id"])} · {esc(q.get("article", ""))}</span>'
            f'</div><ol class="opts">{opts}</ol>'
            f'<div class="pbtns">{"".join(buttons)}</div></div>')
    list_html = ("".join(cards) if cards
                 else '<div class="rows"><div class="empty">здесь пусто</div></div>')

    fails = list_quiz_failures(20)
    fails_html = ""
    if fails:
        rows = "".join(
            f'<div class="row"><div class="name">{esc(f["article"])}'
            f'<div class="note">{esc(f.get("reason", ""))}</div></div></div>'
            for f in fails)
        fails_html = f'<h2>Что не вышло</h2><div class="rows">{rows}</div>'

    note = f'<div class="ok-box">{esc(message)}</div>' if message else ""
    head = ('<header><h1>Викторина</h1>'
            '<div class="ver"><a href="/">← к сводке</a></div></header>')
    refresh = ('<meta http-equiv="refresh" content="10">' if job_running else "")

    body = ("<div class=\"wrap\">" + head + _topbar(csrf, "/quiz") + note + job_html + tiles
            + "<h2>Управление</h2>" + _rows(controls)
            + f'<h2>Вопросы</h2><div class="chips">{tabs}</div>' + list_html
            + fails_html
            + ("<h2>Очистка</h2>" + _rows(danger) if danger else "")
            + '<footer><span><a href="/">← к сводке</a></span></footer></div>')
    return _shell("Викторина — C4_Max", body).replace("</head>", refresh + "</head>")


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
    body = ("<div class=\"wrap\">" + head + _topbar(csrf, "/users")
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

    body = ("<div class=\"wrap\">" + head + _topbar(csrf, "/users") + note
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

def _pform(csrf: str, fields: dict, inner: str, cls: str = "ctl") -> str:
    """Форма, отправляющая действие на страницу промптов."""
    hidden = f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
    for name, value in fields.items():
        hidden += f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'
    return f'<form method="post" action="/prompts" class="{cls}">{hidden}{inner}</form>'


def _personal_prompt_block(csrf: str, viewer_id: int) -> str:
    """
    Личный тумблер «⚙️ PROMPT» — применять ли личность C4_Max к разговору
    с САМИМ владельцем в личке (этап 7, 01.09.2026).

    ⚠️ НАСТРОЙКА ХРАНИТСЯ НАОБОРОТ: "1" в `admin_no_prompt_<id>` означает
    «промпт ВЫКЛЮЧЕН». Тумблер показывает состояние промпта, а не настройки,
    поэтому значение переворачивается — ровно как в панели бота. Забыть про
    переворот значит нарисовать тумблер, врущий в обе стороны сразу.

    ⚠️ Настройка ЛИЧНАЯ, у каждого админа своя: ключ несёт его id. Здесь
    id берётся у того, кто вошёл на сайт.
    """
    from database.history import get_setting

    if not viewer_id:
        return ""
    on = get_setting(f"admin_no_prompt_{viewer_id}", "0") != "1"
    switch = _pform(csrf, {"do": "myprompt"},
                    f'<button type="submit" class="switch {"on" if on else "off"}">'
                    f'<span class="knob"></span>'
                    f'<span class="lbl">{"применяется" if on else "не применяется"}'
                    f'</span></button>', "ctl sw")
    return "<h2>👤 Лично мне</h2>" + _rows([
        ("⚙️ Промпт в разговоре со мной",
         "выключишь — в личке с тобой бот отвечает без личности C4_Max; "
         "на остальных не влияет",
         switch),
    ])


def _proactive_block() -> str:
    """
    «📊 Участие в разговоре»: те же цифры, что на экране бота (этап 7).

    ⚠️ ДВЕ ПОЛОВИНЫ ЖИВУТ ПО РАЗНЫМ ПРАВИЛАМ, и это сказано прямо на экране:
    проверки берутся из журнала в базе и переживают перезапуск, отсев — из
    счётчиков В ПАМЯТИ и обнуляется вместе с ботом. «Привести к общему виду»
    нельзя: считать отсев в базе значит писать в неё на каждое сообщение
    группы.

    Все подписи и расчёты берутся у панели бота — вторая копия названий
    исходов и триггеров разъехалась бы с первой.
    """
    import time as _time

    from database.history import proactive_by_chat, proactive_by_day, proactive_stats
    from handlers.admin.panel_prompts import (_OUTCOME_TITLES, _TRIGGER_TITLES,
                                              _share, _utc_since)
    from handlers.admin.panel_users import _chat_title
    from services.proactive import skip_counts

    day = proactive_stats(_utc_since(24))
    week = proactive_stats(_utc_since(24 * 7))
    day_replies = day["reply"] + day["reply_mute"]
    week_replies = week["reply"] + week["reply_mute"]

    think = ("секунд на ответ модели, в среднем за сутки"
             if day.get("avg_sec") else "за сутки модель ещё не отвечала")
    tiles = (
        '<div class="grid">'
        f'<div class="card"><div class="k">Проверок за сутки</div>'
        f'<div class="v">{esc(day["checks"])}</div>'
        f'<div class="sub">вступил {esc(day_replies)} '
        f'({esc(_share(day_replies, day["checks"]))})</div></div>'
        f'<div class="card"><div class="k">Проверок за неделю</div>'
        f'<div class="v">{esc(week["checks"])}</div>'
        f'<div class="sub">вступил {esc(week_replies)} '
        f'({esc(_share(week_replies, week["checks"]))})</div></div>'
        f'<div class="card"><div class="k">Раздумье</div>'
        f'<div class="v">{esc(f"{day['avg_sec']:.1f}" if day.get("avg_sec") else "—")}</div>'
        f'<div class="sub">{esc(think)}</div></div>'
        '</div>'
    )

    # ── чем кончались проверки за неделю ──
    outcomes = [(title, f'{n} · {_share(n, week["checks"])}', "")
                for code, title in _OUTCOME_TITLES.items()
                for n in (week.get(code, 0),) if n]
    outcomes_html = ("<h3>Чем кончались проверки за неделю</h3>" + _rows(outcomes)
                     if outcomes else
                     '<div class="rows"><div class="empty">проверок за неделю '
                     'не было</div></div>')

    # ── по дням и по чатам ──
    extra = ""
    try:
        by_day = proactive_by_day(7)
    except Exception:
        by_day = []
    if by_day:
        rows = "".join(
            f'<div class="row"><div class="name">{esc(label)}</div>'
            f'<div class="ctlbox"><span class="note">{esc(checks)} → '
            f'<b>{esc(replies)}</b> ({esc(_share(replies, checks))})</span>'
            f'</div></div>' for label, checks, replies in by_day)
        extra += f'<h3>По дням</h3><div class="rows">{rows}</div>'

    try:
        by_chat = proactive_by_chat(_utc_since(24 * 7), limit=5)
    except Exception:
        by_chat = []
    if by_chat:
        rows = "".join(
            f'<div class="row"><div class="name">{esc(_chat_title(chat_id))}</div>'
            f'<div class="ctlbox"><span class="note">{esc(checks)} → '
            f'<b>{esc(replies)}</b></span></div></div>'
            for chat_id, checks, replies in by_chat)
        extra += f'<h3>По чатам за неделю</h3><div class="rows">{rows}</div>'

    if week.get("by_trigger"):
        kinds = " · ".join(
            f'{_TRIGGER_TITLES.get(k, k)} {n}'
            for k, n in sorted(week["by_trigger"].items(), key=lambda kv: -kv[1]))
        extra += ('<h3>Триггеры за неделю</h3><div class="rows"><div class="row">'
                  f'<div class="name">{esc(kinds)}<div class="note">медиа дороже: '
                  'вложение сначала разбирает отдельная модель</div></div>'
                  '</div></div>')

    # ── отсев: считается В ПАМЯТИ ──
    skipped, since = skip_counts()
    total = sum(skipped.values())
    minutes = max(0, (_time.time() - since) / 60)
    uptime = f"{minutes / 60:.0f} ч" if minutes >= 60 else f"{minutes:.0f} мин"
    if total:
        rows = "".join(
            f'<div class="row"><div class="name">{esc(reason)}</div>'
            f'<div class="ctlbox"><span class="note">{esc(n)}</span></div></div>'
            for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]))
    else:
        rows = '<div class="empty">пока ничего не отсеивалось</div>'
    skip_html = (f'<h3>Отсеяно до модели: {esc(total)}</h3>'
                 f'<div class="note">с последнего запуска ({esc(uptime)}). '
                 f'Эти числа живут в памяти и обнуляются перезапуском — на '
                 f'каждое сообщение группы бот в базу не ходит.</div>'
                 f'<div class="rows">{rows}</div>')

    hint = ('<div class="card wide"><div class="note">Вступает часто при малом '
            'числе проверок — правь промпт участия. Проверок много — правь '
            'порог на сводке.</div></div>')

    return ("<h2>📊 Участие в разговоре</h2>" + tiles + outcomes_html + extra
            + skip_html + hint)


def page_prompts(csrf: str = "", confirm: str = "", saved: str = "",
                 viewer_id: int = 0) -> str:
    """
    Страница промптов.

    confirm — ключ промпта, для которого показать вопрос «точно стереть?»
    (человек отправил пустое поле поверх непустого текста).
    saved   — ключ промпта, который только что сохранён (подсветить карточку).
    viewer_id — кто вошёл: от него зависит ЛИЧНЫЙ тумблер промпта (этап 7).
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

    body = ("<div class=\"wrap\">" + head + _topbar(csrf, "/prompts")
            + intro + "".join(cards)
            + _personal_prompt_block(csrf, viewer_id)
            + _proactive_block()
            + foot + "</div>")
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

    body = ("<div class=\"wrap\">" + head + _topbar(csrf, "/") + tiles
            + _models_block(csrf)
            + _thinking_block(csrf)
            + _spec_blocks(csrf)
            + "<h2>Вызовы по моделям</h2>"
            + _chart(stats.get("api_calls_by_model", []))
            + foot + "</div>" + _CONTROLS_JS)
    return _shell("Админка C4_Max", body)
