import sys
import types
import unittest
from unittest import mock


# 这些测试只验证控制流，不发网络请求；在导入项目代码前提供最小 requests 桩。
requests_stub = types.ModuleType("requests")
requests_stub.Session = mock.Mock
requests_stub.post = mock.Mock()
sys.modules["requests"] = requests_stub

import aixinwu  # noqa: E402


class FakeResponse:
    def __init__(self, url, status=302, location=None, payload=None):
        self.url = url
        self.status_code = status
        self.headers = {}
        self._payload = payload or {}
        if location is not None:
            self.headers["Location"] = location

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, cookies=None):
        self.responses = list(responses or [])
        self.cookies = list(cookies or [])
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def post(self, *args, **kwargs):
        return FakeResponse(
            aixinwu.ULOGIN_URL,
            status=200,
            payload={"errno": 0, "url": "/jaccount/jalogin?sid=x"},
        )


class Cookie:
    def __init__(self, name):
        self.name = name


class OAuthRedirectTests(unittest.TestCase):
    def test_extracts_code_from_redirect_location_without_following(self):
        session = FakeSession([
            FakeResponse(
                "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=x",
                location=(
                    "https://aixinwu.sjtu.edu.cn/oauth/redirectback"
                    "?code=test-code&state=test-state"
                ),
            )
        ])

        code = aixinwu._fetch_code(
            session,
            "/jaccount/jalogin?sid=x",
            "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=x",
        )

        self.assertEqual(code, "test-code")
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_follows_relative_redirect_before_extracting_code(self):
        session = FakeSession([
            FakeResponse(
                "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=x",
                location="/jaccount/oauth/continue",
            ),
            FakeResponse(
                "https://jaccount.sjtu.edu.cn/jaccount/oauth/continue",
                location=(
                    "https://aixinwu.sjtu.edu.cn/oauth/redirectback"
                    "?code=second-code"
                ),
            ),
        ])

        code = aixinwu._fetch_code(
            session,
            "/jaccount/jalogin?sid=x",
            "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=x",
        )

        self.assertEqual(code, "second-code")
        self.assertEqual(len(session.calls), 2)

    def test_return_to_login_page_is_retryable(self):
        session = FakeSession([
            FakeResponse(
                "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=x",
                status=200,
            )
        ])

        with self.assertRaises(aixinwu.RetryableAuthError):
            aixinwu._fetch_code(
                session,
                "/jaccount/jalogin?sid=x",
                "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=x",
            )


class LoginRetryTests(unittest.TestCase):
    def _attempt_patches(self):
        return mock.patch.multiple(
            aixinwu,
            _get_authorization_url=mock.Mock(return_value=("https://auth.example", "state")),
            _reach_jalogin=mock.Mock(return_value=(
                "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=x",
                {"sid": "s", "client": "c", "returl": "r", "se": "e"},
                "uuid",
            )),
            _fetch_captcha=mock.Mock(return_value=b"image"),
            ocr_captcha=mock.Mock(return_value="abcd"),
        )

    def test_missing_auth_cookie_returns_retryable_result(self):
        with self._attempt_patches(), mock.patch.object(aixinwu.time, "sleep"):
            self.assertIsNone(aixinwu._attempt_once(FakeSession()))

    def test_redirect_failure_returns_retryable_result(self):
        with self._attempt_patches(), mock.patch.object(aixinwu.time, "sleep"), mock.patch.object(
            aixinwu,
            "_fetch_code",
            side_effect=aixinwu.RetryableAuthError("retry"),
        ):
            session = FakeSession(cookies=[Cookie("JAAuthCookie")])
            self.assertIsNone(aixinwu._attempt_once(session))

    def test_login_retries_with_fresh_sessions(self):
        attempts = iter([None, None, "token"])
        with mock.patch.object(aixinwu, "new_session", side_effect=[object(), object(), object()]), \
                mock.patch.object(aixinwu, "_attempt_once", side_effect=attempts) as attempt:
            self.assertEqual(aixinwu.login(), "token")
            self.assertEqual(attempt.call_count, 3)


if __name__ == "__main__":
    unittest.main()
