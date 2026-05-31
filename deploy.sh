#!/usr/bin/env bash
# deploy.sh — 巡检工单自动化服务一键部署脚本
# 用法: bash deploy.sh
#
# 前提条件:
#   - 服务器能通外网 (pip/npm/dws 均可访问)
#   - 已安装 dws CLI 并完成认证 (dws auth status 显示 authenticated)
#   - PostgreSQL 已安装并运行 (如需新建库和用户，脚本会自动创建)

set -euo pipefail

# ── 配置区 (按实际情况修改) ──────────────────────────────────────────────

APP_NAME="inspection-workflow"
# 自动检测: 优先用脚本所在目录，否则用 /opt
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${SCRIPT_DIR}"
APP_USER="$(whoami)"                    # 用当前用户运行，不额外创建用户
PG_HOST="localhost"
PG_PORT="5432"
PG_USER="inspection"
PG_PASS="inspection"
PG_DB="inspection_workflow"
SERVER_PORT=8100

# ── 颜色输出 ─────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 步骤 0: 检查前置条件 ─────────────────────────────────────────────────

info "===== 步骤 0/7: 检查前置条件 ====="

command -v python3 >/dev/null 2>&1 || error "python3 未安装，请先安装 Python 3.11+"
command -v psql >/dev/null 2>&1     || error "psql 未安装，请先安装 PostgreSQL 客户端"
command -v node >/dev/null 2>&1     || error "node 未安装，请先安装 Node.js 18+"
command -v npm >/dev/null 2>&1      || error "npm 未安装"
command -v dws >/dev/null 2>&1      || warn "dws CLI 未安装，部分功能会失败，请尽快安装并认证"

PYTHON_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python ${PYTHON_VER}, Node $(node -v), npm $(npm -v)"

if command -v dws >/dev/null 2>&1; then
    dws auth status -f json 2>/dev/null | grep -q '"authenticated": true' \
        && info "dws CLI 已认证" \
        || warn "dws CLI 未认证，请运行 dws auth login"
fi

# ── 步骤 1: 确认目录 ───────────────────────────────────────────

info "===== 步骤 1/7: 确认目录结构 ====="

if [ ! -f "${APP_DIR}/requirements.txt" ]; then
    error "未找到 ${APP_DIR}/requirements.txt，请确认代码已拷贝到正确目录"
fi
info "项目目录: ${APP_DIR}"

# ── 步骤 2: PostgreSQL 数据库 ────────────────────────────────────────────

info "===== 步骤 2/7: 初始化 PostgreSQL ====="

# 确保 PostgreSQL 允许密码连接 (pg_hba.conf)
PG_HBA="/etc/postgresql/14/main/pg_hba.conf"
if [ -f "${PG_HBA}" ]; then
    if ! grep -q "inspection" "${PG_HBA}" 2>/dev/null; then
        info "配置 PostgreSQL 允许密码连接..."
        # 在 local all all peer 行之前插入 md5 认证行
        sed -i '/^local\s\+all\s\+all\s\+peer/i local   all             inspection                               md5' "${PG_HBA}"
        sed -i '/^host\s\+all\s\+all\s\+127.0.0.1/s/ident/md5/' "${PG_HBA}" 2>/dev/null || true
        sed -i '/^host\s\+all\s\+all\s\+::1/s/ident/md5/' "${PG_HBA}" 2>/dev/null || true
        systemctl restart postgresql
        info "PostgreSQL 认证配置已更新"
    fi
fi

# 检查数据库是否已存在
DB_EXISTS=$(su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${PG_DB}'\"" 2>/dev/null || echo "")

if [ "${DB_EXISTS}" != "1" ]; then
    info "创建数据库和用户..."
    su postgres -c "psql" <<EOF
CREATE USER ${PG_USER} WITH PASSWORD '${PG_PASS}';
CREATE DATABASE ${PG_DB} OWNER ${PG_USER};
GRANT ALL PRIVILEGES ON DATABASE ${PG_DB} TO ${PG_USER};
EOF
    info "数据库 ${PG_DB} 创建完成"
else
    info "数据库 ${PG_DB} 已存在，跳过"
fi

# ── 步骤 3: Python 依赖 ─────────────────────────────────────────────────

info "===== 步骤 3/7: 安装 Python 依赖 ====="

cd "${APP_DIR}"

# 创建 venv（避免 conda 环境干扰，用系统 python3）
if [ ! -d "venv/bin/python3" ]; then
    info "创建 Python 虚拟环境..."
    # 优先用系统 python3（/usr/bin/python3），避开 conda
    SYS_PYTHON=""
    for p in /usr/bin/python3.11 /usr/bin/python3.12 /usr/bin/python3.10 /usr/bin/python3; do
        if [ -x "$p" ]; then
            SYS_PYTHON="$p"
            break
        fi
    done
    # 回退：用 conda 的 python3
    if [ -z "${SYS_PYTHON}" ]; then
        SYS_PYTHON=$(which python3 2>/dev/null)
        warn "未找到系统 Python，使用 ${SYS_PYTHON}"
    fi
    info "使用 ${SYS_PYTHON} 创建 venv..."

    # 先装 ensurepip 模块（Ubuntu 系统 Python 默认不带）
    ${SYS_PYTHON} -m ensurepip --upgrade 2>/dev/null || true

    # 创建 venv，打印错误信息
    if ${SYS_PYTHON} -m venv venv; then
        info "虚拟环境创建成功"
    else
        # 标准方式失败，用 --without-pip + 手动装 pip
        warn "标准 venv 创建失败，尝试 --without-pip 方式..."
        rm -rf venv
        ${SYS_PYTHON} -m venv --without-pip venv || error "venv 创建失败，请手动执行: ${SYS_PYTHON} -m venv venv"
        source venv/bin/activate
        curl -sS https://bootstrap.pypa.io/get-pip.py | python3
        deactivate
        info "虚拟环境创建成功（pip 已手动安装）"
    fi
    info "Python 版本: $(venv/bin/python3 --version)"
fi

source venv/bin/activate

# 升级 pip
pip install --upgrade pip --quiet

# 安装项目依赖
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt --quiet
    info "Python 依赖安装完成"
else
    error "未找到 requirements.txt，请确认代码已拷贝到 ${APP_DIR}"
fi

# 安装 PyMuPDF (PDF 解析)
pip install PyMuPDF --quiet 2>/dev/null || warn "PyMuPDF 安装失败，PDF 解析功能不可用"

# ── 步骤 4: 配置文件 ────────────────────────────────────────────────────

info "===== 步骤 4/7: 配置 .env ====="

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        warn "已从 .env.example 创建 .env，请编辑填入实际配置后重新运行"
        warn "必须配置: PTS_API_TOKEN, DT_AITABLE_BASE_ID, DT_AITABLE_TABLE_ID, DT_DISPATCH_BASE_ID, DT_DISPATCH_TABLE_ID, AI_API_KEY"
        warn "可选配置: PTS_SESSION_COOKIE, YUNJI_SESSION_COOKIE, INSPECTION_EMAIL_PASSWORD"
        echo ""
        echo "  vi ${APP_DIR}/.env"
        echo ""
        error "请先配置 .env 后重新运行本脚本"
    else
        error "未找到 .env 或 .env.example"
    fi
fi

# 自动更新 DATABASE_URL 和 alembic.ini 为实际值
DATABASE_URL="postgresql+psycopg://${PG_USER}:${PG_PASS}@${PG_HOST}:${PG_PORT}/${PG_DB}"

# 更新 .env 中的 DATABASE_URL (如果和当前不同)
if grep -q "^DATABASE_URL=" .env; then
    CURRENT_DB_URL=$(grep "^DATABASE_URL=" .env | cut -d= -f2-)
    if [ "${CURRENT_DB_URL}" != "${DATABASE_URL}" ]; then
        sed -i "s|^DATABASE_URL=.*|DATABASE_URL=${DATABASE_URL}|" .env
        info "已更新 .env 中的 DATABASE_URL"
    fi
fi

# 更新 alembic.ini 中的 sqlalchemy.url
if [ -f "alembic.ini" ]; then
    sed -i "s|^sqlalchemy.url = .*|sqlalchemy.url = ${DATABASE_URL}|" alembic.ini
    info "已更新 alembic.ini 中的数据库连接"
fi

# ── 步骤 5: 数据库迁移 ──────────────────────────────────────────────────

info "===== 步骤 5/7: 运行数据库迁移 ====="

alembic upgrade head
info "数据库迁移完成"

# ── 步骤 6: 构建前端 ────────────────────────────────────────────────────

info "===== 步骤 6/7: 构建前端 ====="

if [ -d "frontend" ]; then
    cd frontend

    # 检查是否需要安装依赖
    if [ ! -d "node_modules" ]; then
        npm install --prefer-offline 2>&1 | tail -1
        info "npm 依赖安装完成"
    fi

    # 构建
    npm run build 2>&1 | tail -3

    # 拷贝构建产物到 static 目录
    cd "${APP_DIR}"
    rm -rf static
    cp -r frontend/dist static
    info "前端构建完成，已输出到 static/"
else
    warn "未找到 frontend 目录，跳过前端构建"
fi

# ── 步骤 7: 配置 systemd 服务 ────────────────────────────────────────────

info "===== 步骤 7/7: 配置 systemd 服务 ====="

cat > /tmp/inspection-workflow.service <<EOF
[Unit]
Description=Inspection Workflow Service
After=network.target postgresql.service

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PATH=${APP_DIR}/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${APP_DIR}/venv/bin/uvicorn apps.api.main:app --host 10.2.36.228 --port ${SERVER_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cp /tmp/inspection-workflow.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable inspection-workflow
info "systemd 服务已配置 (以 ${APP_USER} 用户运行)"

# ── 启动服务 ─────────────────────────────────────────────────────────────

info "启动服务..."
systemctl start inspection-workflow

sleep 2

if systemctl is-active --quiet inspection-workflow; then
    info "服务启动成功!"
    info "访问地址: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'SERVER_IP'):${SERVER_PORT}"
else
    error "服务启动失败，查看日志: journalctl -u inspection-workflow -n 50"
fi

# ── 健康检查 ─────────────────────────────────────────────────────────────

info "健康检查..."
sleep 2

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${SERVER_PORT}/api/health" 2>/dev/null || echo "000")
if [ "${HTTP_CODE}" = "200" ]; then
    info "健康检查通过 (HTTP 200)"
else
    warn "健康检查返回 HTTP ${HTTP_CODE}，服务可能还在启动中"
    warn "查看日志: journalctl -u inspection-workflow -f"
fi

# ── 完成 ─────────────────────────────────────────────────────────────────

echo ""
info "========================================="
info "  部署完成!"
info "========================================="
echo ""
info "常用命令:"
echo "  查看状态:  systemctl status inspection-workflow"
echo "  查看日志:  journalctl -u inspection-workflow -f"
echo "  重启服务:  systemctl restart inspection-workflow"
echo "  停止服务:  systemctl stop inspection-workflow"
echo ""
info "配置文件: ${APP_DIR}/.env"
info "如需迁移现有数据，在源服务器执行:"
echo "  pg_dump -Fc inspection_workflow > inspection_workflow.dump"
echo "  scp inspection_workflow.dump 目标服务器:/tmp/"
echo "  在目标服务器执行: pg_restore -d inspection_workflow /tmp/inspection_workflow.dump"
