# Inspection Workflow — 巡检工单流程自动化

长亭科技巡检工单全生命周期自动化系统，覆盖同步、派单、邮件、闭环四大环节。

## 服务分工

### AI 自动执行

| # | 任务 | 执行周期 | 说明 |
|---|------|---------|------|
| 1 | PTS 工单拉取同步 | 工作日 16:00 | 自动从 PTS 拉取新增满足条件的工单，写入钉钉 AITable 数据表，并自动调整工单计划完成时间到当月末 |
| 2 | 云集定时探测 + 自动派单 | 工作日 10/12/14/16/18 点 | 轮询钉钉数据表，满足条件时自动创建云集订单并回写订单编号到钉钉数据表 |
| 3 | 待发送邮件报告探测 | 每 2 小时 | 主动探测钉钉数据表，将邮件待发送数据展示到前端页面 |
| 4 | 待发送邮件报告预分析 | 工作日 10:30/14:30/16:30/18:30 | AI 提取钉钉数据表中邮件未发送的巡检报告信息（客户名、产品名、数量、邮箱、总结等），存储预分析结果供人工审核 |
| 5 | PTS 工单闭环检查 | 工作日 10:00 | 自动检测钉钉数据表内巡检已完成、工单未闭环的数据，自动推进 PTS 工单阶段完成闭环 |
| 6 | 云集 Cookie 保活 | 每 3 小时 | 自动访问云集页面保持 Session 有效，过期时触发钉钉告警 |
| 7 | 钉钉机器人信息推送 | 随各任务触发 | 将定时任务的执行情况自动推送到钉钉群 |

> **工作日判定**：使用 `chinesecalendar` 库识别中国法定节假日和调休安排，非工作日自动跳过定时任务。

### 售后负责人执行

1. **确认客户当月是否需要执行巡检**
   - **现场巡检：**
     - 是：同步客户现场地址、联系方式、邮箱等信息到钉钉数据表，随即在钉钉数据表选择伙伴供应商。等待伙伴的负责人确认工程师后，自动拉钉钉群
     - 否：钉钉数据表巡检方式标记为延期巡检，手动修改对应工单的计划完成时间
   - **远程巡检：**
     - 是：约定远程巡检时间，填写进钉钉数据表
     - 否：钉钉数据表巡检方式标记为延期巡检，手动修改对应工单的计划完成时间
2. **审核邮件发送内容** — 在前端页面预览邮件正文、收件人、附件等信息，确认无误后人工点击发送
3. **工单自动闭环失败时** — 人工闭环巡检工单并上传巡检报告

### 伙伴工程师执行

1. 伙伴工程师主动联系客户确认好时间后，同步到钉钉数据表
2. 生成 Word 巡检报告，群内审核无误后，上传 PDF 巡检报告到钉钉数据表
3. 系统预分析后，由售后负责人审核邮件内容并确认发送

---

## 邮件发送流程

```
上传PDF到钉钉数据表
        │
        ▼
定时预分析（AI提取客户名/产品名/数量/邮箱/总结）
        │
        ▼
前端展示预分析结果 ──→ 售后负责人点击"预览发送"
        │
        ▼
展示邮件完整内容（主题/正文/收件人/抄送/附件）
        │
        ▼
确认无误 ──→ 点击"确认发送" ──→ 邮件发出 ──→ 自动闭环工单
```

> 邮件不会自动发送给客户，必须经过人工审核确认后才会发出。

---

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

## 定时任务配置

| 任务 | Cron 表达式 | 环境变量 | 说明 |
|------|------------|---------|------|
| PTS→钉钉同步 | `0 16 * * *` | `SYNC_CRON` | 拉取 PTS 工单并推送 AITable |
| 派单轮询 | `0 10,12,14,16,18 * * 1-5` | `DT_DISPATCH_BASE_ID` | 工作日检测派单条件并自动派单 |
| 邮件待发送探测 | `0 */2 * * *` | `EMAIL_PROBE_CRON` | 刷新邮件待发送缓存 |
| 邮件预分析 | `30 10,14,16,18 * * 1-5` | `EMAIL_PRE_ANALYSIS_ENABLED` | 工作日 AI 提取巡检报告信息 |
| PTS 闭环检查 | `0 10 * * *` | `CLOSURE_CHECK_CRON` | 同步闭环状态并执行闭环 |
| 云集 Cookie 保活 | 每 3 小时 | `YUNJI_SESSION_COOKIE` | 保持云集 Session 有效 |

## API 速览

```
POST /api/sync/run                          # 从 PTS 拉取工单
POST /api/sync/push                         # 推送到 AITable
GET  /api/monitor/dispatch-pending          # 查看待派单记录
POST /api/monitor/dispatch/{id}             # 手动派单
GET  /api/monitor/email-pending             # 查看待发邮件记录
POST /api/monitor/send-email/{id}           # 手动发送邮件
POST /api/monitor/closure-check             # 触发闭环检查
GET  /api/work-orders                       # 工单列表
GET  /api/statistics/overview               # 统计概览
GET  /api/email-tool/pre-analysis           # 获取预分析结果
GET  /api/email-tool/preview/{record_id}    # 邮件内容预览
POST /api/email-tool/send-direct            # 确认发送邮件
WS   /api/ws                                # 实时事件推送
```

## 部署（Docker Compose）

```bash
# 构建并启动
docker compose up -d

# 查看日志
docker compose logs -f app

# 重新构建部署
docker compose build app && docker compose up -d app
```

## 注意事项

- AITable singleSelect 字段写入时必须使用**中文显示名**，不能用 option ID
- 云集 Cookie 需定期保活，过期后需手动更新 `.env` 中的 `YUNJI_SESSION_COOKIE` 并重启服务
- PTS Session Cookie 用于 Playwright 自动化（文件上传、工单闭环），过期后需手动更新
- 完成阶段的工单（审核工单、已闭环）会自动从本地 DB 和 AITable 中删除
- 一个 AITable 记录可能含多个 PDF 附件，系统会独立提取后合并（产品名顿号拼接、总结按产品分段）
- 邮件标记为"未上传"时，系统不会触发邮件发送，需客户上传报告后修改字段值
- 已推送到钉钉的工单不会重复推送，只在本地数据库更新

## License

Internal use only — 长亭科技
