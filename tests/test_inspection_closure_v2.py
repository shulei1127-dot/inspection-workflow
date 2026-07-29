import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import inspection_closure_v2 as v2
from services import pts_closure_service
from services.aitable_fields import DISPATCH


class ClosureSettings(SimpleNamespace):
    inspection_closure_v2_enabled = True
    inspection_closure_dry_run = True
    inspection_closure_report_upload_enabled = False
    inspection_closure_stage_advance_enabled = False
    inspection_closure_aitable_writeback_enabled = False
    inspection_closure_manual_notify_enabled = False
    dt_dispatch_report_link_uploaded_field_id = "fld-report-uploaded"
    inspection_closure_whitelist = ""
    inspection_closure_default_assignee_id = "assignee"
    inspection_closure_upload_max_retries = 2
    inspection_closure_stage_max_attempts = 3
    inspection_closure_retry_backoff_seconds = 0
    inspection_closure_retry_max_backoff_seconds = 0
    dt_dispatch_base_id = "base"
    dt_dispatch_table_id = "table"


def make_record() -> dict:
    return {
        "recordId": "rec-1",
        "cells": {
            DISPATCH["邮件是否发送"]: "是",
            DISPATCH["工单是否闭环"]: "否",
            DISPATCH["巡检工单链接"]: "https://pts.chaitin.net/project/order/pts-1",
            DISPATCH["客户名称"]: "客户",
            DISPATCH["巡检报告"]: [
                {"id": "att-a", "filename": "A.pdf", "url": "https://example.invalid/a"},
                {"id": "att-b", "filename": "B.pdf", "url": "https://example.invalid/b"},
            ],
        },
    }


class ClosureV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_v1_dispatch_is_unchanged_when_v2_disabled(self) -> None:
        settings = SimpleNamespace(inspection_closure_v2_enabled=False)
        expected = {"status": "legacy"}
        with patch.object(pts_closure_service, "get_settings", return_value=settings), \
             patch.object(pts_closure_service, "_run_closure_check_v1", new_callable=AsyncMock, return_value=expected) as legacy:
            result = await pts_closure_service.run_closure_check(None)
        self.assertEqual(result, expected)
        legacy.assert_awaited_once_with(None)

    async def test_missing_upload_field_fails_closed(self) -> None:
        settings = ClosureSettings()
        settings.dt_dispatch_report_link_uploaded_field_id = ""
        with patch.object(v2, "get_settings", return_value=settings):
            result = await v2.run_closure_check_v2(None)
        self.assertTrue(result["configuration_error"])
        self.assertNotIn("records", result)

    async def test_strict_aitable_failure_is_not_empty_success(self) -> None:
        settings = ClosureSettings()
        with patch.object(v2, "get_settings", return_value=settings), \
             patch.object(v2.dingtalk_client, "get_table", new_callable=AsyncMock, return_value={
                 "fields": [{"id": "fld-report-uploaded", "type": "singleSelect", "options": [{"name": "是"}, {"name": "否"}]}],
             }), \
             patch.object(v2.dingtalk_client, "query_records", new_callable=AsyncMock, side_effect=RuntimeError("AITable down")):
            result = await v2.run_closure_check_v2(None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("AITable 查询失败", result["reason"])

    async def test_dry_run_never_mutates_pts_or_aitable(self) -> None:
        settings = ClosureSettings()
        details = {
            "id": "pts-1",
            "is_finished": False,
            "current_stage": {"name": "开始处理工单", "sequence": 2},
            "creator": {"id": "creator", "name": "创建人"},
            "claim_by": {"id": "owner", "name": "负责人"},
            "info": [],
        }
        download = AsyncMock(return_value={"status": "downloaded", "filename": "A.pdf", "size": 3, "sha256": "abc", "content": b"pdf"})
        with patch.object(v2, "get_settings", return_value=settings), \
             patch.object(v2.pts_client, "query_work_order_details", new_callable=AsyncMock, return_value=details), \
             patch.object(v2.pts_client, "download_attachment_with_hash", download), \
             patch.object(v2.pts_client, "upload_file_via_api_with_retry", new_callable=AsyncMock) as upload, \
             patch.object(v2.pts_client, "add_work_order_info", new_callable=AsyncMock) as note, \
             patch.object(v2.pts_client, "confirm_work_order_stage", new_callable=AsyncMock) as confirm, \
             patch.object(v2.dingtalk_client, "update_records", new_callable=AsyncMock) as update:
            result = await v2.coordinate_record(None, make_record())
        self.assertEqual(result["status"], "dry_run")
        upload.assert_not_awaited()
        note.assert_not_awaited()
        confirm.assert_not_awaited()
        update.assert_not_awaited()

    async def test_partial_upload_keeps_attachment_file_mapping(self) -> None:
        settings = ClosureSettings(inspection_closure_dry_run=False, inspection_closure_report_upload_enabled=True)
        details = {
            "id": "pts-1", "is_finished": False,
            "current_stage": {"name": "开始处理工单", "sequence": 2},
            "creator": {}, "claim_by": {}, "info": [],
        }
        verified = {
            **details,
            "info": [{"note": "巡检报告Markdown链接已补充（2个附件）\n[A.pdf](/f/file-a)\n[B.pdf](/f/file-b)"}],
        }
        downloads = [
            {"status": "downloaded", "filename": "A.pdf", "size": 3, "sha256": "a", "content": b"a"},
            {"status": "downloaded", "filename": "B.pdf", "size": 3, "sha256": "b", "content": b"b"},
        ]
        with patch.object(v2, "get_settings", return_value=settings), \
             patch.object(v2.pts_client, "query_work_order_details", new_callable=AsyncMock, side_effect=[details, verified]), \
             patch.object(v2.pts_client, "download_attachment_with_hash", new_callable=AsyncMock, side_effect=downloads), \
             patch.object(v2.pts_client, "upload_file_via_api_with_retry", new_callable=AsyncMock, side_effect=[
                 {"status": "uploaded", "pts_file_id": "file-a"},
                 {"status": "uploaded", "pts_file_id": "file-b"},
             ]), \
             patch.object(v2.pts_client, "add_work_order_info", new_callable=AsyncMock, return_value=True) as note:
            result = await v2.coordinate_record(None, make_record())
        self.assertEqual(result["status"], "report_ready")
        note.assert_awaited_once()
        note_text = note.await_args.kwargs["note"]
        self.assertIn("[A.pdf](/f/file-a)", note_text)
        self.assertIn("[B.pdf](/f/file-b)", note_text)
    async def test_stage_assignee_is_only_sent_on_first_required_confirmation(self) -> None:
        settings = ClosureSettings(
            inspection_closure_dry_run=False,
            inspection_closure_stage_advance_enabled=True,
        )
        record = make_record()
        record["cells"][DISPATCH["巡检报告"]] = [record["cells"][DISPATCH["巡检报告"]][0]]
        initial = {
            "id": "pts-1", "is_finished": False,
            "current_stage": {"name": "指定工单负责人", "sequence": 1},
            "creator": {}, "claim_by": {},
            "info": [{"note": "[A.pdf](/f/file-a)"}],
        }
        processing = {**initial, "current_stage": {"name": "开始处理工单", "sequence": 2}}
        target = {**initial, "current_stage": {"name": "审核工单", "sequence": 3}}
        with patch.object(v2, "get_settings", return_value=settings), \
             patch.object(v2.pts_client, "query_work_order_details", new_callable=AsyncMock, side_effect=[initial, initial, processing, processing, target]), \
             patch.object(v2.pts_client, "download_attachment_with_hash", new_callable=AsyncMock, return_value={"status": "downloaded", "filename": "A.pdf", "size": 3, "sha256": "a", "content": b"a"}), \
             patch.object(v2.pts_client, "confirm_work_order_stage", new_callable=AsyncMock, side_effect=[True, True]) as confirm:
            result = await v2.coordinate_record(None, record)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(confirm.await_args_list[0].kwargs["claim_by"], "assignee")
        self.assertIsNone(confirm.await_args_list[1].kwargs["claim_by"])


if __name__ == "__main__":
    unittest.main()
