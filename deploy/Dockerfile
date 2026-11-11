# syntax=docker/dockerfile:1
#
# 构建耗时几乎全在拉基础镜像。ghcr.io/astral-sh/uv 在国内拉取极慢，
# 所以这里只用 Docker Hub 的 python 镜像 —— uv 是单文件二进制，pip 装它快得多。
#
# 两阶段：builder 装依赖，运行镜像只留 venv，不带 pip 缓存与编译工具。
# 两个 sync 参数都是刻意的：
#   --no-dev      baiduads-sdk 只在生成 capabilities.json 时用，运行时不碰
#   --extra db   装 psycopg（多租户落库用）

FROM python:3.12-slim AS builder

# 依赖源用 build arg 传：默认官方 PyPI；国内服务器传镜像。
# **pip 和 uv 都走它**——装 uv 本身也是 pip 下载的，漏掉它一样慢。
# 实测 files.pythonhosted.org 在腾讯云只有 ~29 KB/s，清缓存后重建会卡死。
# 哈希仍按 uv.lock 校验，换源不降低完整性。
#   PYPI_INDEX=https://mirrors.cloud.tencent.com/pypi/simple
ARG PYPI_INDEX=https://pypi.org/simple
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_DEFAULT_INDEX=${PYPI_INDEX} \
    UV_INDEX_URL=${PYPI_INDEX} \
    PIP_INDEX_URL=${PYPI_INDEX} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

RUN pip install --no-cache-dir uv

WORKDIR /app

# 先只拷依赖清单，让依赖层能命中缓存——改代码不会触发重新装包
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra db --no-install-project

COPY src ./src
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra db


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
