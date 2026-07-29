# 巡检邮件后报告上传与工单阶段自动化实施计划

## 1. 背景与目标

当前 `/tmp` 目录中的 `july_links.py`、`july_links_retry.py`、`july_stage.py`、`july_stage_only.py` 等脚本，曾用于人工批量处理以下动作：

1. 将钉钉数据表中的巡检报告附件上传到 PTS，并在 PTS 工单备注中写入 Markdown 报告链接；
2. 尝试将 PTS 工单处理阶段推进到“审核工单”；
3. 负责人权限不足时，发送钉钉提醒，要求人工指定负责人后继续推进。

后续需要将上述流程正式接入现有项目：

- 当钉钉“客户巡检派单”表中的“邮件是否发送=是”时，自动触发报告上传、Markdown 链接补充和阶段推进；
- 报告上传和 Markdown 链接验证成功，但因负责人权限无法自动推进时，立即向默认钉钉群发送人工提醒；
- 同步维护“工单是否闭环”和“Markdown链接报告是否上传”两个字段；
- 不影响当前同步、派单、邮件发送及旧版闭环流程，支持灰度、回滚和失败补偿。

## 2. 当前代码情况与主要缺口

现有正式代码已经具备部分能力：

- [services/pts_client.py](services/pts_client.py) 已有 PTS 工单查询、文件上传、备注写入和阶段推进接口；
- [services/pts_closure_service.py](services/pts_closure_service.py) 已有自动闭环逻辑和钉钉人工提醒逻辑；
- [services/monitor_service.py](services/monitor_service.py) 已有 AITable 轮询和闭环检查；
- [services/email_pre_analysis.py](services/email_pre_analysis.py) 与 [apps/api/routers/email_tool.py](apps/api/routers/email_tool.py) 在邮件成功后会尝试调用闭环逻辑；
- [scheduler/jobs.py](scheduler/jobs.py) 已有定时闭环补偿任务。

但当前实现不能直接满足需求：

1. 当前只要 PTS 中存在任意一个报告，就可能跳过其他缺失附件；
2. 负责人权限不足时，当前逻辑可能在报告上传前直接进入人工分支；
3. 代码中已有“工单是否闭环”字段，但没有登记“Markdown链接报告是否上传”的真实字段 ID；
4. 当前部分路径只回写“邮件是否发送”，不同入口的闭环行为不统一；
5. 缺少附件级幂等、阶段级状态和 AITable 写回补偿；
6. 临时脚本没有数据库锁，重复执行可能造成重复上传和重复追加 Markdown 备注。

## 3. 总体架构

采用“邮件成功后的即时触发 + 定时补偿”的统一协调器：

```text
邮件是否发送=是
        ↓
检查 AITable 报告附件和 PTS 工单链接
        ↓
查询 PTS 当前状态和已有报告链接
        ↓
逐附件复用或上传 PTS 文件
        ↓
写入 Markdown 链接并重新查询验证
        ↓
尝试设置负责人并推进到“审核工单”
        ↓
验证 PTS 最终阶段
        ↓
同时回写钉钉两个状态字段
```

统一协调器由以下入口调用：

- 邮件发送成功后的即时调用；
- `POST /api/monitor/closure-check` 手动调用；
- scheduler 定时补偿；
- 后续必要时由报告字段变化触发补偿。

禁止直接把 `/tmp` 脚本 import 到生产服务中。

## 4. 第一阶段：确认 AITable 字段

### 4.1 确认字段 ID

当前 [services/aitable_fields.py](services/aitable_fields.py) 已知字段：

| 字段 | 当前 ID |
|---|---|
| 邮件是否发送 | `ZzlBIoW` |
| 巡检报告 | `nd284rT` |
| 巡检工单链接 | `gwpCr99` |
| 工单是否闭环 | `0FdeEiC` |

需要通过 AITable schema discovery 或 dws 查询确认：

- “Markdown链接报告是否上传”的真实 field ID；
- 字段类型；
- 是否存在“是”和“否”两个选项。

在字段 ID 未确认前，不允许在代码中写入占位 ID。

### 4.2 配置方式

将真实字段 ID 加入 `DISPATCH` 常量，并在配置校验中检查字段是否存在。

如果字段 ID 缺失、字段不存在或选项不符合预期，则新流程 fail-closed：记录错误并不执行 AITable 状态写入，避免误写其他字段。

## 5. 第二阶段：增加独立配置开关

在 [core/config.py](core/config.py) 增加独立配置，默认关闭：

```text
INSPECTION_CLOSURE_V2_ENABLED=false
INSPECTION_CLOSURE_DRY_RUN=true
INSPECTION_CLOSURE_REPORT_UPLOAD_ENABLED=false
INSPECTION_CLOSURE_STAGE_ADVANCE_ENABLED=false
INSPECTION_CLOSURE_AITABLE_WRITEBACK_ENABLED=false
INSPECTION_CLOSURE_MANUAL_NOTIFY_ENABLED=false
DT_DISPATCH_REPORT_LINK_UPLOADED_FIELD_ID=
```

同时增加：

- 最大报告上传重试次数；
- 最大阶段推进次数；
- 429/5xx 退避时间；
- 可选的工单白名单，用于灰度验证。

默认关闭时，现有同步、云集派单、邮件发送和旧版闭环逻辑保持不变。

## 6. 第三阶段：实现统一闭环协调器

重点改造 [services/pts_closure_service.py](services/pts_closure_service.py)，将现有单体逻辑拆分为以下职责。

### 6.1 报告协调

新增报告协调函数，负责：

1. 查询 PTS 工单的 `info.note` 和 `info.file`；
2. 对每个钉钉附件生成稳定 fingerprint：
   - 优先使用钉钉附件 ID；
   - 没有稳定 ID 时使用下载内容 SHA-256、文件名和文件大小；
3. 根据 fingerprint 或已验证的 Markdown 链接复用已有 file ID；
4. 只上传缺失附件；
5. 写入类似以下内容的 PTS 工单备注：

```text
巡检报告Markdown链接已补充（2个附件）
[报告A.pdf](/f/file-id-a)
[报告B.docx](/f/file-id-b)
```

6. 写入后重新查询 PTS，确认每个应有附件均存在有效 Markdown 链接。

不能继续使用“只要存在一个报告就认为全部完成”的布尔判断。

### 6.2 阶段推进

新增阶段推进函数，负责：

1. 每次推进前查询 PTS 当前阶段；
2. 已经处于“审核工单”“结束”或“已闭环”时幂等返回；
3. 当前处于“指定工单负责人”时，首次调用 `confirm_work_order_stage` 传入默认负责人 ID；
4. 其他阶段不重复传入负责人；
5. 每次 mutation 后重新查询阶段；
6. 达到“审核工单”后停止；
7. 限制最大推进次数，避免死循环。

### 6.3 负责人权限不足

如果负责人权限不足、PTS 返回“需要设置负责人”或其他明确权限错误：

1. 不回滚已经完成的报告上传；
2. 确认所有 Markdown 链接已经验证成功；
3. “Markdown链接报告是否上传”写为“是”；
4. “工单是否闭环”写为“否”；
5. 保存当前负责人、创建人、当前阶段和错误原因；
6. 使用 [services/dingtalk_notifier.py](services/dingtalk_notifier.py) 向默认钉钉群发送提醒；
7. 同一工单、同一阶段和同一原因只发送一次提醒，避免定时任务重复刷屏。

提醒内容至少包含：

- 客户名称；
- PTS 工单链接；
- 当前负责人；
- 当前阶段；
- 需要指定的负责人；
- 需要人工推进到的目标阶段。

## 7. 第四阶段：统一接入所有入口

### 7.1 定时闭环检查

修改 [services/monitor_service.py](services/monitor_service.py) 的闭环入口：

- 条件固定为“邮件是否发送=是、巡检报告非空、工单是否闭环!=是”；
- AITable 查询失败不能当作空表处理；
- V2 开启时调用新协调器；
- V2 关闭时保留原逻辑。

### 7.2 邮件发送成功入口

统一修改以下路径：

- [services/email_pre_analysis.py](services/email_pre_analysis.py) 的邮件成功处理；
- [apps/api/routers/email_tool.py](apps/api/routers/email_tool.py) 的直接邮件发送；
- [services/monitor_service.py](services/monitor_service.py) 的手动邮件发送。

邮件发送成功后先可靠回写“邮件是否发送=是”，再调用统一协调器。若即时协调失败，由定时任务补偿，不影响邮件发送结果。

### 7.3 Scheduler

修改 [scheduler/jobs.py](scheduler/jobs.py)：

- V2 开启时使用新协调器；
- V2 关闭时维持现有闭环任务；
- 保持 `max_instances=1`；
- 增加数据库锁，防止 scheduler 与 API 同时处理同一工单；
- 输出报告完成、阶段完成、人工处理、可重试失败、最终失败和 AITable 写回失败统计。

## 8. AITable 状态回写规则

两个字段尽量通过一次 `update_records` 同时写回，并在写回后重新读取验证。

| 实际结果 | Markdown链接报告是否上传 | 工单是否闭环 |
|---|---:|---:|
| 所有报告链接验证成功，PTS 阶段达到“审核工单” | 是 | 是 |
| 报告链接验证成功，但负责人权限不足 | 是 | 否 |
| 任一附件下载或上传失败 | 否 | 否 |
| Markdown 备注写入或链接验证失败 | 否 | 否 |
| PTS 工单链接或报告附件缺失 | 否 | 否 |
| PTS 已成功，但 AITable 暂时写回失败 | 本地待补偿 | 本地待补偿 |

不要将“需人工处理”写入单选字段，除非确认该选项真实存在。人工状态和原因应记录在本地审计表和钉钉通知中。

## 9. 第五阶段：增加审计和幂等

新增 `inspection_closure_attempts` 模型及 Alembic 迁移，建议记录：

- AITable record ID；
- PTS order ID；
- 报告 fingerprint；
- 当前状态；
- 附件元数据；
- PTS file ID；
- PTS 阶段前后值；
- 重试次数；
- 最后错误及错误分类；
- 下次重试时间；
- 人工通知时间；
- 两个 AITable 字段的回写时间；
- 创建时间和更新时间。

建议状态：

```text
ELIGIBLE
  → REPORT_RECONCILING
  → REPORT_READY
  → STAGE_RECONCILING
  → COMPLETED
```

异常状态：

```text
MANUAL
RETRYABLE_FAILED
FAILED_FINAL
```

增加唯一约束：

```text
(record_id, pts_order_id, report_fingerprint)
```

并发控制：

- 本地 WorkOrder 使用数据库行锁；
- AITable-only 记录使用审计表唯一键或 PostgreSQL advisory lock；
- 每个阶段完成后保存状态，进程重启后可以继续，不重复执行已完成动作。

继续使用 `TriggerLog` 记录上传、备注写入、阶段推进、人工提醒和 AITable 写回动作，但不得记录 PTS Token、完整下载 URL 或敏感邮件信息。

## 10. 重试和幂等规则

1. 对网络错误、429、5xx 进行有限次数指数退避；
2. 权限错误、字段不存在、附件无下载地址直接进入人工或最终失败；
3. 上传成功后立即保存 file ID；
4. 重试写 Markdown 前，先查询目标链接是否已存在；
5. 阶段推进超时后先查询当前阶段，再决定是否重试；
6. PTS 已成功但 AITable 写回失败时，只补偿 AITable 写回；
7. 不能使用无条件追加 note 的方式重试；
8. 不能使用 `zip(attachments, file_ids)` 处理部分成功的上传结果，必须使用每个附件自己的处理结果。

## 11. 测试计划

新增闭环相关单元测试和集成测试，至少覆盖：

1. 邮件为“是”且报告存在时进入流程；
2. 邮件不是“是”时跳过；
3. 多附件部分已存在时只补传缺失附件；
4. 已有 Markdown 链接时不重复上传和追加备注；
5. 上传部分失败时文件名和 file ID 不错配；
6. 备注写入超时但回查成功时不重复追加；
7. 报告上传成功后负责人权限不足，仍然发送人工提醒；
8. 权限不足时两个 AITable 字段为“是/否”；
9. 已经处于“审核工单”时重复执行不会产生副作用；
10. 阶段推进只在首次需要时传入负责人；
11. AITable 写回失败后下一轮只补偿写回；
12. scheduler 和手动 API 并发时只执行一次；
13. 缺少报告上传字段 ID 时 fail-closed；
14. dry-run 模式不上传、不写 PTS、不推进阶段、不写 AITable。

## 12. 上线步骤

1. 备份 AITable 字段定义和数据库；
2. 确认“Markdown链接报告是否上传”字段及“是/否”选项；
3. 增加数据库迁移并部署代码；
4. 保持所有 V2 开关关闭，验证现有服务正常；
5. 开启 dry-run，使用少量白名单记录核对预期结果；
6. 先开启报告检查、上传、链接验证和人工通知，关闭阶段推进；
7. 人工确认 PTS Markdown 链接正确后，再小范围开启阶段推进；
8. 观察重复上传率、链接验证率、限流错误、人工处理数量和 AITable 写回失败数；
9. 稳定后逐步扩大范围，最后全量开启。

## 13. 回滚方案

发现异常时：

1. 先关闭 V2 和阶段推进开关；
2. 停止新增 PTS 上传、备注和阶段变更；
3. 保留已上传的文件和 Markdown 备注，不自动删除或回退 PTS 数据；
4. 使用审计记录补偿 AITable 字段；
5. 恢复旧版逻辑或回滚代码；
6. 处理遗留人工工单。

由于新功能默认关闭且与旧闭环逻辑隔离，回滚不会影响现有同步、派单和邮件发送。

## 14. 临时脚本不可直接复用的原因

`/tmp` 下脚本只能作为业务行为参考，不能直接 import 到生产服务：

- 硬编码客户、负责人和工单；
- 没有数据库状态和并发锁；
- 只按文件名去重，同名文件可能误判；
- 部分上传失败时可能导致文件与 file ID 错配；
- 重复执行可能重复上传和追加 Markdown 备注；
- 没有统一错误分类和补偿机制；
- 没有可靠验证 AITable 写回结果；
- 不能区分“报告上传成功但阶段推进失败”；
- 可能触发 PTS 限流；
- 一次性脚本的异常处理不适合长期定时运行。

## 15. 交付顺序

1. 确认 AITable 新字段 ID；
2. 增加配置开关和字段校验；
3. 增加闭环审计模型及迁移；
4. 实现逐附件报告上传和 Markdown 验证；
5. 实现阶段推进和人工提醒；
6. 实现 AITable 双字段回写；
7. 统一邮件、手动 API 和 scheduler 入口；
8. 增加测试；
9. dry-run 灰度；
10. 小范围启用后全量启用。
