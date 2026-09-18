import unittest

from services.report_audit_service import (
    attachment_fingerprint,
    deduplicate_ai_findings,
    group_report_attachments,
    is_unsent_report_record,
    parse_ai_findings,
    run_attachment_rules,
    run_hard_rules,
)


class ReportAuditRulesTests(unittest.TestCase):
    def test_groups_pdf_docx_pairs_but_keeps_different_reports_separate(self):
        attachments = [
            {"filename": "客户雷池巡检报告-10.0.0.2.docx"},
            {"filename": "客户雷池巡检报告-10.0.0.1.pdf"},
            {"filename": "客户雷池巡检报告-10.0.0.2.pdf"},
            {"filename": "说明.txt"},
            {"filename": "客户雷池巡检报告-10.0.0.1.docx"},
        ]
        groups = group_report_attachments(attachments)
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            [[item["filename"] for item in group] for group in groups],
            [
                ["客户雷池巡检报告-10.0.0.1.pdf", "客户雷池巡检报告-10.0.0.1.docx"],
                ["客户雷池巡检报告-10.0.0.2.docx", "客户雷池巡检报告-10.0.0.2.pdf"],
            ],
        )

    def test_multi_product_dispatch_accepts_each_expected_product_family(self):
        text = ("安徽省气象局 谛听巡检报告 设备巡检信息汇总 系统运行正常" * 100)
        findings = run_hard_rules(
            "安徽省气象局",
            "风险评估系统（洞鉴）、主动威胁欺骗防御系统（谛听）",
            "安徽省气象局谛听巡检报告.pdf",
            text,
            10,
        )
        self.assertNotIn("COMMON-003", {item["rule_id"] for item in findings})

    def test_only_explicit_unsent_records_with_report_are_candidates(self):
        base_record = {
            "recordId": "rec-1",
            "cells": {
                "ZzlBIoW": {"name": "否"},
                "nd284rT": [{"filename": "客户雷池巡检报告.pdf"}],
            },
        }
        self.assertTrue(is_unsent_report_record(base_record))

        sent_record = {**base_record, "cells": {**base_record["cells"], "ZzlBIoW": {"name": "是"}}}
        empty_status = {**base_record, "cells": {**base_record["cells"], "ZzlBIoW": None}}
        no_report = {**base_record, "cells": {**base_record["cells"], "nd284rT": []}}
        self.assertFalse(is_unsent_report_record(sent_record))
        self.assertFalse(is_unsent_report_record(empty_status))
        self.assertFalse(is_unsent_report_record(no_report))

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

    def test_ai_findings_require_verbatim_evidence_and_ignore_equivalent_dates_and_scopes(self):
        text = "报告日期：2026-09-15。巡检日期：2026年09月15日。共接入设备36台，本次巡检1台设备。"
        data = {
            "findings": [
                {
                    "rule_id": "AI-001",
                    "severity": "error",
                    "title": "日期前后矛盾",
                    "evidence_quotes": ["报告日期：2026-09-15", "巡检日期：2026年09月15日"],
                    "suggestion": "统一日期",
                },
                {
                    "rule_id": "AI-002",
                    "severity": "warning",
                    "title": "设备数量口径需核对",
                    "evidence_quotes": ["共接入设备36台", "本次巡检1台设备"],
                    "suggestion": "说明接入数与本次巡检数的关系",
                },
                {
                    "rule_id": "AI-003",
                    "severity": "warning",
                    "title": "模板混用",
                    "evidence_quotes": ["原文中不存在的模板名称"],
                    "suggestion": "更换模板",
                },
            ]
        }
        findings = parse_ai_findings(data, text)
        self.assertEqual(findings, [])

    def test_ai_findings_are_deduplicated_against_hard_rules(self):
        hard = [{"rule_id": "COMMON-001"}, {"rule_id": "COMMON-006"}]
        ai = [
            {"rule_id": "AI-001", "title": "客户名称不一致"},
            {"rule_id": "AI-002", "title": "缺少设备巡检信息汇总"},
            {"rule_id": "AI-003", "title": "设备数量口径需核对"},
        ]
        self.assertEqual(
            [item["rule_id"] for item in deduplicate_ai_findings(hard, ai)],
            ["AI-003"],
        )

    def test_ai_findings_reject_unprovable_absence_and_unrelated_dimensions(self):
        text = (
            "3 巡检结果分析和处理建议。3.1 巡检概况。3.2 巡检详情。"
            "系统cpu 使用率、内存使用率、磁盘使用率均符合预期。"
            "部分接入异常并存在部分非标日志。"
            "共接入设备36台，本次巡检1台设备。"
        )
        data = {
            "findings": [
                {
                    "rule_id": "AI-001",
                    "severity": "error",
                    "title": "目录或章节缺失",
                    "evidence_quotes": ["3 巡检结果分析和处理建议", "3.1 巡检概况", "3.2 巡检详情"],
                },
                {
                    "rule_id": "AI-002",
                    "severity": "error",
                    "title": "结论与异常矛盾",
                    "evidence_quotes": [
                        "系统cpu 使用率、内存使用率、磁盘使用率均符合预期",
                        "部分接入异常并存在部分非标日志",
                    ],
                },
                {
                    "rule_id": "AI-003",
                    "severity": "error",
                    "title": "设备数量不一致",
                    "evidence_quotes": ["共接入设备36台", "本次巡检1台设备"],
                },
                {
                    "rule_id": "AI-004",
                    "severity": "error",
                    "title": "模板占位或绝对化结论",
                    "evidence_quotes": ["系统cpu 使用率、内存使用率、磁盘使用率均符合预期"],
                },
            ]
        }
        self.assertEqual(parse_ai_findings(data, text), [])

    def test_ai_vague_advice_is_retained_as_warning(self):
        text = "巡检发现部分接入异常并存在部分非标日志，需要进行策略优化。"
        data = {
            "findings": [
                {
                    "rule_id": "AI-001",
                    "severity": "error",
                    "title": "异常没有给出可执行建议",
                    "evidence_quotes": ["部分接入异常并存在部分非标日志", "需要进行策略优化"],
                    "suggestion": "补充具体动作、负责人和完成期限",
                }
            ]
        }
        findings = parse_ai_findings(data, text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["title"], "整改建议缺少可执行细节")
        self.assertEqual(findings[0]["severity"], "warning")

    def test_ai_single_quote_does_not_turn_operational_issue_into_report_defect(self):
        text = "部分接入异常并存在部分非标日志，需要进行策略优化。"
        data = {
            "findings": [
                {
                    "rule_id": "AI-001",
                    "severity": "warning",
                    "title": "日志接入异常",
                    "evidence_quotes": ["部分接入异常并存在部分非标日志，需要进行策略优化"],
                    "suggestion": "修复日志接入",
                }
            ]
        }
        self.assertEqual(parse_ai_findings(data, text), [])


if __name__ == "__main__":
    unittest.main()
