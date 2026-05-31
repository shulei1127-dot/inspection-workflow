"""DingTalk robot notification service."""

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


def _generate_signature(secret: str, timestamp: int) -> str:
    """Generate DingTalk signature with secret.

    Args:
        secret: DingTalk robot secret (SEC...)
        timestamp: Unix timestamp in milliseconds

    Returns:
        URL-encoded signature string
    """
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

    Args:
        title: Message title (bold in text)
        content: Message content
        webhook_url: Optional custom webhook URL (defaults to config)

    Returns:
        True if sent successfully, False otherwise
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


async def notify_sync_job(result: dict, error: str | None = None) -> bool:
    """Send notification after sync job completes.

    Args:
        result: Result dict from run_sync {status, fetched_count, created_count, updated_count, new_orders}
        error: Error message if failed

    Returns:
        True if sent successfully
    """
    if error:
        title = "❌ PTS数据同步任务失败"
        content = f"错误信息：{error}"
    else:
        status = result.get("status", "unknown")
        fetched = result.get("fetched_count", 0)
        created = result.get("created_count", 0)
        updated = result.get("updated_count", 0)

        title = "✅ PTS数据同步任务完成"
        content = f"- 拉取记录：{fetched} 条\n- 新建记录：{created} 条\n- 更新记录：{updated} 条\n\n状态：{status}"

        # Add new order links if available
        new_orders = result.get("new_orders", [])
        if new_orders and created > 0:
            content += "\n\n新建工单："
            for order in new_orders[:5]:  # Show max 5 orders
                pts_order_id = order.get("pts_order_id", "")
                customer_name = order.get("customer_name", "")
                content += f"\n- [{customer_name}](https://pts.chaitin.net/project/order/{pts_order_id})"
            if len(new_orders) > 5:
                content += f"\n- ... 还有 {len(new_orders) - 5} 条"

    return await send_dingtalk_notification(title, content)


async def notify_dispatch_monitor(result: dict, error: str | None = None) -> bool:
    """Send notification after dispatch monitor poll completes.

    Args:
        result: Result dict from run_dispatch_monitor_poll {status, dispatch_triggered, dispatch_failed}
        error: Error message if failed

    Returns:
        True if sent successfully
    """
    if error:
        title = "❌ 派单轮询任务失败"
        content = f"错误信息：{error}"
    else:
        status = result.get("status", "unknown")
        dispatched = result.get("dispatch_triggered", 0)
        failed = result.get("dispatch_failed", 0)

        title = "✅ 派单轮询任务完成"
        content = f"""- 触发派单：{dispatched} 个
- 派单失败：{failed} 个

状态：{status}"""
    return await send_dingtalk_notification(title, content)


async def notify_dispatch_success(
    supplier: str,
    customer_name: str,
    demand_id: str,
    order_id: str,
    pts_url: str,
) -> bool:
    """Send notification after a single dispatch success.

    Args:
        supplier: Supplier name
        customer_name: Customer name
        demand_id: Yunji demand ID
        order_id: Yunji order ID
        pts_url: PTS work order URL

    Returns:
        True if sent successfully
    """
    title = "🚀 派单成功"
    content = f"""- 客户：{customer_name}
- 供应商：{supplier}
- 需求编号：{demand_id}
- 订单编号：{order_id}
- 工单链接：{pts_url}"""
    return await send_dingtalk_notification(title, content)


async def notify_dispatch_failed(
    supplier: str,
    customer_name: str,
    error: str,
) -> bool:
    """Send notification after a single dispatch failure.

    Args:
        supplier: Supplier name
        customer_name: Customer name
        error: Error message

    Returns:
        True if sent successfully
    """
    title = "❌ 派单失败"
    content = f"""- 客户：{customer_name}
- 供应商：{supplier}
- 错误：{error}"""
    return await send_dingtalk_notification(title, content)


async def notify_email_probe(result: dict, error: str | None = None) -> bool:
    """Send notification after email probe completes.

    Args:
        result: Result dict from get_email_pending {total, pending}
        error: Error message if failed

    Returns:
        True if sent successfully
    """
    if error:
        title = "❌ 邮件探测任务失败"
        content = f"错误信息：{error}"
    else:
        total = result.get("total", 0)

        title = "✅ 邮件探测任务完成"
        content = f"- 待发邮件：{total} 条"
    return await send_dingtalk_notification(title, content)


async def notify_closure_check(result: dict, error: str | None = None) -> bool:
    """Send notification after closure check completes.

    Args:
        result: Result dict from run_closure_check {status, checked, closed, failed}
        error: Error message if failed

    Returns:
        True if sent successfully
    """
    if error:
        title = "❌ 闭环检查任务失败"
        content = f"错误信息：{error}"
    else:
        status = result.get("status", "unknown")
        checked = result.get("checked", 0)
        closed = result.get("closed", 0)
        failed = result.get("failed", 0)

        title = "✅ 闭环检查任务完成"
        content = f"""- 检查工单：{checked} 条
- 自动闭环：{closed} 条
- 闭环失败：{failed} 条

状态：{status}"""
    return await send_dingtalk_notification(title, content)


async def notify_yunji_keepalive(result: dict, error: str | None = None) -> bool:
    """Send notification after yunji keepalive completes.

    Args:
        result: Result dict from keepalive_cookie {status}
        error: Error message if failed

    Returns:
        True if sent successfully
    """
    if error:
        title = "❌ 云集保活任务失败"
        content = f"错误信息：{error}"
    else:
        status = result.get("status", "unknown")

        if status == "expired":
            title = "⚠️ 云集Session已过期"
            content = f"""状态：{status}

请尽快更新 YUNJI_SESSION_COOKIE 环境变量！"""
        else:
            title = "✅ 云集保活任务完成"
            content = f"状态：{status}"
    return await send_dingtalk_notification(title, content)


async def notify_email_pre_analysis(result: dict, error: str | None = None) -> bool:
    """Send notification after email pre-analysis job completes.

    Args:
        result: Result dict from run_email_pre_analysis {scanned, new, success, failed, skipped}
        error: Error message if failed

    Returns:
        True if sent successfully
    """
    if error:
        title = "❌ 邮件预分析任务失败"
        content = f"错误信息：{error}"
        return await send_dingtalk_notification(title, content)

    scanned = result.get("scanned", 0)
    new = result.get("new", 0)
    success = result.get("success", 0)
    failed = result.get("failed", 0)
    skipped = result.get("skipped", 0)

    title = "✅ 邮件预分析任务完成"

    if scanned == 0:
        content = "无待处理记录。"
    elif failed == 0:
        content = f"""- 扫描记录：{scanned} 条
- 新分析：{new} 条
- 成功：{success} 条
- 跳过：{skipped} 条

✅ 全部成功"""
    else:
        content = f"""- 扫描记录：{scanned} 条
- 新分析：{new} 条
- 成功：{success} 条
- 失败：{failed} 条
- 跳过：{skipped} 条

⚠️ 存在失败，请查看日志"""

    return await send_dingtalk_notification(title, content)