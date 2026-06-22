# option_bot 单容器：看板 + 操作面 + bot 盯盘（设计增量 §3/§13）
FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用层缓存）；[web] extra 一并装上 flask，由 pip 统一解析版本
COPY pyproject.toml setup.py README.md ./
COPY tigeropen ./tigeropen
RUN pip install --no-cache-dir ".[web]"

# 再拷业务代码
COPY option_bot ./option_bot

# 数据卷挂载点：SQLite / token / state / 私钥
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 看板 8000；操作面 8001（默认仅容器内 127.0.0.1，OBOT_OPS_EXPOSE=true 才 0.0.0.0）
EXPOSE 8000 8001

# 健康检查打看板免认证探针
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)" || exit 1

CMD ["python", "-m", "option_bot.web.server"]
