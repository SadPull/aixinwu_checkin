# 爱心屋每日自动签到

通过 GitHub Actions 定时完成上海交通大学爱心屋登录签到，可选通过 PushPlus 推送结果。
仓库源码可以公开，但真实 JAccount Cookie、账号和密码必须只保存在 GitHub Actions
Secrets 中。

## 安全设计

- 源码和配置模板不包含真实 Cookie、账号、密码或 token。
- 签到工作流只支持定时运行和手动运行，不会在 Pull Request 中使用 Secrets。
- GitHub Token 权限限制为 `contents: read`，检出完成后不保留 Git 凭据。
- 官方 GitHub Actions 固定到具体提交，Python 依赖固定版本并由 Dependabot 检查更新。
- 公开 Actions 日志和 Summary 只显示任务状态，不输出账号、姓名、邮箱、余额或登录参数。
- PushPlus 使用 HTTPS；未配置 `PUSHPLUS_TOKEN` 时不会向 PushPlus 发送请求。
- 验证码图片默认不保存，也不会作为公开 Actions artifact 上传。

GitHub Secrets 并不等于绝对安全：默认分支上的工作流代码和运行时依赖能够读取注入的
Secrets。请保护默认分支、限制仓库写权限，并认真审核依赖更新和外部贡献。

## 部署到 GitHub Actions

### 1. 发布仓库

创建 GitHub 仓库并推送源码。公开仓库中不得包含 `config.py`、`.env`、HAR 抓包、
Cookie、token 或其他登录调试文件。

首次提交前建议检查：

```powershell
git init
git add .
git status --short

# 此命令必须没有输出
git ls-files | rg '(\.har$|config\.py$|\.env$|captcha\.js$|__pycache__|\.pyc$)'
```

不要使用 `git add -f` 强制添加被忽略的敏感文件。

### 2. 配置 Secrets

进入仓库：

`Settings → Secrets and variables → Actions → New repository secret`

添加以下 Repository secrets：

| Secret 名 | 必填 | 说明 |
|---|---:|---|
| `AIXINWU_JACCOUNT_COOKIE` | 二选一 | 推荐；仅填写本地浏览器中的 `JAAuthCookie` 值 |
| `AIXINWU_USERNAME` | 二选一 | 未使用 Cookie 模式时填写 JAccount 用户名 |
| `AIXINWU_PASSWORD` | 二选一 | 未使用 Cookie 模式时与用户名一起填写 |
| `PUSHPLUS_TOKEN` | 否 | PushPlus token；不配置则不推送 |

登录配置有两种模式：推荐只配置 `AIXINWU_JACCOUNT_COOKIE`；或者不配置 Cookie，改为同时
配置用户名和密码。如果三项都存在，Cookie 模式优先；Cookie 失效后任务会直接失败并提醒，
不会自动回退密码登录。

### 3. 获取 JAAuthCookie（推荐模式）

1. 在自己的电脑上用浏览器正常登录 JAccount 并进入爱心屋。
2. 打开浏览器开发者工具，在 Application（应用）/ Storage（存储）的 Cookies 中选择
   `https://jaccount.sjtu.edu.cn`。
3. 只复制名为 `JAAuthCookie` 的 Cookie 值，保存为 Repository Secret
   `AIXINWU_JACCOUNT_COOKIE`。
4. 不要复制完整 Cookie Header，不要保存 `JSESSIONID`、`JAVisitedSites`、`refreshToken`，
   也不要上传 HAR 抓包。

`JAAuthCookie` 可直接代表登录状态，敏感程度不低于密码。其有效期由 JAccount 决定；失效
或被异地风控拒绝后，需要回到本地重新登录并更新 Secret。GitHub 托管 Runner 的出口 IP
会变化，因此该方案只能减少重复登录，不能保证绕过异地验证。

不要把这些值配置成普通 GitHub Variables，也不要写入 workflow、README、Issue 或 Actions
日志。Secrets 的值在工作流中通过环境变量注入，源码不会保存它们。

### 4. 启用并验证

打开仓库的 Actions 页面，选择“爱心屋签到”，点击 `Run workflow` 手动运行一次。
验证成功后，工作流每天按北京时间约 09:13 和 17:13 各运行一次，第二次运行用于降低
偶发网络或验证码失败造成的漏签概率。

公开仓库的 Actions 日志任何人都可能看到。本项目只在日志中输出非个人化的成功或失败
状态；详细账号信息仅在你主动配置 PushPlus 后通过 HTTPS 发送。

## 工作原理

脚本使用 `requests.Session` 完成 JAccount OIDC 登录。配置 Cookie 时：

1. 从爱心屋接口获取 JAccount 授权地址和 OIDC state。
2. 将 `JAAuthCookie` 仅绑定到 `jaccount.sjtu.edu.cn`，手动跟随授权重定向取得 code。
3. 交换爱心屋 access token 并查询签到结果。
4. 若重定向落到登录页或附加验证页，立即失败并提醒更新 Cookie，不提交账号密码。

未配置 Cookie 时保留原有账号密码流程：

1. 从爱心屋接口获取 JAccount 授权地址和 OIDC state。
2. 进入 JAccount 登录页，解析登录表单参数和验证码会话 UUID。
3. 获取验证码，优先使用交大 `geek.sjtu.edu.cn` 识别，失败时使用本地 ddddocr。
4. 提交账号、密码和验证码。
5. 跟随 OAuth 回跳取得 code，并交换爱心屋 access token。
6. 查询签到是否成功；公开日志只记录任务状态。

所有 JAccount、爱心屋、验证码服务和 PushPlus 请求均使用 HTTPS。发送到验证码识别服务的
只有验证码图片，不包含 JAccount 账号、密码或 Session Cookie。

## 本地运行

安装依赖：

```powershell
python -m pip install -r requirements.txt
Copy-Item config_template.py config.py
```

然后只在本地编辑 `config.py`，选择填写 `AIXINWU_JACCOUNT_COOKIE`，或填写用户名和密码，
再运行：

```powershell
python aixinwu.py
```

`config.py` 已被 `.gitignore` 忽略。提交前仍应使用 `git status` 确认它没有被暂存。

如需在本地保存失败验证码用于排错，可临时设置 `AIXINWU_SAVE_CAPTCHA=1`。不要在公开
GitHub Actions 中启用或上传这些文件。

## 常见问题

- 缺少登录配置：检查 Cookie Secret，或用户名与密码两个 Secrets 是否完整配置。
- Cookie 会话失效或被风控拒绝：在本地重新登录并更新 `AIXINWU_JACCOUNT_COOKIE`；脚本不会
  自动回退密码登录。
- 账号或密码错误：脚本会停止重试，避免连续失败触发账号锁定。
- 验证码偶发失败：脚本会使用新会话重试，最多尝试 5 次。
- OAuth 回跳重新进入登录页：账号密码模式会换新会话重试；Cookie 模式会立即停止并要求更新
  `AIXINWU_JACCOUNT_COOKIE`。
- PushPlus 没有消息：确认 `PUSHPLUS_TOKEN` 已配置；未配置时会静默跳过。
- 定时任务没有运行：GitHub 可能延迟 schedule，长期无仓库活动时也可能暂停定时工作流。

## 安全事件处理

如果真实 Cookie、密码、token 或 HAR 抓包曾被提交，即使随后删除，敏感内容仍可能存在于 Git
历史中。请立即轮换相关凭据并清理历史。更多说明见 [SECURITY.md](SECURITY.md)。

## 免责声明

本项目仅用于个人账户自动化。使用前请确认符合相关服务的使用规则，并自行承担账号风控、
接口变更和第三方服务可用性风险。
