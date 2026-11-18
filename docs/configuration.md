# 配置参考

桥接使用 JSON，插件使用 TOML。`scripts/configure.py` 是推荐的初始化入口；两个 example 文件只展示结构，不是可直接上线的配置。

| 桥接字段 | 含义 |
|---|---|
| cli_path | Multica CLI 可执行文件的绝对路径；启动时 MULTICA_CLI_PATH 优先 |
| profile | CLI 配置档，空值使用正常用户默认配置 |
| workspace_id / project_id | 自己工作区和项目的真实 UUID |
| mika_id | 负责讨论及确认后执行编排的协调 Agent；留空回退 assignee_id |
| assignee_id | 兼容的直接提交接口使用的 Agent；直接提交默认关闭 |
| allowed_users | 允许提交/查询的 QQ 用户字符串列表，空列表拒绝所有人 |
| token_file | 桥接配置文件同目录下的共享随机 token 文件 |
| state_dir | 相对桥接配置文件的持久状态目录 |
| cli_timeout | 普通 CLI 操作超时，1–10 秒；附件操作有单独上限 |
| max_submissions_per_hour | 每用户每小时执行提交上限 |
| max_discussions_per_hour | 每用户每小时讨论上限 |
| allow_direct_submit | 默认 false；启用会绕过讨论确认阶段 |

插件的 `[bridge]` 指定 URL、容器可见 token 路径和白名单。`[routing]` 控制任务路由与自动图片回传。`[delivery]` 默认每 20 秒检查，连续失败 5 次后通知并停止。`[closure] enabled=true` 允许完整成功回执后的自动收尾，不是用户已阅读的证明。

生产数据库包含用户/会话标识与结果正文，应置于私有数据卷。不要把 `bridge.json`、`config.toml`、token、数据库、pending JSON 或运行日志提交到 Git。`.gitignore` 只是辅助，不会保护已经被跟踪的文件；提交前运行 `scripts/audit_public.py` 并检查暂存区。
