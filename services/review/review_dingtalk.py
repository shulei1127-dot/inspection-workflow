"""交付转售后审核 — 钉钉数据表写入

将审核结果写入钉钉 AITable：
- 主表（交付转售后回访进展）
- 区域子表（审核拒绝项目，自动推送群聊提醒）
- 用户字段（交付分配人/交付负责人，需单独 update）
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from services.dingtalk_client import create_records, update_records, _run_dws
from services.review.audit.schemas import AuditResult, RuleResult
from core.config import get_settings

logger = logging.getLogger(__name__)

_UNICODE_INVISIBLE = re.compile(r"[​-⁯‌‍‌‍⁠-⁯﻿]")


def _sanitize(text: str) -> str:
    return _UNICODE_INVISIBLE.sub("", text)


# ── 极简标签映射 ────────────────────────────────────────────

_NOTE_COMPACT_MAP: dict[str, str] = {
    "交付阶段为": "阶段不符",
    "不是\"转售后审核\"": "阶段不符",
    "不是\"等待审核是否转售后\"": "状态不符",
    "售后负责人为空": "负责人为空",
    "不是\"冯伟\"": "负责人不符",
    "交付项表格无数据行": "缺交付项",
    "配置项为空": "缺配置项",
    "未找到服务包年限": "缺服务包年限",
    "未匹配到服务包年限": "缺服务包年限",
    "无企业联系人记录": "缺联系人",
    "电话为空": "电话为空",
    "电话格式错误": "电话格式错",
    "纯续保项目但无产品实例": "缺产品实例",
    "不匹配": "配置项套数≠产品信息实例数",
    "无机器码": "缺机器码",
    "无序列号": "缺序列号",
    "无型号": "缺型号",
    "无产品版本": "缺版本",
    "无License ID": "缺LicenseID",
    "无License性质": "缺License类型",
    "无售后有效服务期": "缺少售后有效服务期",
    "无引擎版本": "缺引擎版本",
    "无部署模式": "缺部署模式",
    "无是否HA": "缺HA信息",
    "无是否过保": "缺过保信息",
    "License性质非": "License类型不符",
    "服务期偏差": "服务期偏差",
    "无法获取审核通过时间": "缺审核时间",
}


def _compact_note(rule: RuleResult) -> str | None:
    """将规则 message 转为极简标签。"""
    if rule.rule_id == 9:
        return None

    msg = rule.message
    tags: list[str] = []

    for keyword in sorted(_NOTE_COMPACT_MAP, key=len, reverse=True):
        if keyword in msg:
            tags.append(_NOTE_COMPACT_MAP[keyword])

    seen: set[str] = set()
    unique: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return ",".join(unique) if unique else msg


# ── 区域/类型映射常量 ────────────────────────────────────────

REGION_OPTION_MAP: dict[str, str] = {
    "华东战区": "dtU8OVeJM0",
    "华北东北战区": "6pW9KJf5fw",
    "华南战区": "dUlPuelRVS",
    "西南西北战区": "zWWhy9BF3r",
    "华中战区": "RH96m2aE0m",
    "通信头部战队": "x7Mpm5uGbE",
    "政企行业": "bpcFUBUMKP",
    "金融头部战队": "rwPSYDRLPU",
    "商业策略组": "GRr0sYDlCX",
    "战略伙伴战队": "hsjOHtG0hH",
    "政府头部战队": "o2fBZY4P4o",
    "能源央企头部战队": "XASjOCnSot",
}

DELIVERY_TYPE_OPTION_MAP: dict[str, str] = {
    "生态交付": "5sxBF2nYLP",
    "自交付": "S0SAcwL5tf",
    "原厂交付": "GxnMUksX0o",
    "无交付": "CvB65Wm2KN",
    "不回访": "PuEnRtgXRv",
}

PROJECT_TYPE_OPTION_MAP: dict[str, str] = {
    "新购": "dStkuKUtxl",
    "租用": "xgfRq8AVbr",
    "续保": "MwcXc3NeUc",
    "新购➕续保": "rP1U101AoS",
    "新购➕租用": "JbUVJkSjaf",
}

# ── 区域交付转售后数据表映射 ─────────────────────────────────

REVIEW_REGION_TABLE_MAP: dict[str, str] = {
    "华东战区": "yz5gWVj",
    "华北东北战区": "sdBTYUI",
    "华南战区": "ycBxHBu",
    "西南西北战区": "TlYOyHp",
    "华中战区": "eDWgaIR",
}

REVIEW_REGION_INDUSTRY_TABLE_ID = "RPBc9Rn"
REVIEW_REGION_INDUSTRY_PERSON_IN_CHARGE_FIELD = "Z6FY4Z4"
REVIEW_REGION_INDUSTRY_NAMES: set[str] = {
    "政企行业", "通信头部战队", "金融头部战队",
    "能源央企头部战队", "商业策略组", "战略伙伴战队", "政府头部战队",
}

REGION_AUDIT_REJECT_OPTION_ID = "1MO1Mgbfqu"

# 区域表"交付负责人"用户字段 ID（各子表字段 ID 不同）
REGION_PERSON_IN_CHARGE_FIELD_MAP: dict[str, str] = {
    "华东战区": "Yf3d8nL",
    "华北东北战区": "kbrn7Jk",
    "华南战区": "Yj0evMZ",
    "西南西北战区": "d8MZnDh",
    "华中战区": "Z6FY4Z4",
}

# 主表"交付负责人"用户字段 ID
MAIN_PERSON_IN_CHARGE_FIELD = "qWDHbYc"

# 主表"销售"用户字段 ID
SALES_USER_FIELD = "WLigZyI"

# 主表"CRM项目"链接字段 ID
CRM_PROJECT_FIELD = "zmb66Mo"

# 所有子表统一的"交付分配人"用户字段 ID
ASSIGNER_USER_FIELD = "19r1LyK"

# 交付分配人姓名 → 区域（钉钉选项 ID）
ASSIGNER_REGION_MAP: dict[str, str] = {
    # 华东战区
    "任嘉伟": "dtU8OVeJM0", "鲍金鑫": "dtU8OVeJM0", "高云松": "dtU8OVeJM0", "郭林成": "dtU8OVeJM0",
    "陈欧翔": "dtU8OVeJM0", "夏睿婷": "dtU8OVeJM0", "殷培源": "dtU8OVeJM0", "宋健": "dtU8OVeJM0",
    "陈祎雯": "dtU8OVeJM0", "王瑞": "dtU8OVeJM0", "陈平远": "dtU8OVeJM0", "卢占文": "dtU8OVeJM0",
    "黄瑞": "dtU8OVeJM0", "廖雨田": "dtU8OVeJM0", "田英超": "dtU8OVeJM0", "杜文韬": "dtU8OVeJM0",
    "黄彬": "dtU8OVeJM0", "李东方": "dtU8OVeJM0",
    # 华北东北战区
    "田疆": "6pW9KJf5fw", "王弸彪": "6pW9KJf5fw", "黄诗琦": "6pW9KJf5fw", "刘胜": "6pW9KJf5fw",
    "张镇朝": "6pW9KJf5fw", "王鑫裕": "6pW9KJf5fw", "李京京": "6pW9KJf5fw", "刘超": "6pW9KJf5fw",
    "王均广": "6pW9KJf5fw", "黄建朋": "6pW9KJf5fw",
    # 华南战区
    "唐政": "dUlPuelRVS", "李真真": "dUlPuelRVS", "万姚江": "dUlPuelRVS", "叶利钢": "dUlPuelRVS",
    "闫文军": "dUlPuelRVS", "彭明豪": "dUlPuelRVS", "兰廷灶": "dUlPuelRVS", "杜冠峥": "dUlPuelRVS",
    "郑思成": "dUlPuelRVS", "郑义全": "dUlPuelRVS", "黄泽孟": "dUlPuelRVS", "陈文超": "dUlPuelRVS",
    "邓智峰": "dUlPuelRVS", "吴冬兵": "dUlPuelRVS", "邓万杰": "dUlPuelRVS", "梁圣麟": "dUlPuelRVS",
    "雷昊": "dUlPuelRVS",
    # 西南西北战区
    "饶君睿": "zWWhy9BF3r", "罗果": "zWWhy9BF3r", "黄科": "zWWhy9BF3r", "张强": "zWWhy9BF3r",
    "欧阳凯": "zWWhy9BF3r", "柯李木": "zWWhy9BF3r", "李升明": "zWWhy9BF3r", "刘樊武": "zWWhy9BF3r",
    "栗永顺": "zWWhy9BF3r", "张明江": "zWWhy9BF3r",
    # 华中战区
    "万里秦": "RH96m2aE0m", "李宁愿": "RH96m2aE0m", "杨伦": "RH96m2aE0m", "刘腾": "RH96m2aE0m",
    "朱锟": "RH96m2aE0m",
    # 金融头部战队
    "贾腾辉": "rwPSYDRLPU", "黄宏昊": "rwPSYDRLPU", "张智国": "rwPSYDRLPU", "孟祥宇": "rwPSYDRLPU",
    "侯伟": "rwPSYDRLPU", "罗娇": "rwPSYDRLPU", "仇鑫杰": "rwPSYDRLPU", "刘春洋": "rwPSYDRLPU",
    "张嘉欣": "rwPSYDRLPU", "祝方正": "rwPSYDRLPU", "邢凯迪": "rwPSYDRLPU", "郭文祥": "rwPSYDRLPU",
    # 战略伙伴战队
    "王泳": "hsjOHtG0hH", "孙昊": "hsjOHtG0hH", "任晨赫": "hsjOHtG0hH", "余佳霖": "hsjOHtG0hH",
    "张恒峰": "hsjOHtG0hH", "杨帅": "hsjOHtG0hH", "张国东": "hsjOHtG0hH", "杨祖安": "hsjOHtG0hH",
    "孟凡策": "hsjOHtG0hH", "龚榆宸": "hsjOHtG0hH", "贾诗晨": "hsjOHtG0hH",
    # 通信头部战队
    "王刚": "x7Mpm5uGbE", "田志强": "x7Mpm5uGbE", "田宇辰": "x7Mpm5uGbE", "王帅": "x7Mpm5uGbE",
    "王欣": "x7Mpm5uGbE", "张家祥": "x7Mpm5uGbE",
    # 政企行业
    "沈修阳": "bpcFUBUMKP", "郑祖江": "bpcFUBUMKP", "谢小倩": "bpcFUBUMKP", "王德鑫": "bpcFUBUMKP",
    "喻洁": "bpcFUBUMKP", "周长良": "bpcFUBUMKP", "庞临风": "bpcFUBUMKP",
}

PTS_DELIVERY_TYPE_LABEL: dict[str, str] = {
    "chaitin": "原厂交付",
    "partner": "生态交付",
    "self": "自交付",
}

# Region ID → region name (reverse lookup)
_REGION_ID_TO_NAME = {v: k for k, v in REGION_OPTION_MAP.items()}

# User ID cache
_user_id_cache: dict[str, str] = {}


def compute_region(assigner_name: str | None) -> str | None:
    if not assigner_name:
        return None
    region_id = ASSIGNER_REGION_MAP.get(assigner_name)
    if region_id:
        return _REGION_ID_TO_NAME.get(region_id)
    return None


def _get_region_table_id(region: str | None) -> str | None:
    """根据区域名称查找对应区域表的 table_id。"""
    if not region:
        return None
    table_id = REVIEW_REGION_TABLE_MAP.get(region)
    if table_id:
        return table_id
    if region in REVIEW_REGION_INDUSTRY_NAMES:
        return REVIEW_REGION_INDUSTRY_TABLE_ID
    return None


def compute_delivery_type(
    delivery_items: list, product_details: list, partner_delivery_type: str | None,
) -> str | None:
    from services.review.audit.schemas import ProductType
    if product_details and all(p.type == ProductType.saas for p in product_details):
        return "不回访"
    if delivery_items and any("续保" in (item.product_category or "").split("-")[-1] for item in delivery_items):
        return "无交付"
    if partner_delivery_type:
        return PTS_DELIVERY_TYPE_LABEL.get(partner_delivery_type)
    return None


def compute_project_type(delivery_items: list) -> str | None:
    if not delivery_items:
        return None
    has_renewal = any("续保" in (item.product_category or "").split("-")[-1] for item in delivery_items)
    has_rental = any("租用" in (item.product_category or "") or "订阅" in (item.product_category or "") for item in delivery_items)
    if has_renewal and not has_rental:
        all_renewal = all("续保" in (item.product_category or "").split("-")[-1] for item in delivery_items)
        return "续保" if all_renewal else "新购➕续保"
    if has_renewal and has_rental:
        return "新购➕租用"
    if not has_renewal and has_rental:
        return "租用"
    return "新购"


# ── 钉钉用户 ID 解析 ──────────────────────────────────────────

async def _resolve_dingtalk_user_id(name: str, *, exact: bool = False) -> str | None:
    """通过 dws CLI 按姓名搜索钉钉用户 ID。

    销售字段使用 exact=True，只有返回结果中的姓名与 PTS 销售完全匹配时才写入，
    避免同名或模糊搜索命中错误用户。
    """
    if not name:
        return None
    cache_key = f"{name}:{exact}"
    if cache_key in _user_id_cache:
        return _user_id_cache[cache_key]
    try:
        result = await _run_dws([
            "contact", "user", "search",
            "--query", name, "--format", "json",
        ], timeout=10)
        if isinstance(result, dict) and result.get("success") and result.get("result"):
            candidates = result["result"]
            if exact:
                candidates = [
                    user for user in candidates
                    if user.get("name") == name or user.get("nick") == name
                ]
                if len(candidates) != 1:
                    logger.warning("钉钉销售姓名未精确匹配: name=%s candidates=%d", name, len(candidates))
                    return None
            user_id = candidates[0].get("userId")
            if user_id:
                _user_id_cache[cache_key] = user_id
                return user_id
    except Exception:
        logger.exception("钉钉用户查找失败: name=%s", name)
    return None


# ── 公共用户字段更新 ──────────────────────────────────────────

async def _update_user_fields(
    record_id: str,
    result: AuditResult,
    *,
    base_id: str,
    table_id: str,
    corp_id: str,
    person_in_charge_field: str | None = None,
    sales_field: str | None = None,
) -> None:
    """创建记录后，单独更新钉钉用户类型字段（交付分配人、交付负责人、销售）。

    Args:
        person_in_charge_field: "交付负责人"字段ID。主表为 qWDHbYc，
            区域子表各不相同（查 REGION_PERSON_IN_CHARGE_FIELD_MAP）。
            为 None 表示目标表无此字段，跳过写入。
        sales_field: "销售"字段ID。销售姓名必须从 PTS 公司负责人精确匹配
            到钉钉用户后才写入；为 None 表示目标表无此字段。

    业务规则：如果交付分配人和交付负责人是同一人，只填写交付分配人。
    """
    if not record_id:
        return
    user_cells: dict[str, Any] = {}

    assigner_uid: str | None = None
    if result.assigner_name:
        assigner_uid = await _resolve_dingtalk_user_id(result.assigner_name)
        if assigner_uid:
            user_cells[ASSIGNER_USER_FIELD] = [{"corpId": corp_id, "userId": assigner_uid}]

    if result.person_in_charge_name and person_in_charge_field:
        # 交付分配人和交付负责人是同一人时，只填写交付分配人
        if result.person_in_charge_name == result.assigner_name:
            pass  # 已通过 ASSIGNER_USER_FIELD 写入，跳过
        else:
            pic_uid = await _resolve_dingtalk_user_id(result.person_in_charge_name)
            if pic_uid:
                user_cells[person_in_charge_field] = [{"corpId": corp_id, "userId": pic_uid}]

    if result.sales_name and sales_field:
        # PTS 的销售字段是客户负责人，必须精确匹配钉钉姓名，避免模糊命中错误人员。
        sales_uid = await _resolve_dingtalk_user_id(result.sales_name, exact=True)
        if sales_uid:
            user_cells[sales_field] = [{"corpId": corp_id, "userId": sales_uid}]
        else:
            logger.warning("销售未匹配到钉钉用户，跳过写入: sales_name=%s", result.sales_name)

    if user_cells:
        try:
            await update_records(
                [{"recordId": record_id, "cells": user_cells}],
                base_id=base_id,
                table_id=table_id,
            )
        except Exception:
            logger.warning(
                "Dingtalk user field update failed (record created, user fields empty): "
                "table_id=%s record_id=%s",
                table_id, record_id,
            )


# ── 区域表写入 ───────────────────────────────────────────────

async def _write_to_region_sheet(
    result: AuditResult,
    *,
    base_id: str,
    corp_id: str,
) -> dict[str, Any]:
    """审核拒绝的项目写入对应区域的交付转售后回访进展数据表。"""
    table_id = _get_region_table_id(result.region)
    if not table_id:
        logger.info("region sheet skipped: no table for region=%s", result.region)
        return {"success": False, "reason": "no_region_table"}

    pts_url = f"https://pts.chaitin.net/project/{result.project_id}#base"

    notes: list[str] = []
    for r in result.rules:
        if r.result in ("不通过", "无法判定"):
            tag = _compact_note(r)
            if tag:
                notes.append(tag)
    compact_notes = _sanitize("\n".join(notes)) if notes else ""

    region_option_id = REGION_OPTION_MAP.get(result.region or "")

    cells: dict[str, Any] = {
        "fxg8rhmv7xum7ybd4ejfs": _now_iso_cn(),
        "rbiax8fi5eklvmdlc4v5d": result.customer_name or "",
        "ulembeeuza3ctgftx69n1": pts_url,
        "ab6g79dhla7ta1n4vtryx": result.assigner_name or "",
        "3np07ifl4yfr6jsxghcum": {"id": REGION_AUDIT_REJECT_OPTION_ID},
        "8q5udpxjhanq5eyn0k98a": compact_notes,
    }
    if region_option_id:
        cells["o7dk5r68igm7syh8funhl"] = {"id": region_option_id}

    try:
        resp = await create_records(
            [{"cells": cells}],
            base_id=base_id,
            table_id=table_id,
        )
        if not resp:
            logger.warning("Dingtalk region sheet create failed: no response")
            return {"success": False}

        record_id: str | None = (resp.get("newRecordIds") or [None])[0]

        # 查找该区域/行业的交付负责人字段ID
        pic_field: str | None = None
        if result.region:
            if result.region in REVIEW_REGION_INDUSTRY_NAMES:
                pic_field = REVIEW_REGION_INDUSTRY_PERSON_IN_CHARGE_FIELD
            else:
                pic_field = REGION_PERSON_IN_CHARGE_FIELD_MAP.get(result.region)

        if record_id:
            await _update_user_fields(
                record_id, result,
                base_id=base_id, table_id=table_id,
                corp_id=corp_id, person_in_charge_field=pic_field,
            )

        return {"success": True}
    except Exception as e:
        logger.error("Dingtalk region sheet write failed: %s", e)
        return {"success": False}


# ── 主表写入 ─────────────────────────────────────────────────

async def write_audit_to_dingtalk(result: AuditResult) -> dict[str, Any]:
    """将审核结果写入钉钉交付转售后回访进展主表 + 区域表。"""
    settings = get_settings()
    base_id = settings.review_aitable_base_id
    table_id = settings.review_aitable_main_table_id
    corp_id = settings.review_aitable_corp_id

    pts_url = f"https://pts.chaitin.net/project/{result.project_id}#base"
    pass_label = _map_conclusion_to_label(result.conclusion)

    notes: list[str] = []
    for r in result.rules:
        if r.result in ("不通过", "无法判定"):
            tag = _compact_note(r)
            if tag:
                notes.append(tag)
    if result.value_added_service_reminder:
        notes.append(result.value_added_service_reminder)
    # 转人工审核时注明原因
    if result.conclusion == "转人工审核" and result.manual_review_reason:
        notes.append("转人工原因：" + result.manual_review_reason)

    cells: dict[str, Any] = {
        "审核是否通过": pass_label,
        "审核日期": _now_iso_cn(),
        "PTS交付链接": {"link": pts_url, "text": pts_url},
        "客户名称": result.customer_name or "",
    }
    if result.crm_project_id:
        crm_url = f"https://crm.chaitin.net/project/{result.crm_project_id}#base"
        cells[CRM_PROJECT_FIELD] = {"link": crm_url, "text": crm_url}
    if notes:
        cells["校对备注"] = _sanitize("\n".join(notes))
    if result.service_content:
        cells["doox5joqqw0mae62xhmtq"] = _sanitize(result.service_content)
    if result.after_sales_service_period_summary:
        cells["rgcjj2wcarim8lg9apphh"] = _sanitize(result.after_sales_service_period_summary)
    if result.region:
        region_id = REGION_OPTION_MAP.get(result.region)
        if region_id:
            cells["o7dk5r68igm7syh8funhl"] = {"id": region_id}
    if result.delivery_type:
        type_id = DELIVERY_TYPE_OPTION_MAP.get(result.delivery_type)
        if type_id:
            cells["vth94xg28fxpt6ribmpjd"] = [{"id": type_id}]
    if result.project_type:
        type_id = PROJECT_TYPE_OPTION_MAP.get(result.project_type)
        if type_id:
            cells["oz9yut6kbwr4gagps0us7"] = {"id": type_id}

    try:
        resp = await create_records(
            [{"cells": cells}],
            base_id=base_id,
            table_id=table_id,
        )
        if not resp:
            logger.warning("Dingtalk create failed: no response")
            return {"success": False}

        record_id: str | None = (resp.get("newRecordIds") or [None])[0]

        # 单独更新用户字段（交付分配人、交付负责人、销售）
        if record_id:
            await _update_user_fields(
                record_id, result,
                base_id=base_id, table_id=table_id,
                corp_id=corp_id,
                person_in_charge_field=MAIN_PERSON_IN_CHARGE_FIELD,
                sales_field=SALES_USER_FIELD,
            )

        return {"success": True, "recordId": record_id}
    except Exception as e:
        logger.error("Dingtalk write failed: %s", e)
        return {"success": False}
    finally:
        # 审核拒绝的项目额外写入对应区域表
        if result.conclusion == "不通过" and result.region:
            try:
                region_result = await _write_to_region_sheet(result, base_id=base_id, corp_id=corp_id)
                # 写入区域表成功后，加入缓冲，由流水线结束后合并为一条通知
                if region_result.get("success"):
                    _buffer_region_reject(result)
            except Exception as e:
                logger.warning("Dingtalk region sheet write failed (main record OK): %s", e)


# ── 辅助函数 ─────────────────────────────────────────────────

# ── 审核拒绝通知缓冲（合并为一条发送） ────────────────────────
_region_reject_buffer: list[dict[str, str]] = []


def _buffer_region_reject(result: AuditResult) -> None:
    """将审核拒绝项目加入缓冲，流水线结束后合并为一条通知发送。"""
    _region_reject_buffer.append({
        "project_name": result.project_name or "",
        "customer_name": result.customer_name or "",
        "region": result.region or "",
    })


async def flush_region_reject_notices() -> bool:
    """将缓冲的审核拒绝项目合并成一条钉钉通知发送，并清空缓冲。"""
    if not _region_reject_buffer:
        return False
    items = list(_region_reject_buffer)
    _region_reject_buffer.clear()

    from services.dingtalk_notifier import send_dingtalk_notification

    lines = [
        f"- {it['project_name'] or it['customer_name'] or '未知项目'}（{it['region']}）"
        for it in items
    ]
    title = "🚫 审核拒绝项目已写入钉钉文档"
    content = (
        f"本次审核共 {len(items)} 个项目审核不通过，已写入对应区域交付转售后回访进展数据表：\n\n"
        + "\n".join(lines)
    )
    return await send_dingtalk_notification(title, content)


def _now_iso_cn() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.isoformat()


def _map_conclusion_to_label(conclusion: str) -> str:
    if conclusion == "通过":
        return "通过"
    if conclusion == "转人工审核":
        return "转人工"
    return "拒绝"
