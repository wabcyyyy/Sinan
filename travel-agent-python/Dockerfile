# Agent 运行镜像：依赖来自 uv.lock；runtime 只带 venv 与代码，非 root 运行。
# OTM_API_KEY 等运行配置经 compose env_file 注入，不作为构建参数。

# ---- deps ----
FROM python:3.12-slim AS deps
# uv 版本固定保证构建可重现；须 >= 生成 uv.lock 的本机 uv（0.11.x，lock revision 3）
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv
WORKDIR /app
# 部分 wheel 缺预编译时的编译兜底；runtime 阶段不带
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ---- runtime：venv + 代码，非 root ----
FROM python:3.12-slim
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY . .
ENV PATH="/app/.venv/bin:$PATH" \
    AGENT_HOST=0.0.0.0
EXPOSE 8000
# 非 root 运行。注意：compose 把宿主机 data/ 目录 bind mount 到 /app/data
# （上传图与 PDF 导出落盘处），宿主目录 UID 与 1000 不一致时需宿主侧 chown。
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/agent/health', timeout=4)"]
CMD ["python", "main.py"]
