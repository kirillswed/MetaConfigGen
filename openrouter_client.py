from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import requests

from models import LocalizationResponse, RegionCodeResponse

logger = logging.getLogger(__name__)

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
SAFETY_PREFIX_RE = re.compile(
    r"(?is)^\s*(?:user\s+safety\s*:\s*\w+\s*)+"
)


class OpenRouterError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_retries: int = 10,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "").strip()
        self.model = (model or os.getenv("OPENROUTER_MODEL") or "openrouter/free").strip()
        self.base_url = (
            base_url or os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
        ).rstrip("/")
        self.timeout = timeout or int(os.getenv("OPENROUTER_TIMEOUT", "120"))
        self.max_retries = max_retries
        self.reasoning_enabled = _env_flag("OPENROUTER_REASONING", default=False)

        if not self.api_key:
            raise OpenRouterError(
                "OPENROUTER_API_KEY is missing. Put it in a .env file and do not hardcode it."
            )

    def generate_localizations(
        self,
        languages: list[str],
        product: str,
        wiki_pages: list[dict[str, str]],
        geo: str | None = None,
    ) -> LocalizationResponse:
        payload = self._build_payload(languages, product, wiki_pages, geo)
        logger.info("Sending request to OpenRouter...")
        data = self._complete_json(payload)
        try:
            parsed = LocalizationResponse.model_validate(data)
        except Exception as exc:
            raise OpenRouterError(f"JSON does not match the required schema: {exc}") from exc
        logger.info("Received %s localizations", len(parsed.localizations))
        return parsed

    def resolve_region_code(self, region: str) -> str:
        value = region.strip()
        if len(value) == 2 and value.isalpha():
            return value.upper()

        logger.info("Resolving region %r to ISO country code...", region)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You map a country or region name to its ISO 3166-1 alpha-2 code.\n"
                        "Return ONLY valid JSON: {\"iso_code\": \"TR\", \"country\": \"Turkey\"}.\n"
                        "iso_code must be exactly 2 Latin letters.\n"
                        "If the input is already an ISO code, return it uppercased.\n"
                        "If it is not a country/region, still pick the closest sovereign country code."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"region": value}, ensure_ascii=False),
                },
            ],
            "temperature": 0,
        }
        if self.reasoning_enabled:
            payload["reasoning"] = {"enabled": True}
        data = self._complete_json(payload)
        try:
            parsed = RegionCodeResponse.model_validate(data)
        except Exception as exc:
            raise OpenRouterError(f"Region JSON is invalid: {exc}") from exc
        logger.info("Region %r -> %s", region, parsed.iso_code)
        return parsed.iso_code

    def generate_one_object(self, language: str, used_names: list[str]) -> str:
        logger.info("Generating a physical object for %s...", language)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Reply with ONE English Wikipedia article title for a tangible physical object.\n"
                        "It must be something you can touch. Not food, not a place, not a person, not an animal.\n"
                        "Do not translate an object that was already used.\n"
                        "Return only the word or short name. No JSON, no sentence, no safety labels."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "language": language,
                            "already_used": used_names,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 1.0,
        }
        if self.reasoning_enabled:
            payload["reasoning"] = {"enabled": True}
        raw = self._post_chat(payload, require_json=False)
        name = _parse_object_name(raw)
        if not name:
            raise OpenRouterError("Model did not return an object name")
        if name.casefold() in {item.casefold() for item in used_names}:
            raise OpenRouterError(f"Model repeated object {name!r}")
        logger.info("Generated object for %s: %s", language, name)
        return name

    def _build_payload(
        self,
        languages: list[str],
        product: str,
        wiki_pages: list[dict[str, str]],
        geo: str | None,
    ) -> dict[str, Any]:
        system_prompt = (
            "You write Meta Ads creative copy from Wikipedia articles about physical objects.\n"
            "Each language already has a Wikipedia title, extract, and URL.\n"
            "Write a native ad title and body for that object using ONLY facts from that extract.\n"
            "Do not invent features, discounts, prices, or claims.\n"
            "Do not change the Wikipedia URL. Return the exact URL from the wiki page.\n"
            "Never write safety labels like 'User Safety: safe'.\n"
            "Return ONLY valid JSON. No markdown, no comments, no extra keys."
        )
        user_payload = {
            "geo": geo or "",
            "product": product,
            "languages": languages,
            "wikipedia": wiki_pages,
            "instructions": {
                "count": len(languages),
                "order": "Return localizations in the same order as languages.",
                "json_schema": {
                    "localizations": [
                        {
                            "language": "exactly one of the requested language names",
                            "title": "ad title based on the Wikipedia article title/extract",
                            "body": "ad body based on the Wikipedia extract",
                            "link": "the Wikipedia URL for that language, unchanged",
                        }
                    ]
                },
            },
        }
        body_obj: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Create native advertising localizations from this source data. "
                        "The number of objects in localizations MUST equal the number of languages.\n\n"
                        + json.dumps(user_payload, ensure_ascii=False, indent=2)
                    ),
                },
            ],
            "temperature": 0.4,
        }
        if self.reasoning_enabled:
            body_obj["reasoning"] = {"enabled": True}
        return body_obj

    def _post_chat(self, payload: dict[str, Any], require_json: bool = True) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        referer = os.getenv("OPENROUTER_HTTP_REFERER", "").strip()
        title = os.getenv("OPENROUTER_APP_TITLE", "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=self.timeout,
                )
                if response.status_code == 429:
                    wait_s = _rate_limit_wait(attempt)
                    logger.warning("OpenRouter rate limit. Retrying in %ss...", wait_s)
                    time.sleep(wait_s)
                    continue
                if response.status_code in {500, 502, 503, 504}:
                    wait_s = _rate_limit_wait(attempt)
                    logger.warning(
                        "OpenRouter HTTP %s. Retrying in %ss...",
                        response.status_code,
                        wait_s,
                    )
                    time.sleep(wait_s)
                    continue
                if not response.ok:
                    raise OpenRouterError(
                        f"OpenRouter HTTP {response.status_code}: {_safe_error_text(response)}"
                    )
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise OpenRouterError("OpenRouter response has no choices")
                content = _message_text(choices[0].get("message") or {})
                if not content.strip():
                    logger.warning(
                        "Empty model content (attempt %s/%s)",
                        attempt,
                        self.max_retries,
                    )
                    time.sleep(_rate_limit_wait(attempt))
                    last_error = OpenRouterError("OpenRouter returned empty message content")
                    continue
                if require_json:
                    try:
                        _extract_json_text(content)
                    except OpenRouterError as exc:
                        logger.warning(
                            "Non-JSON model output (attempt %s/%s): %s",
                            attempt,
                            self.max_retries,
                            exc,
                        )
                        time.sleep(_rate_limit_wait(attempt))
                        last_error = exc
                        continue
                return content
            except OpenRouterError:
                raise
            except requests.Timeout as exc:
                last_error = exc
                logger.warning("OpenRouter timeout (attempt %s/%s)", attempt, self.max_retries)
                time.sleep(_rate_limit_wait(attempt))
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("OpenRouter request error: %s", exc)
                time.sleep(_rate_limit_wait(attempt))
            except ValueError as exc:
                raise OpenRouterError(f"OpenRouter returned invalid JSON: {exc}") from exc

        raise OpenRouterError(f"OpenRouter request failed after retries: {last_error}")

    def _complete_json(self, payload: dict[str, Any]) -> Any:
        return self._loads_json(self._post_chat(payload))

    def _parse_content(self, content: str) -> LocalizationResponse:
        data = self._loads_json(content)
        try:
            return LocalizationResponse.model_validate(data)
        except Exception as exc:
            raise OpenRouterError(f"JSON does not match the required schema: {exc}") from exc

    def _loads_json(self, content: str) -> Any:
        text = _extract_json_text(content)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            try:
                return json.JSONDecoder().raw_decode(text)[0]
            except json.JSONDecodeError as exc:
                raise OpenRouterError(
                    f"Model did not return valid JSON: {exc}. Raw content starts with: {content[:300]!r}"
                ) from exc


def _message_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
    for key in ("reasoning", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def _extract_json_text(content: str) -> str:
    text = SAFETY_PREFIX_RE.sub("", content.strip()).strip()
    fenced = JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if text.startswith("{") or text.startswith("["):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    raise OpenRouterError(
        f"Model did not return JSON. Raw content starts with: {content[:300]!r}"
    )


def _parse_object_name(raw: str) -> str:
    text = SAFETY_PREFIX_RE.sub("", raw.strip()).strip()
    try:
        data = json.JSONDecoder().raw_decode(_extract_json_text(text))[0]
        if isinstance(data, dict):
            text = str(data.get("name") or data.get("object") or "").strip()
        elif isinstance(data, str):
            text = data.strip()
    except Exception:
        pass
    line = text.splitlines()[0].strip().strip("`\"'")
    line = re.sub(r"^(name|object)\s*[:=]\s*", "", line, flags=re.I)
    words = [part for part in re.split(r"\s+", line) if part]
    name = " ".join(words[:3]).strip(" .,;:")
    if not name or not any(ch.isalpha() for ch in name):
        return ""
    if not all(ord(ch) < 128 for ch in name):
        return ""
    return name[:1].upper() + name[1:]


def _rate_limit_wait(attempt: int) -> float:
    return 2.0 * attempt


def _safe_error_text(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if err:
                return str(err)
            return json.dumps(data, ensure_ascii=False)[:500]
    except ValueError:
        pass
    return (response.text or "")[:500]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
