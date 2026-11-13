# 服务器部署

将已构建的日报网页、正式日报与媒体数据库快照，通过 SSH 发布到 Linux Docker 服务器的 HTTP 80 端口。公网服务仅接受 GET/HEAD；不配置发布密钥，不开放内容写入接口。前端、历史日期页、分页 API 与媒体读取复用现有 Worker。

## 配置与更新

本机需要 Python、Node.js、npm、系统 SSH/SCP；服务器需要 Docker、curl、tar 与 flock，并允许访问容器镜像仓库。先核验服务器 SSH 主机身份，在仓库根 `.env` 配置：

```dotenv
DAILY_DIGEST_DEPLOY_HOST=服务器IP
DAILY_DIGEST_DEPLOY_USER=root
DAILY_DIGEST_PREVIEW_DB=daily_digest/web/state/preview.sqlite
```

每次代码、样式或正式内容更新后，先将内容导入本地数据库，再执行以下命令。PowerShell 中任一步失败应停止，不继续发布。

```powershell
# 工作目录：仓库根目录
Set-Location daily_digest/web
npm run check
npm test
npm run build
Set-Location ../..
python -B -X utf8 daily_digest/deploy/publish.py
```

SSH/SCP固定使用专用密钥和非交互认证，未授权时立即失败，不等待后台密码输入。私钥默认`~/.ssh/webot_daily_digest_ed25519`，可用根`.env`的`DAILY_DIGEST_DEPLOY_IDENTITY_FILE`指定；相对路径按仓库根解析。首次将公钥追加至服务器授权列表时，通过既有管理员登录输入一次密码；保留已有授权，不更改SSH主机身份或密码登录设置。仅首次替换其他应用时，指定已核实占用80端口的旧容器名：`python -B -X utf8 daily_digest/deploy/publish.py --replace-service 旧容器名`。后续使用默认命令，不重复传入旧应用名称。

`--prepare-only` 只准备发布包；不会上传或切换服务。发布包位于忽略的 `daily_digest/state/deploy/`，包含完整数据库快照，应作为本机运行数据管理。不要提交到仓库。

## 发布行为与恢复

脚本使用 SQLite 在线备份接口创建一致性快照，并检查数据库完整性。正常更新前读取服务器历史索引，本地必须包含所有已发布日期且内容摘要一致，否则停止，避免覆盖或丢失历史。正式日报同日期内容仍遵循不可覆盖约束。

服务器发布目录为`/opt/daily-digest/releases/发布ID`。发布ID由全量历史／媒体的逻辑快照和网页代码摘要生成并持久保存，同一快照重启后沿用，不按重试时间新建版本。发布使用文件锁串行执行；先在回环18081端口验证候选容器的预期日期、摘要及所有绑定媒体，再切换`daily-digest`占用80端口。匿名公网核验成功后才提交确认，保留上一容器、镜像及快照可恢复。启动或核验失败恢复旧代码；不清理其他服务、镜像或用户数据。

新容器随Docker自动启动，使用非root用户、只读文件系统与临时SQLite副本。公网拒绝所有写方法；内容以冻结快照为准。`publisher=ssh_snapshot`时日报调度器会调用同一部署引擎，先通过共享Worker校验将正式日报和图片导入本地库，再冻结、部署和读回；不会向HTTP发送发布密钥。手动部署入口仍可用，模板未变化时每日内容更新复用已验证构建。SQLite快照上限192MiB，匹配256MiB临时空间；发布前同时检查服务器磁盘和可用内存，超限明确停止，不覆盖现服务。

网页源码与Worker产物指纹的验证记录保存在忽略的`daily_digest/state/web-build.json`，CLI与调度器共用构建锁和记录。指纹变化时才依次执行有界的检查、测试、构建；失败不准备发布包，不把账号、模型、MCP或SSH凭据传给构建进程。SSH切换控制进程独立运行，连接中断后继续完成原发布或恢复旧代码；重连仍查询原发布ID。

部署完成后脚本从公网读取最新日报并比对日期、摘要，检查首页、归档与日期页。可通过 `http://服务器IP/` 访问。HTTP 公网检查不代表已完成微信内访问验收；原有朋友圈自动发布配置和激活状态不随部署改变。

在仓库根目录可查询或核对原发布：

```powershell
python -B -X utf8 daily_digest/deploy/publish.py --status
python -B -X utf8 daily_digest/deploy/publish.py --status <原发布ID>
python -B -X utf8 daily_digest/deploy/publish.py --release <原发布ID>
python -B -X utf8 daily_digest/deploy/publish.py --rollback <需要退回代码的发布ID>
```

回滚使用上一代码配合最新完整数据，避免用旧快照丢失新日报。连接中断后先查询原发布与公网日期／摘要，不盲目重复切换。自动流程遇未知仅调用原版本核对；不会创建新发布ID绕过问题。发布确认与朋友圈确认分别记录，不用网站上线推断朋友圈已发送。
