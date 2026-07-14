#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""爱心屋 (aixinwu.sjtu.edu.cn) 每日自动签到 —— 纯 HTTP 版。

登录即签到：完成一次 jaccount OIDC 登录（拿到 access token）即触发余额事件
CONSECUTIVE_LOGIN 发放爱心币。整条链路（经 HAR 抓包 + 真机逐请求验证）：

  1. OIDCRedirect(externalAuthenticationUrl)  取 jaccount 授权 URL + state
  2. GET 授权 URL（跟随 302）落到 jalogin，解析 sid/client/returl/se 与
     loginContext.uuid，Session 存 JSESSIONID
  3. GET /jaccount/captcha?uuid=<页面uuid>&t=<ms>  取验证码图片 → OCR
  4. POST /jaccount/ulogin  提交账号/密码/验证码 → 拿 JAAuthCookie
  5. GET 回跳链  → redirectback?code=<code>
  6. OIDCTokenFetch(externalObtainAccessTokens)  code+state 换 access token
  7. me { ... }  读余额/连续天数/isPoor

关键：验证码 uuid 必须用 jalogin 页面 loginContext.uuid（非随机值），并带 t 时间戳，
否则 jaccount 校验的是另一张图，识别再准也判错。OCR 用交大 geek ResNet 在线识别
（专为 jaccount 训练，高精度）为主，本地 ddddocr 离线兜底。

配置来源：环境变量优先（AIXINWU_USERNAME / AIXINWU_PASSWORD / PUSHPLUS_TOKEN），
本地缺省回落 config.py。成功退出 0，失败退出非 0。
"""

import json
import logging
import os
import re
import sys
import time
from urllib.parse import parse_qs, urljoin, urlparse

import requests

try:  # 通知模块可选
    import notify
except Exception:  # pragma: no cover
    notify = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aixinwu")

# ---------------------------------------------------------------------------
# 配置：环境变量优先，回落本地 config.py
# ---------------------------------------------------------------------------
try:
    import config as _local_config  # 本地运行用；CI 无此文件
except Exception:
    _local_config = None


def _cfg(name, default=None):
    val = os.environ.get(name)
    if val:
        return val
    if _local_config is not None:
        return getattr(_local_config, name, default)
    return default


USERNAME = _cfg("AIXINWU_USERNAME")
PASSWORD = _cfg("AIXINWU_PASSWORD")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
API = "https://aixinwu.sjtu.edu.cn/axw-api-v1/"
JACCOUNT = "https://jaccount.sjtu.edu.cn"
CAPTCHA_URL = JACCOUNT + "/jaccount/captcha"
ULOGIN_URL = JACCOUNT + "/jaccount/ulogin"
PLUGIN_ID = "aixinwu.authentication.openidconnect"
REDIRECT_URI = "https://aixinwu.sjtu.edu.cn/oauth/redirectback"
GEEK_SOLVER = "https://geek.sjtu.edu.cn/captcha-solver/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

MAX_ATTEMPTS = 5
# jaccount 验证码答案在服务端异步绑定，抓图后需等待再提交，否则「读对也判错」。
# 实测：无延迟成功率 ~1/3，延迟 2s 后 ~100%。
CAPTCHA_SUBMIT_DELAY = 2.0
SAVE_CAPTCHA = os.environ.get("AIXINWU_SAVE_CAPTCHA", "").lower() in {
    "1", "true", "yes", "on",
}

# GraphQL 文档（operationName 与字段沿用抓包实测）
Q_OIDC_REDIRECT = """
mutation OIDCRedirect($input: JSONString!, $pluginId: String!) {
  externalAuthenticationUrl(input: $input, pluginId: $pluginId) {
    authenticationData
    errors { field message }
  }
}
"""

Q_OIDC_TOKEN = """
mutation OIDCTokenFetch($input: JSONString!, $pluginId: String!) {
  externalObtainAccessTokens(input: $input, pluginId: $pluginId) {
    token
    refreshToken
    csrfToken
    user { id email firstName balance continuous }
    errors { field message }
  }
}
"""

Q_ME = """
query UserBasicInfo {
  me { email firstName balance continuous isPoor }
}
"""


class PermanentAuthError(Exception):
    """账号/密码错误或被锁定——不应重试。"""


class RetryableAuthError(Exception):
    """验证码、Cookie 或 OAuth 回跳的瞬时失败——可以用新会话重试。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def gql(session, operation_name, query, variables, token=None):
    """统一 GraphQL POST；出错抛异常。"""
    headers = {"Content-Type": "application/json", "Origin": "https://aixinwu.sjtu.edu.cn"}
    if token:
        headers["Authorization"] = "Bearer " + token
    resp = session.post(
        API,
        json={"operationName": operation_name, "query": query, "variables": variables},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL {operation_name} 报错: {payload['errors']}")
    return payload["data"]


def _save_captcha(img, tag):
    """仅在本地显式启用时保存验证码；公开 CI 默认不落盘。"""
    if not SAVE_CAPTCHA:
        return
    try:
        with open(f"captcha_{tag}.png", "wb") as f:
            f.write(img)
    except Exception:
        pass


_ocr_engine = None


def _norm_captcha(text):
    text = re.sub(r"[^a-z0-9]", "", (text or "").strip().lower())
    return text if 4 <= len(text) <= 6 else None


def ocr_captcha(img):
    """先交大 geek ResNet 在线识别（高精度），失败再本地 ddddocr 兜底。"""
    global _ocr_engine

    # 主：geek 在线 ResNet（专为 jaccount 训练）
    try:
        r = requests.post(
            GEEK_SOLVER,
            files={"image": ("captcha.jpg", img, "image/jpeg")},
            timeout=20,
        )
        r.raise_for_status()
        text = _norm_captcha(r.json().get("result"))
        if text:
            log.info("geek 验证码识别成功")
            return text
        log.info("geek 结果不合法，转本地 ddddocr")
    except Exception as e:
        log.warning("geek 在线识别失败，转本地 ddddocr: %s", e)

    # 兜底：本地 ddddocr（离线）
    try:
        import ddddocr
        if _ocr_engine is None:
            _ocr_engine = ddddocr.DdddOcr(show_ad=False)
        text = _norm_captcha(_ocr_engine.classification(img))
        if text:
            log.info("ddddocr 验证码识别成功")
            return text
    except Exception as e:
        log.warning("ddddocr 兜底失败: %s", e)
    return None


# ---------------------------------------------------------------------------
# 登录链路
# ---------------------------------------------------------------------------
def _get_authorization_url(session):
    """步骤 1：拿 jaccount 授权 URL 及其中的 state。"""
    data = gql(
        session,
        "OIDCRedirect",
        Q_OIDC_REDIRECT,
        {"input": json.dumps({"redirectUri": REDIRECT_URI}), "pluginId": PLUGIN_ID},
    )
    node = data["externalAuthenticationUrl"]
    if node.get("errors"):
        raise RuntimeError(f"externalAuthenticationUrl 报错: {node['errors']}")
    auth_data = json.loads(node["authenticationData"])
    authorization_url = auth_data["authorizationUrl"]
    state_list = parse_qs(urlparse(authorization_url).query).get("state")
    if not state_list:
        raise RuntimeError("授权 URL 中缺少 state")
    return authorization_url, state_list[0]


def _reach_jalogin(session, authorization_url):
    """步骤 2：GET 授权 URL 跟随重定向到 jalogin，解析登录表单参数与页面 uuid。"""
    resp = session.get(authorization_url, allow_redirects=True, timeout=30)
    jalogin_url = resp.url
    html = resp.text or ""

    params = {}
    query = parse_qs(urlparse(jalogin_url).query)
    for key in ("sid", "client", "returl", "se"):
        if query.get(key):
            params[key] = query[key][0]
    # 兜底：参数若在页面 HTML 隐藏域里，正则提取
    if not all(k in params for k in ("sid", "client", "returl", "se")):
        for key in ("sid", "client", "returl", "se"):
            if key in params:
                continue
            m = re.search(r'name=["\']%s["\'][^>]*value=["\']([^"\']*)["\']' % key, html) \
                or re.search(r'value=["\']([^"\']*)["\'][^>]*name=["\']%s["\']' % key, html)
            if m:
                params[key] = m.group(1)
    missing = [k for k in ("sid", "client", "returl", "se") if k not in params]
    if missing:
        safe_url = urlparse(jalogin_url)._replace(query="", fragment="").geturl()
        raise RuntimeError(f"jalogin 参数缺失: {missing}（落地地址: {safe_url}）")

    # 页面服务端下发的验证码会话 uuid（loginContext.uuid），必须用它，不能自造
    m = re.search(r'uuid:\s*["\']([0-9a-fA-F-]+)["\']', html)
    if not m:
        raise RuntimeError("jalogin 页面未找到 loginContext.uuid")
    page_uuid = m.group(1)
    return jalogin_url, params, page_uuid


def _fetch_captcha(session, jalogin_url, page_uuid):
    """步骤 3：用页面 uuid + t 时间戳取验证码图片。"""
    resp = session.get(
        CAPTCHA_URL,
        params={"uuid": page_uuid, "t": int(time.time() * 1000)},
        headers={"Referer": jalogin_url},
        timeout=30,
    )
    return resp.content


def _exchange_token(session, code, state):
    """步骤 6：code + state 换 access token。"""
    data = gql(
        session,
        "OIDCTokenFetch",
        Q_OIDC_TOKEN,
        {"input": json.dumps({"code": code, "state": state}), "pluginId": PLUGIN_ID},
    )
    node = data["externalObtainAccessTokens"]
    if node.get("errors"):
        raise RuntimeError(f"externalObtainAccessTokens 报错: {node['errors']}")
    token = node.get("token")
    if not token:
        raise RuntimeError("换取 access token 失败：返回为空")
    return token, node.get("user") or {}


def _fetch_code(session, next_url, referer):
    """步骤 5：带 JAAuthCookie 手动跟随回跳链，从 Location 取 OAuth code。"""
    url = urljoin(JACCOUNT, next_url)
    headers = {
        "Referer": referer,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
    }

    # 不自动跟随：授权码通常出现在跨站 302 的 Location 中，直接截取更稳定，
    # 也避免把短期 code 继续带到不必要的页面或错误日志。
    for _hop in range(10):
        code_list = parse_qs(urlparse(url).query).get("code")
        if code_list:
            return code_list[0]

        resp = session.get(url, headers=headers, allow_redirects=False, timeout=30)
        location = resp.headers.get("Location")
        if location:
            headers["Referer"] = urlparse(url)._replace(query="", fragment="").geturl()
            url = urljoin(url, location)
            continue

        if urlparse(resp.url).path.rstrip("/").endswith("/jaccount/jalogin"):
            raise RetryableAuthError("OAuth 回跳重新落到登录页，登录 Cookie 未生效")
        raise RetryableAuthError(
            f"OAuth 回跳未返回授权码（HTTP {resp.status_code}）"
        )

    raise RetryableAuthError("OAuth 回跳次数过多，未返回授权码")


def _attempt_once(session):
    """单次完整登录尝试（链路 1-6）。

    返回 access token；验证码错误返回 None（可重试）；账号/密码错误抛 PermanentAuthError。
    """
    authorization_url, state = _get_authorization_url(session)
    jalogin_url, params, page_uuid = _reach_jalogin(session, authorization_url)

    img = _fetch_captcha(session, jalogin_url, page_uuid)
    captcha = ocr_captcha(img)
    if not captcha:
        _save_captcha(img, "ocrfail")
        return None

    # 等待服务端完成验证码答案绑定，否则提交过快会被判「验证码错误」
    time.sleep(CAPTCHA_SUBMIT_DELAY)

    form = {
        "sid": params["sid"],
        "client": params["client"],
        "returl": params["returl"],
        "se": params["se"],
        "v": "",
        "uuid": page_uuid,
        "user": USERNAME,
        "pass": PASSWORD,
        "captcha": captcha,
        "lt": "p",
    }
    login_resp = session.post(
        ULOGIN_URL,
        data=form,
        headers={
            "Referer": jalogin_url,
            "Origin": JACCOUNT,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=30,
    )
    login_resp.raise_for_status()
    resp = login_resp.json()

    if resp.get("errno") != 0:
        err = resp.get("error") or f"errno={resp.get('errno')}"
        _save_captcha(img, "wrong")
        if any(kw in str(err) for kw in ("密码", "用户名", "账号", "锁定", "冻结", "禁用")):
            raise PermanentAuthError(err)
        log.warning("验证码校验失败: %s", _safe_error(err))
        return None

    if not any(cookie.name == "JAAuthCookie" for cookie in session.cookies):
        log.warning("JAccount 登录响应未下发认证 Cookie，将使用新会话重试")
        return None

    next_url = resp.get("url")
    if not next_url:
        log.warning("JAccount 登录响应缺少回跳地址，将使用新会话重试")
        return None

    try:
        code = _fetch_code(session, next_url, jalogin_url)
    except RetryableAuthError as e:
        log.warning("%s，将使用新会话重试", _safe_error(e))
        return None
    token, _user = _exchange_token(session, code, state)
    return token


def login():
    """多次尝试登录（每次全新会话跑完整链路），返回 access token。"""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        session = new_session()
        token = _attempt_once(session)  # PermanentAuthError 直接向上抛
        if token:
            log.info("登录成功（第 %d 次尝试，登录=签到完成）", attempt)
            return token
        log.info("第 %d 次尝试未通过，重试", attempt)
    raise RuntimeError(f"登录重试 {MAX_ATTEMPTS} 次仍失败（验证码/网络）")


def get_me(token):
    """步骤 7：读账户余额/连续天数/isPoor。"""
    data = gql(new_session(), "UserBasicInfo", Q_ME, {}, token=token)
    return data["me"]


# ---------------------------------------------------------------------------
# 输出 / 通知
# ---------------------------------------------------------------------------
def _write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def _notify(title, content):
    if notify is None:
        return
    try:
        notify.send(title, content)
    except Exception as e:
        log.warning("推送失败: %s", e)


def _safe_error(err):
    """清除错误文本中可能进入公开 Actions 日志的凭据和 URL 查询参数。"""
    text = str(err)
    for name in ("AIXINWU_USERNAME", "AIXINWU_PASSWORD", "PUSHPLUS_TOKEN"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "[REDACTED]")
    for value in (USERNAME, PASSWORD):
        if value:
            text = text.replace(str(value), "[REDACTED]")
    text = re.sub(
        r"(?i)(https?://[^\s?]+)\?[^\s)\]}]+",
        r"\1?[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(bearer\s+|(?:token|password|passwd|pass|cookie|authorization)\s*[:=]\s*)"
        r"[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    return text[:1000]


def report_success(me):
    name = me.get("firstName") or me.get("email") or USERNAME
    balance = me.get("balance")
    continuous = me.get("continuous")
    is_poor = me.get("isPoor")
    body = "\n".join([
        "✅ 爱心屋签到成功",
        f"账号：{name}",
        f"余额（爱心币）：{balance}",
        f"连续登录天数：{continuous}",
        f"isPoor：{is_poor}",
    ])
    # Actions 日志和 Summary 在公开仓库中可见，因此只写非个人化状态。
    log.info("爱心屋签到成功")
    _write_summary("## ✅ 爱心屋签到成功\n\n本次定时任务已正常完成。")
    _notify("爱心屋签到成功", body)


def report_failure(err):
    safe_err = _safe_error(err)
    body = f"❌ 爱心屋签到失败\n原因：{safe_err}"
    log.error(body.replace("\n", " | "))
    _write_summary(f"## ❌ 爱心屋签到失败\n\n- 原因：`{safe_err}`")
    _notify("爱心屋签到失败", body)


def main():
    if not USERNAME or not PASSWORD:
        report_failure("缺少 AIXINWU_USERNAME / AIXINWU_PASSWORD 配置")
        return 1
    try:
        token = login()
        me = get_me(token)
        report_success(me)
        return 0
    except Exception as e:
        report_failure(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
