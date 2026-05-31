#!/usr/bin/env bash
# pre-check.sh — 迁移前环境检查与自动安装
# 用法: bash pre-check.sh
# 需要以可 sudo 的用户运行

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()   { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()  { echo -e "${RED}[ERROR]${NC} $*"; }
install() { echo -e "${CYAN}[INSTALL]${NC} $*"; }

# ── 系统 OS 检测 ──────────────────────────────────────────────────────────

if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_VER="${VERSION_ID:-unknown}"
else
    OS_ID="unknown"
    OS_VER="unknown"
fi

info "操作系统: ${OS_ID} ${OS_VER}"

if [[ "${OS_ID}" == "centos" || "${OS_ID}" == "rhel" || "${OS_ID}" == "rocky" || "${OS_ID}" == "almalinux" ]]; then
    PKG_MGR="yum"
    PKG_INSTALL="sudo yum install -y"
    PKG_UPDATE="sudo yum makecache -y"
elif [[ "${OS_ID}" == "ubuntu" || "${OS_ID}" == "debian" ]]; then
    PKG_MGR="apt"
    PKG_INSTALL="sudo apt install -y"
    PKG_UPDATE="sudo apt update -y"
else
    warn "未识别的发行版 ${OS_ID}，将尝试 apt"
    PKG_MGR="apt"
    PKG_INSTALL="sudo apt install -y"
    PKG_UPDATE="sudo apt update -y"
fi

ISSUES=0
FIXED=0

# ── 1. Python 3.11+ ──────────────────────────────────────────────────────

echo ""
info "===== 1/9: Python 3.11+ ====="

NEED_PYTHON=false
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}.{v.micro}")')
    PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
    PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "${PY_MAJOR}" -lt 3 ] || [ "${PY_MINOR}" -lt 11 ]; then
        warn "Python ${PY_VER} 版本过低，需要 3.11+"
        NEED_PYTHON=true
    else
        info "Python ${PY_VER} 已安装"
    fi
else
    warn "Python3 未安装"
    NEED_PYTHON=true
fi

if ${NEED_PYTHON}; then
    install "安装 Python 3.11+..."
    if [[ "${PKG_MGR}" == "yum" ]]; then
        sudo yum install -y python3.11 python3.11-pip python3.11-devel 2>/dev/null || {
            # CentOS 可能没有 3.11，从源码编译
            install "从源码编译 Python 3.11..."
            sudo yum groupinstall -y "Development Tools"
            sudo yum install -y openssl-devel bzip2-devel libffi-devel zlib-devel xz-devel sqlite-devel readline-devel tk-devel
            PYTHON_SRC="3.11.9"
            if [ ! -f "/usr/local/bin/python3.11" ]; then
                cd /tmp
                curl -sS https://www.python.org/ftp/python/${PYTHON_SRC}/Python-${PYTHON_SRC}.tgz -o Python-${PYTHON_SRC}.tgz
                tar xzf Python-${PYTHON_SRC}.tgz
                cd Python-${PYTHON_SRC}
                ./configure --enable-optimizations --prefix=/usr/local 2>&1 | tail -1
                make -j$(nproc) 2>&1 | tail -1
                sudo make altinstall 2>&1 | tail -1
                sudo ln -sf /usr/local/bin/python3.11 /usr/bin/python3.11
                sudo ln -sf /usr/local/bin/pip3.11 /usr/bin/pip3.11
            fi
            # 创建 python3 软链接指向 3.11
            sudo alternatives --install /usr/bin/python3 python3 /usr/local/bin/python3.11 1 2>/dev/null || \
                sudo ln -sf /usr/local/bin/python3.11 /usr/bin/python3
        }
    else
        sudo apt update -y
        sudo apt install -y software-properties-common 2>/dev/null
        sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
        sudo apt update -y
        sudo apt install -y python3.11 python3.11-venv python3.11-dev python3.11-distutils 2>/dev/null
        sudo ln -sf /usr/bin/python3.11 /usr/bin/python3
    fi
    FIXED=$((FIXED + 1))

    if command -v python3 >/dev/null 2>&1; then
        info "Python $(python3 --version) 安装成功"
    else
        error "Python 安装失败，请手动安装 3.11+"
        ISSUES=$((ISSUES + 1))
    fi
fi

# ── 2. pip + venv ─────────────────────────────────────────────────────────

echo ""
info "===== 2/9: pip + venv ====="

if python3 -m pip --version >/dev/null 2>&1; then
    info "pip 已可用: $(python3 -m pip --version | head -1)"
else
    install "安装 pip..."
    python3 -m ensurepip --upgrade 2>/dev/null || curl -sS https://bootstrap.pypa.io/get-pip.py | sudo python3
    FIXED=$((FIXED + 1))
fi

if python3 -m venv --help >/dev/null 2>&1; then
    info "venv 模块已可用"
else
    install "安装 venv..."
    if [[ "${PKG_MGR}" == "yum" ]]; then
        sudo yum install -y python3-venv 2>/dev/null || sudo yum install -y python3.11-venv 2>/dev/null
    else
        sudo apt install -y python3.11-venv 2>/dev/null || sudo apt install -y python3-venv
    fi
    FIXED=$((FIXED + 1))
fi

# ── 3. PostgreSQL 14+ ────────────────────────────────────────────────────

echo ""
info "===== 3/9: PostgreSQL ====="

NEED_PG=false
if command -v psql >/dev/null 2>&1; then
    PG_VER=$(psql --version | grep -oP '\d+' | head -1)
    info "PostgreSQL 已安装: $(psql --version | head -1)"

    # 检查服务是否运行
    if sudo systemctl is-active --quiet postgresql 2>/dev/null; then
        info "PostgreSQL 服务运行中"
    elif pg_isready -q 2>/dev/null; then
        info "PostgreSQL 可连接"
    else
        warn "PostgreSQL 已安装但未运行，尝试启动..."
        sudo systemctl start postgresql 2>/dev/null || sudo systemctl start postgresql-14 2>/dev/null || warn "启动失败，请手动处理"
    fi
else
    warn "PostgreSQL 未安装"
    NEED_PG=true
fi

if ${NEED_PG}; then
    install "安装 PostgreSQL..."
    if [[ "${PKG_MGR}" == "yum" ]]; then
        sudo yum install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-$(rpm -E '%{rhel}')-x86_64/pgdg-redhat-repo-latest.noarch.rpm 2>/dev/null || true
        sudo yum install -y postgresql14-server postgresql14
        sudo /usr/pgsql-14/bin/postgresql-14-setup initdb 2>/dev/null || true
        sudo systemctl enable postgresql-14
        sudo systemctl start postgresql-14
    else
        sudo apt install -y postgresql postgresql-contrib
        sudo systemctl enable postgresql
        sudo systemctl start postgresql
    fi
    FIXED=$((FIXED + 1))

    if command -v psql >/dev/null 2>&1; then
        info "PostgreSQL 安装成功: $(psql --version | head -1)"
    else
        error "PostgreSQL 安装失败"
        ISSUES=$((ISSUES + 1))
    fi
fi

# ── 4. Node.js 18+ ──────────────────────────────────────────────────────

echo ""
info "===== 4/9: Node.js 18+ ====="

NEED_NODE=false
if command -v node >/dev/null 2>&1; then
    NODE_VER=$(node -v | grep -oP '\d+' | head -1)
    if [ "${NODE_VER}" -lt 18 ]; then
        warn "Node.js $(node -v) 版本过低，需要 18+"
        NEED_NODE=true
    else
        info "Node.js $(node -v) 已安装"
    fi
else
    warn "Node.js 未安装"
    NEED_NODE=true
fi

if ${NEED_NODE}; then
    install "安装 Node.js 20 LTS..."
    if [[ "${PKG_MGR}" == "yum" ]]; then
        curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
        sudo yum install -y nodejs
    else
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
        sudo apt install -y nodejs
    fi
    FIXED=$((FIXED + 1))

    if command -v node >/dev/null 2>&1; then
        info "Node.js $(node -v) 安装成功"
    else
        error "Node.js 安装失败"
        ISSUES=$((ISSUES + 1))
    fi
fi

# ── 5. dws CLI ───────────────────────────────────────────────────────────

echo ""
info "===== 5/9: dws CLI ====="

if command -v dws >/dev/null 2>&1; then
    info "dws CLI 已安装: $(dws version 2>/dev/null | head -1 || echo 'unknown version')"

    # 检查认证状态
    if dws auth status -f json 2>/dev/null | grep -q '"authenticated": true'; then
        info "dws CLI 已认证"
    else
        warn "dws CLI 未认证！请运行: dws auth login"
        ISSUES=$((ISSUES + 1))
    fi
else
    install "安装 dws CLI..."
    # dws 通常通过内部方式安装，尝试 npm 全局安装或提示手动安装
    sudo npm install -g @anthropic-ai/dws 2>/dev/null || \
    curl -fsSL https://get.dws.dev | sudo bash 2>/dev/null || \
    {
        warn "dws CLI 自动安装失败"
        warn "请手动安装 dws CLI，常见方式:"
        warn "  1. 从内部源下载二进制: curl -fsSL https://get.dws.dev | bash"
        warn "  2. 或从 Mac 拷贝: scp /Users/shulei/.local/bin/dws 服务器:/usr/local/bin/"
        warn "安装后运行: dws auth login"
        ISSUES=$((ISSUES + 1))
    }

    if command -v dws >/dev/null 2>&1; then
        info "dws CLI 安装成功"
        FIXED=$((FIXED + 1))
    fi
fi

# ── 6. 网络连通性 ────────────────────────────────────────────────────────

echo ""
info "===== 6/9: 网络连通性 ====="

check_url() {
    local name=$1 url=$2
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "${url}" 2>/dev/null || echo "000")
    if [ "${code}" != "000" ]; then
        info "  ${name}: 可达 (HTTP ${code})"
    else
        warn "  ${name}: 不可达 ${url}"
        ISSUES=$((ISSUES + 1))
    fi
}

check_url "PTS API (内网)"    "http://10.9.255.197"
check_url "钉钉 API"          "https://api.dingtalk.com"
check_url "智谱 AI"           "https://open.bigmodel.cn"
check_url "SMTP (阿里云)"     "https://smtpdm.aliyun.com"
check_url "PyPI (pip源)"      "https://pypi.org"
check_url "npm 源"            "https://registry.npmjs.org"

# ── 7. 系统资源 ──────────────────────────────────────────────────────────

echo ""
info "===== 7/9: 系统资源 ====="

# CPU
CPU_CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo "?")
info "CPU 核心: ${CPU_CORES}"

# 内存
MEM_TOTAL=$(free -m 2>/dev/null | awk '/^Mem:/{print $2}' || echo "?")
MEM_AVAIL=$(free -m 2>/dev/null | awk '/^Mem:/{print $7}' || echo "?")
if [ "${MEM_TOTAL}" != "?" ]; then
    info "内存: ${MEM_AVAIL}MB 可用 / ${MEM_TOTAL}MB 总计"
    if [ "${MEM_TOTAL}" -lt 2000 ]; then
        warn "内存不足 2GB，建议至少 2GB"
    fi
else
    warn "无法检测内存大小"
fi

# 磁盘
DISK_AVAIL=$(df -BG / 2>/dev/null | awk 'NR==2{print $4}' | tr -d 'G' || echo "?")
DISK_TOTAL=$(df -BG / 2>/dev/null | awk 'NR==2{print $2}' | tr -d 'G' || echo "?")
if [ "${DISK_AVAIL}" != "?" ]; then
    info "磁盘: ${DISK_AVAIL}GB 可用 / ${DISK_TOTAL}GB 总计"
    if [ "${DISK_AVAIL}" -lt 5 ]; then
        warn "磁盘可用空间不足 5GB，建议清理"
    fi
fi

# ── 8. 构建工具链 ────────────────────────────────────────────────────────

echo ""
info "===== 8/9: 构建工具链 ====="

NEED_BUILD=false
for cmd in gcc make; do
    if ! command -v ${cmd} >/dev/null 2>&1; then
        warn "${cmd} 未安装"
        NEED_BUILD=true
    fi
done

if ${NEED_BUILD}; then
    install "安装构建工具..."
    if [[ "${PKG_MGR}" == "yum" ]]; then
        sudo yum groupinstall -y "Development Tools" 2>/dev/null
    else
        sudo apt install -y build-essential
    fi
    FIXED=$((FIXED + 1))
    info "构建工具安装完成"
else
    info "gcc/make 已安装"
fi

# psycopg[binary] 编译依赖
for lib in libpq-dev libssl-dev; do
    if [[ "${PKG_MGR}" == "yum" ]]; then
        RPM_NAME=$(echo ${lib} | sed 's/libpq-dev/postgresql-devel/' | sed 's/libssl-dev/openssl-devel/')
        rpm -q ${RPM_NAME} >/dev/null 2>&1 || sudo yum install -y ${RPM_NAME} 2>/dev/null
    else
        dpkg -l ${lib} >/dev/null 2>&1 || sudo apt install -y ${lib} 2>/dev/null
    fi
done

# ── 9. 端口检查 ──────────────────────────────────────────────────────────

echo ""
info "===== 9/9: 端口占用检查 ====="

for port in 8100 5432; do
    if ss -tlnp 2>/dev/null | grep -q ":${port} " || netstat -tlnp 2>/dev/null | grep -q ":${port} "; then
        warn "  端口 ${port} 已被占用"
    else
        info "  端口 ${port} 可用"
    fi
done

# ── 汇总 ──────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
if [ "${ISSUES}" -eq 0 ]; then
    info "环境检查通过! 自动修复 ${FIXED} 项"
    info "可以执行 bash deploy.sh 开始部署"
else
    warn "发现 ${ISSUES} 个问题需要手动处理:"
    echo ""
    if ! command -v dws >/dev/null 2>&1 || ! dws auth status -f json 2>/dev/null | grep -q '"authenticated": true'; then
        warn "  - dws CLI 未安装或未认证"
    fi
    warn ""
    warn "修复后重新运行: bash pre-check.sh"
fi
echo "=========================================="
