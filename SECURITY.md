# 安全政策

## 凭据处理

本项目不会在源码中保存真实 Cookie、账号、密码或 PushPlus token。部署到 GitHub Actions 时，
请仅通过仓库的 `Settings → Secrets and variables → Actions` 配置以下 Secrets：

- `AIXINWU_JACCOUNT_COOKIE`（推荐；仅保存 `JAAuthCookie` 的值）
- `AIXINWU_USERNAME`
- `AIXINWU_PASSWORD`
- `PUSHPLUS_TOKEN`（可选）

Cookie 模式与账号密码模式二选一即可。若配置 Cookie，脚本不会在 Cookie 失效后自动回退到
密码登录。Cookie 属于可直接代表登录状态的高敏感凭据，其保护等级不得低于账号密码。

请勿提交 `config.py`、`.env`、HAR 抓包、Cookie、访问令牌或验证码调试文件。

## 报告安全问题

如果发现凭据泄露、日志脱敏失效或依赖供应链问题，请不要创建包含敏感信息的公开 Issue。
请通过 GitHub 的 Private vulnerability reporting 功能私下报告。

## 泄露处置

如果 Cookie、账号密码、PushPlus token 或登录抓包曾进入 Git 历史，请立即轮换相关凭据；仅删除
当前分支中的文件并不能清除历史记录。
