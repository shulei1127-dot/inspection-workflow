#!/usr/bin/env bash
set -euo pipefail

# ── 更新 alembic.ini 中的数据库连接 ──────────────────────────────────
# alembic.ini 中的 sqlalchemy.url 是硬编码的 localhost，需要替换为实际值
DB_URL="${DATABASE_URL}"
# Alembic ini 需要 postgresql+psycopg:// 格式，去掉 +psycopg 用于纯 psycopg 驱动
sed -i "s|^sqlalchemy.url = .*|sqlalchemy.url = ${DB_URL}|" alembic.ini
echo "[entrypoint] Updated alembic.ini with DATABASE_URL=${DB_URL}"

# ── 等待 PostgreSQL 就绪 ──────────────────────────────────────────────
echo "[entrypoint] Waiting for PostgreSQL..."
until python -c "
import sqlalchemy, os
engine = sqlalchemy.create_engine(os.environ['DATABASE_URL'])
with engine.connect() as c:
    c.execute(sqlalchemy.text('SELECT 1'))
" 2>/dev/null; do
    sleep 1
done
echo "[entrypoint] PostgreSQL is ready."

# ── 运行数据库迁移 ──────────────────────────────────────────────────
echo "[entrypoint] Running Alembic migrations..."
alembic upgrade head
echo "[entrypoint] Migrations done."

# ── 启动应用 ─────────────────────────────────────────────────────────
echo "[entrypoint] Starting uvicorn on 0.0.0.0:8100..."
exec uvicorn apps.api.main:app --host 0.0.0.0 --port 8100