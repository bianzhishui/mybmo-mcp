# syntax=docker/dockerfile:1
#
# 构建耗时几乎全在拉基础镜像。ghcr.io/astral-sh/uv 在国内拉取极慢，
# 所以这里只用 Docker Hub 的 python 镜像 —— uv 是单文件二进制，pip 装它快得多。
#
# 两阶段：builder 装依赖，运行镜像只留 venv，不带 pip 缓存与编译工具。
# 两个 sync 参数都是刻意的：
#   --no-dev      baiduads-sdk 只在生成 capabilities.json 时用，运行时不碰
#   psycopg      多租户落库用。**必须显式 uv pip install**：`uv sync --extra db`
#                实测没把 extra 装进 venv（80 个包独缺 psycopg），所以这里钉死版本单独装

FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 先只拷依赖清单，让依赖层能命中缓存——改代码不会触发重新装包
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY scripts/import_tenancy.py ./import_tenancy.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
# psycopg 单独装：版本与 uv.lock 对齐（3.3.8），避免 --extra 不生效的坑
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "psycopg[binary,pool]==3.3.8"


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

# 容器内必须监听 0.0.0.0，否则宿主 Caddy 通过端口映射访问不到；
# 端口只绑宿主的 127.0.0.1，公网入口只有 Caddy。
CMD ["mybmo-mcp", "-t", "streamable-http", "--host", "0.0.0.0", "-p", "8000"]
