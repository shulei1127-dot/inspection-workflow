"""Read-only: fetch PTS work orders eligible for DingTalk AITable write
and compare against local DB to find new (not-yet-synced) work orders.

This script performs NO writes — it only queries PTS and the local DB.
"""

import asyncio
import sys

sys.path.insert(0, "/data/inspect/inspection-workflow")

from datetime import datetime, timedelta

from core.db import SessionLocal
from models.work_order import WorkOrder
from models.work_order_sync import WorkOrderSync
from services import pts_client
from services.aitable_fields import current_month


def _fmt_month(plan: str) -> str:
    try:
        utc_dt = datetime.fromisoformat(plan.replace("Z", "+00:00"))
        return (utc_dt + timedelta(hours=8)).strftime("%Y-%m")
    except (ValueError, TypeError):
        return "?"


async def main() -> None:
    sync_month = current_month()
    print(f"目标月份: {sync_month} (Asia/Shanghai)\n")

    # 1) PTS 检索满足写入条件的工单
    raw_orders = await pts_client.query_inspection_work_orders(sync_month)
    print(f"PTS 检索到满足条件工单数: {len(raw_orders)}\n")

    # 2) 本地 DB 现状
    db = SessionLocal()
    local_wo = {
        wo.pts_order_id: wo
        for wo in db.query(WorkOrder).all()
    }
    local_sync = {
        (s.pts_order_id, s.sync_month): s
        for s in db.query(WorkOrderSync).all()
    }

    print("=" * 100)
    print(f"{'PTS单号':<22} {'计划完成':<12} {'客户':<18} {'产品':<14} {'售后':<6} 状态")
    print("=" * 100)

    new_ids: list[str] = []
    already_ids: list[str] = []
    pending_retry: list[str] = []

    for raw in raw_orders:
        pts_id = str(raw.get("id", ""))
        plan = raw.get("plan_complete_date", "")
        month = _fmt_month(plan)
        delivery = raw.get("delivery") or {}
        after_sale = (delivery.get("after_sale") or {}).get("name", "?")
        company = (raw.get("company") or {})
        customer = company.get("name", "?")
        product = ""
        pi = (delivery.get("product_info") or [])
        seen = set()
        names = []
        for p in pi:
            det = (p or {}).get("product_detail") or {}
            prod = (det or {}).get("product") or {}
            n = prod.get("name")
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        product = "、".join(names)

        wo = local_wo.get(pts_id)
        assoc = local_sync.get((pts_id, sync_month))

        if assoc is not None and assoc.sync_status == "synced":
            flag = "已同步(本月)"
            already_ids.append(pts_id)
        elif wo is not None and wo.dt_sync_status == "synced" and wo.dt_synced_month == sync_month:
            flag = "已同步(字段级)"
            already_ids.append(pts_id)
        elif wo is not None and assoc is not None and assoc.sync_status in ("failed", "dedup_conflict"):
            flag = f"待重试({assoc.sync_status})"
            pending_retry.append(pts_id)
        else:
            is_new = wo is None
            flag = "★ 新增" if is_new else "★ 新月份候选(已入库)"
            new_ids.append(pts_id)

        print(f"{pts_id:<22} {month:<12} {customer:<18} {product:<14} {after_sale:<6} {flag}")

    print("=" * 100)
    print(f"\n★ 新增(未写入AITable): {len(new_ids)} 条")
    for pid in new_ids:
        print(f"  - {pid}")
    print(f"\n已同步(本月): {len(already_ids)} 条")
    print(f"待重试: {len(pending_retry)} 条")
    for pid in pending_retry:
        print(f"  - {pid}")

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
