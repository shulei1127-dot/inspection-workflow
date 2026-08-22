"""销售巡检确认推送 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["sales-confirm"])


@router.post("/api/sales-confirm/run")
async def trigger_sales_confirm(dry_run: bool = True):
    """手动触发扫描 + 推送巡检确认表单给销售。

    默认 dry_run=True 只扫描不发送，dry_run=false 才会真正推送消息。
    """
    from services.sales_confirm_service import run_sales_confirm

    return await run_sales_confirm(dry_run=dry_run)
