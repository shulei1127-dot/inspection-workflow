"""交付转售后回访闭环 — 全流程编排服务

正确业务流程：
  1. 人工电话回访，将结果填写到钉钉 AITable「交付转售后回访进展」
  2. 系统从 AITable 查询满足自动闭环条件的记录：
     - 回访状态 = 已回访
     - 回访类型 不为空
     - PTS选择的满意度 不为空
     - 回访链接 为空（尚未创建 PTS 工单）
     - 备注 不为空
  3. 自动调用 PTS API 创建回访工单并闭环
  4. 将 PTS 回访链接写回 AITable

参考 closed_loop_v2 的 VisitPlanner + VisitRealRunner 逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from core.config import get_settings
from models.visit_log import VisitLog
from services.dingtalk_client import query_records
from services.visit.pts_visit_client import (
    VISIT_URL_TEMPLATE,
    create_visit,
    fetch_delivery_metadata,
    fetch_visit_detail,
    find_created_visit,
    finish_visit,
    process_visit,
)
from services.visit.visit_dingtalk import update_visit_link_to_dingtalk

logger = logging.getLogger(__name__)

# ── 钉钉 AITable 字段 ID 常量（与 closed_loop_v2 一致）──────────

# 交付转售后回访进展表的字段 ID
FIELD_VISIT_STATUS = "vd8h8nk8m4tr42nmroa7q"      # 回访状态
FIELD_VISIT_TYPE = "p9wt4e0aqxjbdkpopp04n"          # 回访类型
FIELD_SATISFACTION = "8yhq31vqdn2vop87ryiei"        # pts选择的满意度
FIELD_VISIT_LINK = "35voqsv7xy8pk5pknr47m"          # 回访链接
FIELD_FEEDBACK_NOTE = "jxdh8qmijds6w92j5szwb"       # 备注
FIELD_CUSTOMER_NAME = "rbiax8fi5eklvmdlc4v5d"       # 客户名称
FIELD_PTS_LINK = "ulembeeuza3ctgftx69n1"            # PTS交付链接
FIELD_REGION = "o7dk5r68igm7syh8funhl"               # 区域
FIELD_VISIT_OWNER = "xzHfzQm"                        # 回访人
FIELD_DELIVERY_TYPE = "vth94xg28fxpt6ribmpjd"        # 交付类型

# 回访状态枚举值
VISIT_STATUS_DONE = "已回访"
VISIT_STATUS_OPTION_ID = "khlT6gz2Ab"  # AITable 中"已回访"的选项 ID

# ── 满意度映射（中文 → PTS 数字评分）────────────────────────────

SATISFACTION_SCORE_MAP: dict[str, int] = {
    "十分满意": 5,
    "非常满意": 5,
    "满意": 4,
    "一般": 3,
    "不满意": 2,
    "非常不满意": 1,
    "十分不满意": 1,
}

# 回访类型映射（中文 → PTS visit type）
VISIT_TYPE_MAP: dict[str, str] = {
    "交付满意度评价": "delivery_satisfaction",
    "客户满意度调研": "customer_satisfaction",
    "交付回访": "delivery_followup",
    "售后回访": "after_sales_followup",
}

# 步骤名称常量
STEP_FETCH_DELIVERY = "step_fetch_delivery"
STEP_CREATE_VISIT = "step_create_visit"
STEP_FIND_VISIT = "step_find_visit"
STEP_FILL_FEEDBACK = "step_fill_feedback"
STEP_FINISH_VISIT = "step_finish_visit"
STEP_POST_CHECK = "step_post_check"
STEP_DINGTALK_WRITEBACK = "step_dingtalk_writeback"

STEP_TO_COLUMN = {
    STEP_FETCH_DELIVERY: "step_fetch_delivery",
    STEP_CREATE_VISIT: "step_create_visit",
    STEP_FIND_VISIT: "step_find_visit",
    STEP_FILL_FEEDBACK: "step_fill_feedback",
    STEP_FINISH_VISIT: "step_finish_visit",
    STEP_POST_CHECK: "step_post_check",
    STEP_DINGTALK_WRITEBACK: "step_dingtalk_writeback",
}

_STEP_MAX_RETRIES = 3
_STEP_BASE_DELAY = 2.0


# ── AITable 数据提取辅助函数 ──────────────────────────────────────

def _extract_text(cell_value: Any) -> str:
    """从 AITable 单元格提取文本值。"""
    if cell_value is None:
        return ""
    if isinstance(cell_value, str):
        return cell_value.strip()
    if isinstance(cell_value, dict):
        # singleSelect: {"id": "xxx", "text": "已回访"}
        for key in ("text", "name", "value", "link"):
            v = cell_value.get(key, "")
            if v:
                return str(v).strip()
        return ""
    if isinstance(cell_value, list) and cell_value:
        # 可能有多个值，取第一个
        return _extract_text(cell_value[0])
    return str(cell_value).strip()


def _extract_link(cell_value: Any) -> str:
    """从 AITable 链接字段提取 URL。"""
    if cell_value is None:
        return ""
    if isinstance(cell_value, str):
        return cell_value.strip()
    if isinstance(cell_value, dict):
        for key in ("link", "url", "href"):
            v = cell_value.get(key, "")
            if v:
                return str(v).strip()
        # 可能是 {text: "...", link: "..."} 格式
        text = cell_value.get("text", "")
        if text and text.startswith("http"):
            return text.strip()
        return ""
    if isinstance(cell_value, list) and cell_value:
        return _extract_link(cell_value[0])
    return ""


def _extract_delivery_id(pts_link: str) -> str | None:
    """从 PTS 链接中提取交付 ID（project_id）。

    链接格式: https://pts.chaitin.net/project/{project_id}#base
    """
    import re
    if not pts_link:
        return None
    m = re.search(r"/project/([0-9a-f]{24})", pts_link)
    return m.group(1) if m else None


# ── 核心流程：从 AITable 查询满足条件的记录 ──────────────────────

# 短暂缓存：避免前端反复刷新时重复拉取 AITable 全量数据
_PENDING_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])
_PENDING_CACHE_TTL = 60  # 秒


async def fetch_aitable_visit_pending(force_refresh: bool = False) -> list[dict[str, Any]]:
    """从钉钉 AITable 查询满足自动回访闭环条件的记录。

    条件（参考 closed_loop_v2 VisitPlanner）：
    - 回访状态 = 已回访
    - 回访类型 不为空
    - PTS选择的满意度 不为空
    - 回访链接 为空（尚未创建 PTS 回访工单）
    - 备注 不为空
    - PTS交付链接 不为空（需要 delivery_id）
    """
    global _PENDING_CACHE

    # 缓存命中：60 秒内直接返回，避免重复拉取 AITable
    if not force_refresh:
        cached_at, cached_data = _PENDING_CACHE
        if cached_data and (time.time() - cached_at) < _PENDING_CACHE_TTL:
            logger.debug("返回缓存的回访待闭环数据 (%d 条, %.0fs 前)", len(cached_data), time.time() - cached_at)
            return cached_data

    settings = get_settings()
    base_id = settings.visit_writeback_aitable_base_id or settings.review_aitable_base_id
    table_id = settings.visit_writeback_aitable_table_id or settings.review_aitable_main_table_id

    if not base_id or not table_id:
        logger.warning("回访 AITable 配置不完整: base_id=%s, table_id=%s", base_id, table_id)
        return []

    try:
        # 服务端筛选：将所有筛选条件推到 AITable，直接返回满足条件的记录
        # 条件：回访状态=已回访 + 回访链接为空 + 备注/回访类型/满意度不为空
        # 使用 un_exist（为空）和 exist（有值）操作符，AITable 原生支持
        visit_filter = json.dumps({
            "operator": "and",
            "operands": [
                {"operator": "eq", "operands": [FIELD_VISIT_STATUS, VISIT_STATUS_DONE]},
                {"operator": "un_exist", "operands": [FIELD_VISIT_LINK]},
                {"operator": "exist", "operands": [FIELD_FEEDBACK_NOTE]},
                {"operator": "exist", "operands": [FIELD_VISIT_TYPE]},
                {"operator": "exist", "operands": [FIELD_SATISFACTION]},
            ],
        }, ensure_ascii=False)
        needed_fields = ",".join([
            FIELD_VISIT_STATUS, FIELD_VISIT_TYPE, FIELD_SATISFACTION,
            FIELD_VISIT_LINK, FIELD_FEEDBACK_NOTE, FIELD_CUSTOMER_NAME,
            FIELD_PTS_LINK, FIELD_REGION, FIELD_VISIT_OWNER,
        ])
        result = await query_records(
            limit=100,
            base_id=base_id,
            table_id=table_id,
            fetch_all=True,
            filters=visit_filter,
            field_ids=needed_fields,
        )
    except Exception:
        logger.exception("查询 AITable 回访记录失败")
        return []

    if not result:
        return []

    records = result if isinstance(result, list) else result.get("data", result.get("items", []))
    if not isinstance(records, list):
        logger.warning("AITable 返回格式异常: %s", type(records))
        return []

    pending: list[dict[str, Any]] = []

    for record in records:
        fields = record.get("cells", record) if isinstance(record, dict) else {}

        # 提取各字段值
        visit_status = _extract_text(fields.get(FIELD_VISIT_STATUS))
        visit_type = _extract_text(fields.get(FIELD_VISIT_TYPE))
        satisfaction = _extract_text(fields.get(FIELD_SATISFACTION))
        visit_link = _extract_link(fields.get(FIELD_VISIT_LINK))
        feedback_note = _extract_text(fields.get(FIELD_FEEDBACK_NOTE))
        customer_name = _extract_text(fields.get(FIELD_CUSTOMER_NAME))
        pts_link = _extract_link(fields.get(FIELD_PTS_LINK))
        region = _extract_text(fields.get(FIELD_REGION))
        visit_owner = _extract_text(fields.get(FIELD_VISIT_OWNER))

        # 判断是否满足条件
        # 1. 回访状态 = 已回访
        if visit_status != VISIT_STATUS_DONE and visit_status != VISIT_STATUS_OPTION_ID:
            continue
        # 2. 回访类型不为空
        if not visit_type:
            continue
        # 3. 满意度不为空
        if not satisfaction:
            continue
        # 4. 回访链接为空（尚未创建 PTS 回访工单）
        if visit_link:
            continue
        # 5. 备注不为空
        if not feedback_note:
            continue
        # 6. PTS交付链接不为空
        delivery_id = _extract_delivery_id(pts_link)
        if not delivery_id:
            continue

        # 满足条件
        record_id = record.get("recordId", record.get("id", ""))
        pending.append({
            "record_id": record_id,
            "customer_name": customer_name,
            "pts_link": pts_link,
            "delivery_id": delivery_id,
            "visit_status": visit_status,
            "visit_type": visit_type,
            "satisfaction": satisfaction,
            "feedback_note": feedback_note,
            "region": region,
            "visit_owner": visit_owner,
        })

    logger.info("从 AITable 查询到 %d 条满足回访闭环条件的记录", len(pending))
    _PENDING_CACHE = (time.time(), pending)
    return pending


# ── 辅助函数 ────────────────────────────────────────────────────

def _should_run_step(visit_log: VisitLog, step: str) -> bool:
    column = STEP_TO_COLUMN[step]
    current_status = getattr(visit_log, column, "pending")
    return current_status != "success"


def _mark_step(visit_log: VisitLog, step: str, status: str, db: Session) -> None:
    column = STEP_TO_COLUMN[step]
    setattr(visit_log, column, status)
    visit_log.current_step = step
    db.commit()


def _append_step_log(visit_log: VisitLog, step: str, status: str, error: str | None = None) -> None:
    log_entry = {
        "step": step,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        log_entry["error"] = error
    existing: list = visit_log.step_log if isinstance(visit_log.step_log, list) else []
    existing.append(log_entry)
    visit_log.step_log = existing


async def _run_step_with_retry(
    step_name: str,
    fn,
    *args,
    visit_log: VisitLog,
    db: Session,
    **kwargs,
) -> Any:
    max_retries = _STEP_MAX_RETRIES
    settings = get_settings()
    if settings.visit_max_retries > 0:
        max_retries = settings.visit_max_retries

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            _mark_step(visit_log, step_name, "running", db)
            result = await fn(*args, **kwargs)
            return result
        except PermissionError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                delay = _STEP_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "步骤 %s 失败 (%d/%d)，%.0fs 后重试: %s",
                    step_name, attempt, max_retries, delay, exc,
                )
                await asyncio.sleep(delay)
    raise last_error or RuntimeError(f"步骤 {step_name} 执行失败")


def _map_satisfaction_score(satisfaction_text: str) -> int:
    """将中文满意度映射为 PTS 数字评分。"""
    settings = get_settings()
    # 优先用配置的默认值，如果文本无法映射
    default_score = settings.visit_default_satisfaction
    for key, score in SATISFACTION_SCORE_MAP.items():
        if key in satisfaction_text:
            return score
    # 尝试直接解析数字
    try:
        val = int(satisfaction_text.strip())
        if 1 <= val <= 5:
            return val
    except ValueError:
        pass
    return default_score


def _map_visit_type(visit_type_text: str) -> str:
    """将中文回访类型映射为 PTS visit type。"""
    for key, pts_type in VISIT_TYPE_MAP.items():
        if key in visit_type_text:
            return pts_type
    return "customer_satisfaction"  # 默认值


# ── 主流程 ──────────────────────────────────────────────────────

async def run_visit_pipeline(
    db: Session,
    *,
    record_id: str,
    delivery_id: str,
    customer_name: str | None = None,
    visit_type: str | None = None,
    satisfaction: str | None = None,
    feedback_note: str | None = None,
    region: str | None = None,
    visit_owner: str | None = None,
    trigger_source: str = "manual",
) -> dict[str, Any]:
    """对单条 AITable 记录执行 PTS 回访工单创建和闭环。

    Args:
        db: SQLAlchemy Session
        record_id: AITable 记录 ID
        delivery_id: PTS 交付 ID
        customer_name: 客户名称
        visit_type: 回访类型（中文）
        satisfaction: 满意度（中文）
        feedback_note: 回访备注
        region: 区域
        visit_owner: 回访人
        trigger_source: 触发来源

    Returns:
        汇总结果 dict
    """
    settings = get_settings()

    if not settings.visit_real_execution_enabled:
        return {"status": "skipped", "reason": "visit_real_execution_enabled 未启用"}

    # ── 幂等检查 ─────────────────────────────────────────────
    visit_log = db.query(VisitLog).filter(
        VisitLog.project_id == delivery_id,
        VisitLog.status.in_(["pending", "running", "partial"]),
    ).first()

    if visit_log:
        logger.info("恢复已有回访记录: delivery_id=%s, current_step=%s", delivery_id, visit_log.current_step)
        visit_log.retry_count += 1
        visit_log.trigger_source = "retry"
        db.commit()
    else:
        completed = db.query(VisitLog).filter(
            VisitLog.project_id == delivery_id,
            VisitLog.status == "completed",
        ).first()
        if completed:
            return {"status": "already_completed", "visit_log_id": str(completed.id)}

        visit_log = VisitLog(
            project_id=delivery_id,
            project_name=customer_name,
            customer_name=customer_name,
            region=region,
            execution_mode=settings.visit_execution_mode,
            satisfaction_score=_map_satisfaction_score(satisfaction or ""),
            visit_note=feedback_note,
            trigger_source=trigger_source,
        )
        # 记录回访人信息到 step_log 初始化
        if visit_owner:
            init_log = {"step": "init", "status": "info", "visit_owner": visit_owner,
                        "timestamp": datetime.now(timezone.utc).isoformat()}
            visit_log.step_log = [init_log]
        db.add(visit_log)
        db.commit()

    visit_log.status = "running"
    db.commit()

    try:
        # Step 1: 查询交付元数据
        if _should_run_step(visit_log, STEP_FETCH_DELIVERY):
            metadata = await _run_step_with_retry(
                STEP_FETCH_DELIVERY,
                fetch_delivery_metadata,
                delivery_id,
                visit_log=visit_log, db=db,
            )
            if not metadata:
                raise RuntimeError(f"交付元数据查询为空: delivery_id={delivery_id}")

            visit_log.company_id = metadata.get("company_id")
            visit_log.visitor_id = metadata.get("company_id")
            visit_log.contact_id = metadata.get("contact_id")
            visit_log.product_id = metadata.get("product_id")
            visit_log.form_id = metadata.get("form_id")
            if not visit_log.project_name:
                visit_log.project_name = metadata.get("project_name")
            if not visit_log.customer_name:
                visit_log.customer_name = metadata.get("company_name")

            _mark_step(visit_log, STEP_FETCH_DELIVERY, "success", db)
            _append_step_log(visit_log, STEP_FETCH_DELIVERY, "success")
            db.commit()

        if not visit_log.company_id:
            raise RuntimeError("缺少 company_id")
        if not visit_log.product_id or not visit_log.form_id:
            raise RuntimeError("缺少 product_id 或 form_id")

        # Step 2: 创建回访工单
        if _should_run_step(visit_log, STEP_CREATE_VISIT):
            mapped_type = _map_visit_type(visit_type or "")
            await _run_step_with_retry(
                STEP_CREATE_VISIT,
                create_visit,
                company_id=visit_log.company_id,
                visitor_id=visit_log.visitor_id,
                visit_type=mapped_type,
                contact_id=visit_log.contact_id,
                product_id=visit_log.product_id,
                form_id=visit_log.form_id,
                delivery_id=delivery_id,
                visit_log=visit_log, db=db,
            )
            _mark_step(visit_log, STEP_CREATE_VISIT, "success", db)
            _append_step_log(visit_log, STEP_CREATE_VISIT, "success")
            db.commit()

        # Step 3: 定位回访
        if _should_run_step(visit_log, STEP_FIND_VISIT):
            visit_id = await _run_step_with_retry(
                STEP_FIND_VISIT,
                find_created_visit,
                company_id=visit_log.company_id,
                delivery_id=delivery_id,
                visitor_id=visit_log.visitor_id,
                visit_log=visit_log, db=db,
            )
            if not visit_id:
                raise RuntimeError("无法定位回访工单")

            visit_log.pts_visit_id = visit_id
            visit_log.visit_url = VISIT_URL_TEMPLATE.format(visit_id=visit_id)
            _mark_step(visit_log, STEP_FIND_VISIT, "success", db)
            _append_step_log(visit_log, STEP_FIND_VISIT, "success")
            db.commit()

        # Step 4: 填写反馈
        if _should_run_step(visit_log, STEP_FILL_FEEDBACK):
            detail = await fetch_visit_detail(visit_log.pts_visit_id)
            if not detail:
                raise RuntimeError("回访详情查询为空")

            content_id = detail.get("content_id")
            contact_id = detail.get("contact_id") or visit_log.contact_id
            if not content_id:
                raise RuntimeError("缺少 content_id")

            visit_log.content_id = content_id
            db.commit()

            score = _map_satisfaction_score(satisfaction or "")
            note = feedback_note or settings.visit_default_note
            await _run_step_with_retry(
                STEP_FILL_FEEDBACK,
                process_visit,
                visit_id=visit_log.pts_visit_id,
                contact_id=contact_id,
                content_id=content_id,
                score=score,
                note=note,
                visit_log=visit_log, db=db,
            )
            visit_log.satisfaction_score = score
            visit_log.visit_note = note
            _mark_step(visit_log, STEP_FILL_FEEDBACK, "success", db)
            _append_step_log(visit_log, STEP_FILL_FEEDBACK, "success")
            db.commit()

        # Step 5: 完成回访
        if _should_run_step(visit_log, STEP_FINISH_VISIT):
            await _run_step_with_retry(
                STEP_FINISH_VISIT,
                finish_visit,
                visit_log.pts_visit_id,
                visit_log=visit_log, db=db,
            )
            _mark_step(visit_log, STEP_FINISH_VISIT, "success", db)
            _append_step_log(visit_log, STEP_FINISH_VISIT, "success")
            db.commit()

        # Step 6: 后验证
        if _should_run_step(visit_log, STEP_POST_CHECK):
            detail = await fetch_visit_detail(visit_log.pts_visit_id)
            if not detail or not detail.get("finished"):
                raise RuntimeError("后验证失败：回访未完成")
            _mark_step(visit_log, STEP_POST_CHECK, "success", db)
            _append_step_log(visit_log, STEP_POST_CHECK, "success")
            db.commit()

        # Step 7: 钉钉写入（回访链接写回 AITable）
        if _should_run_step(visit_log, STEP_DINGTALK_WRITEBACK):
            writeback_result = await update_visit_link_to_dingtalk(
                project_id=delivery_id,
                visit_url=visit_log.visit_url,
                dingtalk_record_id=record_id,
                customer_name=visit_log.customer_name,
                region=visit_log.region,
            )
            visit_log.dingtalk_writeback = writeback_result
            wb_status = "success" if writeback_result.get("action") not in ("failed",) else "failed"
            _mark_step(visit_log, STEP_DINGTALK_WRITEBACK, wb_status, db)
            _append_step_log(visit_log, STEP_DINGTALK_WRITEBACK, wb_status)
            db.commit()

        # ── 完成 ─────────────────────────────────────────────
        visit_log.status = "completed"
        visit_log.error = None
        db.commit()

        logger.info("回访闭环完成: delivery_id=%s, visit_url=%s", delivery_id, visit_log.visit_url)
        return {
            "status": "completed",
            "visit_log_id": str(visit_log.id),
            "pts_visit_id": visit_log.pts_visit_id,
            "visit_url": visit_log.visit_url,
        }

    except PermissionError as exc:
        visit_log.status = "failed"
        visit_log.error = f"权限错误: {exc}"
        _append_step_log(visit_log, visit_log.current_step or "unknown", "failed", str(exc))
        db.commit()
        return {"status": "failed", "error": str(exc)}

    except Exception as exc:
        visit_log.status = "partial"
        visit_log.error = str(exc)
        _append_step_log(visit_log, visit_log.current_step or "unknown", "failed", str(exc))
        db.commit()
        return {"status": "partial", "error": str(exc), "current_step": visit_log.current_step}


# ── 批量执行 ────────────────────────────────────────────────────

async def run_visit_batch(db: Session, trigger_source: str = "scheduler") -> dict[str, Any]:
    """从 AITable 查询满足条件的记录，批量执行回访闭环。"""
    settings = get_settings()
    if not settings.visit_real_execution_enabled:
        return {"status": "skipped", "reason": "visit_real_execution_enabled 未启用"}

    pending = await fetch_aitable_visit_pending()
    if not pending:
        return {"status": "success", "total": 0}

    completed_count = 0
    failed_count = 0
    skipped_count = 0
    errors: list[str] = []

    for item in pending:
        try:
            result = await run_visit_pipeline(
                db,
                record_id=item["record_id"],
                delivery_id=item["delivery_id"],
                customer_name=item.get("customer_name"),
                visit_type=item.get("visit_type"),
                satisfaction=item.get("satisfaction"),
                feedback_note=item.get("feedback_note"),
                region=item.get("region"),
                visit_owner=item.get("visit_owner"),
                trigger_source=trigger_source,
            )
            if result.get("status") == "completed":
                completed_count += 1
            elif result.get("status") == "skipped":
                skipped_count += 1
            else:
                failed_count += 1
                if result.get("error"):
                    errors.append(f"{item.get('customer_name', '?')}: {result['error']}")
        except Exception as exc:
            failed_count += 1
            errors.append(f"{item.get('customer_name', '?')}: {exc}")

    return {
        "status": "success",
        "total": len(pending),
        "completed": completed_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "errors": errors[:10],  # 最多返回10条错误
    }


# ── 查询函数 ────────────────────────────────────────────────────

async def list_visit_logs(
    db: Session,
    *,
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[VisitLog], int]:
    """查询回访日志列表。"""
    from sqlalchemy import func as sa_func

    query = db.query(VisitLog)
    count_query = db.query(sa_func.count(VisitLog.id))

    if status:
        query = query.filter(VisitLog.status == status)
        count_query = count_query.filter(VisitLog.status == status)
    if project_id:
        query = query.filter(VisitLog.project_id == project_id)
        count_query = count_query.filter(VisitLog.project_id == project_id)

    total = count_query.scalar() or 0
    items = (
        query.order_by(VisitLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return items, total


async def get_visit_log(db: Session, visit_log_id: str) -> VisitLog | None:
    """获取单个回访日志。"""
    try:
        uid = uuid.UUID(visit_log_id)
    except ValueError:
        return None
    return db.query(VisitLog).filter(VisitLog.id == uid).first()
