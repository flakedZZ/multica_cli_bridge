# multica_cli_bridge

把 QQ 中交办的任务交给 Multica 执行，再把文字、图片和原始文件送回原会话。

这是一个 **MaiBot 插件 + Multica CLI 桥接服务**。插件负责识别请求、审核方案和接收结果；桥接负责调用 CLI、保存任务归属和交付回执。模型不需要得到附件的认证下载地址，也不会拿到 Multica 登录凭据。

## 能做什么

- 先讨论、后确认：规划 Agent 给出版本化方案，QQ 助手审核后创建执行单，重复确认复用原单。
- 查询历史任务：按执行编号查状态或结果，原始请求离开最近消息窗口后仍可查询。
- 原会话交付：发送结果正文、图片，以及 PDF、HTML、压缩包等原始文件。
- 持久去重：发送前记录尝试，保存成功 `file_id`；上传结果不明时停止自动重发。
- 完整回执收尾：所有声明附件和正文交付成功后，自动完成执行单与需求父单。
- 限定权限：白名单、用户、会话、项目和附件归属校验；任务创建速率限制；确认前后核验父子关联。

仓库包含通用源码、合成测试数据和配置模板。运行数据库、凭据、真实 QQ 号、聊天内容、部署记录和原工作区标识均不在仓库内。

## 工作方式

```mermaid
flowchart LR
  Q[QQ 用户] --> M[MaiBot 插件]
  M -->|鉴权 HTTP| B[Python 桥接]
  B --> C[Multica CLI]
  C --> A[Multica 规划与执行 Agent]
  A -->|结果评论和附件| C
  C --> B
  B -->|校验后的正文与文件字节| M
  M -->|SDK / SnowLuma API| Q
```

桥接不直接调用 Multica 的私有 HTTP 接口。所有平台读写和附件下载都通过 CLI 完成。

## 兼容范围

| 组件 | 已验证范围 |
|---|---|
| Python | 3.13；代码使用 Python 3.11+ 标准库功能 |
| 桥接依赖 | Pillow；其余为标准库 |
| MaiBot | 1.4.1，需安装本仓库的宿主身份补丁 |
| MaiBot SDK | 2.21 |
| QQ 适配器 | MaiBot-SnowLuma-Adapter 1.1.8 |
| 普通文件传输 | SnowLuma 2.4.2 / `2.4.2-node`，OneBot v11 |
| 部署方式 | Windows 桥接 + Docker MaiBot；其他系统需自行配置进程与网络 |

没有把不同 OneBot 实现当作等价协议。普通文件发送会检查适配器版本和账号；其他客户端、临时会话不在当前验证范围。图片限制 8 MiB，普通文件限制 32 MiB；最大体积仍可能受宿主或适配器超时限制。

## 安装

### 1. 准备 CLI 和 Python

安装 Multica CLI，**在普通用户终端中完成持久登录**，确认能访问目标工作区和项目。不要使用 Agent 某次任务临时注入的登录信息来启动常驻桥接；服务会拒绝这类环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-bridge.txt
```

规划 Agent 必须能接收讨论任务并按约定返回 JSON 方案；它在确认后也负责组织执行。仓库不会为你创建 Agent 或开放工作区权限。

### 2. 生成你自己的私有配置

从你自己的 Multica 工作区取得真实 UUID。下面的变量须由你填写；不要把这些值或生成的配置提交到 Git。

```powershell
$cli = (Get-Command multica).Source
$workspace = "<你的工作区 UUID>"
$project = "<你的项目 UUID>"
$agent = "<你的协调 Agent UUID>"
$qqUser = "<允许发起任务的 QQ 号>"

.\.venv\Scripts\python.exe scripts/configure.py `
  --output-dir runtime `
  --cli-path $cli `
  --workspace-id $workspace `
  --project-id $project `
  --agent-id $agent `
  --planner-id $agent `
  --allowed-user $qqUser
```

可以重复 `--allowed-user` 指定多个用户；用 `--profile` 指定 CLI 配置档。工具不会覆盖已有目录，生成后插件默认关闭。生成内容：

```text
runtime/
  bridge.json
  bridge.token
  plugin/
    config.toml
    bridge.token
```

两端 token 相同，但与 Multica 的登录凭据无关。Windows 部署时将 token 文件权限限制为运行服务的账号和必要系统账号。示例字段说明见 [docs/configuration.md](docs/configuration.md)。

### 3. 安装宿主身份补丁（必需）

MaiBot 1.4.1 的工具上下文字段可能被同名模型参数保留。不能仅凭工具参数中的 `user_id` 认证查询者。本补丁只对 `multica_mika` 覆盖注入宿主身份，包括空字段。

先查看 [最小补丁](host-patch/component_query.patch)，备份并停用插件。以下示例使用容器名 `maibot-core`；按你的部署调整：

```powershell
docker cp scripts/patch_host.py maibot-core:/tmp/multica-patch-host.py
# 先检查，不写文件
docker exec maibot-core python /tmp/multica-patch-host.py /MaiMBot/src/plugin_runtime/component_query.py
# 确认版本兼容后安装；会创建不覆盖的备份
docker exec maibot-core python /tmp/multica-patch-host.py /MaiMBot/src/plugin_runtime/component_query.py --apply
docker restart maibot-core
```

未知源码版本会拒绝自动替换。**重启宿主后才能启用插件**，只热加载插件不能使宿主补丁生效。容器可写层会在重建时丢失，请把已核验的补丁纳入自建镜像或只读文件挂载；更换镜像时重新检查兼容性。补丁许可证见 [NOTICE.md](NOTICE.md)。

### 4. 放置插件和私有配置

将 `plugins/local_multica` 放到 MaiBot 的插件目录，例如容器内 `/MaiMBot/plugins/local_multica`。

- `runtime/plugin/config.toml` → 插件目录的 `config.toml`。
- `runtime/plugin/bridge.token` → 插件数据目录 `/MaiMBot/data/plugins/local.multica/bridge.token`。
- 桥接机器保留 `runtime/bridge.json`、`runtime/bridge.token` 和随后生成的 `runtime/state`。

优先使用你现有的 Docker bind mount 放置这些文件。配置中的容器路径应指向真实可见的文件；两端白名单应一致。

### 5. 启动桥接，检查后启用插件

在已完成 CLI 持久登录的普通终端，从仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m bridge.server --config runtime/bridge.json --listen 127.0.0.1 --port 18765
```

默认插件连接 `http://host.docker.internal:18765`，适用于相应 Docker Desktop 网络。容器访问宿主 loopback 的行为依平台而异；不通时先检查实际网络与防火墙，再调整绑定地址和插件 URL。仅允许受信任容器/主机访问，不能直接暴露到公网。非本地网络应使用加密通道。

可在能读到插件配置和 token 的环境运行：

```text
python scripts/probe_bridge.py --plugin-config /MaiMBot/plugins/local_multica/config.toml
```

这个检查只验证桥接和 CLI 项目访问，不发消息或创建任务。宿主补丁已加载、配置及网络正确后，在 MaiBot 管理界面启用插件。长期运行时用自己的任务计划或进程管理器托管桥接，保存日志；本仓库不自动修改你的系统服务。

## 使用

用户正常交办任务即可，例如“生成一份某主题的 PDF 报告”。工具流程是：

```text
requests → discuss → plan → confirm → 自动交付
```

`requests` 用于选择真实用户消息，`history` 列出本人当前会话的最近执行任务。

重启后可直接说“查一下还有哪些任务没确认，继续之前中断的任务”。QQ 助手用 `pending` 查询桥接数据库中的未确认讨论（每页 10 条），返回原讨论编号、`dialogue_id`、状态、版本和摘要；`has_more=true` 时带 `next_cursor` 继续查询。随后对选中的原讨论调用 `plan` 读取完整方案，审核后 `confirm`，不重新创建讨论。查询不会自动确认；已确认/已取消讨论不列出，结果未知的请求标为需要人工核对。
`status/result` 使用执行单编号，例如 `DEMO-123`，不是讨论单编号。

存在可信宿主用户身份时，查询和原讨论的 `plan/confirm/cancel` 可以省略 `request_message_id`。群聊没有可信调用者时，工具会拒绝无身份查询；选择本人当前查询消息的完整 ID 即可，不必找回最初交办消息。不会根据任务所有者或群里最后一条消息猜测当前用户。

也支持命令：

```text
/multica 列表
/multica 状态 DEMO-123
/multica 结果 DEMO-123
```

直接提交接口默认关闭。`settings/configure` 可配置本用户/会话、任务类型或指定任务的检查间隔与连续失败上限。

`result` 会恢复该任务的结果跟踪，不仅是文字查询；若任务曾停止回传，可能触发后续交付。`status/history` 不启动发送。旧接口名 `multica_mika` 和配置字段 `mika_id` 为兼容现有工具定义而保留，不绑定任何特定账号。

## 交付约定与恢复

执行 Agent 应将最终文件实际上传为结果评论附件，并在同一条最终评论提供唯一清单：

````text
交付附件：report.pdf

```json
{"multica_delivery":1,"attachments":["report.pdf"]}
```
````

文件名必须与实际附件一致；纯文字结果用空数组。先完成评论和附件发布，再将执行单设为 `in_review`。桥接核对清单、校验原字节、发送并保存回执，最后自动收尾。更深层子任务需要将最终附件汇总到执行主单。

确认结果未知时不重复建单；文件上传结果未知时不盲目重发。删除讨论父单会破坏原生关联，桥接停止交付并保留证据，不自动猜测替代父单。恢复原则、数据位置和回滚说明见 [docs/operations.md](docs/operations.md)。

## 开发与测试

普通 Python 环境运行桥接测试；缺少 MaiBot SDK/源码时，宿主相关测试会明确跳过：

```text
python -m pip install -r requirements-bridge.txt
python -m unittest discover -s tests -v
python scripts/audit_public.py
```

完整测试可使用兼容 MaiBot 镜像，以源码只读、禁网、不挂载生产数据的方式运行（PowerShell）：

```powershell
docker run --rm --network none --read-only --tmpfs /tmp `
  -e PYTHONPATH=/work:/MaiMBot `
  -v "${PWD}:/work:ro" -w /tmp --entrypoint python `
  sengokucola/maibot -m unittest discover -s /work/tests -v
```

完整测试包括真实 SDK 组件校验、历史查询权限、宿主身份覆盖、父子关联检查、重复确认、附件清单、损坏文件拒绝、上传失败/超时、成功回执去重及部分收尾恢复。测试不要求连接真实 QQ 或 Multica。

## 目录

```text
bridge/                  CLI 服务、讨论、附件和交付协议
plugins/local_multica/   MaiBot 插件、持久收件和 QQ 文件传输
host-patch/             最小宿主兼容补丁（不包含完整宿主源码）
scripts/                配置生成、补丁安装、健康检查、公开内容检查
tests/                  使用合成身份的回归测试
docs/                   配置与运维说明
```

主项目使用 MIT 许可证；宿主兼容补丁及其安装器使用 GPL-3.0-only。详情见 [LICENSE](LICENSE) 和 [NOTICE.md](NOTICE.md)。
