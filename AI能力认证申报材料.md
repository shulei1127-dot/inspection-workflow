# AI 能力认证申报材料 — inspection-workflow（巡检工单流程自动化）

目前巡检工单管理涉及 PTS 工单系统、云集外包平台、钉钉 AITable 协作表格、SMTP 邮件等多个平台，数据分散割裂，导致工单同步耗时、外包派单需人工逐条登录云集创建、巡检报告邮件需人工阅读 PDF 手动撰写发送、工单闭环需人工逐条推进阶段等痛点。运用 AI 独立开发了一套自动化管理工具集，实现巡检工单全生命周期自动化。

---

## 项目经历

本项目使用 AI 工具独立完成代码开发、系统架构设计、API 集成和流程编排，将巡检工单管理中分散在多个系统中的调步、派单、邮件、闭环业务流程整合为一站式自动化平台。项目整体以 FastAPI 作为 Web 服务框架，Vue 3 作为前端可视化界面，以 Claude Code 作为 AI 辅助编程工具，通过 PTS API、云集 API、钉钉 AITable、智谱 AI GLM-4-flash 等能力，将巡检工单的 PTS 数据同步、外包派单、AI 邮件生成与发送、工单自动闭环串联成可运行、可追踪、可复核的自动化闭环。

项目定位协助售后部门更高效管理巡检工单的整个生命周期，人工负责业务判断和结果复核。

项目地址：
https://github.com/shulei1127-dot/inspection-workflow（请确认仓库已设为公开或对评审方开放查看权限）

内网服务地址：http://10.20.20.208:8100

### 项目详情

在编码阶段，项目代码主要分为五个子模块：

**第一个模块是 PTS 数据同步模块**，包括 PTS GraphQL 工单数据查询、本地 PostgreSQL 存储、AITable 数据推送、同步日志记录、WebSocket 实时事件推送等能力。通过 PTS GraphQL API 定时拉取巡检工单（筛选条件：类型=产品巡检/日志分析、售后负责人=冯伟、未闭环），将 60+ 交付工程师自动映射至 8 个战区，数据经本地 DB 持久化后逐条推送至钉钉 AITable。

**第二个模块是外包派单模块**，包括 AITable 轮询监控、云集需求创建、供应商匹配、交付分配人解析、需求编号/订单编号回写等能力。当 AITable 记录满足派单条件（伙伴供应商已填 + 需求编号为空 + 工程师已填）时，自动在云集外包平台创建需求并生成订单，支持 10 家外包供应商的模糊匹配。

**第三个模块是 AI 邮件模块**，包括 PDF 巡检报告下载、PyMuPDF 文本提取、智谱 AI（GLM-4-flash）结构化提取、多报告智能合并、SMTP 邮件发送、邮件预分析缓存等能力。通过 AI 从 10-50 页的巡检 PDF 中自动提取客户名称、产品名称、巡检时间、数量、邮箱、巡检总结等信息，生成专业格式的巡检邮件并自动发送给客户，支持多产品多报告的智能合并。

**第四个模块是工单闭环模块**，包括 PTS 闭环状态同步、工单推进阶段、Playwright 浏览器自动化闭环等能力。检测巡检完成状态后，自动在 PTS 中添加备注、推进工单阶段至完成，并同步更新本地 DB 和 AITable 闭环状态。

**第五个模块是中央管理面板**，包括 FastAPI 应用入口、Vue 3 前端仪表盘（统计概览/工单列表/监控面板/同步控制/邮件工具）、WebSocket 实时事件推送、APScheduler 定时任务管理等能力。

### 线上运行

项目已在长亭科技售后巡检交付场景中正式运行，用于巡检工单 PTS 数据同步、云集外包派单、AI 巡检邮件生成与发送、PTS 工单自动闭环。

线上运行过程中遇到的问题主要包括：PTS GraphQL API Token 模式不支持 `$variable` 参数化查询、钉钉 AITable 单选字段写入时 option ID 被当作新选项名而非匹配已有选项、云集 Session Cookie 过期后所有派单接口返回 302 重定向而非明确报错、多个 PDF 巡检报告合并时 AI 提取结果存在重复和中文数量词聚合困难、智谱 AI 常把 PDF 模板创建日期误当作实际巡检时间等。针对这些问题，项目通过实现 GraphQL 变量内联函数、统一使用中文显示名写入 AITable、增加云集 Cookie 保活定时任务和过期检测、开发 SequenceMatcher 去重算法和中文数字合并算法、优化 AI Prompt 引导 AI 区分日期来源等方式进行修复和优化。

---

## AI 工具使用

项目中主要使用的 AI 工具和相关能力包括：

**1. Claude Code（命令行模式）**：作为 AI 辅助编程核心工具，用于代码生成、系统架构设计、API 集成开发、Bug 修复和代码优化。通过自然语言描述业务需求，AI 直接生成可运行的 Python/FastAPI/Vue 代码。

**2. Claude Code（编辑器模式）**：用于代码调试和问题定位，AI 辅助分析 PTS API 返回数据、排查云集派单失败根因（如云集 Session 过期、CRM 项目不存在导致的 `NoneType` 异常）、定位 AITable 写入字段格式问题。

**3. Claude Code（推理模式）**：用于系统架构设计和技术方案选型，AI 分析 FastAPI + SQLAlchemy 异步方案 vs Flask 同步方案的优劣，设计多系统集成数据流和业务流程编排架构，评估 PTS 闭环方案（GraphQL API 直连 vs Playwright 浏览器自动化）。

**4. 智谱 AI GLM-4-flash（PDF 巡检报告智能提取）**：通过智谱 AI API 对接 AI 模型，从巡检报告 PDF 文本中结构化提取客户名称、产品名称、巡检时间、数量、邮箱、巡检总结等字段，结合 Prompt Engineering（引导 AI 区分模板日期与实际日期、提取编号列表格式的巡检结论、严格 JSON 输出）和多报告智能合并算法（产品名顿号拼接、中文数量词聚合、SequenceMatcher 去重、内容自动排版），实现巡检邮件的全自动生成。

**5. PTS GraphQL API**：通过 Bearer Token 认证对接公司内部 PTS 工单系统，实现工单列表查询（支持工单类型、售后负责人、完成状态筛选）、工单详情查询、工单备注添加、工单阶段推进等能力，解决 Token 模式不支持 `$variable` 参数化查询的技术限制。

**6. 云集 API**：通过 HTTP + Session Cookie 对接云集外包平台，实现购物车创建、CRM 项目信息查询、项目产品查询、供应商列表获取、用户列表获取、预算计算、需求创建、订单查询等全链路 API 调用。

**7. 钉钉 AITable / DWS CLI**：通过 dws CLI 命令行工具对接钉钉在线表格，实现记录查询、创建、更新、删除能力；通过钉钉机器人推送派单成功/失败通知。

**8. GitHub**：存放项目代码、部署说明、运行文档和版本迭代记录。

---

## 业务成果

项目已形成一套面向售后巡检交付业务的自动化管理平台，能够辅助完成 PTS 工单同步、外包派单、AI 邮件生成与发送、工单自动闭环。

项目价值主要体现在：

**1. 降低人工操作成本**：自动化处理工单同步、派单创建、邮件撰写、工单闭环等重复性工作，人工负责业务判断和结果复核。

**2. 提升数据准确性**：自动校验 PTS 工单与钉钉 AITable 的数据一致性，实现双向同步；AI 自动从 PDF 提取巡检信息并生成邮件，避免人工阅读遗漏关键信息。

**3. 提升业务响应速度**：巡检邮件从人工阅读 PDF 手动撰写 15-20 分钟缩短到 AI 自动提取生成 1-2 分钟；PTS → AITable 数据同步从人工逐条录入 30+ 分钟缩短到定时自动同步 2 分钟。

**4. 减少业务流失风险**：定时检查工单是否满足派单/邮件/闭环条件，通过 WebSocket 实时推送状态变更，前端仪表盘可视化展示待处理项。

**5. 沉淀运营知识资产**：60+ 工程师区域映射、10 家供应商匹配、8 个战区划分、工单同步规则、派单条件、邮件模板等业务规则固化到代码中，不依赖个人经验。

**6. 业务覆盖全面**：覆盖 5 大产品线（雷池 WAF / 洞鉴扫描器 / 谛听 NDR / 牧云 CWPP / 万象 BAS）、8 个战区、10 家外包供应商。

**7. 7×24 稳定运行**：6 个 APScheduler 定时任务自动化执行（PTS 同步每天 16:00、派单轮询每 5 分钟、邮件探测每 2 小时、闭环检查每天 10:00、Cookie 保活每 3 小时、邮件预分析每天 9:00），20+ REST API 端点 + WebSocket 实时事件推送。

**8. 与个人岗位强相关**：项目直接服务于售后巡检交付业务管理，符合个人岗位职责和公司业务需求。

---

## 可验证材料链接

### 仓库地址

- GitHub 代码仓库链接：https://github.com/shulei1127-dot/inspection-workflow（请确认仓库已设为公开或对评审方开放查看权限）
- 说明：用于证明项目代码、部署说明、运行入口和版本迭代记录。

### 项目文档

- 项目首页 / README：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/README.md
- CLAUDE.md 项目知识文档：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/CLAUDE.md
- 说明：用于证明项目背景、功能概览、技术架构、运维手册、故障排查指南的完整说明。

### 核心代码

**AI 邮件模块：**
- AI 提取 + 多报告合并核心逻辑：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/email_pre_analysis.py
- 智谱 AI 调用 + Prompt 设计 + 邮件发送：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/email_sender.py
- 说明：用于证明 AI 邮件模块的完整实现，包括智谱 AI GLM-4-flash 的 PDF 文本结构化提取、Prompt Engineering、多报告智能合并、SMTP 邮件发送能力。

**外包派单模块：**
- 云集派单全流程编排：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/yunji_dispatch.py
- 云集 HTTP API 客户端：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/yunji_client.py
- 触发服务（派单 + 邮件）：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/trigger_service.py
- 说明：用于证明外包派单模块的完整实现，包括云集 API 对接、供应商匹配、交付分配人解析、幂等性保证能力。

**PTS 数据同步模块：**
- PTS GraphQL API 客户端：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/pts_client.py
- 同步服务流水线：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/sync_service.py
- 说明：用于证明 PTS 数据同步模块的完整实现，包括 GraphQL 查询、变量内联、频率控制、本地 DB 存储、AITable 推送能力。

**工单闭环模块：**
- PTS 闭环服务：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/pts_closure_service.py
- 说明：用于证明工单闭环模块的完整实现，包括 PTS 闭环状态检测、工单备注添加、工单阶段自动推进能力。

**AITable 轮询监控模块：**
- 监控服务（派单/邮件/闭环条件检测）：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/monitor_service.py
- 钉钉 AITable 客户端：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/dingtalk_client.py
- AITable 字段常量 + 提取函数：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/services/aitable_fields.py
- 说明：用于证明 AITable 轮询监控模块的完整实现，包括派单/邮件/闭环条件检测、AITable 读写能力。

**中央管理面板：**
- FastAPI 应用入口：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/apps/api/main.py
- 定时任务编排：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/scheduler/jobs.py
- 配置管理：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/core/config.py
- 前端仪表盘：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/frontend/src/views/Dashboard.vue
- 说明：用于证明中央管理面板的完整实现，包括 FastAPI 路由注册、6 个 APScheduler 定时任务、Pydantic 配置管理、Vue 3 前端仪表盘能力。

### 部署与运维

- Docker Compose 配置：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/docker-compose.yml
- Dockerfile：https://github.com/shulei1127-dot/inspection-workflow/-/blob/main/Dockerfile
- 数据库迁移（Alembic）：https://github.com/shulei1127-dot/inspection-workflow/-/tree/main/migrations
- 说明：用于证明项目的容器化部署能力、数据库 schema 变更管理能力。
