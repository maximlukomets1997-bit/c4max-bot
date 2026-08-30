# ───────────────────────────────────────────────
#  handlers/admin/panel_mod.py — модерация: панель /mod, антиспам-настройки, улики, размут (кнопка и /unmute).
#  Выделен из монолитного admin.py 2026-07-13 разрезом БЕЗ изменения логики.
# ───────────────────────────────────────────────
import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from database.history import set_setting, get_setting
from utils import register_and_clean_bot_message, delete_user_message_safe
from utils import mention, schedule_delete


logger = logging.getLogger(__name__)
from .common import (_adm_back_row, _audit, _filter_keyboard, _fmt_mod_time, _is_group_chat,
                     _onoff, _require)
from .panel_rag import _end_kb_test




def _build_mod_panel_text_and_keyboard(viewer_id: int = 0):
    """
    Текст + клавиатура панели модерации. Читает живые настройки из settings.

    viewer_id — кто смотрит: от него зависит кнопка «🧹 Очистить журнал»
    (только владелец). Панель собирается в четырёх местах — при команде /mod,
    по кнопке из /adm и при двух перерисовках; забыть передать viewer_id хотя
    бы в одном означает, что кнопка пропадёт у владельца после первого же
    нажатия ➖/➕.
    """
    from services.antispam import is_enabled, is_linkfilter_enabled, get_thresholds, get_mute_stats, get_recent_actions, MOD_STATS_DAYS
    from services import greeter
    from database.history import get_join_counts
    from config import LINKFILTER_WHITELIST, LINKFILTER_MUTE_COUNT
    enabled = is_enabled()
    lf_enabled = is_linkfilter_enabled()
    msg_count, window_sec, mute_sec = get_thresholds()

    status = "🟢 ВКЛЮЧЁН" if enabled else "🔴 ВЫКЛЮЧЕН"
    lf_status = "🟢 ВКЛЮЧЁН" if lf_enabled else "🔴 ВЫКЛЮЧЕН"
    minutes = max(1, mute_sec // 60)
    stats = get_mute_stats()

    # 👋 Приветствие новичков (2026-08-04)
    greet_on = greeter.is_enabled()
    greet_captcha = greeter.captcha_enabled()
    greet_kick = greeter.kick_enabled()
    greet_sec = greeter.timeout_sec()
    greet_min = max(1, greet_sec // 60)
    try:
        joins = get_join_counts(MOD_STATS_DAYS)
    except Exception as e:
        logger.debug("👋 Не удалось прочитать журнал вступлений: %s", e)
        joins = {"joins": 0, "ok": 0, "timeouts": 0, "kicks": 0}

    text = (
        "<b>⚙️НАСТРОЙКИ —🛡DDoS-Guard</b>\n"
        "───────────────────────────\n"
        f"Статус🛡<b>DDoS-Guard</b>: <b>{status}</b>\n"
        f"• Порог флуда: <b>{msg_count}</b> сообщ. за <b>{window_sec}</b> сек\n"
        f"• Длительность мута: <b>{mute_sec}</b> сек (~{minutes} мин)\n"
        "───────────────────────────\n"
        f"🔗 Фильтр ссылок: <b>{lf_status}</b>\n"
        f"• Разрешены: {', '.join(LINKFILTER_WHITELIST)}\n"
        f"• {LINKFILTER_MUTE_COUNT} удалённые ссылки за час = мут\n"
        "───────────────────────────\n"
        f"👋 Приветствие новичков: <b>{'🟢 ВКЛЮЧЕНО' if greet_on else '🔴 ВЫКЛЮЧЕНО'}</b>\n"
        f"• Проверка «я не бот»: <b>{'🟢 ВКЛЮЧЕНА' if greet_captcha else '🔴 ВЫКЛЮЧЕНА'}</b>"
        f" · срок <b>{greet_min}</b> мин\n"
        f"• Не прошёл — кикать: <b>{'🟢 ДА' if greet_kick else '🔴 НЕТ'}</b>\n"
        f"• За {MOD_STATS_DAYS} дней: пришло <b>{joins['joins']}</b> · "
        f"прошли <b>{joins['ok']}</b> · не прошли <b>{joins['timeouts']}</b> · "
        f"выгнано <b>{joins['kicks']}</b>\n"
        "───────────────────────────\n"
        f"📋 За {MOD_STATS_DAYS} дней: мутов <b>{stats.get('mutes', 0)}</b> · "
        f"размутов <b>{stats.get('unmutes', 0)}</b> · "
        f"ссылок 🔗 <b>{stats.get('linkdels', 0)}</b>\n"
    )
    # Ручные действия из карточки пользователя показываем отдельной строкой и
    # только если они были — иначе панель мозолит глаза нулями.
    if stats.get("kicks") or stats.get("bans"):
        text += (f"👢 Киков: <b>{stats.get('kicks', 0)}</b> · "
                 f"⛔ банов: <b>{stats.get('bans', 0)}</b>\n")

    recent = get_recent_actions(5)
    evidence_buttons = []
    if recent:
        lines = []
        for a in recent:
            t = _fmt_mod_time(a["ts"])
            action = a["action"]
            # Все типы записей журнала перечислены ЯВНО. Раньше «всё остальное»
            # рисовалось как размут — с приходом ручных действий (кик, бан)
            # это стало враньём в журнале.
            icons = {
                "mute":     ("🛡", "мут"),
                "mute_adm": ("🛡", "мут вручную"),
                "mute_ai":  ("🤚", "мут от бота"),
                "linkdel":  ("🔗", "ссылка удалена"),
                "unmute":   ("🔓", "размут"),
                "kick":     ("👢", "кик"),
                "ban":      ("⛔", "бан"),
                "unban":    ("♻️", "разбан"),
            }
            icon, verb = icons.get(action, ("❔", action))
            nm = (a.get("name") or str(a["user_id"]))[:24]
            # Имя пользователя — чужой текст: экранируем, иначе символ «<» в имени
            # ломает HTML-разметку и панель /mod перестаёт открываться.
            line = f"  {icon} {t} {verb}: {html.escape(nm)}"
            # Кто это сделал — у всех РУЧНЫХ действий (у автоматики графа пуста,
            # старые записи её тоже не имеют).
            adm = a.get("admin_name")
            if adm:
                word = "снял" if action in ("unmute", "unban") else "админ"
                line += f" ({word}: {html.escape(adm[:24])})"
            lines.append(line)
            # Кнопка просмотра удалённого — для мутов и ссылок (у них есть улики).
            if action in ("mute", "linkdel") and a.get("id"):
                evidence_buttons.append(
                    InlineKeyboardButton(f"📜 {t} {nm}"[:30],
                                         callback_data=f"mod:evidence:{a['id']}")
                )
        text += "\n<b>Последние действия:</b>\n" + "\n".join(lines) + "\n"

    text += "\n<i>Настройки общие для всех групп, где бот — админ.</i>"

    # Кнопки показывают СОСТОЯНИЕ (метка — общий _onoff), а не действие:
    # прежние «🔴 Выключить антиспам» читались наоборот — красный кружок при
    # РАБОТАЮЩЕЙ защите (переделано 2026-07-20).
    toggle_label = f"🛡 DDoS-Guard: {_onoff(enabled)}"
    lf_label = f"🔗 Фильтр ссылок: {_onoff(lf_enabled)}"
    keyboard = [
        [InlineKeyboardButton(toggle_label, callback_data="mod:antispam:toggle")],
        [InlineKeyboardButton(lf_label, callback_data="mod:antispam:linkfilter_toggle")],
        [
            InlineKeyboardButton("➖ сообщ.", callback_data="mod:antispam:count_dec"),
            InlineKeyboardButton(f"{msg_count} сообщ.", callback_data="mod:antispam:noop"),
            InlineKeyboardButton("➕ сообщ.", callback_data="mod:antispam:count_inc"),
        ],
        [
            InlineKeyboardButton("➖ окно", callback_data="mod:antispam:window_dec"),
            InlineKeyboardButton(f"{window_sec} сек", callback_data="mod:antispam:noop"),
            InlineKeyboardButton("➕ окно", callback_data="mod:antispam:window_inc"),
        ],
        [
            InlineKeyboardButton("➖ мут", callback_data="mod:antispam:mute_dec"),
            InlineKeyboardButton(f"{mute_sec} сек", callback_data="mod:antispam:noop"),
            InlineKeyboardButton("➕ мут", callback_data="mod:antispam:mute_inc"),
        ],
        # 👋 Приветствие новичков. Свои ряды у каждого тумблера: надписи
        # длинные, в половину ширины не влезают (то же правило, что у
        # «⬇️ ОБНОВЛЕНИЕ» и «🧠 МЫСЛИ ПОД КАПОТОМ»).
        [InlineKeyboardButton(f"👋 Приветствие новичков: {_onoff(greet_on)}",
                              callback_data="mod:greet:toggle")],
        [InlineKeyboardButton(f"🤖 Проверка «я не бот»: {_onoff(greet_captcha)}",
                              callback_data="mod:greet:captcha")],
        [
            InlineKeyboardButton("➖ срок", callback_data="mod:greet:time_dec"),
            InlineKeyboardButton(f"{greet_min} мин", callback_data="mod:greet:noop"),
            InlineKeyboardButton("➕ срок", callback_data="mod:greet:time_inc"),
        ],
        [InlineKeyboardButton(f"👢 Кикать не прошедших: {_onoff(greet_kick)}",
                              callback_data="mod:greet:kick")],
    ]
    # Ряды кнопок просмотра удалённого (по 2 в ряд), если есть недавние муты.
    for i in range(0, len(evidence_buttons), 2):
        keyboard.append(evidence_buttons[i:i + 2])

    # Очистка журнала — надзорный инструмент: модератор не должен иметь
    # возможности стереть записи о собственных действиях. Уровень владельца
    # задан в таблице прав (mod:clearlog), скрывать кнопку здесь не нужно —
    # это сделает фильтр ниже.
    keyboard.append([InlineKeyboardButton("🧹 Очистить журнал",
                                          callback_data="mod:clearlog")])

    keyboard.append(_adm_back_row())
    # Модератору остаётся ВЕСЬ ТЕКСТ панели, кнопки улик и возврат: тумблеры
    # и регуляторы общих настроек живут под уровнем владельца и отсеются здесь.
    return text, InlineKeyboardMarkup(_filter_keyboard(keyboard, viewer_id))


def _adjust_setting(key: str, delta_steps: int) -> int:
    """
    Меняет числовую настройку на delta_steps шагов кнопками ➖/➕.

    Шаги, пределы и начальные значения живут в services/settings_spec.py —
    ОДНОЙ таблицей на весь проект (30.08.2026). Раньше они лежали здесь
    отдельным словарём `_MOD_LIMITS`; после появления сайта у тех же
    настроек стало два хозяина, и две копии пределов разъехались бы.
    """
    from services.settings_spec import adjust
    return adjust(key, delta_steps)


async def _handle_mod_callback(update, context, query, user_id, data):
    """
    Роутер панели модерации. Формат: mod:<секция>:<действие...>.
    Гейт ADMIN_IDS уже пройден выше по стеку (в handle_callback_query).
    Секции: antispam (настройки), greet (приветствие новичков),
    unmute (снять мут по кнопке из уведомления),
    evidence (показ удалённых сообщений за мут / возврат к панели),
    clearlog / clearlog_yes (очистка журнала модерации, только владелец).
    """
    parts = data.split(":")
    section = parts[1] if len(parts) > 1 else ""

    if section == "antispam":
        await _handle_antispam_callback(update, query, parts)
        return

    if section == "greet":
        await _handle_greet_callback(query, parts)
        return

    if section == "unmute":
        await _handle_unmute_callback(update, query, parts)
        return

    if section == "evidence":
        await _handle_evidence_callback(update, query, parts)
        return

    if section in ("clearlog", "clearlog_yes"):
        await _handle_clearlog_callback(query, section == "clearlog_yes", user_id)
        return

    await query.answer()


async def _handle_clearlog_callback(query, confirmed: bool, user_id: int):
    """
    Ветки mod:clearlog (спросить) и mod:clearlog_yes (стереть).

    Журнал стирается ЦЕЛИКОМ и вместе с уликами — восстановить нечего,
    поэтому сначала подменяем клавиатуру на «да / отмена», как это сделано
    в панели базы знаний.
    """
    if not confirmed:
        await query.answer()
        try:
            await query.edit_message_reply_markup(InlineKeyboardMarkup([[
                InlineKeyboardButton("❗️ Да, очистить журнал", callback_data="mod:clearlog_yes"),
                InlineKeyboardButton("Отмена", callback_data="mod:evidence:back"),
            ]]))
        except Exception as e:
            logger.warning("⚠️ Не удалось показать подтверждение очистки журнала: %s", e)
        return

    from database.history import clear_moderation_log
    deleted = clear_moderation_log()
    logger.info("🛡 Владелец %s очистил журнал модерации (%d записей)", user_id, deleted)
    _audit(user_id, "modlog_clear", 0, f"журнал модерации: {deleted} записей")
    await query.answer(f"🧹 Журнал модерации очищен: {deleted} записей.", show_alert=True)

    text, markup = _build_mod_panel_text_and_keyboard(user_id)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception as e:
        logger.debug("🛡 Не удалось обновить панель модерации: %s", e)


async def _handle_antispam_callback(update, query, parts):
    """Ветки mod:antispam:<действие> — тумблер и кнопки +/- порогов."""
    action = parts[2] if len(parts) > 2 else ""
    # Общие настройки действуют на ВСЕ группы сразу — каждая правка попадает
    # в журнал персонала (кто крутил пороги, видно поимённо).
    actor_id = query.from_user.id

    if action == "noop":
        await query.answer()
        return

    if action == "toggle":
        from services.antispam import is_enabled
        new_val = "0" if is_enabled() else "1"
        set_setting("antispam_enabled", new_val)
        state = "включён" if new_val == "1" else "выключен"
        logger.info("🛡 /mod: антиспам %s", state)
        _audit(actor_id, "antispam", 0, f"антиспам {state}")
        await query.answer(f"Антиспам {state}", show_alert=False)
    elif action == "linkfilter_toggle":
        from services.antispam import is_linkfilter_enabled
        new_val = "0" if is_linkfilter_enabled() else "1"
        set_setting("linkfilter_enabled", new_val)
        state = "включён" if new_val == "1" else "выключен"
        logger.info("🛡 /mod: фильтр ссылок %s", state)
        _audit(actor_id, "antispam", 0, f"фильтр ссылок {state}")
        await query.answer(f"Фильтр ссылок {state}", show_alert=False)
    elif action in ("count_inc", "count_dec"):
        new_count = _adjust_setting("antispam_msg_count", 1 if action.endswith("inc") else -1)
        logger.info("🛡 /mod: порог сообщений = %d", new_count)
        _audit(actor_id, "antispam", 0, f"общий порог = {new_count} сообщ.")
        await query.answer()
    elif action in ("window_inc", "window_dec"):
        new_window = _adjust_setting("antispam_window_sec", 1 if action.endswith("inc") else -1)
        logger.info("🛡 /mod: окно засчёта = %d сек", new_window)
        _audit(actor_id, "antispam", 0, f"общее окно = {new_window} сек")
        await query.answer()
    elif action in ("mute_inc", "mute_dec"):
        new_mute = _adjust_setting("antispam_mute_sec", 1 if action.endswith("inc") else -1)
        logger.info("🛡 /mod: длительность мута = %d сек", new_mute)
        _audit(actor_id, "antispam", 0, f"общая длительность мута = {new_mute} сек")
        await query.answer()
    else:
        await query.answer()
        return

    # Перерисовываем панель с новыми значениями
    text, markup = _build_mod_panel_text_and_keyboard(actor_id)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception as e:
        logger.debug("🛡 Не удалось обновить панель модерации: %s", e)


async def _handle_greet_callback(query, parts):
    """
    Ветки mod:greet:<действие> — приветствие новичков (2026-08-04):
    три тумблера и регулятор срока проверки «я не бот».

    Настройки общие на все группы (как пороги антифлуда), поэтому уровень
    владельца — он задан в таблице прав (`mod:greet:` в services/roles.py),
    прятать кнопки здесь не нужно, это сделает `_filter_keyboard`.
    """
    from services import greeter

    action = parts[2] if len(parts) > 2 else ""
    actor_id = query.from_user.id

    if action == "noop":
        await query.answer()
        return

    # Тумблеры: ключ в settings, читалка состояния и слова для попапа/журнала.
    toggles = {
        "toggle":  ("greet_enabled", greeter.is_enabled,      "приветствие новичков"),
        "captcha": ("greet_captcha", greeter.captcha_enabled, "проверка «я не бот»"),
        "kick":    ("greet_kick",    greeter.kick_enabled,    "кик не прошедших проверку"),
    }

    if action in toggles:
        key, reader, title = toggles[action]
        new_val = "0" if reader() else "1"
        set_setting(key, new_val)
        state = "включено" if new_val == "1" else "выключено"
        logger.info("👋 /mod: %s — %s", title, state)
        _audit(actor_id, "greet", 0, f"{title}: {state}")
        await query.answer(f"👋 {title.capitalize()}: {state}", show_alert=False)
    elif action in ("time_inc", "time_dec"):
        before = greeter.timeout_sec()
        after = _adjust_setting("greet_timeout_sec",
                                1 if action.endswith("inc") else -1)
        if after == before:
            # Упёрлись в границу: панель получилась бы точь-в-точь прежней, а
            # Telegram на такую перерисовку отвечает ошибкой «Message is not
            # modified» — кнопка выглядела бы мёртвой, а в логе копился шум.
            await query.answer(f"Уже {max(1, before // 60)} мин — дальше некуда.")
            return
        logger.info("👋 /mod: срок проверки «я не бот» = %d сек", after)
        _audit(actor_id, "greet", 0, f"срок проверки = {after} сек")
        await query.answer()
    else:
        await query.answer()
        return

    text, markup = _build_mod_panel_text_and_keyboard(actor_id)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception as e:
        logger.debug("👋 Не удалось обновить панель модерации: %s", e)


async def _handle_unmute_callback(update, query, parts):
    """Ветка mod:unmute:<chat_id>:<user_id> — снятие мута кнопкой из уведомления."""
    from services.antispam import unmute
    try:
        target_chat_id = int(parts[2])
        target_user_id = int(parts[3])
    except (IndexError, ValueError):
        await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
        return

    bot = query.get_bot()

    # Имя размученного — пробуем достать из участников чата (в callback_data только id).
    target_name = str(target_user_id)
    try:
        member = await bot.get_chat_member(target_chat_id, target_user_id)
        u = member.user
        target_name = u.first_name or (f"@{u.username}" if u.username else str(target_user_id))
    except Exception:
        pass

    admin_name = mention(query.from_user)
    ok = await unmute(bot, target_chat_id, target_user_id, target_name, admin_name)
    if not ok:
        await query.answer("❌ Не удалось снять мут (проверь права бота).", show_alert=True)
        return

    logger.info("🛡 Админ %s снял мут с %s (чат %s)", admin_name, target_name, target_chat_id)
    _audit(query.from_user.id, "unmute", target_user_id, "кнопкой в группе")
    await query.answer("🔓 Мут снят", show_alert=False)
    # Обновляем уведомление в чате: убираем кнопку, помечаем кого и кто размутил.
    try:
        await query.edit_message_text(f"🔓 {target_name} размучен администратором {admin_name}.")
        msg = query.message
        if msg:
            schedule_delete(bot, msg.chat_id, msg.message_id, 30)
    except Exception as e:
        logger.debug("🛡 Не удалось обновить уведомление о муте: %s", e)


async def _handle_evidence_callback(update, query, parts):
    """
    Ветка mod:evidence:<log_id> — показать удалённые сообщения за мут.
    Ветка mod:evidence:back — вернуться к панели модерации.
    """
    action = parts[2] if len(parts) > 2 else ""

    # Возврат к панели (сюда же ведёт «Отмена» подтверждения очистки журнала)
    if action == "back":
        text, markup = _build_mod_panel_text_and_keyboard(query.from_user.id)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            logger.debug("🛡 Не удалось вернуться к панели модерации: %s", e)
        await query.answer()
        return

    try:
        log_id = int(action)
    except (TypeError, ValueError):
        await query.answer("❌ Некорректные данные кнопки.", show_alert=True)
        return

    import html
    from database.history import get_moderation_entry
    from services.antispam import get_evidence, MOD_STATS_DAYS

    entry = get_moderation_entry(log_id)
    if not entry:
        await query.answer("Записи уже нет (возможно, очищена по сроку).", show_alert=True)
        return

    evidence = get_evidence(log_id)
    name = entry.get("name") or str(entry.get("user_id"))
    when = _fmt_mod_time(entry["ts"])
    # logger.debug("🔎 Админ открыл улики мута #%s (%s)", log_id, name)

    # Причина по типу записи: мут за флуд или удаление ссылки фильтром
    reason = "удалена ссылка" if entry.get("action") == "linkdel" else "мут за флуд"
    text = (
        "📜 <b>Удалённые сообщения</b>\n"
        "───────────────────────────\n"
        f"👤 <b>{html.escape(name)}</b> · {when} · {reason}\n"
        "───────────────────────────\n"
    )
    if evidence:
        body_lines = []
        for i, m in enumerate(evidence, 1):
            t = (m.get("text") or "").strip()
            if not t:
                t = "🖼 [фото/медиа — без текста]" if m.get("has_photo") else "[пусто]"
            body_lines.append(f"{i}. {html.escape(t)}")
        text += "\n".join(body_lines) + "\n"
    else:
        text += "<i>Тексты не сохранились (сообщения без текста или запись очищена).</i>\n"
    text += "───────────────────────────\n"
    text += f"<i>Хранится {MOD_STATS_DAYS} дней, затем удаляется автоматически.</i>"

    back_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад к панели", callback_data="mod:evidence:back")
    ]])
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb)
    except Exception as e:
        logger.debug("🛡 Не удалось показать улики мута %s: %s", log_id, e)
    await query.answer()


async def send_mod_panel(bot, chat_id: int, viewer_id: int | None = None):
    """
    Собирает и отправляет панель модерации в указанный чат. Вынесено отдельно
    от cmd_mod, чтобы панель открывалась и по команде, и кнопкой из /adm.

    viewer_id по умолчанию равен chat_id: панель открывается только в личке,
    где это одно и то же число (так же сделано в /adm и /users).
    """
    text, markup = _build_mod_panel_text_and_keyboard(chat_id if viewer_id is None else viewer_id)
    sent_msg = await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup
    )
    if sent_msg:
        await register_and_clean_bot_message(bot, chat_id, sent_msg.message_id)


async def cmd_mod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель модерации. Только в личке; нужно право «🛡DDoS-Guard»."""
    await delete_user_message_safe(update.message)
    user_id = update.effective_user.id
    chat = update.effective_chat

    if not await _require(update, context, "mod"):
        return

    # Панель — только в личке. В группе молча выходим (команда уже удалена
    # выше), ничего не отправляя в чат.
    if _is_group_chat(update):
        return

    # logger.info("🛡 Админ %s открыл панель /mod", user_id)  # скрыто по просьбе
    await _end_kb_test(context.bot, chat.id, context)  # выход из проверки поиска — с уборкой
    await send_mod_panel(context.bot, chat.id, user_id)


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Снимает мут с пользователя. Использование: ответить /unmute на сообщение
    нарушителя в группе. Нужно право «🛡DDoS-Guard», работает в группах.
    """
    await delete_user_message_safe(update.message)
    user_id = update.effective_user.id
    chat = update.effective_chat

    if not await _require(update, context, "mod"):
        return

    if chat.type == "private":
        # Через регистрацию в уборке: иначе подсказка зависнет в личке навсегда
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text="ℹ️ /unmute работает в группе: ответь этой командой на сообщение нарушителя.",
        )
        if sent:
            await register_and_clean_bot_message(context.bot, chat.id, sent.message_id)
        return

    reply = update.message.reply_to_message if update.message else None
    if not reply or not reply.from_user:
        # В группе панелей нет — подсказка самоудаляется через 30 сек,
        # как остальные служебные сообщения модерации.
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text="ℹ️ Ответь командой /unmute на сообщение того, кого нужно размутить.",
        )
        if sent:
            schedule_delete(context.bot, chat.id, sent.message_id, 30)
        return

    target = reply.from_user
    target_name = target.first_name or (f"@{target.username}" if target.username else str(target.id))
    admin_name = mention(update.effective_user)

    from services.antispam import unmute
    ok = await unmute(context.bot, chat.id, target.id, target_name, admin_name)
    if ok:
        logger.info("🛡 Админ %s снял мут с %s (чат %s)", admin_name, target_name, chat.id)
        _audit(user_id, "unmute", target.id, "командой /unmute")
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=f"🔓 {target_name} размучен администратором {admin_name}.",
        )
        if sent:
            schedule_delete(context.bot, chat.id, sent.message_id, 30)
    else:
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text="❌ Не удалось снять мут. Проверь, что бот — админ с правом ограничивать участников.",
        )
        if sent:
            schedule_delete(context.bot, chat.id, sent.message_id, 30)
