from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

WIKI_USER_AGENT = (
    "MetaAdsLocalizer/1.0 (local Excel helper; contact via local CLI; "
    "Wikipedia/Wikidata lookups)"
)
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
REQUEST_GAP_SECONDS = 0.5
MAX_HTTP_RETRIES = 5

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

DISH_CATEGORIES = {
    "ar": ["تصنيف:أطباق"],
    "de": ["Kategorie:Nationalgericht"],
    "en": ["Category:National dishes", "Category:Street food"],
    "es": ["Categoría:Platos"],
    "fr": ["Catégorie:Plat"],
    "it": ["Categoria:Piatti"],
    "ja": ["Category:各国の料理"],
    "ko": ["분류:나라별 요리"],
    "pl": ["Kategoria:Potrawy narodowe"],
    "pt": ["Categoria:Pratos"],
    "ru": ["Категория:Национальные блюда"],
    "tr": ["Kategori:Yemekler"],
    "uk": ["Категорія:Національні страви"],
    "zh": ["Category:各国菜肴"],
}

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


class WikiLookupError(RuntimeError):
    pass


@dataclass
class WikiPage:
    language: str
    wiki_code: str
    title: str
    extract: str
    url: str


_SESSION: requests.Session | None = None
_LAST_REQUEST_AT = 0.0


def is_random_command(value: str) -> bool:
    return value.strip().lower() == "random"


def lookup_random_pages_per_language(languages: list[str]) -> list[WikiPage]:
    pages: list[WikiPage] = []
    used_titles: set[str] = set()
    for language in languages:
        page = _random_page_for_language(language, used_titles)
        used_titles.add(page.title.casefold())
        logger.info("Random %s product: %s (%s)", language, page.title, page.url)
        pages.append(page)
    return pages


def lookup_product_pages(product: str, languages: list[str]) -> list[WikiPage]:
    query = product.strip()
    if not query:
        raise WikiLookupError("Product name is empty")

    logger.info("Searching Wikipedia/Wikidata for %r", query)
    entity_id = _search_wikidata_entity(query)
    sitelinks = _entity_sitelinks(entity_id) if entity_id else {}
    if entity_id:
        logger.info("Wikidata entity: %s", entity_id)

    pages: list[WikiPage] = []
    missing: list[str] = []
    for language in languages:
        wiki_code = wiki_code_for_language(language)
        page_title = sitelinks.get(f"{wiki_code}wiki")
        page = _fetch_page(wiki_code, language, page_title or query)
        if page is None and page_title:
            page = _fetch_page(wiki_code, language, query)
        if page is None:
            missing.append(f"{language} ({wiki_code}.wikipedia.org)")
            continue
        logger.info("Found %s: %s", language, page.url)
        pages.append(page)

    if missing:
        raise WikiLookupError("No Wikipedia article for: " + ", ".join(missing))
    return pages


def wiki_code_for_language(language: str) -> str:
    key = language.strip().lower()
    if key in LANGUAGE_TO_WIKI:
        return LANGUAGE_TO_WIKI[key]
    raise WikiLookupError(
        f"Unknown language {language!r}. Use a Wikipedia language name, e.g. Spanish, English, Portuguese."
    )


def _random_page_for_language(language: str, used_titles: set[str]) -> WikiPage:
    wiki_code = wiki_code_for_language(language)
    candidates = list(_category_titles(wiki_code)) or list(FALLBACK_PRODUCTS)
    random.shuffle(candidates)
    for title in candidates:
        if title.casefold() in used_titles:
            continue
        page = _fetch_page(wiki_code, language, title)
        if page is None:
            hit = _search_title(wiki_code, title)
            if hit:
                page = _fetch_page(wiki_code, language, hit)
        if page is not None:
            return page
    raise WikiLookupError(f"Could not find a random Wikipedia dish for {language}")


@lru_cache(maxsize=32)
def _category_titles(wiki_code: str) -> tuple[str, ...]:
    titles: list[str] = []
    for category in DISH_CATEGORIES.get(wiki_code, DISH_CATEGORIES["en"]):
        try:
            data = _api_get(
                f"https://{wiki_code}.wikipedia.org/w/api.php",
                {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": category,
                    "cmtype": "page",
                    "cmlimit": 100,
                    "format": "json",
                },
            )
        except WikiLookupError as exc:
            logger.warning("Category %s on %s failed: %s", category, wiki_code, exc)
            continue
        members = (data.get("query") or {}).get("categorymembers") or []
        for member in members:
            title = str(member.get("title") or "").strip()
            if not title or title.startswith(("List of", "Lista", "Категория:", "Category:", "Categoría:")):
                continue
            titles.append(title)
    unique = tuple(dict.fromkeys(titles))
    if unique:
        logger.info("Loaded %s dish pages from %s.wikipedia.org", len(unique), wiki_code)
    return unique


def _search_title(wiki_code: str, query: str) -> str | None:
    data = _api_get(
        f"https://{wiki_code}.wikipedia.org/w/api.php",
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": 1,
            "srnamespace": 0,
            "format": "json",
        },
    )
    hits = (data.get("query") or {}).get("search") or []
    if not hits:
        return None
    return str(hits[0].get("title") or "").strip() or None


def _search_wikidata_entity(query: str) -> str | None:
    data = _api_get(
        WIKIDATA_API,
        {
            "action": "wbsearchentities",
            "search": query,
            "language": "en",
            "type": "item",
            "limit": 5,
            "format": "json",
        },
    )
    hits = data.get("search") or []
    if not hits:
        return None
    return str(hits[0]["id"])


def _entity_sitelinks(entity_id: str) -> dict[str, str]:
    data = _api_get(
        WIKIDATA_API,
        {
            "action": "wbgetentities",
            "ids": entity_id,
            "props": "sitelinks",
            "format": "json",
        },
    )
    entity = (data.get("entities") or {}).get(entity_id) or {}
    raw = entity.get("sitelinks") or {}
    titles: dict[str, str] = {}
    for site, payload in raw.items():
        title = (payload or {}).get("title")
        if site.endswith("wiki") and title:
            titles[site] = title
    return titles


def _fetch_page(wiki_code: str, language: str, title: str) -> WikiPage | None:
    data = _api_get(
        f"https://{wiki_code}.wikipedia.org/w/api.php",
        {
            "action": "query",
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "inprop": "url",
            "redirects": 1,
            "titles": title,
            "format": "json",
        },
    )
    pages = ((data.get("query") or {}).get("pages") or {}).values()
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


def _api_get(url: str, params: dict[str, str | int]) -> dict:
    session = _session()
    last_error: Exception | None = None
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        _pace_requests()
        try:
            response = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(min(2 ** attempt, 20))
            continue
        if response.status_code == 429:
            wait_s = _retry_after_seconds(response, attempt)
            logger.warning("Wikipedia/Wikidata 429, waiting %ss...", wait_s)
            time.sleep(wait_s)
            last_error = requests.HTTPError(f"429 Too Many Requests: {response.url}")
            continue
        if response.status_code in {500, 502, 503, 504}:
            time.sleep(min(2 ** attempt, 20))
            last_error = requests.HTTPError(f"{response.status_code}: {response.url}")
            continue
        if not response.ok:
            raise WikiLookupError(f"Wikipedia HTTP {response.status_code}: {response.url}")
        try:
            return response.json()
        except ValueError as exc:
            raise WikiLookupError(f"Wikipedia returned invalid JSON: {exc}") from exc
    raise WikiLookupError(f"Wikipedia request failed after retries: {last_error}")


def _pace_requests() -> None:
    global _LAST_REQUEST_AT
    wait = REQUEST_GAP_SECONDS - (time.monotonic() - _LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait)
    _LAST_REQUEST_AT = time.monotonic()


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(float(header), 1.0)
        except ValueError:
            pass
    return min(2 ** attempt, 30)


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({"User-Agent": WIKI_USER_AGENT})
    return _SESSION


def _shorten_extract(text: str, max_len: int = 700) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_len:
        return cleaned
    cut = cleaned[:max_len]
    period = cut.rfind(".")
    if period >= 120:
        return cut[: period + 1]
    return cut.rstrip() + "..."
