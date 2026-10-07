# 棋牌服务器内网穿透

本机编译服务器生成 `server.tar.gz`，Actions 使用 Node.js 22 安装生产依赖并直接运行，同时运行已有 Linux amd64 隧道二进制。使用 go-tunnel 的第二个 client，将 `127.0.0.1:9527` 映射到 `120.77.176.120:9527`，支持 Socket.IO 的 HTTP 与 WebSocket TCP 流量。无需 Docker 或 Go 编译环境。

`ci.yml` 单轮最多 360 分钟，在运行脚本开始 355 分钟后请求下一轮；同一并发组取消旧轮并清理服务和隧道进程。`ci-watchdog.yml` 每 10 分钟检查，没有运行中或等待中的任务时补启。runner 交接会断开现有连接并清空内存中的房间状态，客户端需重新连接。

## 配置版本化与部署

- 项目根目录 `PAT`：部署目标 GitHub 账号的令牌。
- `my-server/mysql.json`：数据库配置，字段为 `host`、`user`、`password`、`database`、`port`。服务器也支持 `DB_HOST`、`DB_USER`、`DB_PASSWORD`、`DB_NAME`、`DB_PORT` 环境变量，优先于文件配置。
- 本目录 `client.json`：第二个 client 的完整配置，`udid`、`token` 为真实值。
- 上述配置、PAT、隧道二进制和 `server.tar.gz` 随私有 Gitee 仓库版本化，依赖和临时构建目录仍忽略；每轮修改及构建后的运行包变更使用中文信息提交。
- GitHub 只保留公开运行文件。部署脚本在内存中将 `client.json` 的身份与令牌替换为占位符再上传；运行时由 Actions Secrets 生成临时配置。

按发布技能确认本次构建后，在项目根目录执行：

```powershell
python my-server/tunnel/build.py --source .
python my-server/tunnel/deploy.py --source . --dry-run
python my-server/tunnel/deploy.py --source .
```

运行包内只有编译后的 `dist/*.js`、`package.json` 和依赖锁文件。

部署脚本用 `PAT` 调用 `/user` 确认账号，在该账号下创建或更新公开 `Ai-qiPai` 仓库，配置 `GH_PAT`、`TUNNEL_UDID`、`TUNNEL_TOKEN` 和五个 `DB_*` Secrets，只上传固定的运行文件，以中文信息创建提交。不会向 GitHub 上传服务器源码、PAT、数据库配置、真实隧道身份与令牌或 Go 源码。

首次只部署时保持仓库 Actions 关闭。确认要启动后执行：

```powershell
python my-server/tunnel/deploy.py --source . --run
```

`--run` 启用 Actions 与两个工作流并启动 `ci.yml`。之后仅部署不停止已运行的服务。修改服务器代码后，按项目的 `server-build-deploy` 技能先询问是否构建服务端运行包并运行新 Actions。

二进制从 `D:/workspace/go-tunnel/dist/tunnel-client-linux` 复制，校验值见 `tunnel-client-linux.sha256`；本项目和 Actions 均不编译 Go。

## 停止持续运行

先在 GitHub Actions 禁用 `ci-watchdog.yml`，再取消 `ci.yml` 的当前运行；只取消主工作流会被 watchdog 再次补启。
