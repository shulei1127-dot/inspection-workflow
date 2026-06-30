# PTS 工单批量调整计划完成时间 Skill

## 概述

通过 PTS GraphQL API，查询指定条件的工单，并将工单的「计划完成时间」批量调整为当月最后一天。

## 前置条件

- PTS API 令牌（Bearer Token，格式 `pt_xxxxxxxx`），需同时具备 `query` 和 `mutation` 权限
- 令牌在 PTS 平台「个人工作台 -> API 令牌」中创建
- 需在公司内网或通过 VPN 访问

## API 基本信息

| 项目 | 值 |
|------|-----|
| 端点 | `http://api.in.chaitin.net/pts/query` |
| 协议 | GraphQL（所有操作通过 POST 请求） |
| 限流 | 每个 Token 每秒 5 次请求 |
| 认证 | Header: `Authorization: Bearer pt_<令牌>` |
| Content-Type | `application/json` |

---

## 完整操作流程

### 第一步：获取用户 ID

根据售后负责人姓名，查找其用户 ID。

```graphql
{ users { id name username } }
```

返回所有用户列表，在其中找到目标人员的 `id` 字段。

### 第二步：查询符合条件的工单

```graphql
query($search: WorkOrderSearchParam!, $pagination: Pagination, $sort: SortBy!) {
  listWorkOrder(search: $search, pagination: $pagination, sort: $sort) {
    data {
      id
      type
      plan_complete_date
      current_stage { name sequence }
      delivery { id after_sale { id name } }
      company { id name }
    }
  }
}
```

**查询变量示例（筛选产品巡检/日志分析类型工单）：**

```json
{
  "search": {
    "type": ["expert_service__product_inspection", "expert_service__log_analysis"]
  },
  "pagination": { "skip": 0, "limit": 500 },
  "sort": { "by": "plan_complete_date", "sort": 1 }
}
```

**注意事项：**
- `WorkOrderSearchParam` 没有 `plan_complete_date` 和 `after_sale` 过滤字段，需在客户端过滤
- `delivery.after_sale` 是交付项目中的「售后负责人」，不是工单的 `claim_by`
- `current_stage.name` 返回**中文**名称（如"指定工单负责人"、"审核工单"、"结束"），不是枚举英文值
- 需要分页遍历（6352+ 条工单），每页最多 500 条
- 工单按 `plan_complete_date` 升序排列时，大量 `null` 值排在最前面，6 月数据大约在 offset 5150-5400 之间

**客户端过滤条件：**

| 条件 | 说明 |
|------|------|
| `plan_complete_date` 在目标月范围内 | 注意 UTC+8 时区转换：6月1日0点北京 = `2026-05-31T16:00:00Z` |
| `delivery.after_sale.id` = 目标用户 ID | 售后负责人匹配 |
| `current_stage.name` 不等于"审核工单" | 排除已到审核阶段的工单 |
| `current_stage.name` 不等于"结束" | 排除已结束的工单 |

### 第三步：计算目标日期

将每个工单的 `plan_complete_date` 所在月份的最后一天作为新的计划完成时间。

**时区规则：**
- PTS 存储使用 UTC 时间
- 显示和业务逻辑使用北京时间（UTC+8）
- 6月最后一天北京时间 2026-06-30 00:00 = UTC 2026-06-29T16:00:00Z

**Python 计算：**

```python
from datetime import datetime, timedelta
import calendar

def get_month_last_day_utc(utc_str):
    """将计划完成时间调整为当月最后一天（北京时间 00:00，即前一天 UTC 16:00）"""
    dt = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
    bj_time = dt + timedelta(hours=8)
    # 获取当月最后一天
    last_day = calendar.monthrange(bj_time.year, bj_time.month)[1]
    # 北京时间当月最后一天 00:00
    bj_last = bj_time.replace(day=last_day, hour=0, minute=0, second=0, microsecond=0)
    # 转回 UTC
    utc_last = bj_last - timedelta(hours=8)
    return utc_last.strftime("%Y-%m-%dT%H:%M:%SZ")

# 示例：
# 输入: "2026-06-15T16:00:00Z" (6月16日北京时间)
# 输出: "2026-06-29T16:00:00Z" (6月30日北京时间)
```

### 第四步：批量更新工单计划完成时间

对每个需要调整的工单调用 `update_work_order` mutation：

```graphql
mutation($id: ID!, $input: WorkOrderUpdateParam!) {
  update_work_order(id: $id, input: $input)
}
```

**变量示例：**

```json
{
  "id": "6a16899ec926625715f79147",
  "input": {
    "plan_complete_date": "2026-06-29T16:00:00Z"
  }
}
```

**注意：**
- 每个 Token 每秒最多 5 次请求，批量更新时需要控制频率（建议每次请求间隔 200-250ms）
- Token 必须有 `mutation` 权限
- `WorkOrderUpdateParam` 其他字段不传则不会被修改
- `update_work_order` 返回类型是 `Boolean`（成功返回 null/true，失败在 errors 中返回），**不是**对象类型，不能在 mutation 中查询 `id` 或 `plan_complete_date` 字段
- 如需验证更新结果，用 `workOrderByID(id: "xxx") { plan_complete_date }` 单独查询确认

---

## 关键数据字典

### 工单类型 (WorkOrderType)

| 枚举值 | 中文名 |
|--------|--------|
| `expert_service__product_inspection` | 专家服务/产品巡检 |
| `expert_service__log_analysis` | 专家服务/日志分析 |
| `expert_service__major_guarantee` | 专家服务/重大保障 |
| `expert_service__product_duty` | 专家服务/产品值守 |
| `fault_hand__configuration_error__*` | 故障处理/配置错误/* |
| `fault_hand__product_issue__*` | 故障处理/产品问题/* |
| `product_consultation__*` | 产品咨询/* |
| `implementation_deployment__*` | 实施部署/* |
| `product_testing__*` | 产品测试/* |
| `demand_feedback__*` | 需求反馈/* |

### 工单阶段 (WorkOrderStage)

| 枚举值 | 中文名 | `current_stage.name` 返回值 |
|--------|--------|---------------------------|
| `create_work_order` | 发起工单 | 发起工单 |
| `assign_cliamer` | 指定工单负责人 | 指定工单负责人 |
| `begin_deal` | 开始处理工单 | 开始处理工单 |
| `deal_complete` | 完成工单处理 | 完成工单处理 |
| `check_work_order` | 审核工单 | **审核工单** |
| `finished` | 完成 | **结束** |

> **重要：** `current_stage.name` 返回的是中文名称，不是枚举英文值！"finished" 对应 "结束" 而非 "完成"。

### WorkOrderSearchParam 可用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `type` | [WorkOrderType!] | 工单类型过滤 |
| `claim_by` | [ID!] | 工单负责人（按用户ID） |
| `check_by` | [ID!] | 审核人（按用户ID） |
| `delivery_id` | String | 交付项目ID |
| `product_delivery_support_id` | ID | 交付支撑ID |
| `company` | [String!] | 公司名 |
| `company_id` | String | 公司ID |
| `stage` | [WorkOrderStage!] | 工单阶段过滤 |
| `is_end` | Boolean | 是否结束 |
| `fault_level` | [FaultLevel!] | 故障等级 |
| `created_at` | TimeFromTo | 创建时间范围 |
| `desc` | String | 描述关键字 |

> **注意：** `WorkOrderSearchParam` 没有 `plan_complete_date` 和 `after_sale` 过滤字段，需客户端过滤。

### WorkOrderUpdateParam 可用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `plan_complete_date` | Time | 计划完成时间 |
| `desc` | String | 描述 |
| `type` | [WorkOrderType!] | 工单类型 |
| `check_by` | ID | 审核人 |
| `fault_level` | FaultLevel | 故障等级 |
| `customer_affect_level` | CustomerAffectLevel | 客户影响等级 |
| `response_level` | ResponseLevel | 响应等级 |
| `customer_level` | CustomerLevel | 客户等级 |
| `customer_feedback_time` | Time | 客户反馈时间 |

---

## 完整 Python 示例代码

```python
import os
import time
import calendar
import requests
import json
from datetime import datetime, timedelta

# ============ 配置 ============
PTS_API_URL = "http://api.in.chaitin.net/pts/query"
PTS_API_TOKEN = os.environ["PTS_API_TOKEN"]  # 或直接填入令牌
HEADERS = {
    "Authorization": f"Bearer {PTS_API_TOKEN}",
    "Content-Type": "application/json",
}

AFTER_SALE_USER_ID = "61ee12415a2a73f5b1275998"  # 冯伟
TARGET_YEAR = 2026
TARGET_MONTH = 6

# ============ 时区工具 ============
def bj_start_of_month(year, month):
    """返回目标月1日0点北京时间对应的UTC字符串"""
    bj = datetime(year, month, 1, 0, 0, 0)
    utc = bj - timedelta(hours=8)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")

def bj_end_of_month(year, month):
    """返回目标月最后一天23:59北京时间对应的UTC字符串"""
    last_day = calendar.monthrange(year, month)[1]
    bj = datetime(year, month, last_day, 23, 59, 0)
    utc = bj - timedelta(hours=8)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")

def month_last_day_utc(year, month):
    """返回目标月最后一天0点北京时间对应的UTC字符串（用于设置plan_complete_date）"""
    last_day = calendar.monthrange(year, month)[1]
    bj = datetime(year, month, last_day, 0, 0, 0)
    utc = bj - timedelta(hours=8)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")

# ============ 查询工单 ============
def query_work_orders():
    START = bj_start_of_month(TARGET_YEAR, TARGET_MONTH)
    END = bj_end_of_month(TARGET_YEAR, TARGET_MONTH)
    NEW_DATE = month_last_day_utc(TARGET_YEAR, TARGET_MONTH)

    query = """{ listWorkOrder(search: {type: [expert_service__product_inspection, expert_service__log_analysis]}, pagination: {skip: %d, limit: 100}, sort: {by: "plan_complete_date", sort: 1}) { data { id type plan_complete_date current_stage { name } delivery { id after_sale { id name } } company { id name } } } }"""

    matching = []
    for skip in range(5150, 5450, 50):  # 6月数据大致在此范围，可根据实际调整
        resp = requests.post(PTS_API_URL, json={"query": query % skip}, headers=HEADERS)
        result = resp.json()
        if "errors" in result:
            continue
        for wo in result["data"]["listWorkOrder"]["data"]:
            pcd = wo.get("plan_complete_date")
            if not pcd or not (START <= pcd <= END):
                continue
            delivery = wo.get("delivery")
            if not delivery or not delivery.get("after_sale"):
                continue
            if delivery["after_sale"].get("id") != AFTER_SALE_USER_ID:
                continue
            stage = wo.get("current_stage", {}).get("name", "")
            if stage in ("审核工单", "结束"):
                continue
            matching.append(wo)

    # 去重
    unique = list({wo["id"]: wo for wo in matching}.values())
    unique.sort(key=lambda x: x["plan_complete_date"])
    return unique, NEW_DATE

# ============ 更新工单 ============
def update_work_order(order_id, new_date):
    mutation = """mutation($id: ID!, $input: WorkOrderUpdateParam!) {
  update_work_order(id: $id, input: $input)
}"""
    variables = {"id": order_id, "input": {"plan_complete_date": new_date}}
    resp = requests.post(PTS_API_URL, json={"query": mutation, "variables": variables}, headers=HEADERS)
    return resp.json()

# ============ 主流程 ============
def main():
    orders, new_date = query_work_orders()
    print(f"找到 {len(orders)} 个工单，将 plan_complete_date 调整为 {new_date}")

    success = 0
    failed = 0
    for i, wo in enumerate(orders, 1):
        company = wo.get("company", {}).get("name", "N/A")
        old_pcd = wo.get("plan_complete_date", "N/A")
        result = update_work_order(wo["id"], new_date)
        if "errors" in result:
            print(f"  [{i}] FAIL {wo['id']} ({company}): {result['errors']}")
            failed += 1
        else:
            print(f"  [{i}] OK {wo['id']} ({company}): updated to {new_date}")
            success += 1
        time.sleep(0.2)  # 限流：每秒最多5次

    print(f"\n完成: 成功 {success}, 失败 {failed}")

if __name__ == "__main__":
    main()
```

---

## 常见问题

### Q: 为什么不能用 WorkOrderSearchParam 的 stage 字段直接过滤？
A: `stage` 字段接受 WorkOrderStage 枚举列表，在 GraphQL variables 中传枚举列表存在兼容性问题（某些客户端会将枚举值序列化为字符串），建议在客户端通过 `current_stage.name` 进行过滤。

### Q: current_stage.name 返回的是什么？
A: 返回中文名称，如"指定工单负责人"、"开始处理工单"、"审核工单"、"完成工单处理"、"结束"等。特别注意 `finished` 枚举对应返回的是"结束"而非"完成"。

### Q: plan_complete_date 的时区怎么处理？
A: PTS API 存储和返回的时间均为 UTC。北京时间需要 +8 小时转换。设置计划完成时间时，如需北京时间 2026-06-30 00:00，应传 UTC `2026-06-29T16:00:00Z`。

### Q: 如何获取 PTS API 令牌？
A: 登录 PTS 平台，进入「个人工作台 -> API 令牌」，创建新令牌并勾选 `query` 和 `mutation` 权限。令牌以 `pt_` 开头。

### Q: 批量更新时遇到 429 错误怎么办？
A: 每个 Token 每秒最多 5 次请求。在请求之间加入 200ms 延迟即可。如仍超限，增大延迟到 300ms。

### Q: Token 权限不足怎么办？
A: 检查 Token 是否同时勾选了 `query`（查询用）和 `mutation`（更新用）权限。只有 query 权限无法执行 update_work_order。此外，Token 继承创建者的业务权限，需确保创建者有相应工单的操作权限。
