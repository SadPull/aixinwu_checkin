# -*- coding: utf-8 -*-
"""PushPlus 微信推送（可选）。

配置了 PUSHPLUS_TOKEN（环境变量优先，回落 config.py）才推送，否则静默跳过。
"""

import logging
import os

import requests

log = logging.getLogger("aixinwu.notify")

PUSHPLUS_API = "https://www.pushplus.plus/send"


def _token():
    val = os.environ.get("PUSHPLUS_TOKEN")
    if val:
        return val
    try:
        import config
        return getattr(config, "PUSHPLUS_TOKEN", None)
    except Exception:
        return None


def send(title, content):
    token = _token()
    if not token:
        log.info("未配置 PUSHPLUS_TOKEN，跳过微信推送")
        return
    try:
        resp = requests.post(
            PUSHPLUS_API,
            json={"token": token, "title": title, "content": content, "template": "txt"},
            timeout=20,
            allow_redirects=False,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") not in (200, "200"):
            raise RuntimeError("PushPlus 返回业务错误")
        log.info("PushPlus 推送完成（HTTP %s）", resp.status_code)
    except Exception as e:
        log.warning("PushPlus 推送失败: %s", e)
