#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

INSTALL_DIR="${INSTALL_DIR:-/opt/tg2cloud-clouddrive2}"
BACKUP_DIR="/opt/tg2cloud-clouddrive2-backups"
BOT_SERVICE="tg2cloud-clouddrive2-bot"
BOT_CONTAINER="tg2cloud-clouddrive2-bot"
[[ "$INSTALL_DIR" =~ ^/opt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] || exit 2
[[ "/$INSTALL_DIR/" != *"/../"* && "/$INSTALL_DIR/" != *"/./"* ]] || exit 2
[[ "$(realpath -e -- "$INSTALL_DIR")" == "$INSTALL_DIR" ]] || exit 2
cd "$INSTALL_DIR"
# shellcheck disable=SC1091
source ./backup_retention.sh

check_config() {
  local fingerprint
  fingerprint="$(sha256sum app/*.py | LC_ALL=C sort -k2 | sha256sum | cut -d' ' -f1)"
  docker compose config --format json \
    | docker compose exec -T "$BOT_SERVICE" python -m app.deployment_check \
        --expected-code "$fingerprint"
}

validate_config() {
  docker compose config --quiet || return 1
  docker compose run --rm --no-deps --entrypoint python "$BOT_SERVICE" \
    -m app.deployment_check --validate-only
}

wait_healthy() {
  local health
  for _ in $(seq 1 40); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$BOT_CONTAINER" 2>/dev/null || true)"
    if [[ "$health" == healthy ]]; then
      check_config
      return
    fi
    [[ "$health" != unhealthy && "$health" != exited ]] || return 1
    sleep 3
  done
  return 1
}

redact_runtime_logs() {
  sed -E \
    -e '/[Pp]assword|密码/ s/(:|=)[^:=]*$/\1 [REDACTED]/' \
    -e 's/([A-Z0-9_]*(PASSWORD|TOKEN|API_HASH|SECRET)[A-Z0-9_]*=)[^[:space:]]+/\1[REDACTED]/g' \
    -e 's/[0-9]{6,12}:[A-Za-z0-9_-]{20,}/[REDACTED_BOT_TOKEN]/g' \
    -e 's#(https?://)[^[:space:]/:@]+:[^[:space:]/@]+@#\1[REDACTED]@#g'
}

apply_config() {
  local candidate="${2:-}" backup staged restarted=false
  [[ -f "$candidate" && "$candidate" == /* ]] || { echo "请提供新配置文件的绝对路径" >&2; return 2; }
  install -d -m 700 "$BACKUP_DIR"
  backup="$(mktemp "$BACKUP_DIR/env-XXXXXXXX.env")"
  install -m 600 .env "$backup"
  restore_config() {
    install -m 600 "$backup" .env
    if [[ "$restarted" == true ]]; then
      if ! docker compose up -d --no-deps "$BOT_SERVICE" || ! wait_healthy; then
        echo '旧配置已恢复，但容器恢复未通过核验，请人工检查。' >&2
      fi
    fi
    if [[ -n "${staged:-}" && -f "$staged" ]]; then
      rm -f -- "$staged"
    fi
  }
  trap 'trap - ERR INT TERM HUP; restore_config; exit 1' ERR INT TERM HUP
  staged="$(mktemp "$INSTALL_DIR/.env-staged-XXXXXXXX")"
  install -m 600 "$candidate" "$staged"
  sed -i 's/\r$//' "$staged"
  mv -f -- "$staged" .env
  if validate_config; then
    restarted=true
    if docker compose up -d --no-deps "$BOT_SERVICE" && wait_healthy; then
      trap - ERR INT TERM HUP
      printf 'TG2CLOUD_APPLY_CONFIG=OK\n配置备份：%s\n' "$backup"
      return
    fi
  fi
  restore_config
  trap - ERR INT TERM HUP
  printf 'TG2CLOUD_APPLY_CONFIG=FAILED\n已恢复旧配置；备份：%s\n' "$backup" >&2
  return 1
}

case "${1:-status}" in
  status)
    docker compose ps
    bot_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$BOT_CONTAINER" 2>/dev/null || true)"
    if [[ "$bot_health" != healthy ]]; then
      printf 'TG2CLOUD_BOT_HEALTH=%s\nTG2CLOUD_STATUS=STOPPED\n' "${bot_health:-missing}" >&2
      exit 1
    fi
    printf 'TG2CLOUD_BOT_HEALTH=healthy\nTG2CLOUD_STATUS=OK\n'
    ;;
  logs)
    docker compose logs --since=30m --tail=200 -f "$BOT_SERVICE" \
      | redact_runtime_logs
    ;;
  restart)
    docker compose restart "$BOT_SERVICE"
    ;;
  stop)
    docker compose stop "$BOT_SERVICE"
    ;;
  start)
    docker compose up -d "$BOT_SERVICE"
    wait_healthy
    ;;
  update)
    docker compose build "$BOT_SERVICE"
    validate_config
    docker compose up -d --no-deps "$BOT_SERVICE"
    wait_healthy
    printf 'TG2CLOUD_UPDATE=OK\n'
    ;;
  apply-config)
    apply_config "$@"
    ;;
  backups)
    tg115_backup_inventory "$BACKUP_DIR"
    ;;
  prune-backups)
    tg115_prune_backups "$BACKUP_DIR" "${2:-5}"
    ;;
  check|doctor)
    check_config
    docker compose exec -T "$BOT_SERVICE" python -m app.healthcheck
    ;;
  validate)
    validate_config
    ;;
  ready)
    wait_healthy
    ;;
  verify)
    docker compose exec -T "$BOT_SERVICE" python -m app.verify_destination
    ;;
  *)
    echo "用法：$0 {status|logs|restart|stop|start|update|verify|check|doctor|validate|backups|prune-backups [1-50]|apply-config /absolute/config.env}" >&2
    exit 2
    ;;
esac
