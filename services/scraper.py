# ───────────────────────────────────────────────
#  scraper.py — парсер новостей War Thunder Mobile (wtmobile.com)
#
#  КАК РАБОТАЕТ
#
#  ВЁРСТКА: в конце июня 2026 сайт полностью переехал на новый движок —
#  старые классы (news-preview, g-article, title--h1, g-heading …) исчезли.
#  Парсер написан под новую вёрстку:
#    • карточка новости в списке — <a class="news-card">, внутри заголовок
#      <h2 class="heading">, описание <p class="news-description">, обложка
#      <picture class="news-art"> (URL в data-orig), тег — <p> рядом с <time>;
#    • тело статьи — <div class="news-container">; заголовки секций
#      h2.heading / h3.subheading, абзацы p.paragraph, списки ul.ulist,
#      спойлеры <details class="spoiler"> с заголовком <summary>;
#    • обложка-баннер статьи — <picture class="keyvisual-outlet"> (1440×640);
#    • карточки ТТХ — картинки статьи со 'stat' в имени файла (stat_ru на /ru/).
#  Ссылки на статьи стали двух видов: старые слаги (/news/season-dark-waters)
#  и новые числовые (/news/1348) — парсер поддерживает оба.
#
#  Язык: парсер всегда читает РУССКУЮ версию сайта (префикс /ru/ в URL). Это
#  фиксирует язык независимо от geo сервера и даёт русские картинки (карточки
#  ТТХ stat_ru). Сохраняемые URL новостей остаются БЕЗ /ru/ (сайт сам
#  перенаправит читателя на его язык) — дедупликация в БД и ссылка
#  «Читать на сайте» не меняются.
#
#  fetch_latest_news() — свежие новости с /ru/news: заголовок, краткое
#      описание, тег, обложка-превью, чистый URL. Берём первые 4 карточки:
#      первой может висеть «закреплённая» новость крупного обновления,
#      поэтому 4 карточки гарантируют 3 действительно свежие.
#
#  fetch_article(url) — полный разбор одной статьи (/ru/). Возвращает словарь:
#      {"text": <структурированный текст>, "stat_images": [<url ТТХ>, …],
#       "main_image": <url обложки-баннера>}.
#
#  _extract_article_body(soup) собирает текст и картинки так:
#    • Заголовок статьи (первый h2.heading внутри <main>, он стоит до
#      news-container) ставится первой строкой как «# …».
#    • Строка «<техника> можно будет получить в War Thunder Mobile (WTM) …»
#      (обычно в самом конце) выносится СРАЗУ после заголовка.
#    • Тело: заголовки секций (## / ###), абзацы, маркированные списки,
#      заголовки спойлеров — в порядке документа.
#    • ВЫРЕЗАЕТСЯ блок про премиум/стандарт версии техники (по русским маркерам
#      _DROP_PREFIXES / _DROP_WITH_LIST): заголовок «Премиумная версия …»,
#      «Отличия премиум…», «Премиумная/Стандартная версия техники» с их списками,
#      дисклеймер «*Характеристики…» и блоки «Способы получения…» (с 2026-07-20 —
#      целиком, до следующего заголовка). Строка про получение техники СОХРАНЯЕТСЯ.
#    • Длина текста ограничена ARTICLE_MAX_CHARS (предохранитель от аномально
#      длинных страниц); реальные статьи проходят целиком.
#
#  Картинки отдаёт rss-рассылка (jobs.send_news_to_chat): main_image обложкой +
#  первая карточка ТТХ. Текст уходит модели (services.gemini.format_news_as_colonel).
# ───────────────────────────────────────────────

import re
import logging
import requests
# Соединения к моделям и сайтам переиспользуются (2026-07-27): «рукопожатие»
# TLS с сервером стоит 130–200 мс и раньше платилось на КАЖДЫЙ запрос.
# Подробности и запрет на повторы — в services/http.py.
from services.http import session as _http
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _abs_url(src: str) -> str:
    """Относительную ссылку сайта делает абсолютной."""
    if src.startswith("/"):
        return f"https://wtmobile.com{src}"
    return src


def _picture_url(pic) -> str:
    """URL картинки из <picture>: сначала data-orig, потом src/data-src у <img>."""
    if pic is None:
        return ""
    url = pic.get("data-orig") or ""
    if not url:
        img = pic.find("img")
        if img:
            url = img.get("src") or img.get("data-src") or ""
    return _abs_url(url) if url else ""


def fetch_latest_news() -> list[dict]:
    """
    Скачивает и парсит страницу новостей https://wtmobile.com/ru/news.
    Возвращает список свежих новостей в виде структурированных словарей.
    """
    url = "https://wtmobile.com/ru/news"

    try:
        response = _http().get(url, headers=_HEADERS, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.find_all("a", class_="news-card")
        if not cards:
            # Пустой список при статусе 200 — сайт снова сменил вёрстку,
            # молчать нельзя (именно так рассылка тихо умерла в июне 2026).
            logger.error("⚠️ На странице новостей не найдено ни одной карточки news-card — похоже, wtmobile.com снова сменил вёрстку")
            return []

        news_list = []
        # Первой картой может висеть «закреплённая» новость крупного обновления,
        # поэтому берём 4: гарантированно 3 действительно свежие.
        for card in cards[:4]:
            # 1. Ссылка на полную новость: убираем /ru/ — сохраняем чистый URL
            #    (дедупликация в БД и ссылка для читателей, как раньше)
            relative_url = card.get("href", "")
            if relative_url.startswith("/ru/"):
                relative_url = relative_url[3:]
            full_url = _abs_url(relative_url)

            # 2. Заголовок
            title_el = card.find("h2", class_="heading")
            title = title_el.get_text(" ", strip=True) if title_el else "Без названия"

            # 3. Краткое описание
            desc_el = card.find("p", class_="news-description")
            description = desc_el.get_text(" ", strip=True) if desc_el else ""

            # 4. Категория/тег новости — <p> перед датой <time> внизу карточки
            time_el = card.find("time")
            tag_el = time_el.find_previous_sibling("p") if time_el else None
            tag = tag_el.get_text(" ", strip=True) if tag_el else "Новость"

            # 5. Обложка-превью
            img_url = _picture_url(card.find("picture", class_="news-art"))

            news_list.append({
                "url": full_url,
                "title": title,
                "description": description,
                "tag": tag,
                "image_url": img_url
            })

        # Рутинная строка «распарсено N новостей» убрана — каждые 10 минут она
        # захламляла лог (контракт стиля в logging_setup.py: только события).
        return news_list

    except requests.RequestException as e:
        logger.error("⚠️ Не удалось скачать список новостей wtmobile.com: %s", e)
        return []
    except Exception as e:
        logger.error("⚠️ Не удалось разобрать список новостей: %s", e)
        return []


# ───────────────────────────────────────────────
#  Полный текст статьи (для качественной сводки моделью)
# ───────────────────────────────────────────────

ARTICLE_MAX_CHARS = 10000  # ограничение длины текста статьи, уходящего в модель (предохранитель от аномально длинных страниц)

# Блоки статьи, которые НЕ вытягиваем в сводку (язык запроса зафиксирован
# русским через префикс /ru/ в URL, поэтому маркеры — только русские).
# Сверка по началу текста блока в нижнем регистре.
# ⚠️ С 2026-07-20 блок вырезается ЦЕЛИКОМ до следующего заголовка ##/###,
# а не только заголовок+список — иначе абзацы между ними оставались.
_DROP_PREFIXES = (
    "премиумная версия",           # заголовок «Премиумная версия USS …» и абзац «Премиумная версия техники:»
    "премиумный",                  # «Премиумный ♠…» (мужской род; самолёты, танки)
    "в корабельной кампании",
    "отличия премиумной версии",
    "стандартная версия техники",
    "*характеристики",
    "способы получения",           # «Способы получения», «Способы получения чертежей:»
)
# Маркеры, сразу за которыми идёт лишний список — его тоже пропускаем.
# Оставлены для обратной совместимости; с переходом на skip_until_heading
# списки вырезаются вместе со всей секцией.
_DROP_WITH_LIST = (
    "отличия премиумной версии",
    "премиумная версия техники",
    "стандартная версия техники",
    "способы получения",
)


def _clean_text(t: str) -> str:
    """Приводит текст в порядок: убирает пробел перед пунктуацией и двойные пробелы."""
    t = re.sub(r"\s+([:,;.!?])", r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _extract_article_body(soup: BeautifulSoup, max_chars: int = ARTICLE_MAX_CHARS):
    """
    Извлекает из статьи wtmobile.com структурированный текст и картинки.

    Оставляем: заголовки секций (## / ###), абзацы, маркированные списки и
    заголовки спойлеров («Способы получения»). Вырезаем: шапку сайта, блок
    «предыдущая/следующая новость» (они вне news-container) и блоки
    премиум/стандарт версий (_DROP_PREFIXES).

    Возвращает кортеж (text, stat_images, main_image):
      text        — чистый текст статьи с разметкой # / ## / ### / - ;
      stat_images — ссылки на карточки характеристик ('stat' в имени файла);
      main_image  — обложка-баннер статьи (picture.keyvisual-outlet, 1440×640).
    """
    body = soup.find("div", class_="news-container")
    if not body:
        return "", [], ""

    lines = []
    # Заголовок статьи — первый h2.heading внутри <main>: он стоит в шапке
    # статьи, ДО news-container, поэтому в body его нет. Ставим первой
    # строкой (#), чтобы модель видела заголовок и выносила его в начало сводки.
    main_el = soup.find("main") or soup
    h1 = main_el.find("h2", class_="heading")
    if h1:
        h1_txt = _clean_text(h1.get_text(" ", strip=True))
        if h1_txt:
            lines.append("# " + h1_txt)

    skip_until_heading = False  # True = молча пропускаем всё до следующего ##/###
    intro_line = ""  # строка «<техника> можно будет получить в WTM …» — выносим в начало
    # find_all сохраняет порядок документа, поэтому заголовки, абзацы, списки и
    # содержимое спойлеров идут в правильной последовательности.
    for el in body.find_all(["h2", "h3", "p", "ul", "summary"]):
        cls = el.get("class") or []

        # Текст заголовка спойлера продублирован абзацем ВНУТРИ <summary> —
        # берём только сам summary, вложенный дубль пропускаем.
        if el.name != "summary" and el.find_parent("summary") is not None:
            continue

        # Определяем тип блока по классам новой вёрстки; чужие элементы мимо.
        is_heading = el.name == "h2" and "heading" in cls
        is_subheading = el.name == "h3" and "subheading" in cls
        is_paragraph = el.name == "p" and "paragraph" in cls
        is_list = el.name == "ul" and "ulist" in cls
        is_spoiler_header = el.name == "summary"
        if not (is_heading or is_subheading or is_paragraph or is_list or is_spoiler_header):
            continue

        low = _clean_text(el.get_text(" ", strip=True)).lower()

        # Заголовок h2/h3/summary: прекращает пропуск секции (если сам не под маркером)
        if is_heading or is_subheading or is_spoiler_header:
            if any(low.startswith(p) for p in _DROP_PREFIXES):
                skip_until_heading = True
                continue
            skip_until_heading = False
            txt = _clean_text(el.get_text(" ", strip=True))
            if txt:
                if is_subheading:
                    lines.append("### " + txt)
                else:
                    lines.append("## " + txt)
            continue

        if skip_until_heading:
            continue

        if is_list:
            items = [_clean_text(li.get_text(" ", strip=True)) for li in el.find_all("li")]
            block = "\n".join(f"- {i}" for i in items if i)
            if block:
                lines.append(block)
            continue

        # Абзац — проверяем на маркеры вырезания
        if any(low.startswith(p) for p in _DROP_PREFIXES):
            skip_until_heading = True
            continue

        txt = _clean_text(el.get_text(" ", strip=True))
        if not txt:
            continue

        # Строку «<техника> можно будет получить в War Thunder Mobile (WTM) …»
        # (обычно стоит в самом конце) выносим в начало — сразу после заголовка.
        if (not intro_line and "можно будет получить" in low
                and ("war thunder mobile" in low or "wtm" in low)):
            intro_line = txt
            continue
        lines.append(txt)

    # Вынесенную строку ставим сразу после заголовка (#); если заголовка нет — в начало.
    if intro_line:
        pos = 1 if (lines and lines[0].startswith("# ")) else 0
        lines.insert(pos, intro_line)

    text = "\n\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n\n", 1)[0] + "\n\n[…]"

    # Главная картинка — hero-баннер статьи: <picture class="keyvisual-outlet">
    # в шапке, вне news-container. Это широкая обложка новости (1440×640) —
    # крупнее и правильнее, чем мелкое превью-800×580 из списка.
    main_image = _picture_url(soup.find("picture", class_="keyvisual-outlet"))

    # Карточки ТТХ — картинки статьи с 'stat' в имени файла (проверяем ТОЛЬКО
    # имя — в домене static.wtmobile.com подстрока 'stat' тоже встречается).
    # Язык совпадает с версией страницы: на /ru/ это stat_ru. Первая в списке —
    # обычная карточка ТТХ (prem_stat — отдельная премиум-карточка, идёт позже).
    stat_images = []
    for pic in body.find_all("picture"):
        src = _picture_url(pic)
        if not src:
            continue
        fname = src.rsplit("/", 1)[-1].lower()
        if "stat" in fname and src not in stat_images:
            stat_images.append(src)

    return text, stat_images, main_image


def fetch_article(url: str) -> dict:
    """
    Скачивает полную статью по ссылке и возвращает словарь:
      {"text": <структурированный текст>, "stat_images": [<url>, ...],
       "main_image": <url обложки>}

    При любой ошибке возвращает пустые значения — вызывающий код тогда
    использует короткий анонс (description) как раньше.
    """
    # Парсим русскую версию статьи: префикс /ru/ надёжно фиксирует язык, даёт
    # русские картинки (карточки ТТХ stat_ru), а маркеры в _extract_article_body
    # рассчитаны на русский. Исходный url (ссылка для читателей) не меняется.
    ru_url = re.sub(r"(https?://wtmobile\.com)(/news)", r"\1/ru\2", url, count=1)
    try:
        logger.info("📰 Загружаю полную статью: %s", ru_url)
        response = _http().get(ru_url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        text, stat_images, main_image = _extract_article_body(soup)
        if not text:
            logger.warning("⚠️ Из статьи %s не удалось извлечь текст (news-container не найден или пуст)", ru_url)
        logger.info("📰 Статья разобрана: %d символов текста, карточек ТТХ: %d, главная картинка: %s",
                    len(text), len(stat_images), "да" if main_image else "нет")
        return {"text": text, "stat_images": stat_images, "main_image": main_image}
    except requests.RequestException as e:
        logger.error("⚠️ Не удалось скачать статью %s: %s", url, e)
    except Exception as e:
        logger.error("⚠️ Не удалось разобрать статью %s: %s", url, e)
    return {"text": "", "stat_images": [], "main_image": ""}
