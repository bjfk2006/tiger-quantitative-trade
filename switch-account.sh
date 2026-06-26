#!/usr/bin/env bash
# 快速切换 模拟/实盘 账户。
# 关键：bot 实际账户只由 .env 的 TIGEROPEN_PROPS_PATH 决定（service.py 据此 load 配置），
# 不读 data/tiger_openapi_config.properties 符号链接。故本脚本以更新 TIGEROPEN_PROPS_PATH 为准，
# 符号链接同步更新仅作人类参考/向后兼容。
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-}"; CONFIRM="${2:-}"
case "$MODE" in
  paper) TARGET=tiger_openapi_config_模拟.properties ;;
  live)  TARGET=tiger_openapi_config_综合.properties ;;
  status)
    echo "TIGEROPEN_PROPS_PATH -> $(grep -E '^TIGEROPEN_PROPS_PATH=' .env 2>/dev/null | cut -d= -f2- || echo 未设置)  (bot 实际读这个)"
    echo "symlink             -> $(readlink data/tiger_openapi_config.properties || echo 非链接)  (仅参考)"
    exit 0 ;;
  *) echo "用法: $0 paper|live|status   (live 需确认: $0 live yes)"; exit 1 ;;
esac
[ -f "data/$TARGET" ] || { echo "缺少 data/$TARGET"; exit 1; }
if [ "$MODE" = "live" ] && [ "$CONFIRM" != "yes" ]; then
  echo "⚠️  切到【实盘 综合户·真实资金】。确认请执行: $0 live yes"; exit 1
fi
# 权威切换：更新 bot 实际读取的 TIGEROPEN_PROPS_PATH（容器内路径 /app/data/<file>）
CONTAINER_PATH="/app/data/$TARGET"
if grep -q '^TIGEROPEN_PROPS_PATH=' .env 2>/dev/null; then
  sed -i "s#^TIGEROPEN_PROPS_PATH=.*#TIGEROPEN_PROPS_PATH=$CONTAINER_PATH#" .env
else
  echo "TIGEROPEN_PROPS_PATH=$CONTAINER_PATH" >> .env
fi
# 符号链接同步更新（仅人类参考；bot 不读它）
ln -sfn "$TARGET" data/tiger_openapi_config.properties
# C4: 切换账户绝不应自动下单——强制关闭自动开仓，避免切到实盘即开仓
if grep -q '^OBOT_OPEN_ON_START=' .env 2>/dev/null; then
  sed -i 's/^OBOT_OPEN_ON_START=.*/OBOT_OPEN_ON_START=false/' .env
else
  echo 'OBOT_OPEN_ON_START=false' >> .env
fi
echo "active -> $TARGET (TIGEROPEN_PROPS_PATH=$CONTAINER_PATH) ，已强制 OBOT_OPEN_ON_START=false，重建容器中..."
# C1: 必须用 up -d 重新创建容器以重载 .env（docker compose restart 不会重读 env_file）
sudo docker compose up -d >/dev/null 2>&1
sleep 6
sudo docker exec -e PYTHONWARNINGS=ignore option-bot python -c "import os;from option_bot.config.loader import load_client_config_from_env as L;c=L(os.environ[\"TIGEROPEN_PROPS_PATH\"]);print(\"当前账户:\",c.account,\"is_paper=\",c.is_paper)"
