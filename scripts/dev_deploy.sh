#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LINK_BIN=true
ALLOW_LINK_BIN="${WARCRAFT_ALLOW_LINK_BIN:-}"
BIN_NAMES="${WARCRAFT_BIN_NAMES:-warcraft wowhead method icy-veins raiderio warcraft-wiki wowprogress simc warcraftlogs raidbots blizzard curseforge}"

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

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python binary '$PYTHON_BIN' not found." >&2
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel >/dev/null
"$VENV_DIR/bin/pip" install -e '.[dev]'
"$ROOT_DIR/scripts/setup_worktree_env.sh" >/dev/null

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
