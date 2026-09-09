# 主动服务流程自动化 — 回退 / 升级 SOP

> 适用对象：项目维护者（舒磊 / Codex 助手）
> 最后更新：2026-09-09（稳定点 `stable-2026-09-09` = `01852b5`）

## 1. 部署形态速览

| 项 | 值 |
|---|---|
| 代码仓库（主） | `git@git.in.chaitin.net:400/inspection-workflow.git`（GitLab） |
| 代码仓库（镜像） | `git@github.com:shulei1127-dot/inspection-workflow.git`（GitHub） |
| 工作分支 | `feature/review-audit-module` |
| dev-box | `10.2.36.228`，工作目录 `/data/inspect/inspection-workflow` |
| 容器 | `inspection-workflow-app-1`（镜像 `inspection-workflow-app:git-<sha>`）+ `inspection-workflow-db-1` |
| 对外域名 | `https://proactiflow.in.chaitin.net`（网关统一 SSO + 授权名单） |
| 健康检查 | `http://127.0.0.1:8100/api/health`（compose healthcheck） |

## 2. 稳定回退点

当前线上稳定提交：`01852b5`（OIDC 认证接入完成，域名 + IP+端口统一认证）。

- GitLab 标签：https://git.in.chaitin.net/400/inspection-workflow/-/tags/stable-2026-09-09
- GitHub 标签：https://github.com/shulei1127-dot/inspection-workflow/tree/stable-2026-09-09

每次发布新版本上线前，建议先打一个新标签（如 `stable-YYYY-MM-DD`）再部署。

## 3. 回退 SOP（改坏了怎么恢复）

在 dev-box 执行：

```bash
cd /data/inspect/inspection-workflow
git fetch --tags origin
git checkout -B rollback stable-2026-09-09     # 切到稳定点（分支名可自定）
git reset --hard stable-2026-09-09             # 可选：强制还原所有跟踪文件
APP_IMAGE_TAG=git-01852b5 docker compose up -d --build
```

验证：

```bash
docker ps --format "{{.Names}} {{.Status}} {{.Image}}" | grep inspection-workflow
curl -s http://127.0.0.1:8100/api/health
# 浏览器验证：域名与 IP+端口 登录均正常
```

> 不要执行 `git clean`：`.env`（含密钥）和 `static/`（前端构建产物）都不在 git 里，`git clean` 会误删导致服务起不来。

## 4. 发布/升级 SOP（正常上新版本）

1. 改代码并提交：
   ```bash
   cd /data/inspect/inspection-workflow
   git add -A && git commit -m "feat(xxx): 描述"
   git push origin feature/review-audit-module     # GitHub 镜像
   git push gitlab feature/review-audit-module     # GitLab（两个都要推）
   ```
2. 上线（先在 dev-box 拉取，再构建部署）：
   ```bash
   git pull --ff-only origin feature/review-audit-module
   NEW_SHA=$(git rev-parse --short HEAD)
   APP_IMAGE_TAG=git-${NEW_SHA} docker compose up -d --build
   ```
3. 验证：容器 `Up (healthy)`、`/api/health` 200、页面可登录、`/api/oauth/me` 返回登录人。
4. 确认稳定后补打标签：`git tag -a stable-YYYY-MM-DD -m "..." && git push origin stable-YYYY-MM-DD && git push gitlab stable-YYYY-MM-DD`

> CI（`.gitlab-ci.yml`）只跑测试，不会自动部署；部署始终由上面的 compose 命令完成。

## 5. 认证相关备忘

- 开关：`.env` 中 `OIDC_ENABLED=true/false`，改完 `docker compose up -d` 即生效（无需重建镜像）。
- 关键配置：`OIDC_CLIENT_ID/CLIENT_SECRET`、`OIDC_REDIRECT_URI=https://proactiflow.in.chaitin.net/callback`、`OIDC_SCOPE=profile`（该 client 不允许 openid）、认证方式 `client_secret_post`。
- 回调路径必须是 `/callback`（与 IdP 注册一致），改动路由时不要只留 `/oauth/callback`。
- 常见故障：
  - 登录死循环 → 检查 `/callback` 是否被中间件拦截（白名单含 `/callback`）。
  - `invalid_scope` → scope 只能为 `profile`。
  - 授权后被拒 → 账号未加入 IdP 授权名单，走「申请访问」由负责人审批。
- 密钥安全：`client_secret` 只放服务器 `.env`，不要提交仓库/聊天；外泄后联系运维轮换。

## 6. 运维常用命令

```bash
ssh dev-box
docker ps --format "{{.Names}} {{.Status}} {{.Image}}" | grep inspection-workflow
docker logs --tail 200 inspection-workflow-app-1          # 应用日志（含 OAuth login success 记录）
docker logs -f inspection-workflow-app-1
docker exec inspection-workflow-app-1 env | grep OIDC     # 查看容器内认证配置
curl -s http://127.0.0.1:8100/api/oauth/me               # 本机为受信来源，不会触发认证
```
