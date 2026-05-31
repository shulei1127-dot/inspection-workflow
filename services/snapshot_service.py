"""Snapshot service: CRUD and change detection for AITable field snapshots.

Used by monitor_service to detect field-level changes between
polling cycles of DingTalk AITable records.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from models.aitable_snapshot import AITableSnapshot

logger = logging.getLogger(__name__)


async def get_snapshot(
    db: Session,
    source_table: str,
    record_id: str,
) -> AITableSnapshot | None:
    """Get a single snapshot by source_table and record_id."""
    return (
        db.query(AITableSnapshot)
        .filter(
            AITableSnapshot.source_table == source_table,
            AITableSnapshot.record_id == record_id,
        )
        .first()
    )


async def save_snapshot(
    db: Session,
    source_table: str,
    record_id: str,
    field_values: dict,
) -> AITableSnapshot:
    """Create or update a snapshot (upsert).

    If a snapshot already exists for (source_table, record_id),
    update its field_values. Otherwise create a new one.
    """
    existing = await get_snapshot(db, source_table, record_id)
    if existing:
        existing.field_values = field_values
        return existing

    snapshot = AITableSnapshot(
        id=uuid.uuid4(),
        source_table=source_table,
        record_id=record_id,
        field_values=field_values,
    )
    db.add(snapshot)
    return snapshot


def detect_changes(
    old_values: dict,
    new_values: dict,
    watch_fields: list[str],
) -> dict:
    """Compare old and new snapshot values, return changed fields.

    Args:
        old_values: Previous snapshot field_values.
        new_values: Current field values from AITable.
        watch_fields: Only detect changes for these field keys.

    Returns:
        Dict of changed fields: {"field_name": {"old": old_val, "new": new_val}, ...}
    """
    changes: dict = {}
    for field in watch_fields:
        old_val = old_values.get(field)
        new_val = new_values.get(field)
        # Normalize None vs empty string for comparison
        old_norm = old_val if old_val is not None else ""
        new_norm = new_val if new_val is not None else ""
        if str(old_norm) != str(new_norm):
            changes[field] = {"old": old_val, "new": new_val}
    return changes
