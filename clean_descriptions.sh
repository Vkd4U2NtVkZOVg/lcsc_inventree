#!/usr/bin/env bash
# 一键清理：遍历 InvenTree 全部 Part，删除描述中的国内站营销模板文案
#（「提供高清引脚图、PCB焊盘图、3D模型及Datasheet数据手册，…尽在立创商城。」）
#
# 用法：
#   ./clean_descriptions.sh --dry-run    预览（默认 dry-run，除非设置 DRY_RUN）
#   ./clean_descriptions.sh --commit     实际写入
#
# 幂等：重复执行无变化；不访问 LCSC，速度很快。
set -euo pipefail
cd "$(dirname "$0")"

DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  DOCKER="sudo docker"
fi

exec $DOCKER compose exec -T lcsc2inventree-web python3 -m lcsc2inv clean-desc "$@"
