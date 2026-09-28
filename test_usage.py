"""OAuth recovery tests. All credentials, state and HTTP responses are local fakes."""
import io
import json
import tempfile
import time
import unittest
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import appdata
import config
import usage


class OAuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.credentials = root / ".credentials.json"
        for target, value in (
            ("usage.CREDENTIALS_PATH", str(self.credentials)),
            ("usage._LOCK_DIR", str(root / "refresh.lock")),
            ("appdata.APP_DIR", str(root)),
            ("appdata.STATE_PATH", str(root / "state.json")),
        ):
            self.stack.enter_context(patch(target, value))
        self.stack.enter_context(patch("appdata.log"))
        self.http = self.stack.enter_context(patch("usage.urllib.request.urlopen"))
        self.cfg = {**config.DEFAULTS, "fallback_to_ccusage": False,
                    "user_agent": "claude-code/test"}
        self.save_creds(expires_in=-60)

    def save_creds(self, expires_in, access="old-access", refresh="old-refresh"):
        self.credentials.write_text(json.dumps({
            "unrelated": {"keep": True},
            "claudeAiOauth": {
                "accessToken": access, "refreshToken": refresh,
                "expiresAt": (time.time() + expires_in) * 1000,
                "scopes": ["user:profile", "user:inference"],
                "subscriptionType": "pro",
            },
        }), encoding="utf-8")

    @staticmethod
    def response(data):
        return io.BytesIO(json.dumps(data).encode())

    def token_response(self):
        return self.response({"access_token": "new-access", "refresh_token": "new-refresh",
                              "expires_in": 28800})

    def usage_response(self):
        return self.response({"five_hour": {"utilization": 42},
                              "seven_day": {"utilization": 90}})

    @staticmethod
    def http_error(code, headers=None):
        return urllib.error.HTTPError(usage.OAUTH_TOKEN_URL, code, "test", headers or {}, None)

    def test_expired_token_recovers_without_claude_and_preserves_credentials(self):
        self.http.side_effect = [self.token_response(), self.usage_response()]
        with patch("usage.subprocess.run") as subprocess:
            snap = usage.fetch_snapshot(self.cfg)
        subprocess.assert_not_called()
        self.assertTrue(snap.ok, snap.error)
        self.assertEqual(snap.source, "oauth")
        self.assertEqual(snap.session.used, 42)
        saved = json.loads(self.credentials.read_text(encoding="utf-8"))
        self.assertEqual(saved["unrelated"], {"keep": True})
        self.assertEqual(saved["claudeAiOauth"]["subscriptionType"], "pro")
        self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertTrue(usage._token_is_usable(saved["claudeAiOauth"]))
        self.assertEqual(usage._refresh_state()["failures"], 0)

    def test_token_exchange_headers_are_separate_from_usage_headers(self):
        self.http.side_effect = [self.token_response(), self.usage_response()]
        self.assertTrue(usage.fetch_snapshot(self.cfg).ok)
        token_request, usage_request = [call.args[0] for call in self.http.call_args_list]
        self.assertEqual(token_request.full_url, usage.OAUTH_TOKEN_URL)
        self.assertEqual(token_request.get_method(), "POST")
        headers = {k.lower(): v for k, v in token_request.header_items()}
        self.assertEqual(headers["user-agent"], "axios/1.9.0")
        self.assertEqual(headers["accept"], "application/json, text/plain, */*")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("authorization", headers)
        self.assertEqual(json.loads(token_request.data), {
            "grant_type": "refresh_token", "refresh_token": "old-refresh",
            "client_id": usage.OAUTH_CLIENT_ID, "scope": "user:profile user:inference",
        })
        self.assertEqual(usage_request.full_url, usage.OAUTH_USAGE_URL)
        self.assertEqual(usage_request.get_header("User-agent"), "claude-code/test")
        self.assertEqual(usage_request.get_header("Authorization"), "Bearer new-access")

    def test_startup_offline_then_recovery_after_persisted_backoff(self):
        self.http.side_effect = urllib.error.URLError("offline")
        self.assertFalse(usage.fetch_snapshot(self.cfg).ok)
        state = appdata.load_state()["refresh"]
        self.assertEqual(state["failures"], 1)
        self.http.reset_mock()
        self.assertFalse(usage.fetch_snapshot(self.cfg).ok)
        self.http.assert_not_called()
        self.http.side_effect = [self.token_response(), self.usage_response()]
        with patch("usage.time.time", return_value=state["next_attempt_at"] + 1):
            snap = usage.fetch_snapshot(self.cfg)
        self.assertTrue(snap.ok, snap.error)
        self.assertEqual(usage._refresh_state()["failures"], 0)

    def test_rate_limit_respects_retry_after_without_spending_token_again(self):
        self.http.side_effect = self.http_error(429, {"Retry-After": "7200"})
        before = time.time()
        self.assertFalse(usage.fetch_snapshot(self.cfg).ok)
        self.assertGreaterEqual(usage._refresh_state()["next_attempt_at"], before + 7200)
        self.assertFalse(usage.fetch_snapshot(self.cfg).ok)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(usage._read_oauth_creds()["refreshToken"], "old-refresh")

    def test_401_refreshes_once_and_retries_usage(self):
        self.save_creds(expires_in=3600)
        self.http.side_effect = [self.http_error(401), self.token_response(), self.usage_response()]
        snap = usage.fetch_snapshot(self.cfg)
        self.assertTrue(snap.ok, snap.error)
        self.assertEqual(self.http.call_count, 3)

    def test_repeated_401_does_not_loop(self):
        self.save_creds(expires_in=3600)
        self.http.side_effect = [self.http_error(401), self.token_response(), self.http_error(401)]
        self.assertFalse(usage.fetch_snapshot(self.cfg).ok)
        self.assertEqual(self.http.call_count, 3)

    def test_valid_token_only_fetches_usage(self):
        self.save_creds(expires_in=3600)
        self.http.return_value = self.usage_response()
        self.assertTrue(usage.fetch_snapshot(self.cfg).ok)
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.args[0].full_url, usage.OAUTH_USAGE_URL)

    def test_preexpiry_refresh_failure_uses_still_valid_token(self):
        self.save_creds(expires_in=90)
        self.http.side_effect = [urllib.error.URLError("offline"), self.usage_response()]
        self.assertTrue(usage.fetch_snapshot(self.cfg).ok)
        self.assertEqual(self.http.call_args.args[0].get_header("Authorization"), "Bearer old-access")

    def test_auto_refresh_can_be_disabled(self):
        self.cfg["auto_refresh"] = False
        self.assertFalse(usage.fetch_snapshot(self.cfg).ok)
        self.http.assert_not_called()

    def test_token_updated_by_another_process_is_reused(self):
        old = usage._read_oauth_creds()
        self.save_creds(expires_in=3600, access="other-access", refresh="other-refresh")
        result = usage._refresh_oauth_token(old, self.cfg)
        self.assertEqual(result["accessToken"], "other-access")
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
