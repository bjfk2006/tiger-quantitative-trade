#!/usr/bin/env bash
# 快速切换 模拟/实盘 账户：重指向 data/tiger_openapi_config.properties 符号链接并重启容器
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-}"; CONFIRM="${2:-}"
case "$MODE" in
  paper) TARGET=tiger_openapi_config_模拟.properties ;;
  live)  TARGET=tiger_openapi_config_综合.properties ;;
  status) echo "active -> $(readlink data/tiger_openapi_config.properties || echo 非链接)"; exit 0 ;;
  *) echo "用法: $0 paper|live|status   (live 需确认: $0 live yes)"; exit 1 ;;
esac
[ -f "data/$TARGET" ] || { echo "缺少 data/$TARGET"; exit 1; }
if [ "$MODE" = "live" ] && [ "$CONFIRM" != "yes" ]; then
  echo "⚠️  切到【实盘 综合户·真实资金】。确认请执行: $0 live yes"; exit 1
fi
ln -sfn "$TARGET" data/tiger_openapi_config.properties
echo "active -> $TARGET ，重启服务中..."
sudo docker compose restart >/dev/null 2>&1
sleep 6
sudo docker exec -e PYTHONWARNINGS=ignore option-bot python -c "import os;from option_bot.config.loader import load_client_config_from_env as L;c=L(os.environ[\"TIGEROPEN_PROPS_PATH\"]);print(\"当前账户:\",c.account,\"is_paper=\",c.is_paper)"
