import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.review.audit.engine import _build_result
from services.review.audit.schemas import AuditInput, AuditResult
from services.review.extractors.review_api_mapper import map_project_from_graphql
from services.review import review_dingtalk


class ReviewCrmProjectTests(unittest.IsolatedAsyncioTestCase):
    def test_mapper_keeps_pts_delivery_id_and_maps_nested_crm_id(self) -> None:
        data = {
            "delivery_status": "to_after_sale_review",
            "project": {
                "id": "crm-project-1",
                "name": "项目",
                "company": {"id": "company-1", "name": "客户"},
            },
        }

        result = map_project_from_graphql("pts-delivery-1", data)

        self.assertEqual(result.project_id, "pts-delivery-1")
        self.assertEqual(result.crm_project_id, "crm-project-1")

    def test_engine_preserves_crm_project_id(self) -> None:
        audit_input = AuditInput(
            project_id="pts-delivery-1",
            crm_project_id="crm-project-1",
        )

        result = _build_result(
            audit_input,
            [],
            "通过",
            None,
            None,
            None,
        )

        self.assertEqual(result.crm_project_id, "crm-project-1")

    async def test_main_table_writes_crm_and_pts_links(self) -> None:
        result = AuditResult(
            project_id="pts-delivery-1",
            crm_project_id="crm-project-1",
            customer_name="客户",
            conclusion="通过",
        )
        settings = SimpleNamespace(
            review_aitable_base_id="base",
            review_aitable_main_table_id="table",
            review_aitable_corp_id="corp",
        )

        with patch.object(review_dingtalk, "get_settings", return_value=settings), \
             patch.object(
                 review_dingtalk,
                 "create_records",
                 new_callable=AsyncMock,
                 return_value={"newRecordIds": ["record-1"]},
             ) as create, \
             patch.object(
                 review_dingtalk,
                 "_update_user_fields",
                 new_callable=AsyncMock,
             ):
            await review_dingtalk.write_audit_to_dingtalk(result)

        cells = create.await_args.args[0][0]["cells"]
        crm_url = "https://crm.chaitin.net/project/crm-project-1#base"
        pts_url = "https://pts.chaitin.net/project/pts-delivery-1#base"
        self.assertEqual(cells[review_dingtalk.CRM_PROJECT_FIELD], {"link": crm_url, "text": crm_url})
        self.assertEqual(cells["PTS交付链接"], {"link": pts_url, "text": pts_url})

    async def test_main_table_omits_crm_link_when_id_missing(self) -> None:
        result = AuditResult(
            project_id="pts-delivery-1",
            customer_name="客户",
            conclusion="通过",
        )
        settings = SimpleNamespace(
            review_aitable_base_id="base",
            review_aitable_main_table_id="table",
            review_aitable_corp_id="corp",
        )

        with patch.object(review_dingtalk, "get_settings", return_value=settings), \
             patch.object(
                 review_dingtalk,
                 "create_records",
                 new_callable=AsyncMock,
                 return_value={"newRecordIds": ["record-1"]},
             ) as create, \
             patch.object(
                 review_dingtalk,
                 "_update_user_fields",
                 new_callable=AsyncMock,
             ):
            await review_dingtalk.write_audit_to_dingtalk(result)

        cells = create.await_args.args[0][0]["cells"]
        self.assertNotIn(review_dingtalk.CRM_PROJECT_FIELD, cells)


if __name__ == "__main__":
    unittest.main()
