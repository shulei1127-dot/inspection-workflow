"""第 3 步：审核通过时间提取

通过 PTS GraphQL API 查询终验任务的 comment 列表，从最新的"审核通过"记录中
提取审核通过日期。

与其它 extractor 不同，本函数使用 review_pts_query()（抛异常）而非 fetch_* 静默函数，
确保 429 限流等瞬时错误不会被吞掉导致 approval_time 误判为 None。
查询失败会向上传播，触发编排服务层面的自动重试。
"""

from __future__ import annotations

import logging
import re

from services.review.pts_review_client import (
    enum_value,
    review_pts_query,
    QUERY_DELIVERY_TASK_BY_ID,
    QUERY_RELATED_DELIVERY_TASK_LIST,
)

logger = logging.getLogger(__name__)


async def extract_approval_time(project_id: str) -> str | None:
    """提取项目审核通过时间。

    流程：
    1. 查询项目的交付任务列表
    2. 优先找终验任务 (task_type='final_acceptance')，其次找到货验收任务
    3. 查询任务详情（含评论记录）
    4. 从评论列表末尾向前搜索"审核通过"关键字，提取日期
    5. 如果评论中未找到，使用 finished_at 兜底

    使用 review_pts_query() 而非 fetch_* 静默函数，
    使 429 限流错误向上传播而非被静默吞掉返回 None。

    Args:
        project_id: PTS 交付 ID

    Returns:
        审核通过日期字符串 (YYYY-MM-DD)，未找到时返回 None

    Raises:
        RuntimeError: PTS API 请求失败（429 限流、401 过期等），由上层重试
    """
    # 获取交付任务列表 — 用 review_pts_query 保证 429 等错误不被吞掉
    tasks_result = await review_pts_query(
        QUERY_RELATED_DELIVERY_TASK_LIST,
        {
            "related_type": enum_value("product_delivery"),
            "related_id": project_id,
        },
    )
    tasks = (tasks_result or {}).get("related_delivery_task_list") or []

    # 优先找终验任务，其次找到货验收任务
    target_task = None
    for t in tasks:
        if t.get("task_type") == "final_acceptance":
            target_task = t
            break
    if target_task is None:
        for t in tasks:
            if t.get("task_type") == "arrival_acceptance":
                target_task = t
                break

    if not target_task or not target_task.get("id"):
        logger.info("未找到终验或到货验收任务: project_id=%s", project_id)
        return None

    # 获取任务详情（含评论记录）— 同样用 review_pts_query 保证错误传播
    task_result = await review_pts_query(
        QUERY_DELIVERY_TASK_BY_ID,
        {"id": target_task["id"]},
    )
    task_detail = (task_result or {}).get("delivery_task")

    if task_detail:
        # 在评论列表中找"审核通过"记录
        comments = task_detail.get("comment") or []
        # 从后往前找最新的审核通过记录
        for i in range(len(comments) - 1, -1, -1):
            c = comments[i]
            content = c.get("content", "")
            if "审核通过" in content:
                match = re.search(r"(\d{4}-\d{2}-\d{2})", str(c.get("created_at", "")))
                if match:
                    logger.info(
                        "GraphQL 提取审核通过时间: project_id=%s, approval_time=%s",
                        project_id,
                        match.group(1),
                    )
                    return match.group(1)

        # 如果评论中没有审核通过记录，尝试用 finished_at 兜底
        finished_at = task_detail.get("finished_at")
        if finished_at:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", str(finished_at))
            if match:
                logger.info(
                    "使用 finished_at 作为审核通过时间: project_id=%s, approval_time=%s",
                    project_id,
                    match.group(1),
                )
                return match.group(1)

    logger.info("终验任务中未找到审核通过记录: project_id=%s", project_id)
    return None
