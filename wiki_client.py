from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

WIKI_USER_AGENT = "MetaAdsLocalizer/1.0 (local Excel helper; Wikipedia/Wikidata lookup)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Meta Ads language names -> Wikipedia language codes
LANGUAGE_TO_WIKI = {
    "afrikaans": "af",
    "albanian": "sq",
    "arabic": "ar",
    "armenian": "hy",
    "azerbaijani": "az",
    "basque": "eu",
    "belarusian": "be",
    "bengali": "bn",
    "bosnian": "bs",
    "bulgarian": "bg",
    "catalan": "ca",
    "chinese": "zh",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "galician": "gl",
    "georgian": "ka",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "icelandic": "is",
    "indonesian": "id",
    "irish": "ga",
    "italian": "it",
    "japanese": "ja",
    "kazakh": "kk",
    "korean": "ko",
    "latvian": "lv",
    "lithuanian": "lt",
    "macedonian": "mk",
    "malay": "ms",
    "norwegian": "no",
    "persian": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "serbian": "sr",
    "slovak": "sk",
    "slovenian": "sl",
    "spanish": "es",
    "swedish": "sv",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "urdu": "ur",
    "uzbek": "uz",
    "vietnamese": "vi",
}


class WikiLookupError(RuntimeError):
    pass


@dataclass
class WikiPage:
    language: str
    wiki_code: str
    title: str
    extract: str
    url: str


FALLBACK_PRODUCTS = [
    "Shawarma",
    "Khachapuri",
    "Pizza",
    "Sushi",
    "Paella",
    "Pho",
    "Tacos",
    "Borscht",
    "Pad Thai",
    "Ceviche",
    "Falafel",
    "Ramen",
    "Moussaka",
    "Poutine",
    "Kimchi",
    "Goulash",
    "Empanada",
    "Biryani",
    "Feijoada",
    "Bobotie",
]


def is_random_command(value: str) -> bool:
    return value.strip().lower() == "random"


def pick_random_product() -> str:
    titles = _category_product_titles()
    pool = titles or FALLBACK_PRODUCTS
    product = random.choice(pool)
    logger.info("Random product: %s", product)
    return product


def _category_product_titles() -> list[str]:
    titles: list[str] = []
    try:
        session = _session()
        for category in ("Category:National dishes", "Category:Street food"):
            response = session.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": category,
                    "cmtype": "page",
                    "cmlimit": 200,
                    "format": "json",
                },
                timeout=30,
            )
            response.raise_for_status()
            members = (response.json().get("query") or {}).get("categorymembers") or []
            for member in members:
                title = str(member.get("title") or "").strip()
                if not title or title.startswith("List of") or title.startswith("Category:"):
                    continue
                titles.append(title)
    except requests.RequestException as exc:
        logger.warning("Could not load Wikipedia food categories: %s", exc)
        return []
    unique = list(dict.fromkeys(titles))
    return unique


def lookup_product_pages(product: str, languages: list[str]) -> list[WikiPage]:
    query = product.strip()
    if not query:
        raise WikiLookupError("Product name is empty")

    logger.info("Searching Wikipedia/Wikidata for %r", query)
    try:
        entity_id = _search_wikidata_entity(query)
        sitelinks = _entity_sitelinks(entity_id) if entity_id else {}
        if entity_id:
            logger.info("Wikidata entity: %s", entity_id)
    except requests.RequestException as exc:
        raise WikiLookupError(f"Wikidata request failed: {exc}") from exc

    pages: list[WikiPage] = []
    missing: list[str] = []
    for language in languages:
        wiki_code = wiki_code_for_language(language)
        page_title = sitelinks.get(f"{wiki_code}wiki")
        try:
            page = _fetch_page(wiki_code, language, page_title or query)
        except requests.RequestException as exc:
            raise WikiLookupError(
                f"Wikipedia request failed for {language}: {exc}"
            ) from exc
        if page is None:
            missing.append(f"{language} ({wiki_code}.wikipedia.org)")
            continue
        logger.info("Found %s: %s", language, page.url)
        pages.append(page)

    if missing:
        raise WikiLookupError(
            "No Wikipedia article for: " + ", ".join(missing)
        )
    return pages


def wiki_code_for_language(language: str) -> str:
    key = language.strip().lower()
    if key in LANGUAGE_TO_WIKI:
        return LANGUAGE_TO_WIKI[key]
    raise WikiLookupError(
        f"Unknown language {language!r}. Use a Wikipedia language name, e.g. Spanish, English, Portuguese."
    )


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": WIKI_USER_AGENT})
    return session


def _search_wikidata_entity(query: str) -> str | None:
    response = _session().get(
        WIKIDATA_API,
        params={
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "type": "item",
            "limit": 5,
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    hits = response.json().get("search") or []
    if not hits:
        return None
    return str(hits[0]["id"])


def _entity_sitelinks(entity_id: str) -> dict[str, str]:
    response = _session().get(
        WIKIDATA_API,
        params={
            "action": "wbgetentities",
            "ids": entity_id,
            "props": "sitelinks",
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    entity = (response.json().get("entities") or {}).get(entity_id) or {}
    raw = entity.get("sitelinks") or {}
    titles: dict[str, str] = {}
    for site, payload in raw.items():
        title = (payload or {}).get("title")
        if site.endswith("wiki") and title:
            titles[site] = title
    return titles


def _fetch_page(wiki_code: str, language: str, title: str) -> WikiPage | None:
    response = _session().get(
        f"https://{wiki_code}.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "inprop": "url",
            "redirects": 1,
            "titles": title,
            "format": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    pages = ((response.json().get("query") or {}).get("pages") or {}).values()
    for page in pages:
        if page.get("missing") is not None:
            continue
        extract = _shorten_extract(str(page.get("extract") or "").strip())
        page_title = str(page.get("title") or "").strip()
        url = str(page.get("fullurl") or "").strip()
        if not page_title or not extract or not url:
            continue
        return WikiPage(
            language=language,
            wiki_code=wiki_code,
            title=page_title,
            extract=extract,
            url=url,
        )
    return None


def _shorten_extract(text: str, max_len: int = 700) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_len:
        return cleaned
    cut = cleaned[:max_len]
    period = cut.rfind(".")
    if period >= 120:
        return cut[: period + 1]
    return cut.rstrip() + "..."
