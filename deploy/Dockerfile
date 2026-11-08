# syntax=docker/dockerfile:1
#
# 两阶段构建：builder 里用 uv 装依赖并构建 wheel，运行镜像里不留编译期工具。
# 关键点是 --no-dev：baiduads-sdk 只在生成 capabilities.json 时用，运行时不需要，
# 装进镜像会让体积翻倍且引入 urllib3 的传递依赖。

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 先只拷依赖清单，让依赖层能命中缓存——改代码不会触发重新装包
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MYBMO_DATA_DIR=/data

# 命名卷首次挂载时会继承镜像里这个目录的属主，所以非 root 也能写
RUN mkdir -p /data && chown app:app /data

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# 容器内必须监听 0.0.0.0，否则 Caddy 通过服务名访问不到；
# 不映射到宿主机端口，公网入口只有 Caddy。
CMD ["mybmo-mcp", "-t", "streamable-http", "--host", "0.0.0.0", "-p", "8000"]
