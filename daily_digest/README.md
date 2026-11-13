# 每日观察

独立中文深度日报：汇集AI新闻、内地香港热点、A股与港股收盘及行业表现，生成React报纸式网页，并通过现有微信MCP发布导读与当期永久链接。使用公开来源，无法核实的字段明确标缺，不用旧行情冒充当日数据。

## 使用要求

Python使用已有Codex SDK运行环境；自动发布需要电脑开机、微信登录和Hook/MCP/Codex服务可用。网页支持独立本地Node/SQLite预览、SSH/Docker服务器部署，并保留Sites Worker/D1托管实现。React、Vite、TypeScript与普通CSS负责模板。服务器访问使用[部署流程](deploy/README.md)：SSH模式自动导入正式日报与图片、部署冻结快照并公网核验，HTTP 80端口始终只读，不接收内容写入或发布密钥。使用本地预览时不会发布朋友圈。

以下命令在仓库根目录执行：

```powershell
python -B -X utf8 -m daily_digest configure --account wxid_当前账号 --publisher ssh_snapshot --site-url http://服务器IP
python -B -X utf8 -m daily_digest status
```

SSH模式需先在根`.env`配置部署目标，并一次性安装专用公钥；不要求站点Bearer密钥。专用私钥默认`~/.ssh/webot_daily_digest_ed25519`，仅限当前Windows用户读取，后台严格校验服务器主机身份且不弹密码提示。公开地址须与部署主机相同。保留`--publisher http_api --site-url https://日报站点域名`及`set-token`供HTTPS写入API部署使用，该模式凭据由Windows DPAPI保存，云端秘密变量为`DIGEST_PUBLISH_TOKEN`。本地预览不要求账号或站点密钥。状态目录默认`daily_digest/state/live`，可通过根`.env`的`DAILY_DIGEST_STATE_DIR`或CLI全局`--state-dir`指定，配置见[示例](config.example.json)。同目录不可切换账号。

先用国内手机微信验证匿名访问，再执行`python -B -X utf8 -m daily_digest activate`，随后按[服务管理](../scripts/wechat_stack/README.md)正常重载Codex。配置及密钥存在本身不会启动任务；更换站点后需重新验收。

## 时间与状态

每天北京时间16:30准备，在17:00—18:00随机发布一次，周末照常。时间落盘后不因重启重新抽取，超过18:00不补发朋友圈。网页上传并读回同日期同摘要后才提交朋友圈。

朋友圈使用固定格式：首行为`每日观察：<当期永久链接>`，下一行为`简评：`，然后按`1. / 2. / 3.`分条展示当期三项重点。正文在提交前冻结；已经提交或状态未知的旧意图不因模板调整改文案或重发。

```powershell
python -B -X utf8 -m daily_digest runs
python -B -X utf8 -m daily_digest preview --date 2030-01-02
python -B -X utf8 -m daily_digest preview --sample
python -B -X utf8 -m daily_digest pause
python -B -X utf8 -m daily_digest resume
```

模型执行未知不自动重跑，网站部署使用持久发布ID，响应丢失只核对原版本；朋友圈未知只查原意图。`accepted`仅为本地接受；`confirmed`表示取得Hook确认。日报恢复不会解除MCP自身的暂停，见[MCP说明](../wechat_mcp/docs/SNS.md)。不另外发送每日私聊通知。首次启动会同步已部署历史用于去重；同日已有合格正式版时复用，不覆盖原版或补发旧日期。

## 网页开发与回归

Node.js要求22.19+。仓库根目录执行：

```powershell
Set-Location .\daily_digest\web
npm ci
npm run check
npm test
npm run build
npm run preview
```

本地预览默认地址为`http://127.0.0.1:4173/preview`，以程序输出URL为准。本地SQLite保存在`web/state/preview.sqlite`，无站点密钥时不允许写入。首页展示最新一期，`/daily/YYYY-MM-DD`保存永久链接，`/archive`为历史列表，`/preview`为明确标记的模拟排版，不代表真实新闻；未有真实日报时显示空态。

Python回归在仓库根目录执行：

```powershell
python -B -X utf8 -m unittest discover -s daily_digest/tests -v
```

接口、托管和迁移见[运维说明](docs/OPERATIONS.md)。模拟测试不代表真实发帖验收。

## 真实生成与本地阅读

在仓库根目录使用已安装Codex SDK的Python执行（例如现有SDK虚拟环境中的Python）：

```powershell
python -B -X utf8 -m daily_digest generate --output-dir daily_digest/state/local-edition
```

该命令采集当天公开资料并调用真实模型，继承Codex agent配置中的模型和运行程序；不要求站点或微信账号，不创建发布意图。输出目录必须尚不存在，保存采集快照、生成记录、事件计时和校验后的`edition.json`。资料不足时失败，不使用模拟内容补齐。可用`--packet <packet.json>`显式选择已有采集快照，其日期、来源和截止时间保持原样；这不会把旧快照刷新为当前新闻。未知或超时轮次保留原线程和轮次ID，不自动重试。

生成成功后，在`daily_digest/web`目录执行：

```powershell
npm run build
npm run preview -- --edition ../state/local-edition/edition.json
```

打开程序输出的本地首页或`/daily/YYYY-MM-DD`阅读真实日报。导入只写本地SQLite，经过共享内容校验与摘要读回；同日期同摘要幂等，不同摘要拒绝覆盖。未配置发布密钥时，HTTP接口仍保持只读。`/preview`始终是模拟排版样本。

## 自动配图

定时任务和本地 `generate` 在正文生成前共用配图阶段，对全部新闻候选依次选择原文图片、同一事件图片、相关主题图片、本地分类插画。原图也经过下载、解码、尺寸及视觉审核。采集时保存原页图片标签与页面摘要作为出处证据，视觉审核复用这份证据，不因重复联网失败丢弃已核实原图。视觉审核每批最多4张，直接传入图片，每张记录通过、拒绝或无法核实及原因；漏项记录为未完成审核，不等同于原文无图。搜索与视觉核对使用独立只读 Codex 线程，默认总预算 300 秒，可在日报 `config.json` 设置 `image_search_budget_seconds`；定时任务同时受当天发布截止时间约束。图片结果不改变新闻事实来源或采集时间，配图后重新检查新闻时效。

图片转为最长边 1600 像素、最多 512 KiB 的 WebP，按 SHA-256 去重保存到本次目录的 `media/`。`images.json` 保存候选、失败原因和原模型线程/轮次，`packet.imaged.json` 保存本地配图后的快照。无法核实、下载失败、服务不可用及超时均使用前端随包发布的分类插画。主题图和插画显示类型标签；图源及署名在来源区。

配图阶段中断后，可在仓库根目录执行 `python -B -X utf8 -m daily_digest generate --output-dir <原输出目录> --resume`。已完成配图复用；未知搜索/核对轮次保留原记录并降级，不自动重试。正文生成未知仍需人工核查，不能用恢复命令重放。正常的新生成仍须使用新目录。

预览导入会先上传同目录 `media/` 中的绑定图片，再保存日报。页面优先使用当期绑定的同源 `/media/<sha256>`，首页头条加载失败自动切换分类插画；详情页无可用新闻图片时省略图片区，不展示兜底插画。科技短讯仅展示两条；其余新闻仍保留在当期日报中。CSP 保持仅允许同源和 data 图片，不直接代理外部网址。
