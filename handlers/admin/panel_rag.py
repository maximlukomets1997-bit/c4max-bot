# ───────────────────────────────────────────────
#  handlers/admin/panel_rag.py — база знаний: панель /rag, карточки статей, приём файлов, режим «🔍 Проверить поиск».
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import logging
import os

import logging_setup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from config import AVAILABLE_MODELS, AVAILABLE_IMAGE_MODELS, ADMIN_IDS, GEMINI_MODEL
from database.history import set_setting, get_setting, append_prompt_addition, get_active_system_prompt, get_bot_stats, get_news_system_prompt, get_rag_instruction
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete


logger = logging.getLogger(__name__)
from .common import _adm_back_row, _fmt_mod_time, _is_group_chat, _onoff, _reject_non_admin, _require




# ─────────────────────────────────────────────
#  Панель базы знаний RAG (/rag и кнопка в /adm)
#
#  Список статей: 🕐 — ждут одобрения (папка pending), ✅ — в базе (approved).
#  Имена файлов не влезают в callback_data (лимит Telegram 64 байта), поэтому
#  в кнопки кладутся короткие номера-токены, а соответствие токен → (папка,
#  файл) живёт в bot_data["kb_file_map"] и пересобирается при каждом
#  открытии панели. После перезапуска бота токены из старых сообщений
#  протухают — обработчик тогда просто заново открывает панель.
#
#  ТРИ ЭКРАНА (2026-08-12, просьба Максима). Раньше панель была одна: статусы,
#  следом ВСЕ статьи-кнопки (84 штуки), а под ними настройки поиска — около
#  сотни кнопок при потолке Telegram в ~100 и длинная прокрутка ради двух
#  регуляторов. Теперь:
#    • разделы   — статусы, журнал и кнопки разделов со счётчиками (13 кнопок);
#    • список    — статьи одного раздела и возврат «⬅️ К разделам»;
#    • настройки — три регулятора поиска с пояснениями и возврат.
#  Какой экран открыт, помнит user_data["kb_screen"]: "" — разделы,
#  "settings" — настройки, иначе ключ раздела ("pending" или тип из
#  ARTICLE_KINDS). У каждого владельца своя память, как и у номера страницы.
#
#  Раскладка повторяет справочник техники /ttx (handlers/tech.py): те же два
#  столбца, тот же вид заголовка и листания, та же обрезка названий
#  (tech_card.short_title). Двух разных манер для одних и тех же статей быть
#  не должно — Максим смотрит оба экрана.
#
#  Длинный раздел ЛИСТАЕТСЯ страницами по _KB_PAGE_SIZE. Карта токенов при этом
#  собирается по ВСЕМУ списку статей, а не по видимому экрану: иначе кнопка из
#  предыдущего, ещё не затёртого сообщения показала бы чужую статью или
#  сказала бы «список устарел». Номер текущей страницы живёт в
#  user_data["kb_page"] — у каждого владельца свой.
# ─────────────────────────────────────────────

_KB_PAGE_SIZE = 30  # статей-кнопок НА ОДНОЙ СТРАНИЦЕ раздела
#   Ограничение теперь по ЭКРАНУ ТЕЛЕФОНА, а не по потолку Telegram: 30 статей
#   в два столбца — 15 рядов, дальше начинается бесконечная прокрутка. В лимит
#   же экран списка укладывается с огромным запасом (30 + 4 служебных против
#   ~100), и это правильно: в потолок упиралась именно старая единая панель.
#   Самый большой раздел сегодня — наземная техника, ровно 30 статей.

# Ключи экранов панели (user_data["kb_screen"] и callback kb_open:<ключ>).
# Пересечься с типами техники из ARTICLE_KINDS не могут — там ground/air/
# ship/sub/guide/other.
_KB_MAIN = "main"          # экран разделов (в user_data хранится пустой строкой)
_KB_SETTINGS = "settings"  # экран настроек поиска
_KB_PENDING = "pending"    # раздел «ждут одобрения» (он же имя папки)


_KB_ACTION_ICONS = (
    ("одобрена", "✅"), ("добавлена", "➕"), ("заменена", "📝"),
    ("удалена", "🗑"), ("пересборка", "🔄"),
)


def _kb_recent_actions_block(limit: int = 5) -> str:
    """Блок «Последние действия» для панели /rag (из журнала knowledge_log)."""
    from database.history import get_recent_kb_actions
    recent = get_recent_kb_actions(limit)
    if not recent:
        return ""
    lines = []
    for a in recent:
        icon = next((ic for prefix, ic in _KB_ACTION_ICONS if a["action"].startswith(prefix)), "•")
        # Заголовок статьи приходит с сайта/из файла — экранируем, иначе «<»
        # в заголовке ломает HTML-разметку панели /rag.
        article = html.escape((a.get("article") or "")[:35])
        lines.append(f"  {icon} {_fmt_mod_time(a['ts'])} {a['action']}: {article}")
    return "\n\n<b>Последние действия:</b>\n" + "\n".join(lines)


async def _end_kb_test(bot, chat_id: int, context) -> None:
    """
    Завершает режим «🔍 Проверить поиск»: гасит флаг и удаляет из чата все
    накопленные сообщения проверки — и вопросы админа, и диагностические
    ответы бота, чтобы после теста не оставалось мусора. Вызывается на КАЖДОМ
    выходе из режима (кнопка «Завершить», переход в другой раздел, повторный
    ввод команды-панели). Работает только в личке админа, где бот вправе
    удалять входящие сообщения. Ошибки удаления глушатся: сообщение могли уже
    убрать вручную или прошло 48 ч (лимит Telegram на удаление). Если режим не
    был включён — тихо выходит, ничего не удаляя.
    """
    was_on = context.user_data.pop("kb_test_mode", None)
    msg_ids = context.user_data.pop("kb_test_msgs", None) or []
    if not was_on and not msg_ids:
        return
    for mid in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


def _kb_test_row(context):
    """
    Ряд с кнопкой «⏹️ Завершить проверку» — ТОЛЬКО пока режим проверки поиска
    включён, и на КАЖДОМ экране панели.

    Сама проверка включается на экране настроек, рядом с регуляторами. Но выйти
    из неё нужно уметь отовсюду: ушёл смотреть статьи, а режим включён — и все
    сообщения по-прежнему уходят в диагностику, а не в ИИ. Поэтому на прочих
    экранах панели показывается эта кнопка.

    ⚠️ На самом экране настроек ряд НЕ нужен: там кнопка проверки своя, с
    переключающейся меткой, и две кнопки завершения висели бы рядом.
    """
    if not context.user_data.get("kb_test_mode"):
        return []
    return [[InlineKeyboardButton("⏹️ Завершить проверку", callback_data="kb_test")]]


def _build_rag_panel(context, admin_id: int):
    """
    Собирает ТЕКУЩИЙ экран панели базы знаний: (текст, клавиатура). Какой
    именно — помнит user_data["kb_screen"] (см. шапку модуля). Используется и
    при отправке панели, и при любой перерисовке после кнопок, поэтому карта
    токенов обновляется здесь — одна на все экраны.

    admin_id остаётся в подписи для совместимости с вызовами: персональных
    настроек в панели больше нет — тумблер базы знаний стал общим (2026-07-27).
    """
    from services.knowledge_store import list_articles

    articles = list_articles()
    # Карта токенов — по ВСЕМУ списку статей, а не по видимому экрану:
    # кнопка из старого сообщения обязана вести к своей статье, а не к соседке.
    context.application.bot_data["kb_file_map"] = {
        str(i): (art["folder"], art["fname"]) for i, art in enumerate(articles)
    }

    screen = str(context.user_data.get("kb_screen") or "")
    if screen == _KB_SETTINGS:
        return _build_kb_settings(context)
    if screen:
        built = _build_kb_list(context, articles, screen)
        if built is not None:
            return built
        # Раздел опустел, пока экран висел в чате (одобрили последнюю новость,
        # удалили последнюю статью) — вместо пустого списка возвращаем к
        # разделам, а не показываем экран, на котором нечего нажать.
        context.user_data["kb_screen"] = ""
    return _build_kb_main(context, articles)


def _build_kb_main(context, articles):
    """
    Экран разделов: статусы, журнал последних действий и кнопки разделов со
    счётчиками. Пустой раздел кнопки не получает — за ней ничего не стояло бы
    (то же правило, что в каталоге /ttx). Поэтому раздел «📄 Без типа» обычно
    не виден вовсе, а появившись, сам работает сигналом: в первой строке
    какой-то статьи забыли указать тип техники.
    """
    from config import RAG_ENABLED
    from services.knowledge_store import ARTICLE_KINDS

    pending_count = sum(1 for a in articles if a["folder"] == "pending")
    approved = [a for a in articles if a["folder"] == "approved"]

    counts = {}
    for art in approved:
        counts[art.get("kind")] = counts.get(art.get("kind"), 0) + 1

    def kind_btn(kind: str):
        """Кнопка раздела — или None, если статей этого типа нет."""
        n = counts.get(kind)
        if not n:
            return None
        meta = ARTICLE_KINDS.get(kind, ARTICLE_KINDS["other"])
        return InlineKeyboardButton(f"{meta['icon']} {meta['name'].capitalize()} ({n})",
                                    callback_data=f"kb_open:{kind}")

    pending_btn = (InlineKeyboardButton(f"🕐 Ждут одобрения ({pending_count})",
                                        callback_data=f"kb_open:{_KB_PENDING}")
                   if pending_count else None)

    # Раскладка расписана Максимом поимённо (2026-08-12) и порядку ARTICLE_KINDS
    # намеренно НЕ следует: слева очередь и мелкие разделы, справа крупные.
    # Сам порядок в ARTICLE_KINDS этим не отменяется — по нему по-прежнему
    # сортируются статьи внутри списков и строится каталог /ttx (он остался
    # прежним, решение Максима).
    grid = (
        (pending_btn,        kind_btn("ground")),
        (kind_btn("sub"),    kind_btn("ship")),
        (kind_btn("guide"),  kind_btn("air")),
    )
    # ⚠️ Пустая ячейка выпадает, а РЯД ОСТАЁТСЯ (выбор Максима из двух
    # предложенных): соседняя кнопка просто растягивается на всю ширину. В этом
    # и смысл жёсткой раскладки — подлодки всегда под очередью, корабли под
    # наземной, и рука привыкает. Плотное схлопывание переставляло бы разделы
    # каждый раз, как разберёшь очередь.
    rows = [[b for b in pair if b] for pair in grid if any(pair)]
    # «Без типа» в раскладку не входит: он почти всегда пуст, а появляется
    # только когда в первой строке статьи забыли указать тип техники — то есть
    # работает сигналом. Отдельным рядом снизу: и заметен, и сетку не двигает.
    other_btn = kind_btn("other")
    if other_btn:
        rows.append([other_btn])

    # Работа со статьями — одним рядом (2026-08-12, просьба Максима: тумблер
    # базы и проверка поиска переехали внутрь экрана настроек, а на главном
    # остаётся только то, что делают со статьями). Настройки посередине —
    # между «добавить» и «пересобрать», как он и просил.
    rows.append([
        InlineKeyboardButton("➕ Добавить RAG", callback_data="kb_add"),
        InlineKeyboardButton("⚙️ Настройки", callback_data=f"kb_open:{_KB_SETTINGS}"),
        InlineKeyboardButton("🔄 Пересобрать RAG", callback_data="kb_rebuild"),
    ])
    # ⚠️ Тумблер базы знаний с экрана ушёл, а СТРОКА О ЕГО СОСТОЯНИИ в тексте
    # осталась намеренно: выключенная база — редкое, но важное состояние, и
    # узнавать о нём, только зайдя в настройки, было бы поздно.
    kb_on = get_setting("rag_enabled", "1") == "1"
    rows += _kb_test_row(context)
    rows.append([InlineKeyboardButton("🧹 Очистить журнал", callback_data="kb_clearlog")] + _adm_back_row())

    rag_status = "🟢 включён" if RAG_ENABLED else "🔴 выключен (RAG_ENABLED в .env)"
    # ⚠️ Приписка ведёт К ТУМБЛЕРУ, а он с этого экрана уехал в «⚙️ Настройки»
    # (2026-08-12). Было «(тумблер ниже)» — на прежнем экране это была правда.
    kb_status = "🟢 ВКЛЮЧЕНА" if kb_on else "🔴 ВЫКЛЮЧЕНА (включить — в ⚙️ Настройках)"
    text = (
        "📚 <b>База знаний (RAG)</b>\n"
        "───────────────────────────\n"
        f"🌐 Модуль RAG: <b>{rag_status}</b>\n"
        f"📖 База знаний для всех: <b>{kb_status}</b>\n"
        f"🕐 Ждут одобрения: <b>{pending_count}</b>\n"
        f"✅ В базе знаний: <b>{len(approved)}</b>"
        + _kb_recent_actions_block()
        + "\n\nВыбери раздел — статьи внутри."
    )
    return text, InlineKeyboardMarkup(rows)


def _build_kb_list(context, articles, screen: str):
    """
    Экран одного раздела: (текст, клавиатура) — или None, если раздела нет
    либо он пуст (сборщик выше на этом вернёт к разделам).

    Номера-токены берутся из ОБЩЕГО списка статей, а не из порядкового номера
    внутри раздела: карта токенов одна на всю панель, и кнопка обязана вести к
    своей статье с любого экрана.

    Значок типа на кнопках не ставится — он уже в заголовке экрана, а место
    отдано названию: на общем списке оно резалось до 16 знаков и разные статьи
    выглядели одинаково.
    """
    from services.knowledge_store import ARTICLE_KINDS
    from services.tech_card import short_title

    if screen == _KB_PENDING:
        picked = [(i, a) for i, a in enumerate(articles) if a["folder"] == "pending"]
        icon, name = "🕐", "ждут одобрения"
        hint = "Нажми на новость — открою её карточку с кнопкой «Одобрить»."
    else:
        meta = ARTICLE_KINDS.get(screen)
        if meta is None:
            return None
        picked = [(i, a) for i, a in enumerate(articles)
                  if a["folder"] == "approved" and a.get("kind") == screen]
        icon, name = meta["icon"], meta["name"]
        hint = "Нажми на статью — открою её карточку."
    if not picked:
        return None

    # Номер страницы живёт в user_data, но проверяется здесь: статьи могли
    # удалить, и запомненная вторая страница стала бы пустым экраном.
    total_pages = max(1, (len(picked) + _KB_PAGE_SIZE - 1) // _KB_PAGE_SIZE)
    try:
        page = int(context.user_data.get("kb_page", 0))
    except (TypeError, ValueError):
        page = 0
    page = max(0, min(total_pages - 1, page))
    context.user_data["kb_page"] = page
    first = page * _KB_PAGE_SIZE
    chunk = picked[first:first + _KB_PAGE_SIZE]

    # Очередь — В ОДИН СТОЛБЕЦ (2026-08-12, просьба Максима), разделы техники —
    # в два. Причина в длине названий: у сырых новостей это целые заголовки с
    # сайта («[Скоро в игре] La Fayette: Первый стелс-фрегат»), и в узкой
    # кнопке от них оставался огрызок. У техники названия короткие.
    per_row, cut = (1, 40) if screen == _KB_PENDING else (2, 22)
    buttons = [InlineKeyboardButton(short_title(art["title"], cut), callback_data=f"kb_view:{i}")
               for i, art in chunk]
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]

    # Листание. Ряд появляется только когда страниц больше одной — иначе
    # он был бы полосой с надписью «1 из 1», которая никуда не ведёт.
    # Крайние кнопки на границах списка становятся «пустышками» (kb_noop):
    # так ряд не «прыгает» по ширине при переходе со страницы на страницу.
    if total_pages > 1:
        prev_data = f"kb_page:{page - 1}" if page > 0 else "kb_noop"
        next_data = f"kb_page:{page + 1}" if page < total_pages - 1 else "kb_noop"
        rows.append([
            InlineKeyboardButton("⬅️" if page > 0 else "▫️", callback_data=prev_data),
            InlineKeyboardButton(f"📄 {page + 1} из {total_pages}", callback_data="kb_noop"),
            InlineKeyboardButton("➡️" if page < total_pages - 1 else "▫️", callback_data=next_data),
        ])
    rows += _kb_test_row(context)
    rows.append([InlineKeyboardButton("⬅️ К разделам", callback_data=f"kb_open:{_KB_MAIN}")])

    text = (
        f"{icon} <b>{name.upper()}</b>\n"
        "───────────────────────────\n"
        f"Статьи {first + 1}–{first + len(chunk)} из {len(picked)}"
        + (f" · страница {page + 1} из {total_pages}" if total_pages > 1 else "")
        + "\n───────────────────────────\n"
        f"<i>{hint}</i>"
    )
    return text, InlineKeyboardMarkup(rows)


def _build_kb_settings(context):
    """
    Экран настроек поиска: тумблер базы, три регулятора ➖/➕ (значение на
    средней кнопке, она «пустышка» — как в панели модерации) и проверка поиска.

    Собрано здесь всё, что влияет на выдачу базы знаний (2026-08-12, просьба
    Максима). Тумблер стоит ПЕРВЫМ: при выключенной базе регуляторы ни на что
    не влияют, и видеть это надо раньше, чем крутить проценты.

    Пояснения к регуляторам написаны прямо на экране: значения тут неочевидны
    даже по названию, а крутит их владелец редко и вспоминать смысл каждый раз
    заново — верный способ выкрутить не то.
    """
    from services.rag import _live_top_k, _live_min_similarity, _live_peak_margin

    thr_pct = int(round(_live_min_similarity() * 100))
    margin_pct = int(round(_live_peak_margin() * 100))
    # ОБЩИЙ тумблер базы знаний (2026-07-27, просьба Максима — был персональным,
    # только для лички админа). Выключен — база не подмешивается никому и нигде:
    # ни в личке, ни в группах, ни в режиме «Сам в разговор». Проверяется в
    # единственной точке — services/rag.py::retrieve_relevant_context.
    # Ключ хранится ПРЯМО ("1" = включена), переворачивать не надо.
    kb_on = get_setting("rag_enabled", "1") == "1"
    rows = [
        [InlineKeyboardButton(f"📖 База знаний: {_onoff(kb_on)}", callback_data="kb_myrag")],
        [
            InlineKeyboardButton("➖", callback_data="kb_thr_dec"),
            InlineKeyboardButton(f"🎯 Порог: {thr_pct}%", callback_data="kb_noop"),
            InlineKeyboardButton("➕", callback_data="kb_thr_inc"),
        ],
        [
            InlineKeyboardButton("➖", callback_data="kb_topk_dec"),
            InlineKeyboardButton(f"📦 Статей в ответ: {_live_top_k()}", callback_data="kb_noop"),
            InlineKeyboardButton("➕", callback_data="kb_topk_inc"),
        ],
        [
            InlineKeyboardButton("➖", callback_data="kb_margin_dec"),
            InlineKeyboardButton(f"📈 Запас над фоном: {margin_pct}%", callback_data="kb_noop"),
            InlineKeyboardButton("➕", callback_data="kb_margin_inc"),
        ],
    ]
    # Кнопка-переключатель: пока проверка идёт, показывает «Завершить» —
    # чтобы владелец всегда видел, что находится в режиме теста, и как выйти.
    # ⚠️ Здесь она СВОЯ, а _kb_test_row не зовём: иначе на этом экране висели
    # бы сразу две кнопки завершения.
    test_on = bool(context.user_data.get("kb_test_mode"))
    rows.append([InlineKeyboardButton(
        "⏹️ Завершить проверку" if test_on else "🔍 Проверить поиск",
        callback_data="kb_test")])
    rows.append([InlineKeyboardButton("⬅️ К разделам", callback_data=f"kb_open:{_KB_MAIN}")])

    text = (
        "⚙️ <b>НАСТРОЙКИ ПОИСКА</b>\n"
        "───────────────────────────\n"
        "📖 <b>База знаний</b> — общий выключатель: погашена, и статьи не "
        "подмешиваются никому и нигде, ни в личке, ни в группах.\n\n"
        "🎯 <b>Порог</b> — насколько статья должна быть похожа на вопрос, "
        "чтобы её вообще рассматривать. Вспомогательный ограничитель.\n\n"
        "📦 <b>Статей в ответ</b> — сколько найденных статей уходит модели "
        "вместе с вопросом.\n\n"
        "📈 <b>Запас над фоном</b> — насколько статья должна выделяться среди "
        "всех остальных. Это главный судья: у настоящего вопроса одна статья "
        "заметно ближе прочих, у болтовни все похожи одинаково.\n"
        "───────────────────────────\n"
        "<i>Покрутил — проверь кнопкой «🔍 Проверить поиск» ниже: пришли "
        "вопрос сообщением, и я покажу баллы, не тратя модель.</i>"
    )
    return text, InlineKeyboardMarkup(rows)


async def send_rag_panel(bot, chat_id: int, context):
    """Отправляет панель базы знаний: статус RAG, журнал и список статей-кнопок."""
    # Панель живёт только в личке админа, поэтому chat_id == id владельца
    text, markup = _build_rag_panel(context, chat_id)
    sent_msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
    )
    if sent_msg:
        await register_and_clean_bot_message(bot, chat_id, sent_msg.message_id)


def _kb_resolve(context, token: str):
    """Токен из callback → (folder, fname) или None, если карта протухла."""
    return context.application.bot_data.get("kb_file_map", {}).get(token)


def _kb_card_keyboard(token: str, folder: str, confirm_delete: bool = False):
    """Кнопки карточки статьи."""
    if confirm_delete:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("❗️ Да, удалить", callback_data=f"kb_delete_yes:{token}"),
                InlineKeyboardButton("Отмена", callback_data=f"kb_cancel_del:{token}"),
            ],
        ])
    rows = []
    if folder == "pending":
        rows.append([InlineKeyboardButton("✅ Одобрить в базу", callback_data=f"kb_approve:{token}")])
    rows.append([
        InlineKeyboardButton("📝 Заменить", callback_data=f"kb_replace:{token}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"kb_delete:{token}"),
    ])
    rows.append([InlineKeyboardButton("⬅️ К списку", callback_data="kb_panel")])
    return InlineKeyboardMarkup(rows)


async def _kb_sync_in_executor():
    """Пересборка индекса RAG (сетевые вызовы) — в рабочем потоке, не блокируя бота."""
    import asyncio
    from services.rag import sync_knowledge_base
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, sync_knowledge_base)


def _kb_rebuild_running(context) -> bool:
    """
    Идёт ли сейчас фоновая пересборка индекса (защёлка в bot_data).
    Пока она идёт, операции со статьями (одобрить/удалить/добавить) закрыты —
    иначе две синхронизации будут наперегонки писать один файл индекса.
    """
    return bool(context.application.bot_data.get("kb_rebuild_running"))


async def _kb_popup(query, context, chat_id: int, text: str):
    """
    Всплывающее уведомление панели RAG — как при смене модели (show_alert).
    Если callback уже протух (долгая операция, лимит Telegram ~15 сек) —
    запасной вариант: обычное сообщение, которое само удаляется.
    """
    try:
        await query.answer(text, show_alert=True)
    except Exception:
        try:
            msg = await context.bot.send_message(chat_id=chat_id, text=text)
            if msg:
                schedule_delete(context.bot, chat_id, msg.message_id, 30)
        except Exception as e:
            logger.warning("⚠️ Не удалось показать уведомление панели RAG: %s", e)


async def _handle_kb_callback(query, context, data: str, chat_id: int, user_id: int):
    """Обрабатывает все callback-кнопки панели базы знаний (префикс kb_)."""
    from services.knowledge_store import read_article, read_title, approve_article, delete_article

    action, _, token = data.partition(":")

    # Любое действие панели, кроме «Добавить», выключает режим приёма новых
    # файлов — чтобы случайный документ позже не улетел в базу.
    if action != "kb_add":
        context.user_data.pop("kb_add_mode", None)
    # Режим «Проверить поиск» переживает подстройку порога/фрагментов и
    # персональный тумблер (удобно крутить настройки между тестовыми
    # вопросами), остальные действия панели его выключают — с уборкой
    # накопленных тестовых сообщений.
    # Листание списка (kb_page) режим теста тоже переживает: со страницы на
    # страницу — то же «смотрю панель», а не выход из неё; удалять на этом
    # накопленные тестовые сообщения было бы неожиданно.
    # Переходы между экранами (kb_open) — по той же причине, и ещё по одной:
    # регуляторы поиска теперь живут на ОТДЕЛЬНОМ экране, а «спросил →
    # подкрутил порог → спросил снова» и есть основной способ их настроить.
    # Чтобы выход из режима не потерялся на другом экране, кнопка «Завершить
    # проверку» показывается на всех экранах панели (_kb_test_row).
    if action not in ("kb_test", "kb_noop", "kb_thr_dec", "kb_thr_inc",
                      "kb_topk_dec", "kb_topk_inc", "kb_myrag",
                      "kb_margin_dec", "kb_margin_inc", "kb_page", "kb_open"):
        await _end_kb_test(context.bot, chat_id, context)

    # ── Кнопки без токена (не привязаны к конкретному файлу) ────────────
    if action == "kb_noop":
        # «Пустышка» — кнопка-значение между ➖ и ➕
        await query.answer()
        return

    if action == "kb_page":
        # Листание списка статей. Номер страницы проверяет и подрезает сам
        # сборщик панели (_build_rag_panel) — статьи могли удалить, пока
        # сообщение висело в чате, и запрошенной страницы уже нет.
        try:
            context.user_data["kb_page"] = max(0, int(token))
        except (TypeError, ValueError):
            context.user_data["kb_page"] = 0
        await query.answer()
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось перелистнуть список статей: %s", e)
        return

    if action == "kb_open":
        # Переход между экранами панели: разделы ↔ список раздела ↔ настройки.
        # Экран разделов хранится ПУСТОЙ строкой, а не словом "main": пустое
        # значение — то же, что «ключа ещё нет», и панель у нового владельца
        # открывается с разделов без всякой отдельной подготовки.
        context.user_data["kb_screen"] = "" if token == _KB_MAIN else token
        # Страницу всегда начинаем с первой: номер один на все разделы, и
        # вторая страница танков в разделе из трёх подлодок была бы пустой.
        context.user_data["kb_page"] = 0
        await query.answer()
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось открыть экран панели RAG: %s", e)
        return

    if action == "kb_panel":
        # Возврат к панели из карточки статьи и отмена в подтверждении очистки
        # журнала. Экран открывается ТОТ ЖЕ, с которого ушли (kb_screen не
        # трогаем): открыл статью из раздела «Корабли» — вернулся в «Корабли».
        # ⚠️ Ветка нужна явная: без неё kb_panel доходил до разбора токена,
        # не находил пустой токен в карте и всплывал ложным «Список устарел».
        await query.answer()
        await send_rag_panel(context.bot, chat_id, context)
        return

    if action == "kb_test":
        if context.user_data.get("kb_test_mode"):
            # Повторное нажатие — завершаем проверку: гасим режим и убираем
            # из чата все накопленные вопросы/ответы теста.
            await _end_kb_test(context.bot, chat_id, context)
            await query.answer("✅ Проверка завершена — тестовые сообщения убраны.")
        else:
            # Следующие текстовые сообщения этого админа в личке уходят в
            # диагностику поиска (handlers/messages.py), а не в ИИ.
            context.user_data["kb_test_mode"] = True
            context.user_data["kb_test_msgs"] = []
            context.user_data.pop("kb_replace_target", None)
            # ВАЖНО: лимит всплывающего окна Telegram — 200 символов, текст
            # длиннее молча не показывается (ошибка Message_too_long)
            await query.answer(
                "🔍 Пришли вопрос сообщением — покажу, какие разделы базы найдутся.\n\n"
                "Можно несколько подряд; для выхода снова нажми «Завершить проверку».",
                show_alert=True
            )
        # Перерисовываем панель — кнопка теста меняет метку (Проверить ↔ Завершить)
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось обновить панель RAG: %s", e)
        return

    if action in ("kb_thr_dec", "kb_thr_inc", "kb_topk_dec", "kb_topk_inc",
                  "kb_margin_dec", "kb_margin_inc"):
        from config import RAG_TOP_K, RAG_MIN_SIMILARITY, RAG_PEAK_MARGIN
        if action.startswith("kb_margin"):
            # Запас «пик над полкой»: шаг 0.01 — рабочий диапазон узкий
            # (дефолт 0.14, выверен замером 2026-07-19 — см. config.py).
            # 0 = правило выключено, проходит всё, что выше порога сходства.
            try:
                cur = float(get_setting("rag_peak_margin", str(RAG_PEAK_MARGIN)))
            except (TypeError, ValueError):
                cur = RAG_PEAK_MARGIN
            new_val = round(cur + (0.01 if action.endswith("inc") else -0.01), 2)
            new_val = max(0.0, min(0.30, new_val))
            set_setting("rag_peak_margin", f"{new_val:.2f}")
            logger.info("📚 /rag: запас «пик над полкой» = %.2f", new_val)
        elif action.startswith("kb_thr"):
            try:
                cur = float(get_setting("rag_min_similarity", str(RAG_MIN_SIMILARITY)))
            except (TypeError, ValueError):
                cur = RAG_MIN_SIMILARITY
            # Шаг 0.02: зазор между болтовнёй и реальными вопросами узкий
            # (~0.59 против ~0.60 по замерам 2026-07-05), 0.05 слишком грубо
            new_val = round(cur + (0.02 if action.endswith("inc") else -0.02), 2)
            new_val = max(0.05, min(0.95, new_val))
            set_setting("rag_min_similarity", f"{new_val:.2f}")
            logger.info("📚 /rag: порог сходства = %.2f", new_val)
        else:
            try:
                cur = int(get_setting("rag_top_k", str(RAG_TOP_K)))
            except (TypeError, ValueError):
                cur = RAG_TOP_K
            new_val = max(1, min(10, cur + (1 if action.endswith("inc") else -1)))
            set_setting("rag_top_k", str(new_val))
            logger.info("📚 /rag: фрагментов в контекст = %d", new_val)
        await query.answer()
        # Перерисовываем панель с новыми значениями на кнопках
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось обновить панель RAG: %s", e)
        return

    if action == "kb_myrag":
        # ОБЩИЙ тумблер базы знаний (2026-07-27): выключен — статьи не
        # подмешиваются НИКОМУ и НИГДЕ (личка, группы, «Сам в разговор»).
        # Проверяется в services/rag.py::retrieve_relevant_context — в одной
        # точке на всех путях сразу.
        # На кнопку «🔍 Проверить поиск» не влияет — диагностика работает всегда.
        cur_on = get_setting("rag_enabled", "1") == "1"
        new_val = "0" if cur_on else "1"
        set_setting("rag_enabled", new_val)
        state = "включена" if new_val == "1" else "выключена"
        logger.info("📚 /rag: база знаний %s для ВСЕХ (переключил админ %s)", state, user_id)
        await query.answer(f"📖 База знаний {state} для всех", show_alert=False)
        text, markup = _build_rag_panel(context, user_id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("📚 Не удалось обновить панель RAG: %s", e)
        return

    if action == "kb_add":
        # Следующий документ .md/.txt от этого админа станет новой статьёй
        # базы знаний (handle_kb_document). Режим живёт до другого действия.
        context.user_data["kb_add_mode"] = True
        context.user_data.pop("kb_replace_target", None)
        await query.answer(
            "➕ Пришли файл .md или .txt со статьёй — я добавлю её в базу знаний "
            "и сразу посчитаю вектора. Можно прислать несколько файлов подряд.",
            show_alert=True
        )
        return

    if action == "kb_rebuild":
        if _kb_rebuild_running(context):
            await _kb_popup(query, context, chat_id, "⏳ Пересборка уже идёт — дождитесь итога.")
            return
        logger.info("🔧 Админ %s запустил полную пересборку базы знаний", user_id)
        import asyncio
        from services.rag import rebuild_knowledge_base
        from database.history import add_kb_action
        bot = context.bot

        async def _rebuild_and_report():
            # Из-за пауз при лимите Google пересборка может идти несколько
            # минут — поэтому отдельной задачей: очередь апдейтов свободна,
            # бот всё это время отвечает как обычно. Повторный запуск и
            # операции со статьями на это время закрывает защёлка.
            result = None
            try:
                result = await asyncio.get_running_loop().run_in_executor(None, rebuild_knowledge_base)
            except Exception as e:
                logger.error("⚠️ Пересборка базы знаний упала: %s", e)
            finally:
                context.application.bot_data["kb_rebuild_running"] = False
            if result is None or result[1] == 0:
                text = "⚠️ Не удалось пересобрать RAG — база пуста или произошла ошибка. Детали в логе."
            else:
                indexed, total = result
                add_kb_action("пересборка базы", f"проиндексировано {indexed} из {total}", user_id)
                if indexed >= total:
                    text = f"✅ RAG пересобрана заново!\n\nПроиндексировано: {indexed} из {total}"
                else:
                    text = (f"⚠️ Пересборка упёрлась в лимит Google: проиндексировано {indexed} из {total}.\n"
                            f"Недостающее бот доберёт при следующем запуске, "
                            f"либо нажмите пересборку ещё раз чуть позже.")
            try:
                msg = await bot.send_message(chat_id=chat_id, text=text)
                if msg:
                    schedule_delete(bot, chat_id, msg.message_id, 60)
            except Exception as e:
                logger.warning("⚠️ Не удалось отправить итог пересборки: %s", e)
            await send_rag_panel(bot, chat_id, context)

        context.application.bot_data["kb_rebuild_running"] = True
        context.application.create_task(_rebuild_and_report())
        await _kb_popup(query, context, chat_id,
                        "⏳ Пересборка запущена в фоне — итог придёт отдельным сообщением. "
                        "Бот продолжает работать как обычно.")
        return

    if action == "kb_clearlog":
        # Подтверждение прямо в панели: подменяем клавиатуру на «да/отмена»
        await query.answer()
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, очистить журнал", callback_data="kb_clearlog_yes"),
                InlineKeyboardButton("Отмена", callback_data="kb_panel"),
            ]]))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение очистки журнала: %s", e)
        return

    if action == "kb_clearlog_yes":
        from database.history import clear_kb_log
        deleted = clear_kb_log()
        logger.info("🔧 Админ %s очистил журнал базы знаний (%d записей)", user_id, deleted)
        await _kb_popup(query, context, chat_id, f"🧹 Журнал действий очищен (удалено записей: {deleted}).")
        await send_rag_panel(context.bot, chat_id, context)
        return

    resolved = _kb_resolve(context, token)
    if resolved is None:
        # Карта токенов протухла (перезапуск бота) — открываем список заново
        await query.answer("Список устарел — открываю заново.", show_alert=False)
        await send_rag_panel(context.bot, chat_id, context)
        return
    folder, fname = resolved

    if action == "kb_view":
        try:
            path, content = read_article(folder, fname)
        except FileNotFoundError:
            await query.answer("Файл уже удалён.", show_alert=True)
            await send_rag_panel(context.bot, chat_id, context)
            return
        await query.answer()
        status = "🕐 Ждёт одобрения" if folder == "pending" else "✅ В базе знаний"
        caption = f"{status}\n<b>{read_title(path)}</b>\n<code>{fname}</code>"
        # Статью отправляем ФАЙЛОМ: её удобно открыть, отредактировать у себя
        # и прислать обратно через кнопку «Заменить».
        sent_msg = await context.bot.send_document(
            chat_id=chat_id,
            document=content.encode("utf-8"),
            filename=fname,
            caption=caption[:1024],
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_card_keyboard(token, folder),
        )
        if sent_msg:
            await register_and_clean_bot_message(context.bot, chat_id, sent_msg.message_id)
        return

    if action == "kb_approve":
        if _kb_rebuild_running(context):
            await _kb_popup(query, context, chat_id, "⏳ Идёт пересборка базы — одобрите статью после её завершения.")
            return
        if folder != "pending":
            await query.answer("Эта статья уже в базе.", show_alert=True)
            return
        try:
            path, _ = read_article(folder, fname)
            title = read_title(path)
            approve_article(fname)
        except FileNotFoundError:
            await query.answer("Файл уже удалён.", show_alert=True)
            await send_rag_panel(context.bot, chat_id, context)
            return
        logger.info("🔧 Админ %s одобрил статью базы знаний: %s", user_id, fname)
        from database.history import add_kb_action
        add_kb_action("одобрена", title, user_id)
        await _kb_sync_in_executor()
        # Пока идёт векторизация, на кнопке крутится «часики» Telegram;
        # всплывашка приходит уже с фактом — статья В базе.
        await _kb_popup(query, context, chat_id, f"✅ Статья добавлена в базу знаний:\n\n{title}")
        await send_rag_panel(context.bot, chat_id, context)
        return

    if action == "kb_delete":
        # Первое нажатие — просим подтверждение прямо в карточке
        await query.answer()
        try:
            await query.edit_message_reply_markup(_kb_card_keyboard(token, folder, confirm_delete=True))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение удаления: %s", e)
        return

    if action == "kb_cancel_del":
        await query.answer("Отменено.")
        try:
            await query.edit_message_reply_markup(_kb_card_keyboard(token, folder))
        except Exception as e:
            logger.warning("⚠️ Не удалось вернуть кнопки карточки: %s", e)
        return

    if action == "kb_delete_yes":
        if _kb_rebuild_running(context):
            await _kb_popup(query, context, chat_id, "⏳ Идёт пересборка базы — удалите статью после её завершения.")
            return
        try:
            path, _ = read_article(folder, fname)
            title = read_title(path)
        except FileNotFoundError:
            title = fname
        try:
            delete_article(folder, fname)
        except FileNotFoundError:
            pass  # уже удалён — просто обновим список
        logger.info("🔧 Админ %s удалил статью базы знаний (%s): %s", user_id, folder, fname)
        from database.history import add_kb_action
        if folder == "approved":
            add_kb_action("удалена из базы", title, user_id)
            await _kb_sync_in_executor()
            await _kb_popup(query, context, chat_id, f"🗑 Статья удалена из базы знаний:\n\n{title}")
        else:
            add_kb_action("удалена из ожидания", title, user_id)
            await _kb_popup(query, context, chat_id, f"🗑 Статья удалена из ожидания:\n\n{title}")
        await send_rag_panel(context.bot, chat_id, context)
        return

    if action == "kb_replace":
        # Запоминаем, какой файл ждёт замену, — следующий документ .md/.txt
        # от этого админа в личке станет новым содержимым (handle_kb_document).
        context.user_data["kb_replace_target"] = (folder, fname)
        await query.answer(
            f"📝 Пришли файлом (.md или .txt) новое содержимое статьи {fname} — я заменю её целиком.",
            show_alert=True
        )
        return

    await query.answer()


async def cmd_rag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель базы знаний. Только в личке и только у ВЛАДЕЛЬЦА:
    модераторам база знаний не выдаётся ни при каких галочках."""
    await delete_user_message_safe(update.message)
    user_id = update.effective_user.id

    if not await _require(update, context, "owner"):
        return

    if _is_group_chat(update):
        return

    await _end_kb_test(context.bot, update.effective_chat.id, context)  # выход из проверки поиска — с уборкой
    # Команда — осознанное открытие панели заново, поэтому она открывается
    # с экрана разделов и с первой страницы. Возврат из карточки статьи
    # («⬅️ К списку») и перерисовка после одобрения/удаления, наоборот,
    # оставляют владельца там, где он был.
    context.user_data["kb_page"] = 0
    context.user_data["kb_screen"] = ""
    await send_rag_panel(context.bot, update.effective_chat.id, context)


async def handle_kb_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимает документ для базы знаний в двух режимах:
      • замена содержимого статьи (после кнопки «Заменить», kb_replace_target);
      • добавление новой статьи (после кнопки «➕ Добавить RAG», kb_add_mode —
        режим живёт до другого действия в панели, можно слать несколько подряд).
    Срабатывает только у админа в личке и только если режим был включён —
    во всех остальных случаях молча пропускает документ (как и раньше,
    документы бот не обрабатывает).
    """
    if not update.message or not update.message.document:
        return
    user_id = update.effective_user.id
    from services import roles
    if not roles.is_owner(user_id) or _is_group_chat(update):
        return
    replace_target = context.user_data.get("kb_replace_target")
    add_mode = bool(context.user_data.get("kb_add_mode"))
    if not replace_target and not add_mode:
        return

    from services.knowledge_store import replace_article, add_article

    chat_id = update.effective_chat.id
    if _kb_rebuild_running(context):
        msg = await update.message.reply_text("⏳ Идёт пересборка базы — пришлите файл после её завершения.")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return
    doc = update.message.document
    fname_low = (doc.file_name or "").lower()
    # Служебные сообщения об ошибках не должны копиться в чате —
    # они удаляются сами через schedule_delete.
    if not (fname_low.endswith(".md") or fname_low.endswith(".txt")):
        msg = await update.message.reply_text("⚠️ Нужен текстовый файл .md или .txt — попробуй ещё раз.")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return
    if doc.file_size and doc.file_size > 1_000_000:
        msg = await update.message.reply_text("⚠️ Файл слишком большой (лимит 1 МБ).")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return

    try:
        tg_file = await doc.get_file()
        raw = await tg_file.download_as_bytearray()
        new_text = bytes(raw).decode("utf-8", errors="replace")
        if replace_target:
            folder, fname = replace_target
            replace_article(folder, fname, new_text)
        else:
            folder = "approved"
            fname = add_article(doc.file_name or "article.md", new_text)
    except Exception as e:
        verb = "заменить статью" if replace_target else "добавить статью"
        logger.error("⚠️ Не удалось %s: %s", verb, e)
        msg = await update.message.reply_text(f"⚠️ Не получилось {verb} — подробности в логе.")
        if msg:
            schedule_delete(context.bot, chat_id, msg.message_id, 30)
        return

    await delete_user_message_safe(update.message)
    from database.history import add_kb_action
    from services.knowledge_store import read_article, read_title
    try:
        article_path, _ = read_article(folder, fname)
        article_title = read_title(article_path)
    except Exception:
        article_title = fname
    if replace_target:
        context.user_data.pop("kb_replace_target", None)
        logger.info("🔧 Админ %s заменил содержимое статьи (%s): %s", user_id, folder, fname)
        add_kb_action("заменена", article_title, user_id)
        note = "✅ Статья <code>" + fname + "</code> заменена"
        note += " — вектора пересчитаны." if folder == "approved" else "."
    else:
        # Режим добавления НЕ сбрасываем — можно прислать следующий файл
        logger.info("🔧 Админ %s добавил новую статью в базу знаний: %s", user_id, fname)
        add_kb_action("добавлена", article_title, user_id)
        note = (f"✅ Статья <code>{fname}</code> добавлена в базу знаний — вектора посчитаны.\n"
                f"Можно прислать ещё файл или нажать любую кнопку панели.")
    if folder == "approved":
        # Файл в базе — пересчитываем вектора сразу
        await _kb_sync_in_executor()
    await send_rag_panel(context.bot, chat_id, context)
    # Подтверждение под обновлённой панелью; исчезает само, чтобы не мусорить
    confirm = await context.bot.send_message(chat_id=chat_id, text=note, parse_mode=ParseMode.HTML)
    if confirm:
        schedule_delete(context.bot, chat_id, confirm.message_id, 15)


async def handle_kb_test_query(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    """
    Режим «🔍 Проверить поиск» панели RAG: вопрос админа в личке уходит в
    диагностику семантического поиска вместо ИИ. Показывает найденные разделы
    с процентом сходства и отметкой, попали бы они в контекст модели.
    Вызывается из handlers/messages.py, когда включён user_data["kb_test_mode"].
    """
    import asyncio
    from services import rag

    message = update.message
    user_id = update.effective_user.id
    logger.info("📚 /rag: проверка поиска — «%s»", user_text[:80])

    # Эмбеддинг запроса — сетевой вызов, уводим в рабочий поток
    report = await asyncio.get_running_loop().run_in_executor(None, rag.test_search, user_text)

    if report.get("error"):
        text = f"⚠️ {html.escape(report['error'])}"
    else:
        thr_pct = int(round(report["threshold"] * 100))
        base_pct = int(round(report.get("baseline", 0) * 100))
        lines = [
            "🔍 <b>Проверка поиска</b>",
            f"Вопрос: <i>{html.escape(user_text[:200])}</i>",
            f"🎯 Порог: {thr_pct}% · 📦 Статей: {report['top_k']} · 📉 Полка (медиана): {base_pct}%",
            "",
        ]
        any_passed = False
        for r in report["results"]:
            mark = "✅" if r["passes"] else "▫️"
            any_passed = any_passed or r["passes"]
            # Балл = смысл + слова; вклад слов показываем отдельно, чтобы было
            # видно работу гибридного поиска. Для отсеянных — причина.
            lex_note = f" (слова +{r['lex'] * 100:.0f}%)" if r.get("lex") else ""
            reason_note = "" if r["passes"] else f" — {r.get('reason', '')}"
            lines.append(f"{mark} {r['similarity'] * 100:.0f}%{lex_note} — "
                         f"{html.escape(r['title'])}{html.escape(reason_note)}")
        if any_passed:
            lines.append("\n✅ — эти статьи уйдут модели вместе с вопросом.")
        else:
            lines.append("\n⚠️ Ничего не прошло отбор — модель ответит без базы знаний.")
        lines.append("<i>Пришли следующий вопрос или открой панель: /rag</i>")
        text = "\n".join(lines)

    from database.history import add_kb_action
    try:
        add_kb_action("проверка поиска", user_text[:35], user_id)
    except Exception:
        pass
    sent = await message.reply_text(text, parse_mode=ParseMode.HTML)
    # Запоминаем и вопрос, и ответ, чтобы убрать их при выходе из проверки
    # (см. _end_kb_test). Список создаётся при включении режима, но на всякий
    # случай подстраховываемся setdefault.
    msgs = context.user_data.setdefault("kb_test_msgs", [])
    if message.message_id:
        msgs.append(message.message_id)
    if sent:
        msgs.append(sent.message_id)
