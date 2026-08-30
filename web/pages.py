# ───────────────────────────────────────────────
#  web/pages.py — сборка страниц веб-админки (30.08.2026, этап 0).
#
#  Здесь ТОЛЬКО показ: страницы ничего не меняют. Правку настроек добавит
#  этап 1, и она пойдёт через те же функции, что и кнопки панелей, — писать
#  в базу отсюда напрямую нельзя (см. config.py, блок «ВЕБ-АДМИНКА»).
#
#  ⚠️ Ни одного адреса со стороны: ни шрифтов, ни картинок, ни библиотек.
#  Дело не в скорости — каждый чужой адрес на странице сообщал бы чужому
#  серверу, когда владелец открывает свою админку. График рисуется прямым
#  SVG, а не библиотекой, по той же причине.
#
#  ⚠️ Любое значение, пришедшее не из наших констант, проходит через esc().
#  Названия моделей и версия — наши, но правило держим общим: место, где
#  однажды подставят чужой текст, находится само.
# ───────────────────────────────────────────────

import html
import logging

from config import (AVAILABLE_IMAGE_MODELS, AVAILABLE_MODELS, BOT_VERSION,
                    BOT_VERSION_URL, GEMINI_MODEL, PROVIDERS, THINKING_LEVELS,
                    THINKING_PHASES, AUTO_UPDATE_ENABLED_DEFAULT)

logger = logging.getLogger(__name__)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def _shell(title: str, body: str, refresh: int = 0) -> str:
    """Общая обёртка страницы. refresh > 0 — сама перечитывается раз в N секунд."""
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        "<!doctype html><html lang=\"ru\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex, nofollow\">"
        f"{meta}"
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
  var h = location.hash || "";
  var m = h.match(/tgWebAppData=([^&]*)/);
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


# ─── сводка ─────────────────────────────────────────────────────────

def _state(on: bool, yes: str = "включено", no: str = "выключено") -> str:
    cls = "on" if on else "off"
    return f'<span class="state {cls}">{esc(yes if on else no)}</span>'


def _num(value) -> str:
    return f'<span class="state num">{esc(value)}</span>'


def _rows_html(items) -> str:
    """items — список (название, значение-html, подпись-или-пусто)."""
    if not items:
        return '<div class="rows"><div class="empty">пусто</div></div>'
    out = []
    for name, value, note in items:
        sub = f'<div class="note">{esc(note)}</div>' if note else ""
        out.append(f'<div class="row"><div class="name">{esc(name)}{sub}</div>{value}</div>')
    return '<div class="rows">' + "".join(out) + "</div>"


def _thinking_rows() -> list:
    """
    Глубина раздумий по провайдерам — ровно то же, что показывают кнопки
    панели «📡 Настройки API»: значок провайдера, фаза луны, название ступени.
    Значение спрашиваем у services.gemini.thinking_level, а не читаем настройку
    сами: там же живёт откат на начальное положение при мусоре в базе.
    """
    from services.gemini import thinking_level

    rows = []
    for provider, levels in THINKING_LEVELS.items():
        meta = PROVIDERS.get(provider, {})
        codes = [code for code, _ in levels]
        cur = thinking_level(provider)
        pos = codes.index(cur) if cur in codes else 0
        phases = THINKING_PHASES.get(len(codes))
        phase = phases[pos] if phases else ("🌑" if pos == 0 else "🌕")
        title = f'{meta.get("icon", "")} {meta.get("title", provider)}'.strip()
        rows.append((title, _num(f'{phase} {dict(levels)[codes[pos]]}'), ""))
    return rows


def _chart(pairs: list, limit: int = 8) -> str:
    """
    Столбики «вызовов по моделям». Рисуем прямым SVG: библиотеке графиков
    здесь нечего делать, а тянуть её со стороны нельзя (см. шапку файла).
    Ширина в процентах — картинка тянется под ширину окна сама.
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


def page_summary() -> str:
    """
    Главная страница этапа 0: «бот жив и вот его сегодняшнее состояние».
    Ничего не меняет — только показывает.
    """
    import database.history as hist
    from services import daily_report

    stats = hist.get_bot_stats()
    active = stats.get("active_model") or GEMINI_MODEL
    active_title = AVAILABLE_MODELS.get(active, {}).get("name", active)
    active_provider = PROVIDERS.get(
        AVAILABLE_MODELS.get(active, {}).get("provider", ""), {})

    img = hist.get_setting("active_image_model", "")
    img_title = AVAILABLE_IMAGE_MODELS.get(img, {}).get("name", img or "—")

    # Расход с последнего снимка (обычно с полуночи). Считается на лету и
    # снимок не трогает — та же функция, что у кнопки в панели статистики.
    calls_today, money_today, _ = daily_report._open_day_totals()

    version = (f'<a href="{esc(BOT_VERSION_URL)}" target="_blank" '
               f'rel="noopener noreferrer">v{esc(BOT_VERSION)}</a>')

    head = (
        "<header>"
        "<h1>Админка C4_Max</h1>"
        f"<div class=\"ver\">{version}</div>"
        "<div class=\"pill\"><span class=\"dot\"></span>работает</div>"
        "</header>"
    )

    tiles = (
        "<div class=\"grid\">"
        f"<div class=\"card\"><div class=\"k\">Активная модель</div>"
        f"<div class=\"v\">{esc(active_provider.get('icon', ''))} {esc(active_title)}</div>"
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
    )

    thinking = "<h2>Глубина раздумий</h2>" + _rows_html(_thinking_rows())

    switches = "<h2>Тумблеры</h2>" + _rows_html([
        ("Ответы ИИ", _state(hist.get_setting("ai_replies_enabled", "1") == "1"),
         "отвечает ли бот на сообщения вообще"),
        ("Показывать мысли модели", _state(hist.get_setting("toggle_thoughts", "0") == "1"),
         "цитата рассуждений под ответом"),
        ("Антиспам", _state(hist.get_setting("antispam_enabled", "1") == "1"),
         "мут за флуд в группах"),
        ("Фильтр ссылок", _state(hist.get_setting("linkfilter_enabled", "0") == "1"), ""),
        ("Приветствие новичков", _state(hist.get_setting("greet_enabled", "0") == "1"), ""),
        ("База знаний (RAG)", _state(hist.get_setting("rag_enabled", "0") == "1"),
         "подмешивать статьи в ответ"),
        ("Сам в разговор", _state(hist.get_setting("proactive_enabled", "0") == "1"), ""),
        ("Самообновление", _state(
            hist.get_setting("auto_update_enabled", AUTO_UPDATE_ENABLED_DEFAULT) == "1"),
         "забирать новый код с GitHub раз в 5 минут"),
    ])

    chart = "<h2>Вызовы по моделям</h2>" + _chart(stats.get("api_calls_by_model", []))

    foot = ('<footer><span>Страница только показывает — менять настройки '
            'пока можно кнопками в боте.</span>'
            '<span><a href="/exit">выйти</a></span></footer>')

    body = "<div class=\"wrap\">" + head + tiles + thinking + switches + chart + foot + "</div>"
    # 60 секунд: цифры живые, но чаще дёргать базу ради взгляда на экран незачем.
    return _shell("Админка C4_Max", body, refresh=60)
