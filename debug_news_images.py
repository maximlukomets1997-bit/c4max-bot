# ───────────────────────────────────────────────
#  debug_news_images.py — «что парсер видит на странице новости»
#
#  ЗАЧЕМ. Когда бот присылает картинку, которой в новости нет, по логам
#  восстановить причину нельзя: страница живая, а выбор парсера нигде не виден.
#  Скрипт показывает КАЖДУЮ картинку статьи и то, как парсер её оценил:
#  внутри тела статьи она или снаружи, в живой секции или в вырезанной,
#  попадёт ли в рассылку.
#
#  ЗАПУСК (на сервере, из папки бота):
#      .venv/bin/python debug_news_images.py                    # свежая новость
#      .venv/bin/python debug_news_images.py https://wtmobile.com/news/1372
#
#  Бота трогать не надо: скрипт ничего не отправляет и ничего не пишет в базу.
# ───────────────────────────────────────────────

import re
import sys

from bs4 import BeautifulSoup

from services.scraper import (
    _DROP_PREFIXES,
    _HEADERS,
    _abs_url,
    _clean_text,
    _picture_url,
    fetch_article,
    fetch_latest_news,
)
from services.http import session as _http


def _dump(url: str) -> None:
    ru_url = re.sub(r"(https?://wtmobile\.com)(/news)", r"\1/ru\2", url, count=1)
    print(f"\n📰 Статья: {ru_url}")

    response = _http().get(ru_url, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    # ── 1. Главная картинка (hero-баннер) ───────────────────────────────
    main_el = soup.find("main") or soup
    heroes = soup.find_all("picture", class_="keyvisual-outlet")
    print(f"\n🖼  Баннеров keyvisual-outlet на странице: {len(heroes)}")
    for i, pic in enumerate(heroes, 1):
        inside = "внутри <main>" if pic.find_parent("main") is not None else "ВНЕ <main>"
        print(f"   {i}. [{inside}] {_picture_url(pic)}")
    if len(heroes) > 1:
        print("   ⚠️ Баннеров больше одного — парсер берёт первый ВНУТРИ <main>.")

    # ── 2. Картинки тела статьи с разметкой секций ──────────────────────
    body = soup.find("div", class_="news-container")
    if not body:
        print("\n⚠️ news-container не найден — сайт снова сменил вёрстку.")
        return

    print("\n🧩 Картинки внутри статьи (в порядке документа):")
    section = "(начало статьи, до первого заголовка)"
    skip = False
    found = False
    for el in body.find_all(["h2", "h3", "p", "ul", "summary", "picture"]):
        if el.name != "summary" and el.find_parent("summary") is not None:
            continue

        if el.name == "picture":
            src = _picture_url(el)
            if not src:
                continue
            found = True
            fname = src.rsplit("/", 1)[-1].lower()
            marks = []
            if "stat" in fname:
                marks.append("карточка ТТХ")
            if "prem" in fname:
                marks.append("ПРЕМИУМ")
            marks.append("секция ВЫРЕЗАНА из текста" if skip else "секция в тексте есть")
            print(f"   • {src}")
            print(f"     секция: {section}")
            print(f"     признаки: {', '.join(marks)}")
            continue

        cls = el.get("class") or []
        is_heading = el.name == "h2" and "heading" in cls
        is_subheading = el.name == "h3" and "subheading" in cls
        is_paragraph = el.name == "p" and "paragraph" in cls
        is_spoiler = el.name == "summary"
        if not (is_heading or is_subheading or is_paragraph or is_spoiler):
            continue

        txt = _clean_text(el.get_text(" ", strip=True))
        low = txt.lower()
        if is_heading or is_subheading or is_spoiler:
            skip = any(low.startswith(p) for p in _DROP_PREFIXES)
            section = txt[:70]
        elif not skip and any(low.startswith(p) for p in _DROP_PREFIXES):
            skip = True

    if not found:
        print("   (картинок в теле статьи нет)")

    # ── 3. Что в итоге уйдёт в рассылку ─────────────────────────────────
    article = fetch_article(url)
    print("\n📤 В рассылку уйдёт:")
    print(f"   обложка      : {article['main_image'] or '— (откат на превью из списка новостей)'}")
    stats = article["stat_images"]
    print(f"   карточка ТТХ : {stats[0] if stats else '— (карточек нет)'}")
    if len(stats) > 1:
        print(f"   (не отправляются, но найдены: {' | '.join(stats[1:])})")


def main() -> None:
    if len(sys.argv) > 1:
        urls = [_abs_url(a) for a in sys.argv[1:]]
    else:
        news = fetch_latest_news()
        if not news:
            print("⚠️ Список новостей пуст — сайт недоступен или сменил вёрстку.")
            return
        urls = [news[0]["url"]]
        print(f"Ссылка не задана — беру свежую новость: «{news[0]['title']}»")

    for url in urls:
        try:
            _dump(url)
        except Exception as e:
            print(f"⚠️ Не удалось разобрать {url}: {e}")


if __name__ == "__main__":
    main()
