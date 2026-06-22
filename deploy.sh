#!/usr/bin/env bash
# option_bot 一键部署脚本（在 Ubuntu 服务器上运行）
#
# 用法：
#   scp deploy.sh ubuntu@<server>:/tmp/        # 首次把脚本传上去
#   ssh ubuntu@<server>
#   sudo bash /tmp/deploy.sh                    # 首次会 clone 仓库并生成 .env 骨架
#   # 编辑 .env 填凭证、把私钥放到 data/ 后，再跑一次：
#   sudo bash /root/tiger-quantitative-trade/deploy.sh
#
# 设计原则：自包含（无仓库则 clone，有则 pull）；不含任何密钥；
#          不自动开启防火墙（避免把 SSH 锁在门外，只给提示）。
set -euo pipefail

REPO_URL="https://github.com/bjfk2006/tiger-quantitative-trade.git"
DEST="/root/tiger-quantitative-trade"
WEB_PORT="${OBOT_WEB_PORT:-8000}"

log()  { printf '\033[1;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- 0. 需要 root（仓库在 /root）----
if [ "$(id -u)" -ne 0 ]; then
  log "需要 root 权限，尝试用 sudo 重新执行…"
  exec sudo -E bash "$0" "$@"
fi

# ---- 1. 依赖检查 ----
command -v git >/dev/null 2>&1 || die "未安装 git：apt-get update && apt-get install -y git"
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "未找到 docker compose（v2）或 docker-compose。请先安装 Docker Compose。"
fi
docker info >/dev/null 2>&1 || die "Docker 未运行或当前用户无权限（试试 systemctl start docker）"
log "使用 compose 命令：$DC"

# ---- 2. 克隆或更新仓库 ----
if [ -d "$DEST/.git" ]; then
  log "仓库已存在，拉取最新代码…"
  git -C "$DEST" pull --ff-only
else
  log "克隆仓库到 $DEST …"
  git clone "$REPO_URL" "$DEST"
fi
cd "$DEST"
mkdir -p data

# ---- 3. .env 处理（首次生成骨架并停下，等用户填写）----
if [ ! -f .env ]; then
  cp option_bot/.env.example .env
  chmod 600 .env
  warn ".env 不存在，已从 .env.example 生成骨架：$DEST/.env"
  warn "请编辑它填写凭证（至少 TIGEROPEN_TIGER_ID / TIGEROPEN_ACCOUNT /"
  warn "TIGEROPEN_PRIVATE_KEY / OBOT_WEB_USER / OBOT_WEB_PASSWORD），"
  warn "并把 RSA 私钥放到 $DEST/data/（与 .env 中路径一致）。"
  warn "然后重新运行本脚本：sudo bash $DEST/deploy.sh"
  exit 0
fi
chmod 600 .env

# ---- 4. 校验关键配置非空/非占位 ----
get_env() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
need_nonempty() { [ -n "$(get_env "$1")" ] || die ".env 中 $1 为空，请填写后重试"; }
need_nonempty TIGEROPEN_TIGER_ID
need_nonempty TIGEROPEN_ACCOUNT
need_nonempty OBOT_WEB_USER
need_nonempty OBOT_WEB_PASSWORD
[ "$(get_env OBOT_WEB_PASSWORD)" = "change-me-please" ] && die "OBOT_WEB_PASSWORD 仍是示例值，请改成强密码"
[ -z "$(get_env OBOT_OPS_API_KEY)" ] && warn "OBOT_OPS_API_KEY 为空 → 操作命令面不会启动（仅看板）"

# 私钥检查：若 TIGEROPEN_PRIVATE_KEY 指向 /app/data/xxx，则对应 host 文件应存在
PK="$(get_env TIGEROPEN_PRIVATE_KEY)"
case "$PK" in
  /app/data/*)
    host_pk="data/${PK#/app/data/}"
    [ -f "$host_pk" ] || die "私钥文件不存在：$DEST/$host_pk（请 scp 上传并 chmod 600）"
    chmod 600 "$host_pk" || true
    ;;
  "") die ".env 中 TIGEROPEN_PRIVATE_KEY 为空" ;;
esac

# ---- 5. 构建并启动 ----
log "构建并启动容器…"
$DC up -d --build

# ---- 6. 健康检查（最多等 ~40s）----
log "等待看板就绪…"
ok=0
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${WEB_PORT}/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" -eq 1 ]; then
  log "看板已就绪 ✓"
else
  warn "健康检查未通过，请看日志：$DC logs --tail=80"
fi

# ---- 7. 结果与安全提示 ----
ip="$(curl -fsS https://api.ipify.org 2>/dev/null || echo '<server-ip>')"
cat <<EOF

==================== 部署完成 ====================
看板:   http://${ip}:${WEB_PORT}   (用户名/密码见 .env)
查看日志: cd $DEST && $DC logs -f
重启:     cd $DEST && $DC restart
停止:     cd $DEST && $DC down

⚠️ 安全（公网服务器务必处理）:
  1) 看板是明文 Basic 认证，请在【云安全组】仅放开 ${WEB_PORT} 给你的 IP；8001 不要放开。
  2) 想要 HTTPS 请前置 caddy/nginx 反代（可让作者给 caddy compose 片段）。
  3) 操作面默认仅容器内本机（OBOT_OPS_EXPOSE=false），保持默认。
  4) 首期请使用模拟盘账户；私钥/.env 切勿提交 git。
  5) 别用本脚本自动开 ufw —— 若要开，先 'ufw allow 22' 再 enable，避免锁死 SSH。
=================================================
EOF
