"""Change log service — daily change knowledge-base.

Collects git commits, stores change log entries, builds daily summaries,
and pushes them to DingTalk.
"""

import logging
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from core.config import get_settings
from models.change_log import ChangeLog
from models.sync_log import SyncLog
from models.trigger_log import TriggerLog
from models.work_order import WorkOrder

logger = logging.getLogger(__name__)

_SHANGHAI_TZ = timezone(timedelta(hours=8))
_KNOWLEDGE_BASE_URL = "http://10.20.20.208:8100/#/change-logs"

# Commit message keyword → category mapping
_CATEGORY_RULES = [
    (["fix", "bug", "修复", "修补"], "bug_fix"),
    (["feat", "优化", "新增", "支持", "改进", "增强"], "feature_opt"),
    (["refactor", "重构"], "important_change"),
    (["perf", "性能"], "feature_opt"),
    (["docs", "文档", "readme"], "other"),
    (["ci", "deploy", "部署"], "other"),
]


def _categorize_commit(message: str) -> str:
    """Heuristic categorization based on commit message prefix/keywords."""
    lower = message.lower()
    for keywords, category in _CATEGORY_RULES:
        if any(keyword in lower for keyword in keywords):
            return category
    return "other"


def _today_shanghai() -> date:
    """Return today's date in Asia/Shanghai timezone."""
    return datetime.now(_SHANGHAI_TZ).date()


def _day_range_shanghai(target_date: date) -> tuple[datetime, datetime]:
    """Return start/end datetimes for a Shanghai natural day."""
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=_SHANGHAI_TZ)
    return day_start, day_start + timedelta(days=1)


def upsert_git_changes(db: Session, target_date: date, commits: list[dict]) -> dict:
    """Insert git commits as change logs, skipping existing hashes."""
    if not commits:
        return {"collected": 0, "skipped": 0, "errors": []}

    hashes = [item["git_hash"] for item in commits if item.get("git_hash")]
    existing_hashes = set()
    if hashes:
        existing_hashes = {
            value
            for (value,) in db.query(ChangeLog.git_hash)
            .filter(ChangeLog.git_hash.in_(hashes))
            .all()
            if value
        }

    collected = 0
    skipped = 0
    for item in commits:
        git_hash = item.get("git_hash")
        if git_hash and git_hash in existing_hashes:
            skipped += 1
            continue

        db.add(
            ChangeLog(
                change_date=target_date,
                source_type="git_commit",
                category=_categorize_commit(item.get("subject", "")),
                title=item.get("subject") or "(no subject)",
                detail=None,
                git_hash=git_hash,
                author=item.get("author", ""),
                extra_data={"author_date": item.get("author_date")},
            )
        )
        collected += 1

    db.commit()
    return {"collected": collected, "skipped": skipped, "errors": []}


def collect_git_changes(
    db: Session,
    target_date: date | None = None,
    repo_path: str | None = None,
) -> dict:
    """Collect git commits for a given date and store them as change_log entries."""
    if target_date is None:
        target_date = _today_shanghai()
    if repo_path is None:
        repo_path = get_settings().daily_change_summary_repo_path

    repo_dir = Path(repo_path).resolve()
    if not repo_dir.is_dir():
        return {"collected": 0, "skipped": 0, "errors": [f"repo path not found: {repo_dir}"]}

    since, until = _day_range_shanghai(target_date)

    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "--all",
                f"--since={since.strftime('%Y-%m-%d %H:%M:%S %z')}",
                f"--until={until.strftime('%Y-%m-%d %H:%M:%S %z')}",
                "--format=%H|||%s|||%an|||%aI",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_dir),
        )
    except Exception as exc:
        logger.exception("git log execution failed")
        return {"collected": 0, "skipped": 0, "errors": [str(exc)]}

    if result.returncode != 0:
        stderr = result.stderr.strip() or "git log failed"
        logger.warning("git log failed: %s", stderr)
        return {"collected": 0, "skipped": 0, "errors": [stderr]}

    commits = []
    for line in [item.strip() for item in result.stdout.splitlines() if item.strip()]:
        parts = line.split("|||", 3)
        if len(parts) != 4:
            continue
        git_hash, subject, author, author_date = parts
        commits.append(
            {
                "git_hash": git_hash,
                "subject": subject,
                "author": author,
                "author_date": author_date,
            }
        )

    return upsert_git_changes(db, target_date, commits)


def add_manual_entry(
    db: Session,
    change_date: date,
    category: str,
    title: str,
    detail: str | None = None,
    author: str = "",
) -> ChangeLog:
    """Add a manual change log entry."""
    entry = ChangeLog(
        change_date=change_date,
        source_type="manual",
        category=category,
        title=title,
        detail=detail,
        git_hash=None,
        author=author,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def build_daily_summary(db: Session, target_date: date) -> dict:
    """Build the daily summary content for DingTalk notification."""
    entries = (
        db.query(ChangeLog)
        .filter(ChangeLog.change_date == target_date)
        .order_by(ChangeLog.created_at.asc())
        .all()
    )

    bug_fixes = [entry for entry in entries if entry.category == "bug_fix"]
    features = [entry for entry in entries if entry.category == "feature_opt"]
    important = [entry for entry in entries if entry.category == "important_change"]
    others = [entry for entry in entries if entry.category == "other"]

    day_start, day_end = _day_range_shanghai(target_date)

    sync_logs = (
        db.query(SyncLog)
        .filter(SyncLog.started_at >= day_start, SyncLog.started_at < day_end)
        .all()
    )
    trigger_logs = (
        db.query(TriggerLog)
        .filter(TriggerLog.created_at >= day_start, TriggerLog.created_at < day_end)
        .all()
    )

    sync_count = len(sync_logs)
    sync_fetched = sum(log.fetched_count for log in sync_logs)
    sync_created = sum(log.created_count for log in sync_logs)
    sync_updated = sum(log.updated_count for log in sync_logs)

    dispatch_success = sum(
        1 for log in trigger_logs if log.trigger_type == "yunji_dispatch" and log.status == "success"
    )
    dispatch_failed = sum(
        1 for log in trigger_logs if log.trigger_type == "yunji_dispatch" and log.status == "failed"
    )
    email_success = sum(
        1 for log in trigger_logs if log.trigger_type == "inspection_email" and log.status == "success"
    )
    email_failed = sum(
        1 for log in trigger_logs if log.trigger_type == "inspection_email" and log.status == "failed"
    )
    closure_success = sum(
        1 for log in trigger_logs if "closure" in (log.trigger_type or "") and log.status == "success"
    )
    closure_failed = sum(
        1 for log in trigger_logs if "closure" in (log.trigger_type or "") and log.status == "failed"
    )

    dispatch_status_success = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.updated_at >= day_start,
            WorkOrder.updated_at < day_end,
            WorkOrder.dispatch_status == "已派单",
        )
        .count()
    )
    dispatch_status_failed = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.updated_at >= day_start,
            WorkOrder.updated_at < day_end,
            WorkOrder.dispatch_status == "派单失败",
        )
        .count()
    )
    email_status_success = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.updated_at >= day_start,
            WorkOrder.updated_at < day_end,
            WorkOrder.email_trigger_status == "已发送",
        )
        .count()
    )
    email_status_failed = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.updated_at >= day_start,
            WorkOrder.updated_at < day_end,
            WorkOrder.email_trigger_status == "发送失败",
        )
        .count()
    )
    closure_status_success = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.updated_at >= day_start,
            WorkOrder.updated_at < day_end,
            WorkOrder.closure_status == "已闭环",
        )
        .count()
    )
    closure_status_failed = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.updated_at >= day_start,
            WorkOrder.updated_at < day_end,
            WorkOrder.closure_status == "闭环失败",
        )
        .count()
    )

    pushed_count = sum(1 for entry in entries if entry.pushed_to_dingtalk)

    return {
        "date": target_date.isoformat(),
        "total_entries": len(entries),
        "bug_fixes": [
            {"title": entry.title, "git_hash": entry.git_hash, "author": entry.author}
            for entry in bug_fixes
        ],
        "features": [
            {"title": entry.title, "git_hash": entry.git_hash, "author": entry.author}
            for entry in features
        ],
        "important": [
            {"title": entry.title, "detail": entry.detail, "git_hash": entry.git_hash, "author": entry.author}
            for entry in important
        ],
        "others": [
            {"title": entry.title, "detail": entry.detail, "git_hash": entry.git_hash, "author": entry.author}
            for entry in others
        ],
        "operations": {
            "sync_count": sync_count,
            "sync_fetched": sync_fetched,
            "sync_created": sync_created,
            "sync_updated": sync_updated,
            "dispatch_success": dispatch_success,
            "dispatch_failed": dispatch_failed,
            "email_success": email_success,
            "email_failed": email_failed,
            "closure_success": closure_success,
            "closure_failed": closure_failed,
            "work_order_dispatch_success": dispatch_status_success,
            "work_order_dispatch_failed": dispatch_status_failed,
            "work_order_email_success": email_status_success,
            "work_order_email_failed": email_status_failed,
            "work_order_closure_success": closure_status_success,
            "work_order_closure_failed": closure_status_failed,
        },
        "pushed_count": pushed_count,
        "knowledge_base_url": _KNOWLEDGE_BASE_URL,
    }


async def push_daily_summary(db: Session, target_date: date, force: bool = False) -> dict:
    """Push daily summary to DingTalk and mark entries as pushed."""
    entries = db.query(ChangeLog).filter(ChangeLog.change_date == target_date).all()
    if not entries:
        return {"pushed": False, "entries": 0, "message": f"{target_date} 无变更记录，跳过推送"}

    already_pushed = sum(1 for entry in entries if entry.pushed_to_dingtalk)
    if already_pushed == len(entries) and not force:
        return {
            "pushed": False,
            "entries": len(entries),
            "message": f"{target_date} 已推送过，跳过（force=true 可重推）",
        }

    summary = build_daily_summary(db, target_date)
    ops = summary["operations"]

    settings = get_settings()
    webhook_url = settings.daily_change_summary_webhook_url or None

    content_parts: list[str] = []

    if summary["bug_fixes"]:
        content_parts.append("**🐛 Bug 修复**")
        for item in summary["bug_fixes"]:
            hash_short = item["git_hash"][:7] if item.get("git_hash") else ""
            prefix = f"`{hash_short}` " if hash_short else ""
            content_parts.append(f"- {prefix}{item['title']}")
        content_parts.append("")

    if summary["features"]:
        content_parts.append("**✨ 功能优化 / 新增**")
        for item in summary["features"]:
            hash_short = item["git_hash"][:7] if item.get("git_hash") else ""
            prefix = f"`{hash_short}` " if hash_short else ""
            content_parts.append(f"- {prefix}{item['title']}")
        content_parts.append("")

    if summary["important"]:
        content_parts.append("**📌 重要变更**")
        for item in summary["important"]:
            hash_short = item["git_hash"][:7] if item.get("git_hash") else ""
            prefix = f"`{hash_short}` " if hash_short else ""
            content_parts.append(f"- {prefix}{item['title']}")
            if item.get("detail"):
                content_parts.append(f"  > {item['detail']}")
        content_parts.append("")

    if summary["others"]:
        content_parts.append("**📝 其他变更**")
        for item in summary["others"]:
            hash_short = item["git_hash"][:7] if item.get("git_hash") else ""
            prefix = f"`{hash_short}` " if hash_short else ""
            content_parts.append(f"- {prefix}{item['title']}")
            if item.get("detail"):
                content_parts.append(f"  > {item['detail']}")
        content_parts.append("")

    content_parts.append("**📊 当天运行摘要**")
    content_parts.append(
        f"- 同步：{ops['sync_count']} 次，拉取 {ops['sync_fetched']} 条，新建 {ops['sync_created']} 条，更新 {ops['sync_updated']} 条"
    )
    content_parts.append(
        f"- 触发日志：派单成功 {ops['dispatch_success']} / 失败 {ops['dispatch_failed']}，邮件成功 {ops['email_success']} / 失败 {ops['email_failed']}，闭环成功 {ops['closure_success']} / 失败 {ops['closure_failed']}"
    )
    content_parts.append(
        f"- 工单状态：已派单 {ops['work_order_dispatch_success']} / 派单失败 {ops['work_order_dispatch_failed']}，已发送 {ops['work_order_email_success']} / 发送失败 {ops['work_order_email_failed']}，已闭环 {ops['work_order_closure_success']} / 闭环失败 {ops['work_order_closure_failed']}"
    )
    content_parts.append("")
    content_parts.append(f"> 共 {summary['total_entries']} 项变更")
    content_parts.append(f"📚 [查看变更知识库]({summary['knowledge_base_url']})")

    from services.dingtalk_notifier import send_dingtalk_notification

    title = f"项目每日变更摘要（{target_date.isoformat()}）"
    success = await send_dingtalk_notification(title, "\n".join(content_parts), webhook_url=webhook_url)
    if not success:
        return {"pushed": False, "entries": len(entries), "message": "钉钉推送失败"}

    now = datetime.now(_SHANGHAI_TZ)
    for entry in entries:
        entry.pushed_to_dingtalk = True
        entry.pushed_at = now
    db.commit()
    return {"pushed": True, "entries": len(entries), "message": f"推送成功，共 {len(entries)} 项变更"}


async def run_daily_change_summary(db: Session, target_date: date | None = None) -> dict:
    """Full pipeline: collect git changes → build summary → push summary."""
    if target_date is None:
        target_date = _today_shanghai()

    collect_result = collect_git_changes(db, target_date)
    logger.info(
        "Collected git changes for %s: %d new, %d skipped",
        target_date,
        collect_result["collected"],
        collect_result["skipped"],
    )

    push_result = await push_daily_summary(db, target_date)

    summary = build_daily_summary(db, target_date)
    summary["collect_result"] = collect_result
    summary["push_result"] = push_result
    return summary
