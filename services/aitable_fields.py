"""AITable field ID constants and shared extraction helpers.

All AITable field IDs are centralized here so that schema changes
only require updating this single file.

Two tables:
- DISPATCH: 客户巡检派单 (dt_dispatch_base_id / dt_dispatch_table_id)
- DAILY_SERVICE: 日常增值服务进展 (dt_aitable_base_id / dt_aitable_table_id)
"""

# ── 客户巡检派单 table ──────────────────────────────────────────────────────

DISPATCH = {
    "客户名称": "AkjEpbP",
    "产品名称": "XQJu8tp",
    "联系电话": "SsccgRO",
    "工单类型": "iX3H33T",
    "巡检工单链接": "gwpCr99",
    "所属区域": "IsyyIQG",
    "销售": "UtGAxNM",
    "记录时间": "9OtL7li",
    "伙伴供应商": "hCJ8nkj",
    "伙伴负责人": "MkqSbqT",
    "工程师": "p40jtpC",
    "需求编号": "AntmbXo",
    "订单编号": "LyTZGs5",
    "派单等级": "YBMWwzy",
    "邮件是否发送": "ZzlBIoW",
    "报告发送邮箱": "b16cmLx",
    "巡检报告": "nd284rT",
    "巡检是否完成": "SLqCQD6",
    "工单是否闭环": "0FdeEiC",
    "备注": "m3DiVhZ",
    "巡检地址": "NP80n3b",
    "巡检时间": "RYQ1Vd0",
    "巡检方式": "VNgU6pF",
    "巡检申请人": "u3xUs6A",
    "具体到场时间": "wyBiLxz",
}

# ── 日常增值服务进展 table ──────────────────────────────────────────────────

DAILY_SERVICE = {
    "启动时间": "01ZM8y7",
    "增值服务类型": "ysMtLso",
    "巡检工单链接": "3y6GgZx",
    "公司名称": "mHe1U1b",
    "产品名称": "etNirF0",
    "巡检是否完成": "gCf7Ogd",
    "巡检报告": "WFos3pW",
    "邮件是否发送": "ih6XHuL",
    "客户邮箱": "E9t6fjb",
    "销售": "2k6Jeje",
    "工单计划完成时间": "TdAMCWj",
    "PTS客户联系人": "ya3ZXfA",
    "PTS客户电话": "nfMpJpT",
    "所属区域": "Fiya1yk",
}


# ── Shared extraction helpers ────────────────────────────────────────────────

def extract_text(val) -> str | None:
    """Extract text value from AITable cell."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, list) and len(val) > 0:
        # Text fields may return [{"type":"text","text":"xxx"}]
        first = val[0]
        if isinstance(first, dict):
            return first.get("text", str(first))
        return str(first)
    return str(val)


def extract_select_name(val) -> str | None:
    """Extract name from AITable singleSelect field."""
    if val is None:
        return None
    if isinstance(val, dict):
        return val.get("name", str(val))
    if isinstance(val, str):
        return val
    return str(val)


def extract_engineer(val) -> str | None:
    """Extract engineer identifier from AITable user field.

    AITable user field can have different formats:
    - {userId: "xxx", corpId: "xxx"}  (钉钉企业内部用户)
    - {userName: "舒磊", userRef: "ur_xxx"}  (外部/其他格式)
    """
    if val is None:
        return None
    if isinstance(val, list) and len(val) > 0:
        names = []
        for u in val:
            if not isinstance(u, dict):
                continue
            # Prefer userId (内部用户), fall back to userName (外部用户), then userRef
            name = u.get("userId") or u.get("userName") or u.get("userRef", "")
            if name:
                names.append(name)
        return ", ".join(names) if names else None
    if isinstance(val, str):
        return val
    return None


def extract_user_ids(val) -> str | None:
    """Extract user IDs from AITable user field (returns userId only)."""
    if val is None:
        return None
    if isinstance(val, list) and len(val) > 0:
        ids = [u.get("userId", "") for u in val if isinstance(u, dict) and u.get("userId")]
        return ", ".join(ids) if ids else None
    if isinstance(val, str):
        return val
    return None


# ── Shared business constants ────────────────────────────────────────────────

COMPLETION_STAGES = {"审核工单", "已闭环"}


def current_month() -> str:
    """Return the current month in YYYY-MM format (UTC)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m")
