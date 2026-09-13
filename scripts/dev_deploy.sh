#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LINK_BIN=true
ALLOW_LINK_BIN="${WARCRAFT_ALLOW_LINK_BIN:-}"
# Console scripts are declared once, in the root pyproject; never duplicate the list here.
read_script_names() {
  "$1" - "$ROOT_DIR/pyproject.toml" <<'PYEOF'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    print(" ".join(tomllib.load(handle)["project"]["scripts"]))
PYEOF
}

while (($#)); do
  case "$1" in
    --link-bin)
      LINK_BIN=true
      ;;
    --no-link-bin)
      LINK_BIN=false
      ;;
    --bin-name)
      shift
      if [[ -z "${1:-}" ]]; then
        echo "--bin-name requires a value" >&2
        exit 2
      fi
      BIN_NAMES="$1"
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ -n "${WARCRAFT_BIN_NAMES:-}" ]]; then
  # Removed override: failing here beats silently relinking every console script.
  echo "WARCRAFT_BIN_NAMES is no longer supported; pass --bin-name <name> instead." >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python binary '$PYTHON_BIN' not found." >&2
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if command -v uv >/dev/null 2>&1; then
  (cd "$ROOT_DIR" && uv sync --all-extras)
else
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null
  "$VENV_DIR/bin/pip" install -e '.[dev]'
fi
"$ROOT_DIR/scripts/setup_worktree_env.sh" >/dev/null

BIN_NAMES="${BIN_NAMES:-$(read_script_names "$VENV_DIR/bin/python")}"

if [[ "$LINK_BIN" == "true" ]]; then
  if [[ ! "$ALLOW_LINK_BIN" =~ ^(1|true|yes)$ ]]; then
    echo "Refusing to relink ~/.local/bin without WARCRAFT_ALLOW_LINK_BIN=1." >&2
    echo "Use make dev-deploy-no-link for normal branch-local setup." >&2
    exit 1
  fi
  LOCAL_BIN_DIR="${WARCRAFT_LOCAL_BIN_DIR:-$HOME/.local/bin}"
  mkdir -p "$LOCAL_BIN_DIR"
  for BIN_NAME in $BIN_NAMES; do
    WRAPPER_PATH="$LOCAL_BIN_DIR/$BIN_NAME"
    cat > "$WRAPPER_PATH" <<WRAP
#!/usr/bin/env bash
exec "$VENV_DIR/bin/$BIN_NAME" "\$@"
WRAP
    chmod +x "$WRAPPER_PATH"
    echo "Linked $WRAPPER_PATH -> $VENV_DIR/bin/$BIN_NAME"
  done
fi

echo "Dev deploy complete."
echo "Worktree env: $ROOT_DIR/.warcraft/worktree-env.sh"
echo "Run with: warcraft search \"defias\""
