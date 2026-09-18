import unittest

from services.report_audit_service import attachment_fingerprint, run_attachment_rules, run_hard_rules


class ReportAuditRulesTests(unittest.TestCase):
    def test_detects_wrong_customer_product_toc_and_missing_summary(self):
        text = """中远海运集装箱运输有限公司
        谛听巡检报告
        TOC will populate after updating fields
        本次巡检未见异常。
        """
        findings = run_hard_rules(
            "南洋商业银行（中国）有限公司",
            "风险评估系统（洞鉴）",
            "中远海运谛听巡检报告.pdf",
            text,
            8,
        )
        ids = {item["rule_id"] for item in findings}
        self.assertIn("COMMON-001", ids)
        self.assertIn("COMMON-003", ids)
        self.assertIn("COMMON-005", ids)
        self.assertIn("COMMON-006", ids)

    def test_muyun_does_not_require_device_summary(self):
        text = "中山农村商业银行股份有限公司 牧云巡检报告 系统运行正常" * 100
        findings = run_hard_rules(
            "中山农村商业银行股份有限公司",
            "云工作负载保护平台（牧云）",
            "中山农商银行牧云巡检报告.pdf",
            text,
            10,
        )
        self.assertNotIn("COMMON-006", {item["rule_id"] for item in findings})

    def test_customer_legal_entity_name_must_match_in_full(self):
        text = ("华福证券有限责任公司 万象巡检报告 设备巡检信息汇总 系统运行正常" * 100)
        findings = run_hard_rules(
            "华福证券股份有限公司",
            "安全分析与运营管理平台（万象）",
            "华福证券有限责任公司万象巡检报告-2026.09.15.pdf",
            text,
            11,
        )
        self.assertIn("COMMON-001", {item["rule_id"] for item in findings})

    def test_attachment_fingerprint_is_order_independent(self):
        attachments = [
            {"resourceId": "2", "filename": "b.docx", "size": 2, "url": "https://old"},
            {"resourceId": "1", "filename": "a.pdf", "size": 1, "url": "https://old"},
        ]
        changed_urls = [dict(item, url="https://renewed") for item in reversed(attachments)]
        self.assertEqual(attachment_fingerprint(attachments), attachment_fingerprint(changed_urls))

    def test_warns_when_word_or_pdf_version_is_missing(self):
        findings = run_attachment_rules([{"filename": "客户雷池巡检报告.pdf"}])
        self.assertEqual([item["rule_id"] for item in findings], ["COMMON-010"])
        self.assertIn("Word", findings[0]["evidence"])

    def test_accepts_pdf_and_docx_pair(self):
        findings = run_attachment_rules(
            [{"filename": "客户雷池巡检报告.pdf"}, {"filename": "客户雷池巡检报告.docx"}]
        )
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
