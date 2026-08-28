# syntax=docker/dockerfile:1

# Build đa tầng: tầng builder giữ uv và toàn bộ cache cài đặt, tầng runtime chỉ
# nhận /app đã dựng xong. Ảnh cuối không có uv, không có compiler, không có
# lockfile — nhỏ hơn và bề mặt tấn công hẹp hơn.

FROM ghcr.io/astral-sh/uv:0.11.16 AS uv

FROM python:3.12-slim-bookworm AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Cài dependency trước, copy source sau: sửa server.py không làm mất cache của
# lớp `uv sync`, vốn là lớp tốn thời gian nhất.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md kg_loader.py metrics.py server.py ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# BẮT BUỘC: check_freshness chạy `git rev-parse HEAD` và `git diff` bên trong
# thư mục dự án để so graph với HEAD hiện tại. Base python:3.12-slim KHÔNG kèm
# git, nên nếu thiếu dòng này thì mọi dự án đều báo FRESHNESS = UNKNOWN — và
# báo trong im lặng, vì FileNotFoundError bị nuốt ở tầng dưới. Rất khó nhận ra:
# nhìn từ ngoài giống hệt như dự án không phải git checkout.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1001 ua-mcp \
 && useradd \
      --uid 1001 \
      --gid 1001 \
      --no-create-home \
      --home-dir /app \
      --shell /usr/sbin/nologin \
      ua-mcp

WORKDIR /app
COPY --from=builder --chown=1001:1001 /app /app

USER 1001:1001

# Server nói MCP qua stdio, không mở cổng nào. Chạy bằng `docker run -i` hoặc
# `docker compose run --rm -T`.
CMD ["/app/.venv/bin/python", "/app/server.py"]
