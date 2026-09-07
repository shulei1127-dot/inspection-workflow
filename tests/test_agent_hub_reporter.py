"""Agent Hub 快照上报模块单元测试。

覆盖验收清单：
- 指标白名单与真实 PostgreSQL 口径（SQL 语句/表/字段/过滤条件断言 + 可选真实库冒烟）；
- source_updated_at 回填与新鲜度 stale 表达（Agent Hub v1 只接受 healthy）；
- verify 携带完整 {"manifest": ...}、register/update 携带完整 manifest；
- snapshot_id / Idempotency-Key 稳定（同端点同载荷幂等）；
- 401/非 2xx 中止（保留 Hub 上一份快照语义）。

真实库冒烟（需可连 DATABASE_URL）：
    AGENT_HUB_TEST_REAL_DB=1 python3 -m unittest tests.test_agent_hub_reporter.RealPostgresSemanticsTest -v
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from services import agent_hub_reporter as ah

FIXED_NOW = _dt.datetime(2026, 9, 7, 4, 30, 0, tzinfo=_dt.timezone.utc)
AGENT_KEY = "support.inspection-workflow"


def make_settings(**overrides) -> SimpleNamespace:
    base = dict(
        agent_hub_enabled=True,
        agent_hub_api_base="https://hub.example.invalid/api/v1",
        agent_hub_token="",
        agent_hub_token_file="",
        agent_hub_dashboard_url="",
        agent_hub_agent_key=AGENT_KEY,
        agent_hub_interval_minutes=30,
        agent_hub_run_on_startup=True,
        agent_hub_timeout_seconds=20.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def fixed_now_patch():
    """把 reporter 模块内的 datetime.now 固定为 FIXED_NOW，保证快照确定性。"""
    fake = MagicMock()
    fake.now.return_value = FIXED_NOW
    return patch.object(ah, "datetime", fake)


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class FakeDB:
    """记录 SQL 并返回预制行数的伪连接，用于纯单测（不发网络/不开库）。"""

    def __init__(self, counts=None, last_success=None):
        self.counts = counts or {}
        self.last_success = last_success
        self.executed: list[str] = []

    def execute(self, clause):
        sql = str(clause)
        self.executed.append(sql)
        if "max(started_at)" in sql:
            return _FakeResult(self.last_success)
        if "dt_sync_status" in sql and "synced" in sql:
            return _FakeResult(self.counts.get("synced", 0))
        if "FROM work_order_syncs" in sql:
            return _FakeResult(self.counts.get("syncs", 0))
        if "review_audit_logs" in sql and "转人工审核" in sql:
            return _FakeResult(self.counts.get("manual", 0))
        if "review_audit_logs" in sql:
            return _FakeResult(self.counts.get("audit", 0))
        if "FROM work_orders" in sql:
            return _FakeResult(self.counts.get("tracked", 0))
        return _FakeResult(0)


class BuildSnapshotTests(unittest.TestCase):
    """白名单结构 + 真实 PG 口径 + 新鲜度/SLA 语义。"""

    def _build(self, db):
        with patch.object(ah, "get_settings", return_value=make_settings()), fixed_now_patch():
            return ah.build_snapshot(db)

    def test_metric_whitelist_and_structure(self):
        db = FakeDB(counts=dict(tracked=12, synced=10, syncs=30, audit=7, manual=2), last_success=FIXED_NOW)
        snap = self._build(db)

        keys = [m["key"] for m in snap["metrics"]]
        self.assertEqual(keys, list(ah.METRIC_KEYS), "指标 key 必须严格等于白名单，新增指标需同步白名单/样例快照")
        self.assertEqual(len(keys), len(set(keys)), "指标 key 不允许重复")

        required_fields = ("key", "label", "value", "unit", "definition", "source", "refresh_frequency", "threshold")
        for metric in snap["metrics"]:
            for field in required_fields:
                self.assertIn(field, metric)
            self.assertIsInstance(metric["value"], int)

        by_key = {m["key"]: m["value"] for m in snap["metrics"]}
        self.assertEqual(by_key["work_order_tracked_count"], 12)
        self.assertEqual(by_key["work_order_synced_count"], 10)
        self.assertEqual(by_key["monthly_sync_association_count"], 30)
        self.assertEqual(by_key["review_audit_processed_7d_count"], 7)
        self.assertEqual(by_key["review_audit_manual_7d_count"], 2)

        # 脱敏契约：只允许白名单指标与健康/时间字段，不含任何项目/工单/人员明细
        self.assertEqual(snap["data_classification"], "aggregated_sanitized")
        self.assertEqual(snap["agent_key"], AGENT_KEY)
        self.assertEqual(snap["schema_version"], "v1")

    def test_sql_matches_real_postgres_schema(self):
        """SQL 口径与真实 PG 表结构一致（表名/字段/过滤条件/时间窗口）。"""
        db = FakeDB(last_success=FIXED_NOW)
        self._build(db)

        self.assertEqual(len(db.executed), 6, "一轮快照应执行 5 条计数 + 1 条最新成功时间查询")
        joined = "\n".join(db.executed)
        self.assertIn("FROM work_orders", joined)
        self.assertIn("dt_sync_status = 'synced'", joined)
        self.assertIn("FROM work_order_syncs", joined)
        self.assertIn("FROM review_audit_logs", joined)
        self.assertIn("conclusion <> 'error'", joined)
        self.assertIn("conclusion = '转人工审核'", joined)
        self.assertIn("interval '7 days'", joined)
        self.assertIn("SELECT max(started_at) FROM sync_logs", joined)
        self.assertIn("status IN ('success', 'partial')", joined)

    def test_healthy_when_fresh(self):
        db = FakeDB(last_success=FIXED_NOW - _dt.timedelta(hours=1))
        snap = self._build(db)

        self.assertEqual(snap["health"]["status"], "healthy", "Agent Hub v1 只接受 healthy")
        self.assertFalse(snap["health"]["stale"])
        self.assertEqual(snap["source_updated_at"], "2026-09-07T03:30:00Z")
        self.assertEqual(snap["health"]["last_success_at"], "2026-09-07T03:30:00Z")
        self.assertEqual(snap["suggestions"], [], "fresh 时不应产生建议")

    def test_stale_when_last_success_older_than_sla(self):
        db = FakeDB(last_success=FIXED_NOW - _dt.timedelta(days=2))
        snap = self._build(db)

        self.assertEqual(snap["health"]["status"], "healthy", "过期不得发送 offline/degraded")
        self.assertTrue(snap["health"]["stale"], "过期用 stale=true 表达")
        self.assertEqual(snap["source_updated_at"], "2026-09-05T04:30:00Z")
        self.assertEqual(len(snap["suggestions"]), 1, "stale 应给出新鲜度 watch 建议")

    def test_stale_when_no_success_or_very_stale(self):
        db = FakeDB(last_success=None)
        snap = self._build(db)
        self.assertEqual(snap["health"]["status"], "healthy")
        self.assertTrue(snap["health"]["stale"])
        self.assertEqual(snap["source_updated_at"], snap["observed_at"], "无成功记录时回退为观测时间")
        self.assertEqual(len(snap["suggestions"]), 1)

        stale_db = FakeDB(last_success=FIXED_NOW - _dt.timedelta(days=100))
        stale_snap = self._build(stale_db)
        self.assertEqual(stale_snap["health"]["status"], "healthy")
        self.assertTrue(stale_snap["health"]["stale"])

    def test_snapshot_id_and_idempotency_key_stable(self):
        db = FakeDB(counts=dict(tracked=1), last_success=FIXED_NOW)
        snap1 = self._build(db)
        snap2 = self._build(db)

        self.assertEqual(snap1["snapshot_id"], snap2["snapshot_id"], "同一分钟/同一观测点快照 ID 必须稳定")
        self.assertRegex(snap1["snapshot_id"], r"^support\.inspection-workflow-\d{8}T\d{4}Z$")
        self.assertEqual(snap1["snapshot_id"], f"{AGENT_KEY}-20260907T0430Z")

        payload = {"a": 1, "b": [2, 3]}
        key1 = ah.idempotency_key(AGENT_KEY, "snapshot", payload)
        key2 = ah.idempotency_key(AGENT_KEY, "snapshot", payload)
        self.assertEqual(key1, key2, "同端点同载荷的 Idempotency-Key 必须稳定")
        self.assertNotEqual(key1, ah.idempotency_key(AGENT_KEY, "verify", payload), "不同端点必须使用不同幂等键")
        self.assertNotEqual(key1, ah.idempotency_key(AGENT_KEY, "snapshot", {"a": 1}), "载荷变化必须产生新键")

    def test_source_updated_at_falls_back_to_observed_at(self):
        db = FakeDB(last_success=None)
        snap = self._build(db)
        self.assertEqual(snap["source_updated_at"], snap["observed_at"])


class ManifestTests(unittest.TestCase):
    def test_manifest_matches_agent_registration_requirements(self):
        settings = make_settings()
        manifest = ah.manifest_for(settings)
        self.assertEqual(manifest["schema_version"], "v1")
        self.assertEqual(manifest["agent_key"], AGENT_KEY)
        self.assertEqual(manifest["data_classification"], "aggregated_sanitized")
        for key in ("display_name", "description", "owner", "dashboard_url"):
            self.assertTrue(str(manifest.get(key, "")).strip())
        for key in ("capabilities", "workflow", "automations"):
            self.assertIsInstance(manifest.get(key), list)
            self.assertTrue(manifest[key])

    def test_dashboard_url_override(self):
        settings = make_settings(agent_hub_dashboard_url="https://dash.example/")
        self.assertEqual(ah.manifest_for(settings)["dashboard_url"], "https://dash.example/")


class TokenResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write_token_file(self, content: str) -> str:
        path = os.path.join(self.tmp.name, "agent_hub_token")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o600)
        return path

    def test_prefers_env_token_over_file(self):
        path = self._write_token_file("file-token\n")
        settings = make_settings(agent_hub_token="env-token", agent_hub_token_file=path)
        self.assertEqual(ah.resolve_token(settings), "env-token")

    def test_reads_root_only_token_file_when_env_empty(self):
        path = self._write_token_file("file-token\n")
        settings = make_settings(agent_hub_token="", agent_hub_token_file=path)
        self.assertEqual(ah.resolve_token(settings), "file-token")

    def test_missing_file_returns_empty_without_logging_secret(self):
        settings = make_settings(agent_hub_token="", agent_hub_token_file="/nonexistent/agent_hub_token")
        with self.assertLogs(ah.logger, level="WARNING"):
            self.assertEqual(ah.resolve_token(settings), "")

    def test_is_configured(self):
        path = self._write_token_file("x")
        self.assertTrue(ah.is_configured(make_settings(agent_hub_token="t")))
        self.assertTrue(ah.is_configured(make_settings(agent_hub_token="", agent_hub_token_file=path)))
        self.assertFalse(ah.is_configured(make_settings(agent_hub_token="")))
        self.assertFalse(ah.is_configured(make_settings(agent_hub_token="", agent_hub_token_file="")))
        self.assertFalse(ah.is_configured(make_settings(agent_hub_enabled=False, agent_hub_token="t")))


class AgentHubHTTPFlowTests(unittest.IsolatedAsyncioTestCase):
    def _calls(self, responses):
        """responses: 按端点返回状态码；记录请求便于断言。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            path = request.url.path
            if path.endswith("/agents/verify") and request.method == "POST":
                return httpx.Response(responses.get("verify", 200), json={"valid": True})
            if "/agents/support.inspection-workflow/snapshots" in path and request.method == "POST":
                return httpx.Response(responses.get("snapshot", 201), json={"snapshot_id": "s1"})
            if path.endswith("/agents/support.inspection-workflow") and request.method == "PUT":
                return httpx.Response(responses.get("register", 200), json={"ok": True})
            return httpx.Response(404)

        return calls, httpx.MockTransport(handler)

    def _run(self, calls, transport, token="secret-token"):
        db = FakeDB(counts=dict(tracked=3), last_success=FIXED_NOW)
        return patch.object(ah, "get_settings", return_value=make_settings(agent_hub_token=token)), db

    async def test_full_round_sends_full_manifest_and_healthy_snapshot(self):
        calls, transport = self._calls({})
        settings_patch, db = self._run(calls, transport)
        with settings_patch, fixed_now_patch():
            result = await ah.report_once(db, transport=transport)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["agent_key"], AGENT_KEY)
        self.assertEqual(result["snapshot_id"], f"{AGENT_KEY}-20260907T0430Z")
        self.assertEqual([c.method + " " + c.url.path for c in calls], [
            "POST /api/v1/agents/verify",
            "PUT /api/v1/agents/support.inspection-workflow",
            "POST /api/v1/agents/support.inspection-workflow/snapshots",
        ], "必须严格按 verify -> register/update -> snapshot 顺序上报")

        expected_manifest = ah.manifest_for(make_settings())
        verify_body = json.loads(calls[0].content)
        self.assertEqual(verify_body, {"manifest": expected_manifest}, "verify 必须携带完整 manifest")
        register_body = json.loads(calls[1].content)
        self.assertEqual(register_body, expected_manifest, "register/update 必须携带完整 manifest")
        self.assertEqual(register_body["agent_key"], AGENT_KEY)
        self.assertEqual(register_body["data_classification"], "aggregated_sanitized")

        snapshot_body = json.loads(calls[2].content)
        self.assertEqual(snapshot_body["health"]["status"], "healthy", "Agent Hub v1 只接受 healthy")
        self.assertEqual(snapshot_body["metrics"][0]["value"], 3)
        self.assertNotIn("offline", json.dumps(snapshot_body, ensure_ascii=False))
        self.assertNotIn("degraded", json.dumps(snapshot_body, ensure_ascii=False))

        for req in calls:
            self.assertEqual(req.headers.get("authorization"), "Bearer secret-token")

        # 幂等键：同端点同载荷稳定、不同端点互不相同
        verify_key = calls[0].headers.get("idempotency-key")
        register_key = calls[1].headers.get("idempotency-key")
        snapshot_key = calls[2].headers.get("idempotency-key")
        self.assertNotEqual(verify_key, register_key)
        self.assertNotEqual(register_key, snapshot_key)
        self.assertEqual(
            verify_key,
            ah.idempotency_key(AGENT_KEY, "verify", {"manifest": expected_manifest}),
        )
        self.assertEqual(
            register_key,
            ah.idempotency_key(AGENT_KEY, "register", expected_manifest),
        )

    async def test_repeat_round_is_idempotent_per_endpoint(self):
        calls1, transport = self._calls({})
        settings_patch, db = self._run(calls1, transport)
        with settings_patch, fixed_now_patch():
            await ah.report_once(db, transport=transport)

        calls2, transport2 = self._calls({})
        with settings_patch, fixed_now_patch():
            await ah.report_once(db, transport=transport2)

        for first, second in zip(calls1, calls2):
            self.assertEqual(
                first.headers.get("idempotency-key"),
                second.headers.get("idempotency-key"),
                "相同快照重试必须使用相同幂等键",
            )

    async def test_401_on_verify_aborts_round(self):
        calls, transport = self._calls({"verify": 401})
        db = FakeDB(last_success=FIXED_NOW)
        with patch.object(ah, "get_settings", return_value=make_settings(agent_hub_token="t")), fixed_now_patch():
            with self.assertRaises(RuntimeError):
                await ah.report_once(db, transport=transport)
        self.assertEqual(len(calls), 1, "401 必须立即中止本轮，不得继续 register/snapshot")

    async def test_non2xx_on_register_aborts_before_snapshot(self):
        calls, transport = self._calls({"register": 500})
        db = FakeDB(last_success=FIXED_NOW)
        with patch.object(ah, "get_settings", return_value=make_settings(agent_hub_token="t")), fixed_now_patch():
            with self.assertRaises(RuntimeError):
                await ah.report_once(db, transport=transport)
        self.assertEqual(len(calls), 2, "register 500 后必须停止，快照请求不应发出")

    async def test_disabled_without_token_never_requests(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(200))
        db = FakeDB()
        with patch.object(ah, "get_settings", return_value=make_settings(agent_hub_token="", agent_hub_token_file="")):
            result = await ah.report_once(db, transport=transport)
        self.assertEqual(result["status"], "disabled")
        with patch.object(ah, "get_settings", return_value=make_settings(agent_hub_enabled=False, agent_hub_token="t")):
            result = await ah.report_once(db, transport=transport)
        self.assertEqual(result["status"], "disabled")


@unittest.skipUnless(
    os.environ.get("AGENT_HUB_TEST_REAL_DB"),
    "需设置 AGENT_HUB_TEST_REAL_DB=1 且可连接真实 PostgreSQL 才运行真实口径冒烟",
)
class RealPostgresSemanticsTest(unittest.TestCase):
    """真实 PostgreSQL 冒烟：build_snapshot 结果与直连 SQL 逐项对账。"""

    def test_metrics_agree_with_direct_queries(self):
        from core.config import get_settings
        from sqlalchemy import create_engine, text

        engine = create_engine(get_settings().database_url)
        with engine.connect() as conn:
            snap = ah.build_snapshot(conn)

        by_key = {m["key"]: m["value"] for m in snap["metrics"]}

        with engine.connect() as conn:
            self.assertEqual(by_key["work_order_tracked_count"],
                             int(conn.execute(text("SELECT count(*) FROM work_orders")).scalar()))
            self.assertEqual(by_key["work_order_synced_count"],
                             int(conn.execute(text("SELECT count(*) FROM work_orders WHERE dt_sync_status = 'synced'")).scalar()))
            self.assertEqual(by_key["monthly_sync_association_count"],
                             int(conn.execute(text("SELECT count(*) FROM work_order_syncs")).scalar()))
            self.assertEqual(by_key["review_audit_processed_7d_count"],
                             int(conn.execute(text("SELECT count(*) FROM review_audit_logs WHERE created_at > now() - interval '7 days' AND conclusion <> 'error'")).scalar()))
            self.assertEqual(by_key["review_audit_manual_7d_count"],
                             int(conn.execute(text("SELECT count(*) FROM review_audit_logs WHERE created_at > now() - interval '7 days' AND conclusion = '转人工审核'")).scalar()))
            last_success = conn.execute(text("SELECT max(started_at) FROM sync_logs WHERE status IN ('success', 'partial')")).scalar()

        expected_updated_at = ah._iso(last_success) if last_success else snap["observed_at"]
        self.assertEqual(snap["source_updated_at"], expected_updated_at)
        self.assertEqual(snap["health"]["status"], "healthy", "Agent Hub v1 只接受 healthy")


if __name__ == "__main__":
    unittest.main()
