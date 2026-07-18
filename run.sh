#!/usr/bin/env bash
# VPS 上由 cron 调用：激活虚拟环境执行签到，输出追加到 checkin.log。
# 用法（cron）：13 9 * * * /bin/bash /home/<user>/aixinwu_checkin/run.sh
# 依赖：同目录下已建好虚拟环境 venv/，并已 cp config_template.py config.py 填好凭据。

cd "$(dirname "$0")" || exit 1

PY="./venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

echo "===== $(date '+%F %T %Z') 开始签到 =====" >> checkin.log
"$PY" aixinwu.py >> checkin.log 2>&1
rc=$?
echo "===== $(date '+%F %T %Z') 结束（退出码 $rc） =====" >> checkin.log
exit "$rc"
