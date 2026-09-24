# TMM 命令→文件映射

> 最后更新: 2026-08-07
> 用途: 改代码后对照检查，哪个命令受了影响

## 内置斜杠命令 (command_handler.py)

| 命令 | 文件 | 依赖 |
|------|------|------|
| `/voice` | `core/command_handler.py` | Whisper 模型 |
| `/stats` | `core/command_handler.py` | `core/pipeline.py` |
| `/cost` | `core/command_handler.py` | `core/pipeline.py` |
| `/tool <name>` | `core/command_handler.py` | `gateway/plugin_mgr.py` |
| `/hive <msg>` | `core/command_handler.py` | `core/hive_mind.py` |
| `/remember <内容>` | `core/command_handler.py` | `config.settings.DATA_DIR` |
| `/memory [keyword\|stats]` | `core/command_handler.py` | `config.settings.DATA_DIR` |
| `/reload` | `core/command_handler.py` → `core/pipeline.py` | 重载 py 模块 + `core/auto_guide.py` |
| `/changelog` | `core/command_handler.py` | `PROGRESS.md` |
| `/changes` | `core/command_handler.py` | Git diff |
| `/backup` | `core/command_handler.py` → `core/backup.py` | ZIP 打包 |
| `/backups` | `core/command_handler.py` → `core/backup.py` | 列出备份 |
| `/plan` | `core/pipeline.py` | `core/task_planner.py` |
| `/audit` | `core/pipeline.py` | 审计脚本 |
| `/diag` | `core/pipeline.py` | 诊断工具 |
| `/config` | `core/cli.py` | `config/settings.py` |

## 自然语言关键词路由 (intent_router.py)

| 关键词 | 路由到 | 工具文件 |
|--------|--------|----------|
| 邮件/收件箱/inbox | `check_mail` | `tools/check_mail.py` |
| 天气/气温/下雨/下雪 | `weather` | `tools/openmeteo.py` |
| 截图/截屏/截个图 | `screenshot` | `tools/windows_desktop.py` |
| 翻译/translate/译/中英 | `libretranslate` | `tools/libretranslate.py` |
| 备份/备份列表/创建备份 | `backup` | `tools/backup.py` |

## 工具插件 (tools/)

| 工具 | 文件 | 功能 |
|------|------|------|
| file_ops | `tools/file_ops.py` | 文件读写列删搜 |
| shell_exec | `tools/shell_exec.py` | 三级安全过滤命令 |
| browser | `tools/browser.py` | Playwright headless (含 snapshot) |
| check_mail | `tools/check_mail.py` | 163 邮箱查询 |
| send_email | `tools/send_email.py` | 163 邮箱发送 |
| ntfy | `tools/ntfy.py` | ntfy 推送通知 |
| openmeteo | `tools/openmeteo.py` | 天气查询 |
| libretranslate | `tools/libretranslate.py` | Google 翻译 |
| nominatim | `tools/nominatim.py` | 地理编码 |
| windows_desktop | `tools/windows_desktop.py` | 桌面操控(截图/OCR/窗口) |
| voice | `tools/voice.py` | Whisper 语音转文字 |
| sms_send | `tools/sms_send.py` | 短信发送 |
| plantuml | `tools/plantuml.py` | UML 图 |
| backup | `tools/backup.py` | ZIP 备份创建/列表 |
| kb_import | `tools/kb_import.py` | 文档入库(切块+向量化), 依赖 core/kb_store.py |
| kb_search | `tools/kb_search.py` | 知识库语义检索, 依赖 core/kb_store.py |
| kb_list | `tools/kb_list.py` | 知识库文档清单 |
| kb_delete | `tools/kb_delete.py` | 删除文档/清空知识库 |
| chart | `tools/chart.py` | 数据图表生成(pie/bar/line→PNG), 依赖 matplotlib, 中文字体自动探测 |

## 核心引擎 (core/)

| 文件 | 职责 | 被谁依赖 |
|------|------|----------|
| `kb_store.py` | 知识库向量存储(sqlite+ollama bge-m3, 零依赖) | kb_import, kb_search |
| `pipeline.py` | 主处理链，调用一切 | main.py, cli.py, command_handler |
| `auto_guide.py` | 463条规则0ms响应，知识库 | pipeline, command_handler(/reload) |
| `intent_router.py` | 关键词→工具路由 | pipeline |
| `model_router.py` | 多模型调度 | pipeline |
| `model_client.py` | API 调用 | pipeline, health_probe |
| `health_probe.py` | 模型健康探测 | model_router |
| `http_bridge.py` | HTTP 服务(19529) | main.py, relay_client |
| `relay_client.py` | WS 直连 relay | main.py(后台线程) |
| `code_engine.py` | 代码沙箱执行 | pipeline |
| `command_handler.py` | 所有 /commands | pipeline |
| `backup.py` | ZIP 备份 | command_handler, pipeline |
| `cli.py` | 终端交互 | main.py |

## 改码前必检

1. `command_handler.py` — 动了可能影响 /voice /stats /memory 等所有斜杠命令
2. `intent_router.py` — 动了可能影响自然语言→工具路由
3. `pipeline.py` — 动了可能影响全局处理链
4. `auto_guide.py` — 动了可能影响 463 条规则匹配
5. `tools/*.py` — 动了只影响那个工具
| `` | `tools/.py` | 时钟工具 (自动注册) |
