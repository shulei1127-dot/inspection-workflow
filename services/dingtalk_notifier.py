"""DingTalk robot notification service.

Notification policy: "无事不报，有事必达"
- Silent on routine "0 results" / "all normal" messages
- Merge daily summaries into a single daily digest (sent ~17:30)
- Always send immediately: errors, dispatch failures, cookie expiry, audit rejections
"""

import base64
import datetime
import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

# ── Daily digest cache (in-process, reset each day) ─────────────
# Keyed by category; each entry is a dict with whatever the digest needs.
# Populated by individual notify_* functions, consumed by notify_daily_digest().

_digest_date: str = ""  # YYYY-MM-DD, tracks cache freshness
_digest_data: dict[str, dict] = {}


def _ensure_digest_cache() -> None:
    """Reset the digest cache if the date has changed."""
    global _digest_date, _digest_data
    today = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%d")
    if _digest_date != today:
        _digest_date = today
        _digest_data = {}


def store_digest_entry(category: str, data: dict) -> None:
    """Store data for the daily digest under a given category."""
    _ensure_digest_cache()
    existing = _digest_data.get(category, {})
    existing.update(data)
    _digest_data[category] = existing


def get_digest_data() -> dict[str, dict]:
    """Return the current day's digest data."""
    _ensure_digest_cache()
    return dict(_digest_data)


# ── Workday check ────────────────────────────────────────────────


def _is_workday_today() -> bool:
    """Check if today (Asia/Shanghai) is a workday.

    Uses chinesecalendar for legal holidays + 调休 awareness.
    Falls back to weekday check (Mon-Fri) if the library doesn't
    support the current year.
    """
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=8))
    ).date()
    try:
        import chinese_calendar
        return chinese_calendar.is_workday(today)
    except (ImportError, NotImplementedError):
        # Library missing or year not covered — fall back to Mon-Fri
        return today.weekday() < 5


# ── Signature & send ─────────────────────────────────────────────


def _generate_signature(secret: str, timestamp: int) -> str:
    """Generate DingTalk signature with secret."""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    signature = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return signature


async def send_dingtalk_notification(
    title: str,
    content: str,
    webhook_url: str | None = None,
) -> bool:
    """Send text message to DingTalk robot.

    Returns True if sent successfully, False otherwise.
    """
    settings = get_settings()

    # Skip notifications on non-workdays (legal holidays / weekends)
    if settings.dingtalk_holiday_mute and not _is_workday_today():
        logger.info("DingTalk notification suppressed: today is a non-workday — %s", title)
        return False

    url = webhook_url or settings.dingtalk_webhook_url

    if not url:
        logger.warning("DingTalk webhook URL not configured, skipping notification")
        return False

    # Add signature if secret is configured
    if settings.dingtalk_secret:
        timestamp = int(time.time() * 1000)
        sign = _generate_signature(settings.dingtalk_secret, timestamp)
        url = f"{url}&timestamp={timestamp}&sign={sign}"

    message = f"### {title}\n\n{content}"

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": message,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode") == 0:
                logger.info("DingTalk notification sent: %s", title)
                return True
            else:
                logger.error("DingTalk notification failed: %s", result.get("errmsg"))
                return False
    except Exception as e:
        logger.error("DingTalk notification error: %s", e)
        return False


async def notify_closure_manual(
    *,
    pts_order_id: str,
    customer_name: str,
    pts_url: str,
    claim_by: str,
    current_stage: str,
    creator_name: str,
    assignee_id: str,
    reason: str,
) -> bool:
    """Send a sanitized immediate alert for a persisted V2 manual state."""
    return await send_dingtalk_notification(
        "⚠️ 巡检工单需要人工推进阶段",
        (
            f"工单 [{pts_order_id}]({pts_url})（客户：{customer_name}）\n\n"
            f"- 当前负责人：{claim_by}\n- 当前阶段：{current_stage}\n"
            f"- 创建人：{creator_name}\n- 需要指定负责人：{assignee_id}\n"
            f"- 目标阶段：审核工单\n- 原因：{reason[:500]}"
        ),
    )


# ── Individual notification functions ─────────────────────────────
# Policy: errors always send immediately; routine results go to digest cache.


async def notify_sync_job(result: dict, error: str | None = None) -> bool:
    """Sync job notification. Error → send immediately; success → digest cache."""
    if error:
        return await send_dingtalk_notification(
            "❌ PTS数据同步任务失败",
            f"错误信息：{error}",
        )

    fetched = result.get("fetched_count", 0)
    created = result.get("created_count", 0)
    updated = result.get("updated_count", 0)

    # Build digest entry
    new_orders = result.get("new_orders", [])
    new_names = [o.get("customer_name", "") for o in new_orders[:5]] if created > 0 else []
    store_digest_entry("sync", {
        "fetched": fetched,
        "created": created,
        "updated": updated,
        "new_names": new_names,
    })
    return False  # Suppressed — will appear in daily digest


async def notify_dispatch_monitor(result: dict, error: str | None = None) -> bool:
    """Dispatch monitor notification. Error → send; 0 results → silent; else → digest cache."""
    if error:
        return await send_dingtalk_notification(
            "❌ 派单轮询任务失败",
            f"错误信息：{error}",
        )

    dispatched = result.get("dispatch_triggered", 0)
    failed = result.get("dispatch_failed", 0)

    # Silent if nothing happened
    if dispatched == 0 and failed == 0:
        return False

    store_digest_entry("dispatch_summary", {
        "dispatched": dispatched,
        "failed": failed,
    })
    return False  # Suppressed — will appear in daily digest


async def notify_dispatch_success(
    supplier: str,
    customer_name: str,
    demand_id: str,
    order_id: str,
    pts_url: str,
) -> bool:
    """Per-dispatch success. Store in digest cache, do NOT send immediately."""
    items = get_digest_data().get("dispatch_successes", {}).get("items", [])
    items.append({
        "customer": customer_name,
        "supplier": supplier,
        "pts_url": pts_url,
    })
    store_digest_entry("dispatch_successes", {"items": items})
    return False  # Suppressed — will appear in daily digest


async def notify_dispatch_failed(
    supplier: str,
    customer_name: str,
    error: str,
) -> bool:
    """Per-dispatch failure. Send immediately AND store in digest cache."""
    # Store for digest
    items = get_digest_data().get("dispatch_failures", {}).get("items", [])
    items.append({"customer": customer_name, "supplier": supplier, "error": error})
    store_digest_entry("dispatch_failures", {"items": items})

    # Send immediately — important alert
    return await send_dingtalk_notification(
        "❌ 派单失败",
        f"- 客户：{customer_name}\n- 供应商：{supplier}\n- 错误：{error}",
    )


async def notify_email_probe(result: dict, error: str | None = None) -> bool:
    """Email probe notification. Error → send; 0 pending → silent; else → digest cache."""
    if error:
        return await send_dingtalk_notification(
            "❌ 邮件探测任务失败",
            f"错误信息：{error}",
        )

    total = result.get("total", 0)

    # Silent if nothing pending
    if total == 0:
        return False

    store_digest_entry("email_probe", {"pending": total})
    return False  # Suppressed — will appear in daily digest


async def notify_closure_check(result: dict, error: str | None = None) -> bool:
    """Closure check notification. Error → send; else → digest cache."""
    if error:
        return await send_dingtalk_notification(
            "❌ 闭环检查任务失败",
            f"错误信息：{error}",
        )

    checked = result.get("checked", 0)
    closed = result.get("closed", result.get("completed", 0))
    failed = result.get("failed", result.get("retryable_failed", 0))
    manual = result.get("manual", 0)
    synced = result.get("synced", 0)
    recovered = result.get("recovered", 0)

    store_digest_entry("closure", {
        "checked": checked,
        "closed": closed,
        "failed": failed,
        "manual": manual,
        "synced": synced,
        "recovered": recovered,
    })
    return False  # Suppressed — will appear in daily digest


async def notify_yunji_keepalive(result: dict, error: str | None = None) -> bool:
    """Yunji keepalive notification. Only send when cookie expired or error."""
    if error:
        return await send_dingtalk_notification(
            "❌ 云集保活任务失败",
            f"错误信息：{error}",
        )

    status = result.get("status", "unknown")

    if status == "expired":
        # Important alert — always send
        return await send_dingtalk_notification(
            "⚠️ 云集Session已过期",
            f"状态：{status}\n\n请尽快更新 YUNJI_SESSION_COOKIE 环境变量！",
        )

    # Normal keepalive — silent
    return False


async def notify_email_pre_analysis(result: dict, error: str | None = None) -> bool:
    """Email pre-analysis notification. Error → send; 0 scanned → silent; else → digest cache."""
    if error:
        return await send_dingtalk_notification(
            "❌ 邮件预分析任务失败",
            f"错误信息：{error}",
        )

    scanned = result.get("scanned", 0)
    new = result.get("new", 0)
    success = result.get("success", 0)
    failed = result.get("failed", 0)
    skipped = result.get("skipped", 0)

    # Silent if nothing to analyze
    if scanned == 0:
        return False

    store_digest_entry("email_pre_analysis", {
        "scanned": scanned,
        "new": new,
        "success": success,
        "failed": failed,
        "skipped": skipped,
    })
    return False  # Suppressed — will appear in daily digest


async def notify_review_pipeline(result: dict, error: str | None = None) -> bool:
    """Review pipeline notification. Error → send; else → digest cache."""
    if error:
        return await send_dingtalk_notification(
            "❌ 交付转售后审核失败",
            f"错误信息：{error}",
        )

    total = result.get("total", 0)
    passed = result.get("passed", 0)
    rejected = result.get("rejected", 0)
    manual = result.get("manual", 0)
    errors = result.get("errors", 0)

    store_digest_entry("review", {
        "total": total,
        "passed": passed,
        "rejected": rejected,
        "manual": manual,
        "errors": errors,
    })
    return False  # Suppressed — will appear in daily digest


async def notify_daily_change_summary(_result: dict, error: str | None = None) -> bool:
    """Daily change summary. Only notify on error (success report already pushed separately)."""
    if error:
        return await send_dingtalk_notification(
            "❌ 每日变更摘要任务失败",
            f"错误信息：{error}",
        )
    return False


async def notify_visit_pipeline(result: dict, error: str | None = None) -> bool:
    """Visit pipeline notification. Error → send; else → digest cache."""
    if error:
        return await send_dingtalk_notification(
            "❌ 交付转售后回访失败",
            f"错误信息：{error}",
        )

    total = result.get("total", 0)
    completed = result.get("completed", 0)
    failed = result.get("failed", 0)
    skipped = result.get("skipped", 0)

    store_digest_entry("visit", {
        "total": total,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
    })
    return False  # Suppressed — will appear in daily digest


# ── Daily digest ─────────────────────────────────────────────────


async def notify_daily_digest() -> bool:
    """Send a merged daily digest of all routine notifications.

    Called by the daily_digest scheduler job (~17:30 on workdays).
    If no data was collected, sends nothing.
    """
    data = get_digest_data()

    # Nothing collected today — skip
    if not data:
        logger.info("Daily digest: no data collected, skipping")
        return False

    today = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%m-%d")

    sections: list[str] = []
    alerts: list[str] = []

    # ── Sync ──
    sync = data.get("sync")
    if sync:
        line = f"🔄 同步：拉取 {sync['fetched']} 条，新建 {sync['created']} 条"
        names = sync.get("new_names", [])
        if names:
            line += f"（{', '.join(names[:3])}{'等' if len(names) > 3 else ''}）"
        sections.append(line)

    # ── Dispatch ──
    dispatch_successes = data.get("dispatch_successes", {}).get("items", [])
    dispatch_failures = data.get("dispatch_failures", {}).get("items", [])
    dispatch_summary = data.get("dispatch_summary", {})

    if dispatch_successes or dispatch_summary:
        count = len(dispatch_successes) or dispatch_summary.get("dispatched", 0)
        line = f"📦 派单：成功 {count} 个"
        if dispatch_successes:
            names = [f"{d['customer']}→{d['supplier']}" for d in dispatch_successes[:3]]
            line += f"（{', '.join(names)}{'等' if len(dispatch_successes) > 3 else ''}）"
        sections.append(line)

    if dispatch_failures:
        for d in dispatch_failures:
            alerts.append(f"派单失败：{d['customer']}→{d['supplier']}（{d['error']}）")

    # ── Email ──
    email = data.get("email_pre_analysis")
    if email:
        line = f"📧 邮件：预分析 {email['scanned']} 条，成功 {email['success']}"
        if email["failed"] > 0:
            line += f"，失败 {email['failed']}"
            alerts.append(f"邮件预分析失败 {email['failed']} 条")
        sections.append(line)

    email_pending = data.get("email_probe", {}).get("pending", 0)
    if email_pending > 0:
        sections.append(f"📧 待发邮件：{email_pending} 条")

    # ── Closure ──
    closure = data.get("closure")
    if closure and closure.get("checked", 0) > 0:
        line = f"🔒 闭环：检查 {closure['checked']} 条，自动闭环 {closure['closed']} 条"
        if closure.get("failed", 0) > 0:
            line += f"，失败 {closure['failed']} 条"
            alerts.append(f"闭环失败 {closure['failed']} 条")
        if closure.get("manual", 0) > 0:
            line += f"，需人工 {closure['manual']} 条"
            alerts.append(f"闭环需人工处理 {closure['manual']} 条")
        if closure.get("recovered", 0) > 0:
            line += f"，恢复 {closure['recovered']} 条"
        sections.append(line)

    # ── Review ──
    review = data.get("review")
    if review and review.get("total", 0) > 0:
        line = f"✅ 审核：{review['total']} 个项目，通过 {review['passed']}，拒绝 {review['rejected']}，转人工 {review['manual']}"
        if review.get("errors", 0) > 0:
            line += f"，失败 {review['errors']}"
            alerts.append(f"审核失败 {review['errors']} 条")
        sections.append(line)

    # ── Visit ──
    visit = data.get("visit")
    if visit and visit.get("total", 0) > 0:
        line = f"📞 回访：{visit['total']} 个项目，已完成 {visit['completed']}"
        if visit.get("failed", 0) > 0:
            line += f"，失败 {visit['failed']}"
            alerts.append(f"回访失败 {visit['failed']} 条")
        sections.append(line)

    # Nothing actionable — skip
    if not sections and not alerts:
        logger.info("Daily digest: all sections empty, skipping")
        return False

    # Build message — each section separated by double newline for DingTalk markdown
    content = "\n\n".join(sections)

    if alerts:
        content += "\n\n---\n\n⚠️ **需关注：**\n\n"
        for a in alerts:
            content += f"- {a}\n"

    title = f"📋 巡检工作日报 ({today})"
    return await send_dingtalk_notification(title, content)
