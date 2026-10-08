#!/bin/bash
# dsh-somni one-shot installer.
#
#   ./install.sh                  # install plugin into the "default" profile
#   ./install.sh --profile demo   # install into a named profile under $DSH_HOME/profiles
#
# What it does:
#   1. checks prerequisites (node >= 18, python >= 3.9, an existing DSH install)
#   2. pip-installs the Python sidecar (pure stdlib — no third-party deps)
#   3. links the plugin into <profile>/node_modules
#   4. merges a ready-made config into <profile>/cordis.patch.yml (idempotent)
#   5. verifies the sidecar boots and answers a ping over stdio JSON-RPC
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="default"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --dsh-home) DSH_HOME="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

say()  { printf '[install] %s\n' "$*"; }
die()  { printf '[fail] %s\n' "$*" >&2; exit 1; }

# 1. prerequisites ----------------------------------------------------------
command -v node  >/dev/null || die "node not found (need >= 18 with DSH installed)"
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
[[ $NODE_MAJOR -ge 18 ]] || die "node >= 18 required, got $(node --version)"

# pick a python >= 3.9: prefer the default python3, else probe common names
PYTHON=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
  command -v "$cand" >/dev/null 2>&1 || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null; then
    PYTHON="$cand"; break
  fi
done
[[ -n "$PYTHON" ]] || die "python >= 3.9 required (sidecar needs asyncio.to_thread); no suitable interpreter found"
PY_VER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "node $(node --version), python $PY_VER ($PYTHON)"

DSH_JS="$(node -e "console.log(require.resolve('@deepseek-ai/dsh/lib/bin.js'))" 2>/dev/null || true)"
[[ -n "$DSH_JS" ]] || die "DSH runtime not found. Install it first: npm i -g @deepseek-ai/dsh"
say "DSH runtime: $DSH_JS"

# 1b. build the TS plugin if lib/ is absent (fresh git clone) ----------------
if [[ ! -f "$REPO_DIR/lib/index.js" ]]; then
  command -v npm >/dev/null || die "npm not found — needed to build the plugin from source"
  say "lib/ missing (fresh clone) — building (npm install + tsc, may take a minute)..."
  (cd "$REPO_DIR" && npm install --no-audit --no-fund --legacy-peer-deps --silent 2>&1 | tail -2 || true)
  if [[ ! -x "$REPO_DIR/node_modules/.bin/tsc" ]]; then
    die "npm install failed to provide tsc — run manually: cd $REPO_DIR && npm install --legacy-peer-deps && npm run build"
  fi
  (cd "$REPO_DIR" && npm run build) || die "plugin build failed — run manually: cd $REPO_DIR && npm install --legacy-peer-deps && npm run build"
  say "plugin built: $REPO_DIR/lib"
fi

PROFILE_DIR="$DSH_HOME/profiles/$PROFILE"
mkdir -p "$PROFILE_DIR/node_modules" "$DSH_HOME/profiles"
say "profile dir: $PROFILE_DIR"

# 2. sidecar ----------------------------------------------------------------
if "$PYTHON" -c 'import somni_memory' 2>/dev/null; then
  say "somni_memory already importable — skip pip install"
else
  "$PYTHON" -m pip install --quiet "$REPO_DIR/python" \
    || die "pip install failed — check network, or run manually: $PYTHON -m pip install $REPO_DIR/python"
  say "sidecar installed (pure stdlib, zero third-party deps)"
fi

# 3. plugin into profile node_modules --------------------------------------
PLUGIN_DST="$PROFILE_DIR/node_modules/dsh-somni"
if [[ -e "$PLUGIN_DST" && ! -L "$PLUGIN_DST" ]]; then
  mv "$PLUGIN_DST" "${PLUGIN_DST}.bak.$(date +%s)"
fi
ln -sfn "$REPO_DIR" "$PLUGIN_DST"
say "plugin linked: $PLUGIN_DST -> $REPO_DIR"

# 0b. data dir (must be set before config merge uses it) ------------------------------
SOMNI_DATA_DIR="${SOMNI_DATA_DIR:-$("$PYTHON" -c "import os; print(os.path.expanduser('~/.dsh/somni'))")}"
mkdir -p "$SOMNI_DATA_DIR/memory"
export SOMNI_DATA_DIR

# 4. config merge (idempotent) ---------------------------------------------
PATCH="$PROFILE_DIR/cordis.patch.yml"
touch "$PATCH"
if grep -q 'id: dsh-somni' "$PATCH" 2>/dev/null; then
  say "config already present in $PATCH — skip merge"
else
  DATA_DIR_YML="$SOMNI_DATA_DIR"
  PYTHON_YML="$(command -v "$PYTHON")"
  cat >> "$PATCH" <<YML

- insert:
    - id: dsh-somni
      name: dsh-somni
      config:
        dataDir: ${DATA_DIR_YML}
        python: ${PYTHON_YML}  # probed by installer (>=3.9)
        capture:
          enabled: true
          maxMessageChars: 4000
        inject:
          identity: true
          intentions: true
          discipline: true
        assoc:
          enabled: false        # set true after enabling embed
        tools:
          enabled: true
        dream:
          enabled: true
          idleMs: 900000        # 15 min quiet before a dream may start
          maxIntervalMs: 21600000
          minPendingLogs: 1
          llm: host
          maxIterations: 60
        embed:
          provider: off         # off | local | http
        logLevel: info
YML
  say "config merged into $PATCH"
fi

# 5. sidecar ping over stdio JSON-RPC --------------------------------------
say "verifying sidecar..."
SOMNI_DATA_DIR="${SOMNI_DATA_DIR:-$("$PYTHON" -c "import os; print(os.path.expanduser('~/.dsh/somni'))")}"
mkdir -p "$SOMNI_DATA_DIR/memory"
export SOMNI_INSTALL_DATA_DIR="$SOMNI_DATA_DIR"
"$PYTHON" - <<'PY' && say "sidecar OK — memory tools will register on next DSH boot"
import json, subprocess, sys, os
data_dir = os.environ['SOMNI_INSTALL_DATA_DIR']
proc = subprocess.Popen(
    [sys.executable, "-m", "somni_memory", "--data-dir", data_dir],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}) + "\n")
proc.stdin.flush()
line = proc.stdout.readline()  # blocks until the sidecar answers (local process)
proc.stdin.close()
proc.wait(timeout=10)
resp = json.loads(line.strip())
assert resp.get("id") == 1, resp
print("sidecar ping ->", json.dumps(resp.get("result", resp))[:200])
PY

cat <<EOF

Done. Try it:
  DSH_HOME=$DSH_HOME node "$DSH_JS" --profile $PROFILE "remember: this install works"
  # then, in a NEW session:
  DSH_HOME=$DSH_HOME node "$DSH_JS" --profile $PROFILE "what did I ask you to remember?"

Optional (semantic recall): drop BAAI/bge-small-zh-v1.5 ONNX files into
  ~/.dsh/somni/models/bge-small-zh-v1.5/   and set embed.provider: local
EOF
