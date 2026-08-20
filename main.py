from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from excel_handler import (
    ExcelTemplateError,
    backup_workbook,
    load_template,
    normalize_region_code,
    save_workbook,
    write_localizations,
    write_region,
)
from models import Localization
from openrouter_client import OpenRouterClient, OpenRouterError
from validator import (
    ValidationError,
    assert_language_slots,
    validate_languages,
    validate_localizations,
    validate_output_workbook,
)
from wiki_client import WikiLookupError, lookup_random_pages_per_language

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("meta_ads_localizer")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill Meta Ads Bulk Import Excel localizations via OpenRouter."
    )
    parser.add_argument("template", type=Path, help="Path to the source .xlsx template")
    parser.add_argument(
        "--languages",
        help='Comma-separated languages, e.g. "Spanish,English,Portuguese"',
    )
    parser.add_argument(
        "--geo",
        help="Region/country for Special Ad Category Country (D2), e.g. PE or Peru",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("localized_result.xlsx"),
        help="Output xlsx path (default: localized_result.xlsx)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting the original template file",
    )
    return parser.parse_args(argv)


def parse_languages(raw: str) -> list[str]:
    return validate_languages([part.strip() for part in raw.split(",")])


def prompt_languages() -> list[str]:
    raw = input("Enter languages separated by comma:\n").strip()
    return parse_languages(raw)


def prompt_required(label: str) -> str:
    value = input(f"{label}:\n").strip()
    if not value:
        raise ValidationError(f"{label} cannot be empty")
    return value


def collect_region(args: argparse.Namespace) -> str:
    if args.geo and args.geo.strip():
        return args.geo.strip()
    if sys.stdin.isatty():
        return prompt_required("Enter region")
    raise ValidationError("Pass --geo when running without an interactive terminal")


def collect_languages(args: argparse.Namespace) -> list[str]:
    if args.languages:
        return parse_languages(args.languages)
    if sys.stdin.isatty():
        return prompt_languages()
    raise ValidationError("Pass --languages when running without an interactive terminal")


def _load_env() -> None:
    load_dotenv()
    script_env = Path(__file__).resolve().parent / ".env"
    if script_env.exists():
        load_dotenv(script_env, override=False)


def _require_api_key() -> None:
    if os.getenv("OPENROUTER_API_KEY", "").strip():
        return
    env_files = [Path(".env"), Path(__file__).resolve().parent / ".env"]
    if not any(path.exists() for path in env_files):
        raise ValidationError(
            "Missing .env file. Copy .env.example to .env and set OPENROUTER_API_KEY."
        )
    raise ValidationError("OPENROUTER_API_KEY is missing. Set it in the .env file.")


def main(argv: list[str] | None = None) -> int:
    _load_env()
    try:
        args = parse_args(argv)
        template_path = args.template.expanduser().resolve()
        if not template_path.exists():
            raise ExcelTemplateError(f"File not found: {template_path}")
        if template_path.suffix.lower() != ".xlsx":
            raise ExcelTemplateError("Only .xlsx templates are supported")

        output_path = args.output.expanduser().resolve()
        if output_path == template_path and not args.overwrite:
            raise ExcelTemplateError(
                "Refusing to overwrite the original file. Pass --output or --overwrite."
            )

        _require_api_key()
        client = OpenRouterClient()
        languages = collect_languages(args)
        logger.info("Languages: %s", ", ".join(languages))
        region_input = collect_region(args)
        region_code = normalize_region_code(client.resolve_region_code(region_input))
        logger.info("Region: %s -> %s", region_input, region_code)

        logger.info("Picking a random Wikipedia dish per language...")
        wiki_pages = lookup_random_pages_per_language(languages)
        product = "; ".join(f"{page.language}: {page.title}" for page in wiki_pages)
        wiki_payload = [
            {
                "language": page.language,
                "title": page.title,
                "extract": page.extract,
                "link": page.url,
            }
            for page in wiki_pages
        ]
        wiki_urls = {page.language: page.url for page in wiki_pages}

        backup_workbook(template_path)
        info = load_template(template_path)
        if not info.data_rows:
            raise ExcelTemplateError("No data rows found under the header row")

        pending: list[tuple[int, list[Localization]]] = []

        for row in info.data_rows:
            logger.info("Processing row %s", row)
            geo = region_input
            raw = client.generate_localizations(
                languages=languages,
                product=product,
                wiki_pages=wiki_payload,
                geo=geo,
            )
            checked = validate_localizations(raw, languages, wiki_urls)
            logger.info("Validation passed")
            pending.append((row, checked))

        for row, localizations in pending:
            write_localizations(info, row, languages, localizations)
            write_region(info, row, region_code)

        save_workbook(info, output_path)
        try:
            validate_output_workbook(template_path, output_path, [row for row, _ in pending])
            for row, _ in pending:
                assert_language_slots(output_path, row, languages)
        except Exception:
            if output_path.exists() and output_path != template_path:
                output_path.unlink()
            raise
        return 0
    except FileNotFoundError as exc:
        if ".env" in str(exc).lower():
            logger.error("Missing .env file. Copy .env.example to .env and set OPENROUTER_API_KEY.")
        else:
            logger.error("%s", exc)
        return 1
    except (ValidationError, ExcelTemplateError, OpenRouterError, WikiLookupError) as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 1


if __name__ == "__main__":
    sys.exit(main())
