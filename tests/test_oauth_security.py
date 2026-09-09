"""Unit tests for OIDC session/state token helpers (stdlib only)."""

import unittest

from core import security


class SecurityTokenTest(unittest.TestCase):
    def test_roundtrip(self):
        token = security.sign_token(
            {"sub": "u-1", "name": "张三"}, "top-secret", ttl_seconds=3600
        )
        data = security.verify_token(token, "top-secret")
        self.assertEqual(data["sub"], "u-1")
        self.assertEqual(data["name"], "张三")
        self.assertNotIn("exp", data)

    def test_tampered_token_rejected(self):
        token = security.sign_token({"sub": "u-1"}, "s", ttl_seconds=3600)
        bad = token[:-2] + ("ab" if not token.endswith("ab") else "cd")
        self.assertIsNone(security.verify_token(bad, "s"))

    def test_wrong_secret_rejected(self):
        token = security.sign_token({"sub": "u-1"}, "s1", ttl_seconds=3600)
        self.assertIsNone(security.verify_token(token, "s2"))

    def test_expired_token_rejected(self):
        token = security.sign_token(
            {"sub": "u-1"}, "s", ttl_seconds=60, now=1_000_000
        )
        self.assertIsNone(security.verify_token(token, "s", now=1_001_000))
        self.assertIsNotNone(security.verify_token(token, "s", now=1_000_050))

    def test_state_payload_roundtrip(self):
        state = security.generate_state()
        token = security.sign_token(
            {"state": state, "next": "/dashboard"}, "s", ttl_seconds=600
        )
        data = security.verify_token(token, "s")
        self.assertEqual(data["state"], state)
        self.assertEqual(data["next"], "/dashboard")

    def test_sanitize_next_path(self):
        from apps.api.routers.oauth import sanitize_next_path

        self.assertEqual(sanitize_next_path("https://evil.example"), "/")
        self.assertEqual(sanitize_next_path("//evil.example"), "/")
        self.assertEqual(sanitize_next_path("\\evil"), "/")
        self.assertEqual(sanitize_next_path("/review?month=8"), "/review?month=8")
        self.assertEqual(sanitize_next_path(""), "/")


if __name__ == "__main__":
    unittest.main()
