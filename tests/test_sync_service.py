import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from models.work_order import WorkOrder
from services import dingtalk_client
from services import sync_service


class SyncAITablePolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            dt_dispatch_base_id="base",
            dt_dispatch_table_id="table",
        )

    def make_work_order(self, *, eligible: bool = True) -> WorkOrder:
        return WorkOrder(
            pts_order_id="pts-1",
            customer_name="客户",
            product_name="产品",
            order_type="产品巡检",
            contact_phone="13800138000",
            region="华南战区",
            sales_name="销售",
            dt_create_eligible=eligible,
            dt_sync_status="pending",
        )

    async def test_existing_link_is_reused_without_update_or_create(self) -> None:
        wo = self.make_work_order()
        with patch.object(sync_service, "get_settings", return_value=self.settings), \
             patch.object(dingtalk_client, "update_records", new_callable=AsyncMock) as update, \
             patch.object(dingtalk_client, "create_records", new_callable=AsyncMock) as create:
            await sync_service._sync_to_aitable(
                None,
                wo,
                aitable_lookup={"pts-1": ["rec-existing"]},
            )

        self.assertEqual(wo.dt_record_id, "rec-existing")
        self.assertEqual(wo.dt_sync_status, "synced")
        self.assertFalse(wo.dt_create_eligible)
        update.assert_not_awaited()
        create.assert_not_awaited()

    async def test_new_work_order_creates_when_link_is_absent(self) -> None:
        wo = self.make_work_order()
        with patch.object(sync_service, "get_settings", return_value=self.settings), \
             patch.object(dingtalk_client, "create_records", new_callable=AsyncMock,
                          return_value={"newRecordIds": ["rec-new"]}) as create:
            lookup = {}
            await sync_service._sync_to_aitable(None, wo, aitable_lookup=lookup)

        self.assertEqual(wo.dt_record_id, "rec-new")
        self.assertEqual(wo.dt_sync_status, "synced")
        self.assertFalse(wo.dt_create_eligible)
        self.assertEqual(lookup, {"pts-1": ["rec-new"]})
        create.assert_awaited_once()

    async def test_previous_month_record_does_not_block_target_month(self) -> None:
        wo = self.make_work_order()
        with patch.object(sync_service, "get_settings", return_value=self.settings), \
             patch.object(dingtalk_client, "create_records", new_callable=AsyncMock,
                          return_value={"newRecordIds": ["rec-aug"]}) as create:
            lookup = {("pts-1", "2026-07"): ["rec-jul"]}
            await sync_service._sync_to_aitable(
                None,
                wo,
                sync_month="2026-08",
                aitable_lookup=lookup,
            )

        self.assertEqual(wo.dt_record_id, "rec-aug")
        self.assertEqual(lookup[("pts-1", "2026-07")], ["rec-jul"])
        self.assertEqual(lookup[("pts-1", "2026-08")], ["rec-aug"])
        args = create.await_args_list[0]
        cells = args.args[0][0]["cells"]
        self.assertEqual(cells["9OtL7li"], "2026-08-01")

    async def test_same_month_record_is_reused(self) -> None:
        wo = self.make_work_order()
        with patch.object(sync_service, "get_settings", return_value=self.settings), \
             patch.object(dingtalk_client, "create_records", new_callable=AsyncMock) as create:
            await sync_service._sync_to_aitable(
                None,
                wo,
                sync_month="2026-08",
                aitable_lookup={("pts-1", "2026-08"): ["rec-aug"]},
            )

        self.assertEqual(wo.dt_record_id, "rec-aug")
        create.assert_not_awaited()

    async def test_duplicate_links_refuse_to_write(self) -> None:
        wo = self.make_work_order()
        with patch.object(sync_service, "get_settings", return_value=self.settings), \
             patch.object(dingtalk_client, "update_records", new_callable=AsyncMock) as update, \
             patch.object(dingtalk_client, "create_records", new_callable=AsyncMock) as create:
            await sync_service._sync_to_aitable(
                None,
                wo,
                aitable_lookup={"pts-1": ["rec-a", "rec-b"]},
            )

        self.assertEqual(wo.dt_sync_status, "dedup_conflict")
        self.assertIsNone(wo.dt_record_id)
        update.assert_not_awaited()
        create.assert_not_awaited()

    async def test_ineligible_existing_work_order_never_writes(self) -> None:
        wo = self.make_work_order(eligible=False)
        with patch.object(sync_service, "get_settings", return_value=self.settings), \
             patch.object(dingtalk_client, "update_records", new_callable=AsyncMock) as update, \
             patch.object(dingtalk_client, "create_records", new_callable=AsyncMock) as create:
            await sync_service._sync_to_aitable(None, wo, aitable_lookup={})

        update.assert_not_awaited()
        create.assert_not_awaited()


class StrictAITableQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_failure_is_not_an_empty_table(self) -> None:
        with patch.object(dingtalk_client, "_run_dws", new_callable=AsyncMock, return_value=None):
            with self.assertRaises(RuntimeError):
                await dingtalk_client.query_records(
                    base_id="base",
                    table_id="table",
                    fetch_all=True,
                    strict=True,
                )


if __name__ == "__main__":
    unittest.main()
