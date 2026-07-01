FROM python:3.12-slim-trixie

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir PyMuPDF

# 应用代码
COPY core/ core/
COPY models/ models/
COPY services/ services/
COPY scheduler/ scheduler/
COPY apps/ apps/
COPY migrations/ migrations/
COPY alembic.ini alembic.ini
COPY scripts/ scripts/
COPY static/ static/

# dws CLI
COPY dws /usr/local/bin/dws
RUN chmod +x /usr/local/bin/dws

# 邮件工具数据目录
RUN mkdir -p /app/email_tool/data

# 入口脚本
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8100

ENTRYPOINT ["/entrypoint.sh"]
