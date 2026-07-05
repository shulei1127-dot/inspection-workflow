---
name: pts-api-ops
description: PTS 工单 API 操作。适用于通过 PTS 内网 API Token 上传文件到工单、添加工单记录备注、查询工单状态、推进工单阶段、从钉钉 AITable 获取巡检报告并上传到 PTS 工单等场景。仅使用内网 API（api.in.chaitin.net），不走公网（pts.chaitin.net）。
argument-hint: "[工单ID或操作描述]"
---

# PTS API Ops — 工单文件上传与操作

## 核心原理

PTS 有两套访问方式，**必须区分清楚**：

| 访问方式 | 地址 | 认证 | 限制 |
|----------|------|------|------|
| 公网 Web | `https://pts.chaitin.net/` | 浏览器 Session Cookie（`_ct_auth`） | API Token 不被接受，所有请求 302 到登录页 |
| 内网 API | `http://api.in.chaitin.net/pts/` | `Authorization: Bearer pt_xxx` | Token 模式不支持 `$variable` 参数化查询 |

**本技能只使用内网 API + Bearer Token 方式。**

## 端点

| 用途 | URL | 方法 |
|------|-----|------|
| GraphQL 查询 | `http://api.in.chaitin.net/pts/query` | POST |
| 文件上传 | `http://api.in.chaitin.net/pts/api/upload` | POST (multipart) |

## 认证

```
Authorization: Bearer pt_xxx
```

Token 格式为 `pt_` 开头的十六进制字符串。可从 inspection-workflow 项目的 `.env` 中读取 `PTS_API_TOKEN`。

## 关键规则

### 1. GraphQL 必须内联变量

PTS API Token 模式**不支持** `$variable` 参数化查询，所有变量必须内联到查询字符串中。

错误写法：
```json
{
  "operationName": "AddWorkOrderInfo",
  "variables": {"id": "xxx", "note": "yyy"},
  "query": "mutation AddWorkOrderInfo($id: ID!, $note: String) { add_work_order_info(id: $id, note: $note) }"
}
```

正确写法：
```json
{
  "query": "mutation { add_work_order_info(id: \"xxx\", note: \"yyy\") }"
}
```

### 2. 频率限制

PTS API 限流 4 req/s，批量操作时请求间至少间隔 250ms。

### 3. 文件上传需要分类参数

上传文件时必须传 `cat` 表单字段，工单附件用 `work_order`。

## 完整流程：钉钉 AITable 巡检报告 → PTS 工单

### Step 1: 从钉钉 AITable 获取记录

使用 `dws` CLI 查询 AITable：

```bash
dws aitable record query \
  --base-id YndMj49yWjPL7gq7TwPpArYyJ3pmz5aA \
  --table-id UWdhzcr \
  --limit 100
```

关键字段 ID（客户巡检派单表）：

| 字段ID | 含义 |
|--------|------|
| `AkjEpbP` | 客户名称 |
| `gwpCr99` | 巡检工单链接（PTS URL） |
| `nd284rT` | 巡检报告（附件列表） |
| `XQJu8tp` | 产品 |
| `VNgU6pF` | 工程师 |
| `ZzlBIoW` | 巡检是否完成 |

附件字段返回格式：
```json
[
  {
    "filename": "巡检报告.pdf",
    "downloadUrl": "https://oss.xxx/...",
    "fileSize": 3245678
  }
]
```

### Step 2: 下载附件

从 AITable 附件的 `downloadUrl` 下载文件：

```bash
curl -L -o /tmp/report.pdf "https://oss.xxx/..."
```

或用 Python：
```python
import requests
resp = requests.get(download_url)
with open("/tmp/report.pdf", "wb") as f:
    f.write(resp.content)
```

### Step 3: 上传文件到 PTS

```bash
curl -X POST http://api.in.chaitin.net/pts/api/upload \
  -H "Authorization: Bearer pt_xxx" \
  -F "file=@/tmp/report.pdf" \
  -F "cat=work_order"
```

成功返回：
```json
{"err": 0, "id": "6a0ea81319ab1b9837973a00", "filename": "report.pdf", "msg": "OK"}
```

记下 `id`，后续需要用来关联到工单。

Python 版本：
```python
import requests

TOKEN = "pt_xxx"
UPLOAD_URL = "http://api.in.chaitin.net/pts/api/upload"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

with open("/tmp/report.pdf", "rb") as f:
    resp = requests.post(UPLOAD_URL, headers=HEADERS,
                         files={"file": ("report.pdf", f)},
                         data={"cat": "work_order"})
file_id = resp.json()["id"]
```

### Step 4: 添加工单记录（含文件附件）

将上传后的 file ID 关联到工单：

```bash
curl -X POST http://api.in.chaitin.net/pts/query \
  -H "Authorization: Bearer pt_xxx" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation { add_work_order_info(id: \"647c2360104976e599db3333\", note: \"巡检报告已上传\", file: [\"6a0ea81319ab1b9837973a00\"]) }"}'
```

Python 版本：
```python
import requests

QUERY_URL = "http://api.in.chaitin.net/pts/query"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# 多个文件用数组
file_ids = ["id1", "id2"]
file_ids_str = ", ".join(f'"{fid}"' for fid in file_ids)
query = f'mutation {{ add_work_order_info(id: "{work_order_id}", note: "巡检报告已上传", file: [{file_ids_str}]) }}'

resp = requests.post(QUERY_URL, headers=HEADERS, json={"query": query})
result = resp.json()
# result["data"]["add_work_order_info"] == true 表示成功
```

也可以只添加文字备注不上传文件：
```
mutation { add_work_order_info(id: "xxx", note: "文字备注") }
```

### Step 5: 推进工单阶段

```bash
curl -X POST http://api.in.chaitin.net/pts/query \
  -H "Authorization: Bearer pt_xxx" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation { confirm_work_order_stage(id: \"647c2360104976e599db3333\") }"}'
```

工单阶段流程：发起工单(0) → 指定工单负责人(1) → 开始处理工单(2) → 完成工单处理(3) → 审核工单(4) → 结束(5)

需要多次调用直到到达目标阶段。最多尝试 10 次，防止无限循环。

## 其他 GraphQL 查询

### 查询当前用户信息
```
{ me { id name username } }
```

### 查询工单状态
```
{ workOrderByID(id: "647c2360104976e599db3333") { id is_finished current_stage { name sequence } } }
```

### 查询巡检工单列表
```
{
  listWorkOrder(
    search: { type: [expert_service__product_inspection, expert_service__log_analysis] }
    pagination: { skip: 0, limit: 50 }
    sort: { sort: 1, by: "plan_complete_date" }
  ) {
    total
    data {
      id
      type
      is_finished
      company { id name }
      plan_complete_date
      current_stage { name sequence }
      delivery {
        after_sale { id name }
        assigner { id name username }
      }
    }
  }
}
```

### Schema 内省
```
{ __schema { queryType { name } mutationType { name } types { name kind fields { name } } } }
```

## 从 AITable 记录提取 PTS 工单 ID

巡检工单链接字段（`gwpCr99`）的值是 PTS URL：
```
https://pts.chaitin.net/project/order/647c2360104976e599db3333
```

用正则提取工单 ID：
```python
import re
url = "https://pts.chaitin.net/project/order/647c2360104976e599db3333"
match = re.search(r'/project/order/([^/?]+)', url)
pts_order_id = match.group(1) if match else None
```

## 错误处理

| 状态码 | 含义 | 处理 |
|--------|------|------|
| 401 | Token 无效或过期 | 更新 Token |
| 429 | 请求过于频繁 | 等待后重试，间隔 ≥250ms |
| 302 | 公网重定向到登录 | 切换到内网 API 地址 |
| GraphQL `errors` | 查询语法或权限错误 | 检查变量内联格式、转义字符 |

### 常见错误

1. **`GRAPHQL_PARSE_FAILED`**: note 内容含 `\n` 或引号未正确转义。解决：用 `\\n` 替换换行，用 `\\"` 转义引号。
2. **`internal system error`**: 使用了 `$variable` 参数化查询。解决：改为内联变量。
3. **上传返回 404**: 内网上传端点未配置。联系运维确认 `http://api.in.chaitin.net/pts/api/upload` 已启用。

## 完整 Python 示例

```python
#!/usr/bin/env python3
"""钉钉 AITable 巡检报告 → PTS 工单上传"""

import json
import re
import subprocess
import tempfile
import time

import requests

PTS_TOKEN = "pt_xxx"
PTS_QUERY_URL = "http://api.in.chaitin.net/pts/query"
PTS_UPLOAD_URL = "http://api.in.chaitin.net/pts/api/upload"
AITABLE_BASE_ID = "YndMj49yWjPL7gq7TwPpArYyJ3pmz5aA"
AITABLE_TABLE_ID = "UWdhzcr"

# AITable 字段 ID
FIELD_CUSTOMER = "AkjEpbP"
FIELD_PTS_LINK = "gwpCr99"
FIELD_REPORT = "nd284rT"

HEADERS = {"Authorization": f"Bearer {PTS_TOKEN}"}


def query_aitable():
    """查询 AITable 记录"""
    result = subprocess.run(
        ["dws", "aitable", "record", "query",
         "--base-id", AITABLE_BASE_ID,
         "--table-id", AITABLE_TABLE_ID,
         "--limit", "100"],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def extract_pts_order_id(link_val):
    """从 AITable URL 字段提取 PTS 工单 ID"""
    url = None
    if isinstance(link_val, dict):
        url = link_val.get("link") or link_val.get("text", "")
    elif isinstance(link_val, str) and link_val.startswith("http"):
        url = link_val
    if not url:
        return None
    match = re.search(r'/project/order/([^/?]+)', url)
    return match.group(1) if match else None


def download_attachment(url, local_path):
    """下载附件到本地"""
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(resp.content)
    return local_path


def upload_to_pts(file_path, filename=None):
    """上传文件到 PTS，返回 file ID"""
    import os
    if not filename:
        filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        resp = requests.post(
            PTS_UPLOAD_URL, headers=HEADERS,
            files={"file": (filename, f)},
            data={"cat": "work_order"}
        )
    resp.raise_for_status()
    data = resp.json()
    if data.get("err") != 0:
        raise RuntimeError(f"上传失败: {data}")
    return data["id"]


def add_work_order_info(pts_order_id, note="", file_ids=None):
    """添加工单记录"""
    file_part = ""
    if file_ids:
        ids = ", ".join(f'"{fid}"' for fid in file_ids)
        file_part = f', file: [{ids}]'

    escaped_note = note.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    query = f'mutation {{ add_work_order_info(id: "{pts_order_id}", note: "{escaped_note}"{file_part}) }}'

    resp = requests.post(
        PTS_QUERY_URL,
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query}
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("add_work_order_info", False)


def confirm_work_order_stage(pts_order_id):
    """推进工单阶段"""
    query = f'mutation {{ confirm_work_order_stage(id: "{pts_order_id}") }}'
    resp = requests.post(
        PTS_QUERY_URL,
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"query": query}
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("confirm_work_order_stage")


# 主流程示例
if __name__ == "__main__":
    # 1. 查询 AITable
    records = query_aitable()

    for record in records.get("items", records if isinstance(records, list) else []):
        cells = record.get("cells", {})
        customer = cells.get(FIELD_CUSTOMER, "")
        link_val = cells.get(FIELD_PTS_LINK)
        attachments = cells.get(FIELD_REPORT)

        pts_order_id = extract_pts_order_id(link_val)
        if not pts_order_id:
            continue

        if not isinstance(attachments, list) or not attachments:
            print(f"跳过 {customer}：无巡检报告")
            continue

        # 2. 下载附件
        file_ids = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            url = att.get("downloadUrl", "")
            filename = att.get("filename", "report")
            if not url:
                continue

            tmp = tempfile.mktemp(suffix=f"_{filename}")
            download_attachment(url, tmp)

            # 3. 上传到 PTS
            fid = upload_to_pts(tmp, filename)
            file_ids.append(fid)
            print(f"已上传: {filename} → {fid}")

        # 4. 添加工单记录
        if file_ids:
            success = add_work_order_info(
                pts_order_id,
                note="巡检报告已上传至钉钉文档",
                file_ids=file_ids
            )
            print(f"工单 {pts_order_id} ({customer}): {'成功' if success else '失败'}")

        time.sleep(0.3)  # 频率限制
```

## 注意事项

1. **只使用内网 API**：`http://api.in.chaitin.net/pts/`，不要尝试 `https://pts.chaitin.net/`
2. **GraphQL 变量必须内联**：不支持 `$variable` 参数化，直接在查询字符串中写入值
3. **频率限制 4 req/s**：批量操作时请求间至少间隔 250ms
4. **AITable 附件字段**是列表，一个记录可能有多个附件（PDF + DOCX），每个都需要单独上传
5. **上传分类**：工单附件统一用 `cat=work_order`
6. **note 中的特殊字符**：引号用 `\\"` 转义，换行用 `\\n` 替换
