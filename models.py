from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Localization(BaseModel):
    language: str
    title: str
    body: str
    link: str = ""

    @field_validator("language", "title", "body")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if value is None or not str(value).strip():
            raise ValueError("Field cannot be empty")
        return str(value).strip()

    @field_validator("link")
    @classmethod
    def normalize_link(cls, value: str | None) -> str:
        return "" if value is None else str(value).strip()


class LocalizationResponse(BaseModel):
    localizations: list[Localization] = Field(min_length=1)


class RegionCodeResponse(BaseModel):
    iso_code: str
    country: str = ""

    @field_validator("iso_code")
    @classmethod
    def iso_alpha2(cls, value: str) -> str:
        code = str(value or "").strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise ValueError("iso_code must be a 2-letter ISO country code")
        return code
