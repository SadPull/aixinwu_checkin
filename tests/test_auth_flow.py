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
    def __init__(self, url, status=302, location=None, payload=None, text=""):
        self.url = url
        self.status_code = status
        self.headers = {}
        self._payload = payload or {}
        self.text = text
        if location is not None:
            self.headers["Location"] = location

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None, cookies=None):
        self.responses = list(responses or [])
        self.cookies = CookieJar(cookies or [])
        self.calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return FakeResponse(
            aixinwu.ULOGIN_URL,
            status=200,
            payload={"errno": 0, "url": "/jaccount/jalogin?sid=x"},
        )


class Cookie:
    def __init__(self, name, value=None, **kwargs):
        self.name = name
        self.value = value
        self.domain = kwargs.get("domain")
        self.path = kwargs.get("path")
        self.secure = kwargs.get("secure", False)


class CookieJar(list):
    def set(self, name, value, **kwargs):
        self.append(Cookie(name, value, **kwargs))


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


class CookieLoginTests(unittest.TestCase):
    def test_valid_cookie_gets_code_without_password_login(self):
        session = FakeSession([
            FakeResponse(
                "https://jaccount.sjtu.edu.cn/jaccount/oauth2/authorize",
                location=(
                    "https://aixinwu.sjtu.edu.cn/oauth/redirectback"
                    "?code=cookie-code&state=test-state"
                ),
            )
        ])

        with mock.patch.object(
            aixinwu,
            "_get_authorization_url",
            return_value=(
                "https://jaccount.sjtu.edu.cn/jaccount/oauth2/authorize?state=test-state",
                "test-state",
            ),
        ), mock.patch.object(
            aixinwu,
            "_exchange_token",
            return_value=("access-token", {}),
        ) as exchange:
            token = aixinwu._attempt_cookie_login(session, "cookie-secret")

        self.assertEqual(token, "access-token")
        self.assertEqual(session.post_calls, [])
        self.assertEqual(exchange.call_args.args[1:], ("cookie-code", "test-state"))
        cookie = session.cookies[0]
        self.assertEqual(cookie.name, "JAAuthCookie")
        self.assertEqual(cookie.value, "cookie-secret")
        self.assertEqual(cookie.domain, "jaccount.sjtu.edu.cn")
        self.assertEqual(cookie.path, "/")
        self.assertTrue(cookie.secure)

    def test_invalid_cookie_fails_without_password_fallback(self):
        session = FakeSession([
            FakeResponse(
                "https://jaccount.sjtu.edu.cn/jaccount/jalogin?sid=sensitive",
                status=200,
            )
        ])

        with mock.patch.object(
            aixinwu,
            "_get_authorization_url",
            return_value=(
                "https://jaccount.sjtu.edu.cn/jaccount/oauth2/authorize?state=secret-state",
                "secret-state",
            ),
        ), self.assertRaises(aixinwu.CookieSessionError) as raised:
            aixinwu._attempt_cookie_login(session, "cookie-secret")

        self.assertEqual(session.post_calls, [])
        self.assertNotIn("cookie-secret", str(raised.exception))
        self.assertNotIn("secret-state", str(raised.exception))
        self.assertIn("会话失效或被风控拒绝", str(raised.exception))

    def test_full_cookie_header_is_rejected_before_network_request(self):
        session = FakeSession()

        with mock.patch.object(aixinwu, "_get_authorization_url") as authorize, \
                self.assertRaises(aixinwu.CookieSessionError) as raised:
            aixinwu._attempt_cookie_login(
                session,
                "JAAuthCookie=cookie-secret; JSESSIONID=session-secret",
            )

        authorize.assert_not_called()
        self.assertEqual(session.cookies, [])
        self.assertEqual(session.calls, [])
        self.assertNotIn("cookie-secret", str(raised.exception))
        self.assertIn("只能填写 JAAuthCookie 的值", str(raised.exception))

    def test_cookie_mode_takes_precedence_and_does_not_fallback(self):
        with mock.patch.object(aixinwu, "JACCOUNT_COOKIE", "cookie-secret"), \
                mock.patch.object(aixinwu, "new_session", return_value="cookie-session"), \
                mock.patch.object(
                    aixinwu,
                    "_attempt_cookie_login",
                    return_value="access-token",
                ) as cookie_login, \
                mock.patch.object(aixinwu, "_login_with_password") as password_login:
            self.assertEqual(aixinwu.login(), "access-token")

        cookie_login.assert_called_once_with("cookie-session", "cookie-secret")
        password_login.assert_not_called()


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
            self.assertEqual(aixinwu._login_with_password(), "token")
            self.assertEqual(attempt.call_count, 3)


class ConfigurationAndRedactionTests(unittest.TestCase):
    def test_cookie_only_configuration_is_accepted(self):
        with mock.patch.object(aixinwu, "JACCOUNT_COOKIE", "cookie-secret"), \
                mock.patch.object(aixinwu, "USERNAME", None), \
                mock.patch.object(aixinwu, "PASSWORD", None), \
                mock.patch.object(aixinwu, "login", return_value="token"), \
                mock.patch.object(aixinwu, "get_me", return_value={}), \
                mock.patch.object(aixinwu, "report_success") as success:
            self.assertEqual(aixinwu.main(), 0)

        success.assert_called_once_with({})

    def test_safe_error_redacts_cookie_and_authorization_query(self):
        message = (
            "cookie=cookie-secret "
            "https://jaccount.sjtu.edu.cn/oauth?code=oauth-code&state=oauth-state"
        )
        with mock.patch.object(aixinwu, "JACCOUNT_COOKIE", "cookie-secret"):
            safe = aixinwu._safe_error(message)

        self.assertNotIn("cookie-secret", safe)
        self.assertNotIn("oauth-code", safe)
        self.assertNotIn("oauth-state", safe)
        self.assertIn("[REDACTED]", safe)


if __name__ == "__main__":
    unittest.main()
