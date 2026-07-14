# -*- coding: utf-8 -*-
# 本地运行用：cp config_template.py config.py，然后填入真实值。
# GitHub Actions 用 Secrets 注入同名环境变量，不需要此文件（config.py 已被 .gitignore）。

AIXINWU_JACCOUNT_COOKIE = ""          # 推荐：JAAuthCookie 的值；配置后优先使用
AIXINWU_USERNAME = "your_jaccount"    # Cookie 留空时使用：jaccount 用户名
AIXINWU_PASSWORD = "your_password"    # Cookie 留空时使用：jaccount 密码
PUSHPLUS_TOKEN = ""                   # 可选：PushPlus token，留空则不推送
