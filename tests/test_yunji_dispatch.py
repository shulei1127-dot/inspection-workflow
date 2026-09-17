import unittest

from services.yunji_dispatch import SUPPLIER_MAP, _find_partner


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


if __name__ == "__main__":
    unittest.main()
