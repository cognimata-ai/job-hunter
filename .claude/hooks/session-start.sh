#!/bin/bash
# SessionStart hook for Claude Code cloud sessions (job-hunter).
# Idempotent and non-interactive. Every step is best-effort: a failing step logs a
# warning and the session still starts.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
LOG_DIR="${TMPDIR:-/tmp}/job-hunter-session"
mkdir -p "$LOG_DIR"
PGVECTOR_IMAGE_DEFAULT="pgvector/pgvector:0.8.7-pg17-bookworm"

note() { echo "[session-start] $*"; }
warn() { echo "[session-start] WARN: $*"; }

# 1. Docker daemon: integration tests and the dev stack need it.
start_docker() {
  if docker info >/dev/null 2>&1; then
    note "docker: daemon already running"
    return 0
  fi
  if ! command -v dockerd >/dev/null 2>&1; then
    warn "docker: dockerd not installed"
    return 1
  fi
  setsid nohup dockerd >"$LOG_DIR/dockerd.log" 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      note "docker: daemon started"
      return 0
    fi
    sleep 1
  done
  warn "docker: daemon did not become ready in 60 s (see $LOG_DIR/dockerd.log)"
  return 1
}

# 2. Pinned images: digests from deploy/images.lock once M0 Task 4 creates it,
#    otherwise the pinned pgvector tag from the spec (section 4.1).
pull_images() {
  local images=()
  if [ -f "$PROJECT_DIR/deploy/images.lock" ]; then
    while IFS='=' read -r _key value; do
      value="${value%\"}"; value="${value#\"}"
      [ -n "$value" ] && images+=("$value")
    done < <(grep -E '^[A-Za-z0-9_]+=' "$PROJECT_DIR/deploy/images.lock")
  else
    images+=("$PGVECTOR_IMAGE_DEFAULT")
  fi
  for img in "${images[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      note "image present: $img"
    elif timeout 600 docker pull -q "$img" >/dev/null 2>&1; then
      note "image pulled: $img"
    else
      warn "could not pull image: $img"
    fi
  done
}

# 3. Global CLIs used for 4-eyes reviews (Codex) and UI verification (playwright-cli).
install_global_clis() {
  local missing=()
  command -v codex >/dev/null 2>&1 || missing+=("@openai/codex@latest")
  command -v playwright-cli >/dev/null 2>&1 || missing+=("@playwright/cli@latest")
  if [ ${#missing[@]} -eq 0 ]; then
    note "global CLIs present: codex $(codex --version 2>/dev/null | awk '{print $NF}'), playwright-cli $(playwright-cli --version 2>/dev/null)"
    return 0
  fi
  if npm install -g "${missing[@]}" >"$LOG_DIR/npm-global.log" 2>&1; then
    note "installed: ${missing[*]}"
  else
    warn "npm install -g failed for: ${missing[*]} (see $LOG_DIR/npm-global.log)"
  fi
}

# 4. Codex: file-based credential store, then report login status. The device-code
#    login is interactive, so the hook never attempts it.
codex_status() {
  command -v codex >/dev/null 2>&1 || return 0
  mkdir -p "$HOME/.codex"
  if ! grep -q '^cli_auth_credentials_store' "$HOME/.codex/config.toml" 2>/dev/null; then
    printf 'cli_auth_credentials_store = "file"\n' >> "$HOME/.codex/config.toml"
  fi
  if codex login status >/dev/null 2>&1; then
    note "codex: $(codex login status 2>&1 | tail -1)"
  else
    note "codex: NOT logged in. Run 'codex login --device-auth' and approve the code at https://auth.openai.com/codex/device"
  fi
}

# 5. Project dependencies, once their manifests exist (M0 Task 1 / Task 19).
install_project_deps() {
  if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
    if command -v uv >/dev/null 2>&1; then
      local uv_args=(sync)
      [ -f "$PROJECT_DIR/uv.lock" ] && uv_args+=(--frozen)
      if (cd "$PROJECT_DIR" && uv "${uv_args[@]}" >"$LOG_DIR/uv-sync.log" 2>&1); then
        note "python deps: uv ${uv_args[*]} ok"
      else
        warn "python deps: uv ${uv_args[*]} failed (see $LOG_DIR/uv-sync.log)"
      fi
    else
      warn "python deps: uv not installed"
    fi
  fi
  if [ -f "$PROJECT_DIR/frontend/package.json" ]; then
    if (cd "$PROJECT_DIR/frontend" && npm install --no-audit --no-fund >"$LOG_DIR/npm-frontend.log" 2>&1); then
      note "frontend deps: npm install ok"
    else
      warn "frontend deps: npm install failed (see $LOG_DIR/npm-frontend.log)"
    fi
  fi
}

start_docker && pull_images
install_global_clis
codex_status
install_project_deps
exit 0
