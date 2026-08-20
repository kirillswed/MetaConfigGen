from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

WIKI_USER_AGENT = (
    "MetaAdsLocalizer/1.0 (local Excel helper; contact via local CLI; "
    "Wikipedia/Wikidata lookups)"
)
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
REQUEST_GAP_SECONDS = 0.05
MAX_HTTP_RETRIES = 3
MAX_429_WAIT_SECONDS = 2.0

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
    candidates = list(FALLBACK_PRODUCTS)
    random.shuffle(candidates)
    used_titles: set[str] = set()
    for language in languages:
        page = _random_page_for_language(language, candidates, used_titles)
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


def _random_page_for_language(
    language: str,
    candidates: list[str],
    used_titles: set[str],
) -> WikiPage:
    wiki_code = wiki_code_for_language(language)
    tried = 0
    for title in list(candidates):
        if title.casefold() in used_titles:
            continue
        tried += 1
        page = _fetch_page(wiki_code, language, title)
        if page is not None:
            candidates.remove(title)
            return page
        if tried >= 4:
            break
    raise WikiLookupError(f"Could not find a random Wikipedia dish for {language}")


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
            time.sleep(min(2 ** attempt, 2))
            continue
        if response.status_code == 429:
            wait_s = _retry_after_seconds(response, attempt)
            logger.warning("Wikipedia/Wikidata 429, waiting %ss...", wait_s)
            time.sleep(wait_s)
            last_error = requests.HTTPError(f"429 Too Many Requests: {response.url}")
            continue
        if response.status_code in {500, 502, 503, 504}:
            time.sleep(min(2 ** attempt, 2))
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
    wait_s = float(min(2 ** attempt, MAX_429_WAIT_SECONDS))
    if header:
        try:
            wait_s = min(float(header), MAX_429_WAIT_SECONDS)
        except ValueError:
            pass
    return max(wait_s, 0.2)


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
