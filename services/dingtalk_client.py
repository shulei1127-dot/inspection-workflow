"""DingTalk AITable client using dws CLI.

Wraps the dws CLI for AITable record operations:
- query: dws aitable record query
- create: dws aitable record create
- update: dws aitable record update
- search base: dws aitable base search
- get table: dws aitable table get
"""

import asyncio
import json
import logging

from core.config import get_settings

logger = logging.getLogger(__name__)

DWS_TIMEOUT = 30  # seconds
MAX_RETRIES = 3


async def _run_dws(args: list[str], timeout: int = DWS_TIMEOUT) -> dict | list | None:
    """Run a dws CLI command and return parsed JSON output."""
    cmd = ["dws"] + args
    for attempt in range(MAX_RETRIES):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode != 0:
                logger.warning("dws command failed (attempt %d): %s", attempt + 1, stderr.decode()[:500])
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1)
                    continue
                return None
            return json.loads(stdout.decode())
        except asyncio.TimeoutError:
            logger.warning("dws command timed out (attempt %d): %s", attempt + 1, " ".join(cmd))
            proc.kill()
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(1)
                continue
        except json.JSONDecodeError as e:
            logger.error("dws output not valid JSON: %s", e)
            return None
        except Exception as e:
            logger.error("dws command error: %s", e)
            return None
    return None


def _base_table_args(
    base_id: str | None = None,
    table_id: str | None = None,
) -> list[str]:
    settings = get_settings()
    return [
        "--base-id", base_id or settings.dt_aitable_base_id,
        "--table-id", table_id or settings.dt_aitable_table_id,
    ]


async def search_bases(query: str = "巡检") -> list[dict]:
    """Search for AITable bases matching the query."""
    result = await _run_dws([
        "aitable", "base", "search",
        "--query", query,
        "-f", "json",
    ])
    if result is None:
        return []
    if isinstance(result, dict) and "data" in result:
        data = result["data"]
        bases = data.get("bases", [])
        return bases if isinstance(bases, list) else []
    if isinstance(result, list):
        return result
    return []


async def get_base(base_id: str) -> dict | None:
    """Get base info including table list."""
    return await _run_dws([
        "aitable", "base", "get",
        "--base-id", base_id,
        "-f", "json",
    ])


async def get_table(base_id: str, table_id: str) -> dict | None:
    """Get table schema with field IDs."""
    return await _run_dws([
        "aitable", "table", "get",
        "--base-id", base_id,
        "--table-id", table_id,
        "-f", "json",
    ])


async def query_records(
    limit: int = 100,
    base_id: str | None = None,
    table_id: str | None = None,
    fetch_all: bool = False,
) -> list[dict]:
    """Query records from the configured AITable.

    Args:
        limit: Max records per page (max 100 per AITable API).
        base_id: Override base ID.
        table_id: Override table ID.
        fetch_all: If True, paginate through all records automatically.
    """
    page_limit = min(limit, 100)  # AITable API limit max is 100
    all_records = []
    cursor = None

    while True:
        args = [
            "aitable", "record", "query",
            *_base_table_args(base_id=base_id, table_id=table_id),
            "--limit", str(page_limit),
            "-f", "json",
        ]
        if cursor:
            args.extend(["--cursor", cursor])

        logger.debug("query_records args: %s", args)
        result = await _run_dws(args)

        if result is None:
            logger.warning("query_records: dws returned None")
            break

        # dws returns {"data": {"records": [...], "nextCursor": ...}}
        if isinstance(result, dict):
            data = result.get("data", result)
            records = data.get("records", [])
            if isinstance(records, list):
                all_records.extend(records)
            next_cursor = data.get("nextCursor", "")
            if not fetch_all or not next_cursor or len(records) < page_limit:
                break
            cursor = next_cursor
        elif isinstance(result, list):
            all_records.extend(result)
            break
        else:
            break

    # Normalize: dws v1.0.33+ returns "cells" instead of "fields"
    for rec in all_records:
        if "cells" in rec and "fields" not in rec:
            rec["fields"] = rec.pop("cells")

    logger.debug("query_records: found %d records total", len(all_records))
    return all_records


async def create_records(
    records: list[dict],
    base_id: str | None = None,
    table_id: str | None = None,
) -> dict | None:
    """Create records in the configured AITable.

    Each record: {"cells": {"fieldId": "value", ...}}
    """
    result = await _run_dws([
        "aitable", "record", "create",
        *_base_table_args(base_id=base_id, table_id=table_id),
        "--records", json.dumps(records, ensure_ascii=False),
        "-y", "-f", "json",
    ])
    if result is None:
        return None
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


async def update_records(
    records: list[dict],
    base_id: str | None = None,
    table_id: str | None = None,
) -> dict | None:
    """Update records in the configured AITable.

    Each record: {"recordId": "recXxx", "cells": {"fieldId": "value", ...}}
    """
    result = await _run_dws([
        "aitable", "record", "update",
        *_base_table_args(base_id=base_id, table_id=table_id),
        "--records", json.dumps(records, ensure_ascii=False),
        "-y", "-f", "json",
    ])
    if result is None:
        return None
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


async def delete_records(
    record_ids: str,
    base_id: str | None = None,
    table_id: str | None = None,
) -> dict | None:
    """Delete records from the configured AITable.

    record_ids: comma-separated record IDs (e.g. "rec1,rec2")
    """
    result = await _run_dws([
        "aitable", "record", "delete",
        *_base_table_args(base_id=base_id, table_id=table_id),
        "--record-ids", record_ids,
        "--yes", "-f", "json",
    ])
    if result is None:
        return None
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


async def check_dws_available() -> bool:
    """Check if dws CLI is available and authenticated."""
    result = await _run_dws(["auth", "status", "-f", "json"], timeout=10)
    return result is not None
