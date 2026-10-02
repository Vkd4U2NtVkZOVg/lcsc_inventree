#!/usr/bin/env bash
# 一键回填封装信息：为 InvenTree 中所有 LCSC 导入的器件补写封装。
#
# 用法（在仓库根目录执行）：
#   ./backfill_package.sh --dry-run     预览将改动哪些器件（不写 InvenTree）
#   ./backfill_package.sh --commit      实际写入
#   ./backfill_package.sh --limit 5 --dry-run   只看前 5 个
#
# 不带参数时按 DRY_RUN 环境变量决定（默认 dry-run）。
# 幂等：重复执行无副作用；LCSC 抓取有 1 req/s 限速 + 本地缓存，第二次很快。
set -euo pipefail
cd "$(dirname "$0")"

DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  # 群晖等环境当前用户无 docker.sock 权限，自动尝试 sudo
  DOCKER="sudo docker"
fi

exec $DOCKER compose exec -T lcsc2inventree-web python3 -m lcsc2inv backfill-package "$@"
