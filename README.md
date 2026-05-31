# Inspection Workflow — 巡检工单流程自动化

长亭科技巡检工单全生命周期自动化系统，覆盖同步、派单、邮件、闭环四大环节。

## 功能概览

| 环节 | 说明 |
|------|------|
| **同步** | 从 PTS 拉取巡检工单 → 存入 PostgreSQL → 推送至钉钉 AITable |
| **派单** | 监控 AITable 记录，满足条件时自动触发云集外包平台派单 |
| **邮件** | 检测 AITable 巡检报告附件，AI 提取信息后自动发送邮件给客户 |
| **闭环** | 巡检完成后自动在 PTS 中推进阶段、闭环工单 |

## 技术栈

**后端：** FastAPI · SQLAlchemy 2.0 · PostgreSQL · Alembic · APScheduler · PyMuPDF · Playwright

**前端：** Vue 3 · TypeScript · Vite · Element Plus · ECharts

**AI：** 智谱 GLM-4-flash（PDF 文本结构化提取）

**外部依赖：** PTS GraphQL API · 钉钉 AITable (dws CLI) · 云集外包平台 · 阿里云 SMTP

## 目录结构

```
├── apps/api/              # FastAPI 后端
│   ├── main.py            # 应用入口（lifespan、路由、SPA 托管）
│   └── routers/           # API 路由（sync、monitor、triggers 等）
├── core/                  # 配置、数据库、日志
├── models/                # SQLAlchemy 数据模型
├── services/              # 业务逻辑（同步、派单、邮件、闭环、AITable 客户端等）
├── scheduler/             # APScheduler 定时任务
├── migrations/            # Alembic 数据库迁移
├── email_tool/            # 独立 Streamlit 邮件工具
├── frontend/              # Vue 3 前端源码
├── static/                # 前端构建产物（由 Vite 生成）
├── scripts/               # 辅助脚本
├── deploy.sh              # 一键部署
└── pre-check.sh           # 迁移前环境检查
```

## 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone https://github.com/shulei1127-dot/inspection-workflow.git
cd inspection-workflow

# 创建 Python 虚拟环境
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入实际的数据库连接、API Token、SMTP 等配置
```

必须配置的变量：

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | PostgreSQL 连接字符串 |
| `PTS_API_TOKEN` | PTS GraphQL API Bearer Token |
| `DT_AITABLE_BASE_ID` / `DT_AITABLE_TABLE_ID` | 钉钉 AITable（日常增值服务进展） |
| `DT_DISPATCH_BASE_ID` / `DT_DISPATCH_TABLE_ID` | 钉钉 AITable（客户巡检派单） |
| `AI_API_KEY` | 智谱 AI API Key |

### 3. 数据库迁移

```bash
alembic upgrade head
```

### 4. 构建前端

```bash
cd frontend
npm install
npm run build
# 构建产物自动输出到 ../static/
```

### 5. 启动服务

```bash
uvicorn apps.api.main:app --host 0.0.0.0 --port 8100
```

访问 `http://localhost:8100` 即可使用。

## 定时任务

| 任务 | 时间 | 功能 |
|------|------|------|
| PTS→钉钉同步 | 每天 16:00 | 拉取 PTS 工单并推送 AITable |
| 派单轮询 | 每 5 分钟 | 检测派单条件 |
| 邮件待发送探测 | 每 2 小时 | 刷新邮件待发送缓存 |
| PTS 闭环检查 | 每天 10:00 | 同步闭环状态并执行闭环 |
| 云集 Cookie 保活 | 每 3 小时 | 保持云集 Session 有效 |

> 默认 `AUTO_DISPATCH_ENABLED=false`、`AUTO_EMAIL_ENABLED=false`，定时任务仅做检测，不自动触发。

## API 速览

```
POST /api/sync/run                 # 从 PTS 拉取工单
POST /api/sync/push                # 推送到 AITable
GET  /api/monitor/dispatch-pending # 查看待派单记录
POST /api/monitor/dispatch/{id}    # 手动派单
GET  /api/monitor/email-pending    # 查看待发邮件记录
POST /api/monitor/send-email/{id}  # 手动发送邮件
POST /api/monitor/closure-check    # 触发闭环检查
GET  /api/work-orders              # 工单列表
GET  /api/statistics/overview      # 统计概览
WS   /api/ws                       # 实时事件推送
```

## 部署（systemd）

```bash
# 使用部署脚本
sudo bash deploy.sh

# 或手动管理
sudo systemctl start inspection-workflow
sudo systemctl status inspection-workflow
sudo journalctl -u inspection-workflow -f
```

## 注意事项

- AITable singleSelect 字段写入时必须使用**中文显示名**，不能用 option ID
- 云集 Cookie 需定期保活，过期后需手动更新 `.env` 中的 `YUNJI_SESSION_COOKIE`
- PTS Session Cookie 用于 Playwright 自动化（文件上传、工单闭环），过期后需手动更新
- 完成阶段的工单（审核工单、已闭环）会自动从本地 DB 和 AITable 中删除
- 一个 AITable 记录可能含多个 PDF 附件，系统会独立提取后合并（产品名顿号拼接、总结按产品分段）

## License

Internal use only — 长亭科技
