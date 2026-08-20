#!/usr/bin/env bash
# Minimal setup from GitHub: curl + Python 3.10+. Git is not required.
set -euo pipefail

REPO_ZIP="${REPO_ZIP:-https://github.com/kirillswed/MetaConfigGen/archive/refs/heads/main.zip}"
DIR="${DIR:-MetaConfigGen}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing: $1"
    exit 1
  }
}

python_bin() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  echo "Need Python 3.10+"
  exit 1
}

download_repo() {
  need curl
  local tmp zip_path
  tmp="$(mktemp -d)"
  zip_path="$tmp/repo.zip"
  echo "Downloading $REPO_ZIP"
  curl -fsSL "$REPO_ZIP" -o "$zip_path"
  "$PY" - "$zip_path" "$tmp/extracted" "$DIR" <<'PY'
import shutil
import sys
import zipfile
from pathlib import Path

zip_path = Path(sys.argv[1])
extracted = Path(sys.argv[2])
dest = Path(sys.argv[3]).resolve()
extracted.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(zip_path) as archive:
    archive.extractall(extracted)
roots = [path for path in extracted.iterdir() if path.is_dir()]
if not roots:
    raise SystemExit("Archive is empty")
src = roots[0]
if dest.exists():
    shutil.rmtree(dest)
shutil.move(str(src), str(dest))
PY
  rm -rf "$tmp"
}

repo_root() {
  if [[ -f main.py && -f requirements.txt ]]; then
    pwd
    return
  fi
  local here=""
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "$here/main.py" && -f "$here/requirements.txt" ]]; then
      echo "$here"
      return
    fi
  fi
  if [[ ! -f "$DIR/main.py" ]]; then
    download_repo
  fi
  echo "$(cd "$DIR" && pwd)"
}

write_api_key() {
  local env_file="$1"
  local key="${OPENROUTER_API_KEY:-}"
  if [[ -z "$key" && -t 0 ]]; then
    printf "OpenRouter API key (Enter to skip): "
    read -r key || true
  fi
  [[ -z "$key" ]] && return 0
  python_write_key "$env_file" "$key"
}

python_write_key() {
  local env_file="$1"
  local key="$2"
  "$PY" - "$env_file" "$key" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
key = sys.argv[2]
text = path.read_text(encoding="utf-8")
if "OPENROUTER_API_KEY=" in text:
    lines = []
    for line in text.splitlines(True):
        if line.startswith("OPENROUTER_API_KEY="):
            lines.append("OPENROUTER_API_KEY=" + key + ("\n" if line.endswith("\n") else ""))
        else:
            lines.append(line)
    path.write_text("".join(lines), encoding="utf-8")
else:
    path.write_text(text.rstrip() + "\nOPENROUTER_API_KEY=" + key + "\n", encoding="utf-8")
PY
}

need curl
PY="$(python_bin)"
ROOT="$(repo_root)"
cd "$ROOT"

"$PY" -m venv .venv
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif [[ -f .venv/Scripts/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/Scripts/activate
else
  echo "Could not activate virtualenv"
  exit 1
fi

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
fi
write_api_key .env

echo
echo "Done: $ROOT"
echo "Activate:"
if [[ -f .venv/bin/activate ]]; then
  echo "  source .venv/bin/activate"
else
  echo "  source .venv/Scripts/activate"
fi
echo "Run:"
echo "  python main.py \"example.xlsx\" --languages Spanish,English,Portuguese"
echo "If .env has no key, edit OPENROUTER_API_KEY"
