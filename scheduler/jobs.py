"""APScheduler job registration for inspection workflow.

Jobs:
- Sync job: PTS → local DB → DingTalk AITable (daily at 16:00)
- Dispatch monitor: 客户巡检派单 AITable poll (workdays 10/12/14/16)
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

    # Sync job: PTS → DingTalk
    sync_cron = settings.sync_cron.strip()
    if sync_cron:
        scheduler.add_job(
            _run_sync_job,
            trigger=CronTrigger.from_crontab(sync_cron),
            id="sync:pts-to-dingtalk",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered_ids.append("sync:pts-to-dingtalk")
        logger.info("Registered sync job with cron: %s", sync_cron)

    # Monitor job: DingTalk AITable poll (客户巡检派单) — workdays 10/12/14/16
    if settings.dt_dispatch_base_id and settings.dt_dispatch_table_id:
        scheduler.add_job(
            _run_dispatch_monitor_job,
            trigger=CronTrigger(hour="10,12,14,16", minute="0", day_of_week="mon-fri"),
            id="monitor:dispatch-aitable-poll",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        registered_ids.append("monitor:dispatch-aitable-poll")
        logger.info("Registered dispatch monitor job: cron workdays 10/12/14/16")

    # Email probe job: refresh email-pending cache for frontend display
    email_probe_cron = settings.email_probe_cron.strip()
    if email_probe_cron:
        scheduler.add_job(
            _run_email_probe_job,
            trigger=CronTrigger.from_crontab(email_probe_cron),
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
            trigger=CronTrigger.from_crontab(closure_check_cron),
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
                trigger=CronTrigger.from_crontab(pre_analysis_cron),
                id="monitor:email-pre-analysis",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            registered_ids.append("monitor:email-pre-analysis")
            logger.info("Registered email pre-analysis job with cron: %s", pre_analysis_cron)

    return registered_ids


def _run_sync_job() -> None:
    """Scheduled sync job runner.

    Full pipeline: PTS pull → local DB → push AITable → adjust planned completion.
    """
    from services.dingtalk_notifier import notify_sync_job
    from models.work_order import WorkOrder

    error = None
    result = None

    try:
        from services.sync_service import run_sync, adjust_planned_completion_to_month_end

        with SessionLocal() as db:
            # Step 1: Pull PTS work orders and push to AITable
            sync_log = asyncio.run(run_sync(db, trigger_source="scheduler", push_to_aitable=True, only_new_for_month=True))
            logger.info(
                "Scheduled sync completed: status=%s fetched=%d created=%d updated=%d",
                sync_log.status,
                sync_log.fetched_count,
                sync_log.created_count,
                sync_log.updated_count,
            )

            # Step 2: Auto-adjust planned completion to month end for current month
            adjust_result = adjust_planned_completion_to_month_end(db, month=sync_log.sync_month)
            logger.info(
                "Auto-adjust planned_completion: adjusted=%d skipped=%d PTS=%d/%d AITable=%d/%d",
                adjust_result.get("adjusted", 0),
                adjust_result.get("skipped", 0),
                adjust_result.get("pts_updated", 0),
                adjust_result.get("pts_failed", 0),
                adjust_result.get("aitable_updated", 0),
                adjust_result.get("aitable_failed", 0),
            )

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
            # Build result dict with new orders and adjust result
            result = sync_log.__dict__.copy()
            result["new_orders"] = new_orders
            result["adjust_result"] = adjust_result
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled sync job failed")

    # Send DingTalk notification
    asyncio.run(notify_sync_job(result if result else {}, error))


def _run_dispatch_monitor_job() -> None:
    """Scheduled dispatch monitor job runner (客户巡检派单).

    Only runs on workdays (respects Chinese holidays + 调休).
    """
    from services.dingtalk_notifier import _is_workday_today

    if not _is_workday_today():
        logger.info("Dispatch monitor skipped: today is a non-workday")
        return

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
                    result.get("closed", 0),
                    result.get("failed", 0),
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
            result = asyncio.run(run_email_pre_analysis(db, auto_send=True))
            logger.info(
                "Scheduled email pre-analysis completed: scanned=%d new=%d success=%d failed=%d skipped=%d sent=%d send_failed=%d",
                result.get("scanned", 0),
                result.get("new", 0),
                result.get("success", 0),
                result.get("failed", 0),
                result.get("skipped", 0),
                result.get("sent", 0),
                result.get("send_failed", 0),
            )
    except Exception as e:
        error = str(e)
        logger.exception("Scheduled email pre-analysis job failed")

    # Send DingTalk notification
    asyncio.run(notify_email_pre_analysis(result, error))