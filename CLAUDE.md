# 巡检工单流程自动化项目 — Claude Skill 文档

> 本文档供内网服务器上的 Claude 参考，帮助理解项目架构、运行机制和常见运维操作。

---

## 一、项目概述

**项目名称:** inspection-workflow（巡检工单流程自动化）
**所属公司:** 长亭科技（Chaitin Technology）
**服务地址:** http://10.20.20.208:8100
**项目目录:** `/data/inspect/inspection-workflow`

本服务自动化巡检工单的完整生命周期：
1. **同步**: 从 PTS（内部项目跟踪系统）拉取巡检工单数据 → 存入本地 PostgreSQL → 推送到钉钉 AITable
2. **派单**: 监控 AITable 记录，当满足条件时自动触发云集（Yunji）外包平台派单
3. **邮件**: 监控 AITable 记录，当满足条件时自动发送巡检报告邮件给客户
4. **闭环**: 检测巡检完成后，自动在 PTS 中闭环工单

---

## 二、技术架构

### 后端
- **框架:** FastAPI + Uvicorn
- **ORM:** SQLAlchemy 2.0（异步风格声明式映射）
- **数据库:** PostgreSQL 14+（psycopg v3 驱动）
- **迁移:** Alembic
- **定时任务:** APScheduler（BackgroundScheduler，Asia/Shanghai 时区）
- **PDF 解析:** PyMuPDF
- **AI 提取:** 智谱 AI（GLM-4-flash 模型）
- **邮件发送:** SMTP_SSL（阿里云 SMTP）
- **浏览器自动化:** Playwright（PTS 文件上传/闭环需要）
- **钉钉交互:** dws CLI（通过 subprocess 调用，AITable 记录 CRUD）

### 前端
- **框架:** Vue 3 + TypeScript + Vite
- **UI:** Element Plus
- **图表:** ECharts
- **路由:** Vue Router 4
- **构建产物:** `static/` 目录（Vite 配置直接输出到 `../static/`）

### 外部依赖
| 服务 | 交互方式 | 用途 |
|------|----------|------|
| PTS GraphQL API | Bearer Token 认证 | 拉取/更新工单数据 |
| 钉钉 AITable | dws CLI | 协作表格记录读写 |
| 云集 (yunji.chaitin.cn) | HTTP + Session Cookie | 创建外包需求/派单 |
| 智谱 AI (open.bigmodel.cn) | API Key | PDF 文本结构化提取 |
| 阿里云 SMTP | SSL 465 端口 | 发送巡检邮件 |
| PTS Web | Playwright + Cookie | 文件上传、工单闭环 |

---

## 三、目录结构

```
/data/inspect/inspection-workflow/
├── .env                    # 环境配置（密钥、API Token 等）
├── .env.example            # 环境配置模板
├── alembic.ini             # Alembic 迁移配置
├── deploy.sh               # 一键部署脚本
├── pre-check.sh            # 迁移前环境检查脚本
├── requirements.txt        # Python 依赖列表
├── venv/                   # Python 虚拟环境
├── static/                 # Vue 前端构建产物（服务直接托管）
│
├── apps/api/
│   ├── main.py             # FastAPI 入口（lifespan、路由注册、SPA 静态文件托管）
│   ├── utils.py            # 共享工具函数（fmt_cst: UTC→北京时间）
│   └── routers/
│       ├── health.py       # 健康检查
│       ├── sync.py         # PTS 数据同步端点
│       ├── monitor.py      # 监控/触发端点
│       ├── triggers.py     # 手动触发端点
│       ├── work_orders.py  # 工单查询端点
│       ├── statistics.py   # 统计概览端点
│       ├── email_tool.py   # 邮件工具端点
│       ├── ws.py           # WebSocket 实时事件推送
│
├── core/
│   ├── config.py           # Pydantic Settings（所有配置变量）
│   ├── db.py               # SQLAlchemy engine/session/create_all
│   ├── logging.py          # 日志配置
│
├── models/
│   ├── base.py             # Base、UUIDPrimaryKeyMixin、TimestampMixin
│   ├── work_order.py       # WorkOrder 模型（核心工单表）
│   ├── sync_log.py         # SyncLog 模型（同步日志）
│   ├── trigger_log.py      # TriggerLog 模型（触发日志）
│   ├── aitable_snapshot.py # AITableSnapshot 模型（AITable 快照变更检测）
│   ├── email_pre_analysis.py # EmailPreAnalysis 模型（邮件预分析）
│
├── services/
│   ├── sync_service.py     # PTS→本地DB→AITable 同步流水线
│   ├── trigger_service.py  # 云集派单 & 邵件触发逻辑
│   ├── monitor_service.py  # AITable 轮询监控（派单/邮件/闭环条件检测）
│   ├── dingtalk_client.py  # 钉钉 AITable 客户端（dws CLI 包装）
│   ├── pts_client.py       # PTS GraphQL API 客户端
│   ├── yunji_client.py     # 云集 HTTP 客户端
│   ├── yunji_dispatch.py   # 云集派单全流程
│   ├── email_sender.py     # 郵件发送 + AI 提取
│   ├── email_pre_analysis.py # 邵件预分析（先提取AI结果，发送时重下PDF）
│   ├── snapshot_service.py # AITable 快照 CRUD + 变更检测
│   ├── pts_closure_service.py # PTS 工单自动闭环
│   ├── aitable_fields.py   # AITable 字段ID常量 + 提取函数
│
├── scheduler/
│   ├── jobs.py             # APScheduler 定时任务注册
│
├── migrations/
│   ├── env.py              # Alembic 环境配置
│   └── versions/
│       ├── 001_initial.py  # 初始表结构
│       ├── 002_email_pre_analysis.py
│       ├── 003_add_summaries_column.py
│
├── email_tool/
│   ├── app.py              # 独立 Streamlit 邵件工具（可选）
│
└── frontend/               # Vue 前端源码（构建后产物在 static/）
    ├── src/
    │   ├── api/index.ts    # API 调用函数 + WebSocket
    │   ├── router/index.ts # 路由定义
    │   ├── views/
    │   │   ├── Dashboard.vue    # 统计概览
    │   │   ├── WorkOrders.vue   # 工单列表
    │   │   ├── Monitor.vue      # 监控/派单/邮件待处理
    │   │   ├── Sync.vue         # 数据同步控制
    │   │   ├── EmailTool.vue    # 邵件工具（PDF提取+发送）
```

---

## 四、服务运行方式

### 启动命令
```bash
/data/inspect/inspection-workflow/venv/bin/uvicorn apps.api.main:app --host 10.20.20.208 --port 8100
```

### systemd 管理
```bash
sudo systemctl status inspection-workflow   # 查看状态
sudo systemctl restart inspection-workflow  # 重启
sudo systemctl stop inspection-workflow     # 停止
sudo journalctl -u inspection-workflow -f   # 实时日志
```

### 服务生命周期 (apps/api/main.py lifespan)
1. 启动时：创建数据库表（如果不存在）、启动 APScheduler 定时任务
2. 运行时：FastAPI 处理 HTTP/WebSocket 请求，Scheduler 在后台执行定时任务
3. 关闭时：停止 Scheduler

---

## 五、API 端点一览

### 健康检查
- `GET /api/health` — 检查 PTS API、dws CLI、云集 Session 的可用性

### 数据同步 (sync)
- `POST /api/sync/run` — 从 PTS 拉取工单到本地 DB（可选 sync_month=YYYY-MM）
- `POST /api/sync/push` — 将本地待同步工单推送到 AITable（可选 sync_month）
- `GET /api/sync/logs` — 查看同步日志列表

### 监控/触发 (monitor)
- `POST /api/monitor/poll` — 触发日常增值服务进展 AITable 轮询
- `POST /api/monitor/poll-dispatch` — 触发客户巡检派单 AITable 轮询
- `GET /api/monitor/dispatch-pending` — 查看满足派单条件的 AITable 记录
- `POST /api/monitor/dispatch/{record_id}` — 手动派单（云集需求创建）
- `GET /api/monitor/email-pending` — 查看满足邮件发送条件的 AITable 记录
- `POST /api/monitor/send-email/{record_id}` — 手动发送巡检邮件
- `POST /api/monitor/closure-check` — 手动触发 PTS 工单闭环检查
- `POST /api/monitor/sync-closure-status` — 同步 PTS 闭环状态到本地

### 手动触发 (triggers)
- `POST /api/triggers/yunji/{work_order_id}` — 按 WorkOrder UUID 手动派单
- `POST /api/triggers/email/{work_order_id}` — 按 WorkOrder UUID 手动发邮件
- `GET /api/triggers/logs` — 查看触发日志

### 工单查询 (work-orders)
- `GET /api/work-orders` — 工单列表（支持 month/order_type/status 等筛选）
- `GET /api/work-orders/pending` — 待处理工单列表

### 统计概览 (statistics)
- `GET /api/statistics/overview` — 月度总览
- `GET /api/statistics/by-region` — 按战区统计
- `GET /api/statistics/by-type` — 按工单类型统计
- `GET /api/statistics/by-status` — 按状态统计
- `GET /api/statistics/monthly-trend` — 月度每日趋势
- `GET /api/statistics/triggers` — 触发成功/失败统计

### 邵件工具 (email-tool)
- `POST /api/email-tool/extract` — 上传 PDF + AI 提取巡检信息
- `POST /api/email-tool/re-extract` — 对已存储文件重新 AI 提取
- `POST /api/email-tool/convert-name` — 中文名转 chaitin.com 邮箱（拼音）
- `GET /api/email-tool/fetch-aitable-attachments/{record_id}` — 从 AITable 下载附件并 AI 提取
- `POST /api/email-tool/save-config` — 保存 SMTP 配置
- `GET /api/email-tool/config` — 获取 SMTP 配置（密码脱敏）
- `GET /api/email-tool/history` — 邵件发送历史
- `POST /api/email-tool/send` — 发送巡检邮件
- `GET /api/email-tool/pre-analysis` — 获取预分析结果
- `POST /api/email-tool/pre-analysis/run` — 手动触发预分析
- `POST /api/email-tool/send-direct` — 用预分析数据直接发送

### WebSocket
- `WS /api/ws` — 实时事件推送（sync.started/completed, trigger.success/failed, monitor.poll.completed 等）

---

## 六、定时任务 (scheduler/jobs.py)

| 任务 | 触发方式 | 时间 | 功能 |
|------|----------|------|------|
| PTS→钉钉同步 | CronTrigger | 每天 16:00 | 拉取 PTS 工单 + 推送 AITable |
| AITable 派单轮询 | IntervalTrigger | 每 2 小时 | 检测派单条件并触发云集 |
| 邵件待发送探测 | CronTrigger | 每 2 小时 | 刷新邮件待发送缓存 |
| PTS 闭环检查 | CronTrigger | 每天 10:00 | 同步 PTS 闭环状态 + 执行闭环 |
| 云集 Cookie 保活 | IntervalTrigger | 每 3 小时 | 访问云集页面保持 Session |
| 邵件预分析 | CronTrigger | 每天 9:00 | AI 提取邮件待发送记录（默认禁用） |

**注意:** `auto_dispatch_enabled` 和 `auto_email_enabled` 默认为 false，定时任务只做检测不做自动触发。`email_pre_analysis_enabled` 也默认为 false。

---

## 七、核心业务流程

### 7.1 同步流程 (sync_service.py)

```
PTS GraphQL API → pts_client.query_inspection_work_orders()
                    ↓ 筛选条件: type=产品巡检/日志分析, after_sale=冯伟, 未完成
                → _extract_fields(raw) 映射字段
                    ↓ 区域映射: assigner_name → _PERSON_TO_REGION → 战区名
                → 本地 DB upsert（按 pts_order_id 唯一键）
                    ↓
                → push_to_aitable: 逐条调用 dingtalk_client.create_records
                    ↓ _work_order_to_cells(wo) 转换为 AITable 字段
                → 更新 dt_sync_status = "synced"/"failed"
                → WebSocket 广播 sync.completed/sync.pushed
```

**重要:** 完成阶段的工单（审核工单、已闭环）会从本地 DB 和 AITable 中删除。

### 7.2 派单流程 (monitor_service + yunji_dispatch)

```
AITable 轮询 → dingtalk_client.query_records(客户巡检派单表)
              ↓ 检测条件: 伙伴供应商已填 + 需求编号为空 + 工程师已填
              → get_dispatch_pending 返回待派单列表
              ↓
              → trigger_manual_dispatch:
                  1. 从 AITable 字段获取 PTS URL + 供应商名
                  2. yunji_dispatch.create_yunji_requirement:
                     a. fetch_pts_info: 先查本地 DB，再查 PTS GraphQL
                     b. resolve_delivery_info: assigner → 部门 → 区域负责人
                     c. yunji_client API 调用: 创建需求 + 创建订单
                  3. 写回 AITable: 需求编号 + 订单编号
                  4. 创建 TriggerLog
                  5. WebSocket 广播 trigger.dispatch.success/failed
```

### 7.3 邵件流程 (monitor_service + email_sender/email_pre_analysis)

**方式一: 直接发送 (Monitor 页面)**
```
AITable 轮询 → 检测条件: 有巡检报告附件 + 邵件是否发送≠是
              → trigger_manual_email:
                  1. 下载 PDF 附件
                  2. PyMuPDF 提取文本
                  3. 智谱 AI 结构化提取（客户名、产品名、数量、邮箱、总结）
                  4. SMTP 发送巡检邮件
                  5. 写回 AITable: 邵件是否发送=是
                  6. 触发 PTS 工单闭环
```

**方式二: 预分析 + 直接发送 (EmailTool 页面)**
```
预分析阶段 → run_email_pre_analysis:
              1. 查找邮件待发送的 AITable 记录
              2. 下载 PDF 附件，AI 提取
              3. 存入 EmailPreAnalysis 表
              4. 前端显示提取结果

发送阶段 → send_email_from_pre_analysis:
              1. 使用已存储的 AI 提取结果
              2. 重新下载 PDF 附件（确保最新）
              3. SMTP 发送邮件
              4. 写回 AITable + 触发 PTS 闭环
```

**多报告支持:** 一个 AITable 记录可能有多个 PDF 附件，系统会对每个 PDF 独立 AI 提取，然后合并结果：
- 产品名: 用顿号拼接（如 "雷池、洞鉴"）
- 数量: 带产品名拼接（如 "1套雷池、4台洞鉴"）
- 总结: 多报告按 `【产品名】\n总结` 分段

### 7.4 闭环流程 (pts_closure_service)

```
run_closure_check → 查找未闭环工单
                  ↓ 匹配 AITable: 巡检是否完成=是 + 有巡检报告
                  → _close_single_work_order:
                      1. pts_client.add_work_order_info: 添加备注
                      2. pts_client.confirm_work_order_stage: 推进阶段（最多10次）
                  → 更新本地 closure_status + AITable 工单是否闭环=是
```

---

## 八、数据库模型

### WorkOrder (work_orders 表) — 核心工单
| 关键字段 | 说明 |
|----------|------|
| pts_order_id | PTS 工单 ID（唯一索引） |
| customer_name | 客户名称 |
| product_name | 产品名（雷池、洞鉴、谛听等） |
| engineer | 工程师 |
| region | 战区（华东/华南/华北东北等8个） |
| dt_record_id | AITable 记录 ID |
| dt_sync_status | 同步状态: pending/synced/failed |
| dispatch_status | 派单状态: 待派单/已派单/派单失败 |
| email_trigger_status | 邵件状态: 待发送/已发送/发送失败 |
| closure_status | 闭环状态: 未闭环/已闭环/闭环中/闭环失败 |

### 其他表
- **SyncLog** (sync_logs): 同步日志（fetched/created/updated/skipped 计数）
- **TriggerLog** (trigger_logs): 触发日志（request/response payload, retry_count）
- **AITableSnapshot** (aitable_snapshots): AITable 快照（用于变更检测）
- **EmailPreAnalysis** (email_pre_analysis): 邵件预分析（AI 提取结果 + summaries JSONB）

---

## 九、配置变量 (.env)

### 必须配置
| 变量 | 说明 |
|------|------|
| DATABASE_URL | PostgreSQL 连接字符串 |
| PTS_API_TOKEN | PTS GraphQL API Bearer Token |
| DT_AITABLE_BASE_ID | 钉钉 AITable Base ID（日常增值服务进展） |
| DT_AITABLE_TABLE_ID | 钉钉 AITable Table ID（日常增值服务进展） |
| DT_DISPATCH_BASE_ID | 钉钉 AITable Base ID（客户巡检派单） |
| DT_DISPATCH_TABLE_ID | 钉钉 AITable Table ID（客户巡检派单） |
| AI_API_KEY | 智谱 AI API Key |

### 可选配置
| 变量 | 说明 |
|------|------|
| PTS_SESSION_COOKIE | PTS Web Session Cookie（文件上传/闭环需要） |
| YUNJI_SESSION_COOKIE | 云集 Session Cookie（yunji_session_id + go-server-token 格式） |
| INSPECTION_EMAIL_PASSWORD | 邵件 SMTP 密码 |
| AUTO_DISPATCH_ENABLED | 自动派单（默认 false） |
| AUTO_EMAIL_ENABLED | 自动发邮件（默认 false） |
| EMAIL_PRE_ANALYSIS_ENABLED | 邵件预分析定时任务（默认 false） |

---

## 十、钉钉 AITable 交互注意事项

### dws CLI
所有 AITable 操作通过 `dws` CLI 完成（子进程调用），需要已认证（`dws auth status` 显示 authenticated）。

### 单选字段 (singleSelect)
**关键:** 写入 AITable singleSelect 字段时，必须使用**中文名称**（如 "华南战区"), 不能使用 option ID（如 "Dk7TGiNHfP"）。AITable 会将 option ID 当作新的选项名创建。

### 两个 AITable 表
1. **日常增值服务进展** — 主数据源，字段 ID 在 `aitable_fields.py` 的 `DAILY_SERVICE` 常量
2. **客户巡检派单** — 派单/邮件操作表，字段 ID 在 `aitable_fields.py` 的 `DISPATCH` 常量

---

## 十一、常见运维操作

### 查看服务日志
```bash
sudo journalctl -u inspection-workflow -f --since "1 hour ago"
```

### 重启服务
```bash
sudo systemctl restart inspection-workflow
```

### 数据库迁移
```bash
cd /data/inspect/inspection-workflow
source venv/bin/activate
alembic upgrade head
```

### 手动同步
```bash
curl -X POST http://10.20.20.208:8100/api/sync/run
curl -X POST "http://10.20.20.208:8100/api/sync/run?sync_month=2026-06"
```

### 手动推送 AITable
```bash
curl -X POST http://10.20.20.208:8100/api/sync/push
```

### 手动触发派单/邮件
```bash
# 查看待派单记录
curl http://10.20.20.208:8100/api/monitor/dispatch-pending

# 手动派单
curl -X POST http://10.20.20.208:8100/api/monitor/dispatch/{record_id}

# 查看待发送邮件
curl http://10.20.20.208:8100/api/monitor/email-pending

# 手动发邮件
curl -X POST http://10.20.20.208:8100/api/monitor/send-email/{record_id}
```

### 触发闭环检查
```bash
curl -X POST http://10.20.20.208:8100/api/monitor/closure-check
```

### 检查云集 Cookie
```bash
curl http://10.20.20.208:8100/api/health
# 返回中 yunji_session 字段显示 session 是否有效
```

### 更新云集 Cookie
1. 登录 yunji.chaitin.cn
2. 从浏览器 Cookie 中获取 `yunji_session_id` 和 `go-server-token`
3. 编辑 `.env`，更新 `YUNJI_SESSION_COOKIE`（格式: `yunji_session_id=xxx; go-server-token=xxx`）
4. 重启服务

### 更新 PTS Token/Cookie
1. 编辑 `.env`，更新 `PTS_API_TOKEN` 或 `PTS_SESSION_COOKIE`
2. 重启服务

### 前端重新构建
```bash
cd /data/inspect/inspection-workflow/frontend
npm install
npm run build
# 构建产物自动输出到 ../static/
# 无需重启服务，静态文件实时生效
```

---

## 十二、关键静态映射表

### 区域映射 (sync_service.py _PERSON_TO_REGION)
约60个工程师名字 → 8个战区：
华东战区、华南战区、华北东北战区、西南西北战区、金融头部战区、政府头部战区、通信头部战区、华中战区

### 供应商映射 (yunji_dispatch.py SUPPLIER_MAP)
10个供应商简称 → 全称，如 "平云" → "成都平云小匠网络有限公司"

### 派单参数 (yunji_dispatch.py DEFAULTS)
默认值: unit_price=600, delivery_content="产品巡检", outsource_specialist="冯伟"

---

## 十三、故障排查

| 问题 | 检查方法 |
|------|----------|
| 服务无法启动 | `journalctl -u inspection-workflow -n 50` 查看错误 |
| AITable 操作失败 | `dws auth status` 检查认证；检查 dws CLI 版本 |
| PTS 拉取失败 | `/api/health` 检查 pts_api 状态；检查 PTS_API_TOKEN |
| 云集派单失败 | `/api/health` 检查 yunji_session；可能需要更新 Cookie |
| 邵件发送失败 | 检查 SMTP 配置（host/port/password）；检查阿里云 SMTP 限制 |
| AI 提取失败 | 检查 AI_API_KEY；检查智谱 AI API 可达性 |
| 工单不闭环 | 检查 PTS_SESSION_COOKIE 是否有效；检查 Playwright 是否可用 |
| 数据库连接失败 | 检查 PostgreSQL 服务；检查 .env 中 DATABASE_URL |
| Alembic 迁移失败 | 检查 `migrations/env.py` 中 sys.path 是否包含项目根目录 |

---

## 十四、WebSocket 实时事件

前端通过 `WS /api/ws` 接收实时事件，事件类型：
- `sync.started` / `sync.completed` / `sync.pushed`
- `trigger.dispatch.success` / `trigger.dispatch.failed`
- `trigger.email.success` / `trigger.email.failed`
- `monitor.poll.completed` / `monitor.dispatch_poll.completed`
- `monitor.closure_check.completed`
- `work_order.closure_updated`

WebSocket 断线后 5 秒自动重连。

---

## 十五、重要提醒

1. **AITable singleSelect 字段必须写中文显示名，不能写 option ID**
2. **云集 Cookie 需要定期保活**（定时任务每3小时访问一次），过期后需手动更新 `.env`
3. **PTS Session Cookie 用于 Playwright 浏览器自动化**（文件上传和工单闭环），过期后需手动更新
4. **完成阶段工单会自动删除**（审核工单、已闭环的工单从本地 DB 和 AITable 中清除）
5. **dt_sync_status 只在 AITable 写入成功且有有效 record_id 时才标记为 synced**，防止幽灵同步记录
6. **多 PDF 报告合并逻辑**：产品名顿号拼接、数量带产品名、总结按产品分段