# ── Stage 1: Build frontend ──────────────────────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --prefer-offline
COPY frontend/ ./
RUN npm run build
# 产物输出到 /build/static (vite.config.ts: outDir: '../static')

# ── Stage 2: Runtime ────────────────────────────────────────────────────
FROM python:3.12-slim

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 依赖（单独层，利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir PyMuPDF

# 复制源码
COPY core/ core/
COPY models/ models/
COPY services/ services/
COPY scheduler/ scheduler/
COPY apps/ apps/
COPY migrations/ migrations/
COPY alembic.ini alembic.ini
COPY scripts/ scripts/

# 复制前端构建产物
COPY --from=frontend-builder /build/static static/

# 复制 dws CLI（钉钉 AITable 交互必需）
# dws 二进制需要通过 docker-compose volume 或构建时 COPY 进来
# 这里留一个占位——实际文件由 docker-compose volume 或构建参数注入
# 如果本地 /usr/local/bin/dws 存在，COPY 会在 .dockerignore 不排除时生效
COPY dws /usr/local/bin/dws
RUN chmod +x /usr/local/bin/dws

# email_tool 数据目录（邮件工具存储配置和历史）
RUN mkdir -p /app/email_tool/data

# 入口脚本
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8100

ENTRYPOINT ["/entrypoint.sh"]