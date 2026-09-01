"""APScheduler job registration for inspection workflow.

Jobs:
- Sync job: PTS → local DB → DingTalk AITable (daily at 16:00)
- Dispatch monitor: 客户巡检派单 AITable poll (every 2 hours)
- Email pre-analysis: pre-analyze email-pending records (daily at 9:00)
- Closure check: auto-close PTS work orders (daily at 10:00)
- Yunji keepalive: refresh session cookie (every 3 hours)

Removed:
- monitor:aitable-poll (日常增值服务进展) — no longer needed, use manual API instead
"""

import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import get_settings
from core.db import SessionLocal

logger = logging.getLogger(__name__)


def register_jobs(scheduler: BackgroundScheduler) -> list[str]:
    """Register all scheduled jobs."""
    settings = get_settings()
    registered_ids: list[str] = []
    tz = settings.scheduler_timezone

    # Sync job: PTS → DingTalk
    sync_cron = settings.sync_cron.strip()
    if sync_cron:
        scheduler.add_job(
            _run_sync_job,
            trigger=CronTrigger.from_crontab(sync_cron, timezone=tz),
            id="sync:pts-to-dingtalk",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered_ids.append("sync:pts-to-dingtalk")
        logger.info("Registered sync job with cron: %s", sync_cron)

    # Monitor job: DingTalk AITable poll (客户巡检派单)
    if settings.dt_dispatch_base_id and settings.dt_dispatch_table_id:
        scheduler.add_job(
            _run_dispatch_monitor_job,
            trigger=IntervalTrigger(seconds=settings.dt_poll_interval),
            id="monitor:dispatch-aitable-poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered_ids.append("monitor:dispatch-aitable-poll")
        logger.info("Registered dispatch monitor job with interval: %ds", settings.dt_poll_interval)

    # Email probe job: refresh email-pending cache for frontend display
    email_probe_cron = settings.email_probe_cron.strip()
    if email_probe_cron:
        scheduler.add_job(
            _run_email_probe_job,
            trigger=CronTrigger.from_crontab(email_probe_cron, timezone=tz),
            id="monitor:email-probe",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered_ids.append("monitor:email-probe")
        logger.info("Registered email probe job with cron: %s", email_probe_cron)

    # Closure check job: auto-close PTS work orders
    closure_check_cron = settings.closure_check_cron.strip()
    if closure_check_cron:
        scheduler.add_job(
            _run_closure_check_job,
            trigger=CronTrigger.from_crontab(closure_check_cron, timezone=tz),
            id="monitor:closure-check",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered_ids.append("monitor:closure-check")
        logger.info("Registered closure check job with cron: %s", closure_check_cron)

    # Yunji cookie keepalive job (every 3 hours)
    if settings.yunji_session_cookie:
        scheduler.add_job(
            _run_yunji_keepalive_job,
            trigger=IntervalTrigger(hours=3),
            id="keepalive:yunji-cookie",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered_ids.append("keepalive:yunji-cookie")
        logger.info("Registered yunji cookie keepalive job (every 3h)")

    # Email pre-analysis job (separate from monitor poll)
    if settings.email_pre_analysis_enabled:
        pre_analysis_cron = settings.email_pre_analysis_cron.strip()
        if pre_analysis_cron:
            scheduler.add_job(
                _run_email_pre_analysis_job,
                trigger=CronTrigger.from_crontab(pre_analysis_cron, timezone=tz),
                id="monitor:email-pre-analysis",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("monitor:email-pre-analysis")
            logger.info("Registered email pre-analysis job with cron: %s", pre_analysis_cron)

    # Review pipeline job (交付转售后审核)
    if settings.review_pipeline_enabled:
        review_cron = settings.review_pipeline_cron.strip()
        if review_cron:
            scheduler.add_job(
                _run_review_pipeline_job,
                trigger=CronTrigger.from_crontab(review_cron, timezone=tz),
                id="review:pipeline",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("review:pipeline")
            logger.info("Registered review pipeline job with cron: %s", review_cron)

    # Inspection library job (巡检信息库: 同步 + 回写缺失地址/邮箱)
    if settings.inspection_library_enabled and settings.dt_dispatch_base_id and settings.dt_dispatch_table_id:
        library_cron = settings.inspection_library_cron.strip()
        if library_cron:
            scheduler.add_job(
                _run_inspection_library_job,
                trigger=CronTrigger.from_crontab(library_cron, timezone=tz),
                id="inspection-library:sync-backfill",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("inspection-library:sync-backfill")
            logger.info("Registered inspection library job with cron: %s", library_cron)

    # Sales confirm job (巡检确认表单推送)
    if settings.sales_confirm_enabled:
        sales_confirm_cron = settings.sales_confirm_cron.strip()
        if sales_confirm_cron:
            scheduler.add_job(
                _run_sales_confirm_job,
                trigger=CronTrigger.from_crontab(sales_confirm_cron, timezone=tz),
                id="sales-confirm:push",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("sales-confirm:push")
            logger.info("Registered sales confirm job with cron: %s", sales_confirm_cron)

    # Daily change summary job
    if settings.daily_change_summary_enabled:
        daily_summary_cron = settings.daily_change_summary_cron.strip()
        if daily_summary_cron:
            scheduler.add_job(
                _run_daily_change_summary_job,
                trigger=CronTrigger.from_crontab(daily_summary_cron, timezone=tz),
                id="daily:change-summary",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("daily:change-summary")
            logger.info("Registered daily change summary job with cron: %s", daily_summary_cron)

    # Visit pipeline job (交付转售后回访闭环)
    if settings.visit_pipeline_enabled:
        visit_cron = settings.visit_pipeline_cron.strip()
        if visit_cron:
            scheduler.add_job(
                _run_visit_pipeline_job,
                trigger=CronTrigger.from_crontab(visit_cron, timezone=tz),
                id="visit:pipeline",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("visit:pipeline")
            logger.info("Registered visit pipeline job with cron: %s", visit_cron)

    # Daily digest job
    if settings.daily_digest_enabled:
        digest_cron = settings.daily_digest_cron.strip()
        if digest_cron:
            scheduler.add_job(
                _run_daily_digest_job,
                trigger=CronTrigger.from_crontab(digest_cron, timezone=tz),
                id="daily:digest",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("daily:digest")
            logger.info("Registered daily digest job with cron: %s", digest_cron)

    return registered_ids


def _run_sync_job() -> None:
    """Scheduled sync job runner.

    Pulls PTS work orders for the current month and auto-pushes
    new/updated data to DingTalk AITable.
    """
    from services.dingtalk_notifier import notify_sync_job
    from models.work_order import WorkOrder

    error = None
    result = None

    try:
        from services.sync_service import run_sync

        with SessionLocal() as db:
            sync_log = asyncio.run(run_sync(db, trigger_source="scheduler", push_to_aitable=True, only_new_for_month=True))
            logger.info(
                "Scheduled sync completed: status=%s fetched=%d created=%d updated=%d",
                sync_log.status,
                sync_log.fetched_count,
                sync_log.created_count,
                sync_log.updated_count,
            )

            # Planned-completion write-back is intentionally explicit; the
            # normal sync must not mutate any existing AITable record.
            # Fetch newly created work orders from the same session
            new_orders = []
            if sync_log.started_at and sync_log.completed_at:
                new_orders = db.query(WorkOrder).filter(
                    WorkOrder.created_at >= sync_log.started_at,
                    WorkOrder.created_at <= sync_log.completed_at,
                ).all()
                new_orders = [
                    {
                        "pts_order_id": wo.pts_order_id,
                        "customer_name": wo.customer_name,
                        "product_name": wo.product_name,
                    }
                    for wo in new_orders
                ]
            # Build result dict with new orders
            result = sync_log.__dict__.copy()
            result["new_orders"] = new_orders
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled sync job failed")

    # Send DingTalk notification
    asyncio.run(notify_sync_job(result if result else {}, error))


def _run_dispatch_monitor_job() -> None:
    """Scheduled dispatch monitor job runner (客户巡检派单)."""
    from services.dingtalk_notifier import notify_dispatch_monitor

    error = None
    result = {}

    try:
        from services.monitor_service import run_dispatch_monitor_poll

        with SessionLocal() as db:
            result = asyncio.run(run_dispatch_monitor_poll(db))
            logger.info(
                "Scheduled dispatch monitor poll completed: status=%s dispatch=%d failed=%d",
                result.get("status"),
                result.get("dispatch_triggered", 0),
                result.get("dispatch_failed", 0),
            )
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled dispatch monitor job failed")

    # Send DingTalk notification
    asyncio.run(notify_dispatch_monitor(result, error))


def _run_email_probe_job() -> None:
    """Scheduled email probe job runner.

    Probes 日常增值服务进展 AITable for email-pending records
    and refreshes the cache for frontend display. Does NOT auto-send.
    """
    from services.dingtalk_notifier import notify_email_probe

    error = None
    result = {}

    try:
        from services.monitor_service import get_email_pending

        with SessionLocal() as db:
            result = asyncio.run(get_email_pending(db))
            logger.info(
                "Scheduled email probe completed: total=%d pending",
                result.get("total", 0),
            )
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled email probe job failed")

    # Send DingTalk notification
    asyncio.run(notify_email_probe(result, error))


def _run_yunji_keepalive_job() -> None:
    """Scheduled yunji cookie keepalive job runner."""
    from services.dingtalk_notifier import notify_yunji_keepalive

    error = None
    result = {}

    try:
        from services.yunji_client import keepalive_cookie

        result = asyncio.run(keepalive_cookie())
        logger.info(
            "Yunji cookie keepalive: status=%s",
            result.get("status"),
        )
        if result.get("status") == "expired":
            logger.warning("Yunji session cookie 已过期，请尽快更新 YUNJI_SESSION_COOKIE")
    except Exception as e:
        error = str(e)
        logger.exception("Yunji cookie keepalive job failed")

    # Send DingTalk notification
    asyncio.run(notify_yunji_keepalive(result, error))


def _run_closure_check_job() -> None:
    """Scheduled PTS work order closure check job runner.

    First syncs closure status from PTS (marks already-closed work orders),
    then runs the active closure check for remaining unclosed orders.
    """
    from services.dingtalk_notifier import notify_closure_check

    error = None
    result = {}

    try:
        import asyncio as _asyncio

        async def _closure_check_workflow():
            from services.pts_closure_service import sync_closure_status_from_pts
            from services.monitor_service import run_closure_check
            from core.db import SessionLocal as _SL

            with _SL() as db:
                # Step 1: Sync closure status from PTS for already-closed work orders
                sync_result = await sync_closure_status_from_pts(db)
                logger.info(
                    "Scheduled closure status sync: status=%s checked=%d updated=%d failed=%d",
                    sync_result.get("status"),
                    sync_result.get("checked", 0),
                    sync_result.get("updated", 0),
                    sync_result.get("failed", 0),
                )

                # Step 2: Run active closure check for remaining unclosed orders
                result.update(await run_closure_check(db))
                logger.info(
                    "Scheduled closure check completed: status=%s checked=%d closed=%d failed=%d",
                    result.get("status"),
                    result.get("checked", 0),
                    result.get("completed", result.get("closed", 0)),
                    result.get("retryable_failed", result.get("failed", 0)),
                )

        _asyncio.run(_closure_check_workflow())
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled closure check job failed")

    # Send DingTalk notification
    asyncio.run(notify_closure_check(result, error))


def _run_email_pre_analysis_job() -> None:
    """Scheduled email pre-analysis job runner."""
    from services.dingtalk_notifier import notify_email_pre_analysis

    error = None
    result = {}

    try:
        from services.email_pre_analysis import run_email_pre_analysis

        with SessionLocal() as db:
            result = asyncio.run(run_email_pre_analysis(db))
            logger.info(
                "Scheduled email pre-analysis completed: scanned=%d new=%d success=%d failed=%d skipped=%d",
                result.get("scanned", 0),
                result.get("new", 0),
                result.get("success", 0),
                result.get("failed", 0),
                result.get("skipped", 0),
            )
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled email pre-analysis job failed")

    # Send DingTalk notification
    asyncio.run(notify_email_pre_analysis(result, error))


def _run_review_pipeline_job() -> None:
    """Scheduled review pipeline job runner (交付转售后审核)."""
    from services.dingtalk_notifier import notify_review_pipeline

    error = None
    result = {}

    try:
        from services.review.review_service import run_review_pipeline

        with SessionLocal() as db:
            result = asyncio.run(run_review_pipeline(db, trigger_source="scheduler"))
            logger.info(
                "Scheduled review pipeline completed: total=%d passed=%d rejected=%d manual=%d errors=%d",
                result.get("total", 0),
                result.get("passed", 0),
                result.get("rejected", 0),
                result.get("manual", 0),
                result.get("errors", 0),
            )
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled review pipeline job failed")

    # Send DingTalk notification
    asyncio.run(notify_review_pipeline(result, error))


def _run_daily_change_summary_job() -> None:
    """Scheduled daily change summary job runner.

    Collects git commits, builds daily summary, and pushes the detailed report.
    """
    from services.dingtalk_notifier import notify_daily_change_summary

    error = None
    result = {}

    try:
        from services.change_log_service import run_daily_change_summary

        with SessionLocal() as db:
            result = asyncio.run(run_daily_change_summary(db))
            logger.info(
                "Scheduled daily change summary completed: date=%s entries=%d pushed=%s",
                result.get("date"),
                result.get("total_entries", 0),
                result.get("push_result", {}).get("pushed", False),
            )
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled daily change summary job failed")

    if error:
        asyncio.run(notify_daily_change_summary(result, error))


def _run_visit_pipeline_job() -> None:
    """Scheduled visit pipeline job runner (交付转售后回访闭环).

    从 AITable 查询满足条件的记录，批量执行回访闭环。
    """
    from services.dingtalk_notifier import notify_visit_pipeline

    error = None
    result = {}

    try:
        from services.visit.visit_service import run_visit_batch

        with SessionLocal() as db:
            result = asyncio.run(run_visit_batch(db, trigger_source="scheduler"))
            logger.info(
                "Scheduled visit pipeline completed: total=%d completed=%d failed=%d skipped=%d",
                result.get("total", 0),
                result.get("completed", 0),
                result.get("failed", 0),
                result.get("skipped", 0),
            )
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled visit pipeline job failed")

    # Send DingTalk notification
    asyncio.run(notify_visit_pipeline(result, error))


def _run_daily_digest_job() -> None:
    """Scheduled daily digest job runner.

    Merges all routine notifications collected during the day into
    a single digest message. Runs on workdays after all other jobs.
    """
    from services.dingtalk_notifier import notify_daily_digest

    try:
        asyncio.run(notify_daily_digest())
        logger.info("Daily digest job completed")
    except Exception as e:
        logger.exception("Daily digest job failed: %s", e)

def _run_sales_confirm_job() -> None:
    """Scheduled sales confirm push job (巡检确认表单推送)."""
    from core.config import get_settings as _gs
    from services.sales_confirm_service import run_sales_confirm

    settings = _gs()
    try:
        result = asyncio.run(run_sales_confirm(dry_run=settings.sales_confirm_dry_run))
        logger.info(
            "Scheduled sales confirm job completed: scanned=%d candidates=%d sent=%d skipped=%d errors=%d dry_run=%s",
            result.get("scanned", 0),
            result.get("candidates", 0),
            result.get("sent", 0),
            result.get("skipped", 0),
            result.get("errors", 0),
            result.get("dry_run", True),
        )
    except Exception as e:
        logger.exception("Scheduled sales confirm job failed: %s", e)


def _run_inspection_library_job() -> None:
    """Scheduled inspection library job runner.

    1. 从钉钉表 + 本地工单同步巡检信息库
    2. 将同 交付ID+项目ID 的历史现场地址/报告邮箱回写到缺失字段的钉钉记录
    """
    try:
        from services import inspection_library_service

        with SessionLocal() as db:
            sync_result = asyncio.run(inspection_library_service.sync_library(db))
            logger.info(
                "Scheduled inspection library sync completed: total=%d new=%d updated=%d skipped=%d",
                sync_result.get("total", 0),
                sync_result.get("new", 0),
                sync_result.get("updated", 0),
                sync_result.get("skipped", 0),
            )
            backfill_result = asyncio.run(inspection_library_service.backfill_missing_info(db, dry_run=False))
            logger.info(
                "Scheduled inspection library backfill completed: checked=%d filled=%d no_source=%d",
                backfill_result.get("checked", 0),
                backfill_result.get("filled", 0),
                backfill_result.get("no_source", 0),
            )
    except Exception as e:
        logger.exception("Scheduled inspection library job failed: %s", e)
