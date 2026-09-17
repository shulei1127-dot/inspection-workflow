import unittest

from services.yunji_dispatch import (
    DEPT_LEADER,
    SUPPLIER_MAP,
    _find_partner,
    resolve_delivery_info,
)


class YunjiSupplierMatchingTests(unittest.TestCase):
    def test_pingyun_uses_current_company_name(self):
        self.assertEqual(
            SUPPLIER_MAP["平云"],
            "广州平云数安科技服务有限公司",
        )

    def test_blank_partner_is_never_selected(self):
        partners = [
            {"label": "", "value": 785},
            {"label": "广州平云数安科技服务有限公司", "value": 498},
        ]

        partner = _find_partner(partners, "广州平云数安科技服务有限公司")

        self.assertEqual(partner["value"], 498)
        self.assertEqual(partner["label"], "广州平云数安科技服务有限公司")

    def test_only_blank_partner_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "未找到供应商"):
            _find_partner([{"label": "", "value": 785}], "广州平云数安科技服务有限公司")

    def test_partner_without_id_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "未找到供应商"):
            _find_partner(
                [{"label": "广州平云数安科技服务有限公司", "value": None}],
                "广州平云数安科技服务有限公司",
            )

    def test_north_and_northeast_stays_with_liu_chao(self):
        self.assertEqual(DEPT_LEADER["华北东北技术支持"], "刘超")

    def test_industry_departments_are_owned_by_wang_xin(self):
        for department in (
            "政府央企能源行业技术支持",
            "通信行业技术支持",
            "金融行业技术支持",
        ):
            with self.subTest(department=department):
                self.assertEqual(DEPT_LEADER[department], "王欣")

    def test_wang_dexin_routes_to_wang_xin(self):
        resolved = resolve_delivery_info("王德鑫")

        self.assertEqual(resolved["department"], "政府央企能源行业技术支持")
        self.assertEqual(resolved["region_leader"], "王欣")


if __name__ == "__main__":
    unittest.main()
