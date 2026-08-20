# Meta Ads Bulk Import Localizer

Python CLI that fills language slots in an existing Meta Ads Bulk Import `.xlsx` template. It opens the file with `openpyxl`, changes only localization cells, and writes a new workbook. The original column structure is never rebuilt.

## What the template actually contains

The file `example.xlsx` has **103 columns** on `Sheet1`. These headers exist and are used by name (not by column index):

| Slot | Language | Title | Body | Link | Display Link |
| --- | --- | --- | --- | --- | --- |
| 1 (default) | `Default Language` | `Title` | `Body` | `Link` | `Display Link` |
| 2–8 | `Additional Language 1–7` | `Additional Title 1–7` | `Additional Body 1–7` | `Additional Link 1–7` | `Additional Display Link 1–7` |

Notes from the real file:

- There is no extra creative `Link` column besides the existing `Link` header (column `AB`) plus `Additional Link 1–7`.
- `Display Link` / `Additional Display Link 1–7` also exist. If present, they are filled with the URL host and cleared for unused slots. The LLM still never invents URLs.
- `Countries` is not changed. Region is written to `Special Ad Category Country` (D2), e.g. `PE`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Put your OpenRouter key in `.env`:

```
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=openrouter/free
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

The API key is never logged.

## Run

Interactive run — the script asks for languages (if missing) and region. A random Wikipedia dish is chosen for each language. Region is written to `Special Ad Category Country` (cell D2):

```bash
python main.py "example.xlsx" --languages языки здесь
```

Then it asks:

```
Enter region:
Турция
```

Region names in any language are resolved by the model to an ISO country code and written to D2 (`TR` for Турция). Two-letter codes like `PE` are used as-is.

```bash
python main.py "example.xlsx" --languages языки здесь --geo Peru
```

The original file is copied to `*_backup.xlsx`. The result is written to `localized_result.xlsx` unless `--output` is set. The source file is not overwritten unless you pass `--overwrite`.

## Rules

- 1 to 8 languages. More than 8 languages stops the run and does not write Excel.
- Unused slots 4–8 stay empty; columns are never deleted.
- All LLM localizations are validated before any cell is written.
- If the model returns a different URL, Python restores the Wikipedia URL for that language.
- After save, the result is reopened and checked: same row count, same column count, same header names/order, no edits outside the allowed localization columns.
