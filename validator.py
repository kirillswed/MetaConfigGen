from __future__ import annotations

import logging
from pathlib import Path

from excel_handler import ALLOWED_COLUMNS, ExcelTemplateError, snapshot_allowed_and_rest
from models import Localization, LocalizationResponse

logger = logging.getLogger(__name__)


class ValidationError(RuntimeError):
    pass


def validate_languages(languages: list[str], max_languages: int = 8) -> list[str]:
    cleaned = [item.strip() for item in languages if item and item.strip()]
    if not cleaned:
        raise ValidationError("Provide at least one language")
    if len(cleaned) > max_languages:
        raise ValidationError(
            f"A maximum of {max_languages} languages is supported, got {len(cleaned)}"
        )
    return cleaned


def validate_source_text(product: str) -> None:
    if not product.strip():
        raise ValidationError("Product name cannot be empty")


def validate_localizations(
    response: LocalizationResponse,
    languages: list[str],
    wiki_urls: dict[str, str],
) -> list[Localization]:
    items = response.localizations
    if len(items) != len(languages):
        raise ValidationError(
            f"Expected {len(languages)} localizations, got {len(items)}"
        )

    by_language = {item.language.strip().lower(): item for item in items}
    ordered: list[Localization] = []
    for language in languages:
        key = language.strip().lower()
        item = by_language.get(key)
        if item is None:
            raise ValidationError(f"Missing localization for language: {language}")
        if not item.title.strip():
            raise ValidationError(f"Missing title for language: {language}")
        if not item.body.strip():
            raise ValidationError(f"Missing body for language: {language}")

        expected_link = wiki_urls.get(language) or wiki_urls.get(key)
        if not expected_link:
            raise ValidationError(f"Missing Wikipedia URL for language: {language}")
        if item.link.strip() != expected_link:
            logger.warning(
                "Model changed the URL for %s; restoring the Wikipedia link",
                language,
            )
        ordered.append(
            item.model_copy(update={"language": language, "link": expected_link})
        )
    return ordered


def validate_output_workbook(
    original_path: Path,
    result_path: Path,
    processed_rows: list[int],
) -> None:
    orig_headers, orig_rows, orig_cols, orig_values = snapshot_allowed_and_rest(original_path)
    res_headers, res_rows, res_cols, res_values = snapshot_allowed_and_rest(result_path)

    if res_rows != orig_rows:
        raise ExcelTemplateError(
            f"Row count changed: original={orig_rows}, result={res_rows}"
        )
    if res_cols != orig_cols:
        raise ExcelTemplateError(
            f"Column count changed: original={orig_cols}, result={res_cols}"
        )
    if res_headers != orig_headers:
        raise ExcelTemplateError("Column names or column order changed")

    header_by_col = {idx + 1: name for idx, name in enumerate(orig_headers)}
    processed = set(processed_rows)
    illegal: list[str] = []

    for (row, col), original_value in orig_values.items():
        result_value = res_values.get((row, col))
        if _same(original_value, result_value):
            continue
        column_name = header_by_col.get(col, f"COL_{col}")
        if row == 1:
            illegal.append(f"header cell changed: {column_name}")
            continue
        if row not in processed or column_name not in ALLOWED_COLUMNS:
            illegal.append(f"row {row} / {column_name}")

    if illegal:
        preview = ", ".join(illegal[:8])
        extra = "" if len(illegal) <= 8 else f" (+{len(illegal) - 8} more)"
        raise ExcelTemplateError(
            "Cells outside the allowed localization columns were changed: "
            + preview
            + extra
        )

    logger.info("Output workbook structure validation passed")


def assert_language_slots(
    result_path: Path,
    row: int,
    languages: list[str],
) -> None:
    from excel_handler import (
        BODY_COLUMNS,
        DISPLAY_LINK_COLUMNS,
        LANGUAGE_COLUMNS,
        LINK_COLUMNS,
        TITLE_COLUMNS,
        load_template,
    )

    info = load_template(result_path)
    for columns in (LANGUAGE_COLUMNS, TITLE_COLUMNS, BODY_COLUMNS, LINK_COLUMNS):
        for slot, column_name in enumerate(columns):
            value = info.worksheet.cell(row, info.headers[column_name]).value
            text = "" if value is None else str(value).strip()
            if slot < len(languages):
                if columns is LANGUAGE_COLUMNS and text != languages[slot]:
                    raise ExcelTemplateError(
                        f"{column_name} expected {languages[slot]!r}, got {text!r}"
                    )
                if columns is not LANGUAGE_COLUMNS and not text:
                    raise ExcelTemplateError(f"{column_name} should be filled")
            elif text:
                raise ExcelTemplateError(f"{column_name} should be empty, got {text!r}")

    for slot, column_name in enumerate(DISPLAY_LINK_COLUMNS):
        if column_name not in info.headers:
            continue
        value = info.worksheet.cell(row, info.headers[column_name]).value
        text = "" if value is None else str(value).strip()
        if slot < len(languages):
            if not text:
                raise ExcelTemplateError(f"{column_name} should be filled")
        elif text:
            raise ExcelTemplateError(f"{column_name} should be empty, got {text!r}")


def _same(left: object, right: object) -> bool:
    if left is None and (right is None or str(right).strip() == ""):
        return True
    if right is None and (left is None or str(left).strip() == ""):
        return True
    return left == right
