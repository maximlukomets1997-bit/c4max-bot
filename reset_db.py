# reset_db.py — сброс базы данных бота C4_Max.
#
# ⚠️ ЗАПУСКАТЬ ТОЛЬКО ПРИ ОСТАНОВЛЕННОМ БОТЕ (иначе файл БД занят).
#
# Очищает ВСЕ таблицы списка USER_TABLES: переписку и контексты, викторину
# вместе с банком вопросов, расходы, архив групп, журналы, личные дела,
# модераторов (staff), список своих групп (known_chats), подписки на новости.
#
# Настройка KEEP_SETTINGS:
#   True  — сохранить таблицу settings (выбранная модель, промпты, тумблеры).
#           Таблицы из USER_TABLES стираются в любом случае.
#   False — стереть ВСЁ, включая настройки (модель и промпты вернутся к
#           значениям по умолчанию при следующем запуске).
#
# Проще почти «с нуля»: остановить бота, удалить history.db, запустить снова —
# развернётся заготовка seed.db (промпты, настройки, персонал), без неё — пустая.

import sqlite3

DB_FILE = "history.db"
KEEP_SETTINGS = False   # ← поменяй на True, если настройки (модель, промпты) нужно СОХРАНИТЬ

# Таблицы с данными пользователей
USER_TABLES = [
    "messages",
    "user_context",
    "quiz_stats",
    "quiz_bank",
    "quiz_failed",
    "user_token_usage",
    "user_image_calls",
    "api_calls",
    "group_messages",
    "bot_sent_messages",
    "news_subscriptions",
    "sent_news",
    "moderation_log",
    "mute_evidence",
    "knowledge_log",
    "user_dossier",
    "user_settings",
    "known_chats",
    "staff",
    "staff_log",
    "stats_snapshots",
    "proactive_log",
    "join_log",
]

def main():
    con = sqlite3.connect(DB_FILE)
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}

    tables = list(USER_TABLES)
    if not KEEP_SETTINGS:
        tables.append("settings")

    cleared = []
    for t in tables:
        if t in existing:
            con.execute(f"DELETE FROM {t}")
            cleared.append(t)

    # сбрасываем счётчики автоинкремента
    if "sqlite_sequence" in existing:
        con.execute("DELETE FROM sqlite_sequence")

    con.commit()
    con.execute("VACUUM")   # сжать файл после удаления
    con.close()

    print("Очищены таблицы:", ", ".join(cleared))
    print("Настройки (модель, промпты) СОХРАНЕНЫ." if KEEP_SETTINGS
          else "Настройки тоже СТЁРТЫ (вернутся к значениям по умолчанию).")
    print("Готово. Можно запускать бота.")

if __name__ == "__main__":
    main()
