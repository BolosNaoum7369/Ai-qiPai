# 棋牌服务器内网穿透

本机编译服务器生成 `server.tar.gz`，Actions 使用 Node.js 22 安装生产依赖并直接运行，同时运行已有 Linux amd64 隧道二进制。使用 go-tunnel 的第二个 client，将 `127.0.0.1:9527` 映射到 `120.77.176.120:9527`，支持 Socket.IO 的 HTTP 与 WebSocket TCP 流量。无需 Docker 或 Go 编译环境。

`ci.yml` 单轮最多 360 分钟，在运行脚本开始 355 分钟后请求下一轮；同一并发组取消旧轮并清理服务和隧道进程。`ci-watchdog.yml` 每 10 分钟检查，没有运行中或等待中的任务时补启。runner 交接会断开现有连接并清空内存中的房间状态，客户端需重新连接。

## 本机配置与部署

- 项目根目录 `PAT`：部署目标 GitHub 账号的令牌。
- `my-server/mysql.local.json`：本机数据库配置，字段为 `host`、`user`、`password`、`database`、`port`，被 Git 忽略。服务器也支持 `DB_HOST`、`DB_USER`、`DB_PASSWORD`、`DB_NAME`、`DB_PORT` 环境变量，优先于本机配置。
- 本目录 `client.local.json`：与 `client.json` 相同结构，其中 `udid`、`token` 填第二个 client 的真实值。该文件被 Git 忽略。
- 公开的 `client.json` 只保存端口、节点与占位符，运行时由 `TUNNEL_UDID`、`TUNNEL_TOKEN` Actions Secrets 生成临时配置。

按发布技能确认本次构建后，在项目根目录执行：

```powershell
python my-server/tunnel/build.py --source .
python my-server/tunnel/deploy.py --source . --dry-run
python my-server/tunnel/deploy.py --source .
```

运行包内只有编译后的 `dist/*.js`、`package.json` 和依赖锁文件。

部署脚本用 `PAT` 调用 `/user` 确认账号，在该账号下创建或更新公开 `Ai-qiPai` 仓库，配置 `GH_PAT`、`TUNNEL_UDID`、`TUNNEL_TOKEN` 和五个 `DB_*` Secrets，只上传固定的运行文件。不会上传服务器源码、PAT、真实本机配置、数据库凭据或 Go 源码。

首次只部署时保持仓库 Actions 关闭。确认要启动后执行：

```powershell
python my-server/tunnel/deploy.py --source . --run
```

`--run` 启用 Actions 与两个工作流并启动 `ci.yml`。之后仅部署不停止已运行的服务。修改服务器代码后，按项目的 `server-build-deploy` 技能先询问是否构建服务端运行包并运行新 Actions。

二进制从 `D:/workspace/go-tunnel/dist/tunnel-client-linux` 复制，校验值见 `tunnel-client-linux.sha256`；本项目和 Actions 均不编译 Go。

## 停止持续运行

先在 GitHub Actions 禁用 `ci-watchdog.yml`，再取消 `ci.yml` 的当前运行；只取消主工作流会被 watchdog 再次补启。
