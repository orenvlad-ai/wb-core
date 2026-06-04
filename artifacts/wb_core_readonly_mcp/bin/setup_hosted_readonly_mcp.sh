#!/usr/bin/env bash
set -euo pipefail

COMMAND="${1:-install-or-update}"

BASE_DIR="${WB_CORE_READONLY_MCP_BASE_DIR:-/opt/wb-core-readonly-mcp}"
APP_DIR="${WB_CORE_READONLY_MCP_APP_DIR:-$BASE_DIR/app}"
REPO_DIR="${WB_CORE_READONLY_MCP_REPO_DIR:-$BASE_DIR/repo}"
CONFIG_DIR="${WB_CORE_READONLY_MCP_CONFIG_DIR:-$BASE_DIR/config}"
ENV_DIR="${WB_CORE_READONLY_MCP_ENV_DIR:-$BASE_DIR/env}"
CONFIG_FILE="${WB_CORE_READONLY_MCP_CONFIG_FILE:-$CONFIG_DIR/remote.config.json}"
ENV_FILE="${WB_CORE_READONLY_MCP_ENV_FILE:-$ENV_DIR/wb-core-readonly-mcp.env}"
REPO_URL="${WB_CORE_READONLY_MCP_REPO_URL:-https://github.com/orenvlad-ai/wb-core.git}"
BRANCH="${WB_CORE_READONLY_MCP_BRANCH:-main}"
SERVICE_USER="${WB_CORE_READONLY_MCP_SERVICE_USER:-wb-core-readonly-mcp}"
SERVICE_NAME="${WB_CORE_READONLY_MCP_SERVICE_NAME:-wb-core-readonly-mcp.service}"
SYSTEMD_UNIT_PATH="${WB_CORE_READONLY_MCP_SYSTEMD_UNIT_PATH:-/etc/systemd/system/$SERVICE_NAME}"
HOST="${WB_CORE_READONLY_MCP_HOST:-127.0.0.1}"
PORT="${WB_CORE_READONLY_MCP_PORT:-8766}"
TOKEN_ENV_NAME="${WB_CORE_READONLY_MCP_TOKEN_ENV_NAME:-WB_CORE_READONLY_MCP_TOKEN}"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "error: this command must run as root" >&2
    exit 2
  fi
}

ensure_user() {
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    return
  fi
  useradd --system --home-dir "$BASE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
}

ensure_dirs() {
  install -d -m 0755 "$BASE_DIR" "$CONFIG_DIR"
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR" "$REPO_DIR"
  install -d -m 0700 "$ENV_DIR"
}

git_as_service_user() {
  runuser -u "$SERVICE_USER" -- git "$@"
}

ensure_git_clone() {
  local dir="$1"
  if [ ! -d "$dir/.git" ]; then
    if [ -d "$dir" ] && [ -n "$(find "$dir" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
      echo "error: $dir exists but is not an empty git clone directory" >&2
      exit 3
    fi
    git_as_service_user clone --branch "$BRANCH" --single-branch "$REPO_URL" "$dir"
    return
  fi
  if [ "$(git_as_service_user -C "$dir" config --get remote.origin.url)" != "$REPO_URL" ]; then
    echo "error: $dir origin does not match expected repo URL" >&2
    exit 3
  fi
  if [ -n "$(git_as_service_user -C "$dir" status --porcelain=v1 --untracked-files=all)" ]; then
    echo "error: $dir is dirty; refusing to update managed clone" >&2
    exit 3
  fi
  git_as_service_user -C "$dir" fetch --prune origin "$BRANCH"
  git_as_service_user -C "$dir" checkout "$BRANCH"
  git_as_service_user -C "$dir" pull --ff-only origin "$BRANCH"
}

write_config() {
  cat >"$CONFIG_FILE" <<EOF
{
  "repo_root": "$REPO_DIR",
  "source_mode": "managed_clone",
  "repo_url": "$REPO_URL",
  "branch": "$BRANCH",
  "refresh_policy": "external_manual",
  "remote_auth_token_env": "$TOKEN_ENV_NAME",
  "max_file_bytes": 1048576,
  "max_response_chars": 262144,
  "max_range_lines": 400,
  "max_search_matches": 50,
  "max_find_results": 200,
  "max_tree_items": 500,
  "max_tree_depth": 3
}
EOF
  chmod 0644 "$CONFIG_FILE"
}

ensure_env_file() {
  local token_value="${!TOKEN_ENV_NAME:-}"
  if [ -n "$token_value" ]; then
    umask 077
    printf '%s=%s\n' "$TOKEN_ENV_NAME" "$token_value" >"$ENV_FILE"
    return
  fi
  if [ -f "$ENV_FILE" ]; then
    return
  fi
  if [ "${WB_CORE_READONLY_MCP_GENERATE_TOKEN:-}" = "1" ]; then
    umask 077
    printf '%s=' "$TOKEN_ENV_NAME" >"$ENV_FILE"
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -hex 32 >>"$ENV_FILE"
    else
      python3 - <<'PY' >>"$ENV_FILE"
import secrets
print(secrets.token_hex(32), end="")
PY
    fi
    printf '\n' >>"$ENV_FILE"
    return
  fi
  umask 077
  cat >"$ENV_FILE" <<EOF
# Set the runtime-only bearer token before starting $SERVICE_NAME.
# Example:
# $TOKEN_ENV_NAME=replace-with-runtime-only-token
EOF
  echo "error: missing $TOKEN_ENV_NAME; set it or run with WB_CORE_READONLY_MCP_GENERATE_TOKEN=1" >&2
  exit 4
}

install_systemd_unit() {
  local unit_source="$APP_DIR/artifacts/wb_core_readonly_mcp/systemd/wb-core-readonly-mcp.service"
  if [ ! -f "$unit_source" ]; then
    echo "error: unit template not found in app clone: $unit_source" >&2
    exit 5
  fi
  install -m 0644 "$unit_source" "$SYSTEMD_UNIT_PATH"
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
}

set_permissions() {
  chown root:root "$BASE_DIR" "$CONFIG_DIR" "$ENV_DIR" "$CONFIG_FILE" "$ENV_FILE"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$REPO_DIR"
  chmod 0755 "$BASE_DIR" "$APP_DIR" "$REPO_DIR" "$CONFIG_DIR"
  chmod 0700 "$ENV_DIR"
  chmod 0600 "$ENV_FILE"
}

print_plan() {
  cat <<EOF
wb-core-readonly-mcp hosted service plan:
  app_dir: $APP_DIR
  repo_dir: $REPO_DIR
  config_file: $CONFIG_FILE
  env_file: $ENV_FILE
  service_name: $SERVICE_NAME
  service_user: $SERVICE_USER
  bind: $HOST:$PORT
  repo_url: $REPO_URL
  branch: $BRANCH
  product_plane_route: disabled
EOF
}

install_or_update() {
  require_root
  ensure_user
  ensure_dirs
  ensure_git_clone "$APP_DIR"
  ensure_git_clone "$REPO_DIR"
  write_config
  ensure_env_file
  set_permissions
  install_systemd_unit
  systemctl restart "$SERVICE_NAME"
  systemctl --no-pager --full status "$SERVICE_NAME"
}

loopback_probe() {
  local probe="$APP_DIR/apps/wb_core_readonly_mcp_hosted_probe.py"
  if [ ! -f "$probe" ]; then
    echo "error: probe not found in app clone: $probe" >&2
    exit 6
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  python3 "$probe" --base-url "http://$HOST:$PORT"
}

case "$COMMAND" in
  print-plan)
    print_plan
    ;;
  install-or-update)
    print_plan
    install_or_update
    ;;
  loopback-probe)
    loopback_probe
    ;;
  *)
    echo "usage: $0 [print-plan|install-or-update|loopback-probe]" >&2
    exit 2
    ;;
esac
