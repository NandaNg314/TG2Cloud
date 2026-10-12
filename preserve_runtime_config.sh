#!/usr/bin/env bash

# Called only after the current Edition's existing TG2Cloud compose file is
# identified. Keep the original .env on the VPS; never send it to the desktop.
tg2cloud_prepare_redeploy_config() {
  local candidate="$1" existing="$2"
  if grep -Fxq 'TG2CLOUD_REDEPLOY_APPLY_CONFIG=true' "$candidate"; then
    printf 'TG2CLOUD_CONFIG=APPLY_REQUESTED\n'
    return 0
  fi
  if [[ ! -f "$existing" || -L "$existing" ]]; then
    printf '现有 TG2Cloud 配置缺失或不是普通文件；停止重复部署，避免覆盖配置\n' >&2
    return 1
  fi
  install -m 600 -- "$existing" "$candidate" || return 1
  printf 'TG2CLOUD_CONFIG=PRESERVED\n'
}
