# Tiger.M.M (MARY III)

本地 AI 全栈 Agent，跑在 Windows 上。两个入口：**CLI** 与 **网页版（localhost:8800）**。
跨模型调度（deepseek / mimo / ollama），36 个工具（`ls tools/` 可数），25 个技能。
Think → Execute → Verify → Fix 四步闭环。

---

## 快速开始

```bash
# 1. 安装（建目录 + 从模板生成 keys.json + 自检）
python install.py

# 2. 配模型 —— 编辑 keys.json，填 deepseek 或 mimo 的 API Key
#    （模板见 keys_template.json；只想本地跑就装 Ollama，不用填任何 Key）

# 3. 启动
python main.py                 # CLI 入口
python web_server.py           # 网页版 → http://localhost:8800

# 4. 自检（可选，一条命令跑全部门禁）
python scripts/verification/verify_distribution_ready.py   # 分发就绪
python -m pytest tests/ -q                                 # 单元测试
```

> 没有 API Key 也能起来：向导会生成模板，引擎会如实告诉你"哪家没配"，
> 不会假装成功。只想零成本试就装 [Ollama](https://ollama.com) 并 `ollama pull qwen2.5:7b`。

---

## 命令参考

| 命令 | 功能 | 示例 |
|------|------|------|
| `/backup` | 手动备份 | `/backup` |
| `/backups` | 备份列表 | `/backups` |
| `/memory` | 查询记忆 | `/memory` |
| `/remember xxx` | 写入记忆 | `/remember 张三 邮箱 z@t.com` |
| `/changes` | 本次会话文件变更 | `/changes` |
| `/changelog` | 启动后外部文件变更 | `/changelog` |
| `/reload` | 清缓存+扫描变更 | `/reload` |
| `/stats` | 运行统计 | `/stats` |
| `/cost` | Token用量 | `/cost` |
| `/tasks` | 任务会话列表 | `/tasks` |
| `/voice` | 语音输入(5s录音) | `/voice` |
| `/help` | 显示帮助 | `/help` |
| `/exit` | 退出（自动备份） | `/exit` |

## 模型切换

| 切换 | 说明 |
|------|------|
| `@deepseek` | 切到DeepSeek |
| `@mimo` | 切到MiMo |
| `@auto` | 自动路由 |
| `@ollama` | 本地Ollama模型 |
| `@voice` | 语音唤醒模式（说"虎哥"唤醒） |

---

## 自然语言

直接说话，不用命令：

| 说法 | 效果 |
|------|------|
| 查邮件 / 查最近3封邮件 | POP3查收件箱 |
| 发邮件给某某 主题xxx 内容xxx | SMTP 发邮件 |
| 备份 / 备份一下 | 创建备份 |
| 备份列表 | 列出备份 |
| 搜索xxx / 北京天气 | 网页搜索/天气 |
| 截图 / 桌面有什么 | 桌面操控 |
| 列出core目录 | 文件操作 |
| 记住：xxx | 写入记忆 |

---

## 可用工具（36 个）

全部在 `tools/` 下，每个文件带 `TOOL` 元数据（名称/参数/关键词）。

| 工具 | 功能 |
|------|------|
| `backup` | 创建或列出项目备份 |
| `browser` | Playwright 浏览器操控（无头/有头） |
| `chart` | 数据图表生成: 根据数据生成饼图/柱状图/折线图, 保存PNG到路径 |
| `check_mail` | POP3 收件（163），关键词过滤 |
| `file_ops` | 文件读写/列目录/搜索/删除/移动 |
| `firecrawl` | 网页正文抓取（trafilatura + readability） |
| `github_api` | GitHub REST（仓库/issue/PR，自研零依赖） |
| `hermes_bridge` | Hermes 本地桥（零 RPC 依赖） |
| `im_notify` | 钉钉/飞书 webhook 机器人通知 |
| `image_gen` | 生图 (Image generation) — 文生图, 走阿里百炼 (DashScope) 通义万相。 |
| `kb_delete` | 删除知识库中的文档（说删掉哪份），或清空整个知识库。删除的是索引，原文件不受影响 |
| `kb_import` | 导入文档到知识库——传file=文件路径或text=文本内容，自动切块向量化。支持txt/md/docx/pdf |
| `kb_list` | 列出知识库中的全部文档、切片数和入库时间 |
| `kb_search` | 知识库语义检索——传query=问题，返回最相关的文档片段和来源。回答文档类问题前先调这个 |
| `libretranslate` | 多语言翻译 |
| `nominatim` | 地名↔坐标（地理编码/反查） |
| `ntfy` | ntfy.sh 推送通知 |
| `ocr` | 图片文字识别（Tesseract） |
| `office_cli` | Word/Excel/PPT 处理 |
| `openmeteo` | 获取天气——直接用city参数传城市名即可，如city=济南。不需要先调nominatim查坐标。免费无API  |
| `plantuml` | PlantUML 图（架构/时序/类图） |
| `push_notify` | 推送通知（Server酱 / PushPlus） |
| `send_email` | SMTP 发信（163）+ 附件 |
| `service_check` | 服务/端口存活检查工具 — "引擎还活着吗 / ollama 通不通"。 |
| `session_logs` | 对话史查询工具 — 让 TMM 能"记得"你跟它聊过什么。 |
| `shell_exec` | 命令执行（白名单 + 安全策略） |
| `sms_send` | 腾讯云短信 |
| `system_info` | 系统/磁盘/进程/网络信息 |
| `tiger_office` | TigerOffice 办公大插件 |
| `usage_stats` | 模型用量统计工具 — "钱花在哪了"。 |
| `vision` | 看图 (Vision) — 让 TMM 真正"看"图片内容。 |
| `voice` | 录音 + Whisper 转写 + Edge TTS 播报 |
| `web_search` | 网页搜索 + 天气（可插拔 provider） |
| `whisper` | Whisper 语音转文字 |
| `win32_input` | Win32 底层输入（后台也可发键鼠） |
| `windows_desktop` | 桌面 UIA 自动化（截图/窗口/点击/输入） |

---

## 排错指南

### 启动报错

| 现象 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError` | 缺少依赖 | `python install.py` |
| `NameError: name 'keys'` | 配置未加载 | 检查 keys.json 是否存在 |
| `连接被拒绝` | 网络/代理不可达 | 检查本机网络；如走代理请确认代理已启动且模型域名可达 |

### 模型问题

| 现象 | 原因 | 解决 |
|------|------|------|
| deepseek 400 | 工具重复/代理问题 | 清`__pycache__`重启 |
| deepseek 401 | Key过期 | 去 platform.deepseek.com 重新生成 |
| mimo 400 "duplicate names" | 工具schema重复 | 更新到最新代码 |
| ollama 未运行 | Ollama没启动 | 运行 `ollama serve` |

### 工具问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 查邮件失败 "Unable to log on" | POP3 授权码过期 | 去 163 网页版重新生成授权码，更新 `keys.json` 的 `mail` 段（凭据已不在源码里） |
| 发邮件失败 "535 auth" | SMTP 授权码过期 | 同上 |
| 提示"未知工具: xxx" | 该工具未登记 `SAFE_PLUGINS`，被安全扫描拒绝加载 | 把工具名加进 `config/settings.py` 的 `SAFE_PLUGINS`（门禁 `verify_tool_registry.py` 会提前抓） |
| shell_exec 被拦截 | 安全策略 | 正常，用其他工具替代 |
| 语音没反应 | 缺依赖 | `pip install sounddevice soundfile numpy openai-whisper playsound` |

---

## 架构

```
用户 → CLI → process()
              ├── intent_router (意图分类→工具链)
              ├── model_router (deepseek↔mimo调度)
              ├── Verify→Fix (错误分类→恢复→上报)
              └── _process_external (模型+工具调用)
                    ├── check_evidence (嘴炮检测+闭环)
                    ├── ToolGateway (权限+限流+审计)
                    └── PlanExecutor (多步任务执行)
```

---

## 项目结构

```
mary3/
├── main.py              # 入口
├── install.py           # 安装
├── pack.py              # 打包
├── keys.json            # API密钥（不提交）
├── keys_template.json   # 密钥模板
├── core/                # 核心引擎
├── tools/               # 36 个工具插件
├── mcp/                 # MCP桥接
├── tests/               # pytest测试
├── docs/                # 文档
└── data/                # 运行时数据
```

---

## 版本

v5.0 — 完整交付版本

---

## 已知限制

| 项 | 说明 |
|----|------|
| 平台 | **仅 Windows**（UIA 桌面自动化、win32 输入、SAPI 兜底都依赖 Win32） |
| 模型 | 需要至少一家：云端 Key（deepseek/mimo/qwen，填 `keys.json`）或本地 Ollama |
| 部分工具需自备服务 | 邮件（163 授权码）、短信（腾讯云）、推送（ntfy/Server酱）、relay 消息 —— 未配置时**会如实报"没配置"**，不会假装成功 |
| 凭据位置 | 全部在 `keys.json`（**不进 git**）；模板见 `keys_template.json` |
| 语音 | 需要 `sounddevice soundfile openai-whisper`（Edge TTS 走内置） |
| 数据 | 对话史/备份/日志都在 `data/ logs/ backups/`，**随包不带个人数据**（见 `docs/data_boundary.md`） |

---

## 自检与门禁

```bash
# 全部门禁（自动发现 scripts/verification/verify_*.py）
python scripts/hermes_verify.py

# 只跑分发就绪检查
python scripts/verification/verify_distribution_ready.py
```

`verify_distribution_ready.py` 会检查：明文凭据、运行时个人路径、依赖清单覆盖、
`PROJECT_ROOT` 不依赖工作目录、数据边界清单是否存在。

---

## 启动器 (2026-09-21)

所有 `.bat` 都用 **`%~dp0` 相对路径** + 共用 `_find_python.bat` 探测解释器
(不再写死任何绝对路径 —— 目录放哪都能跑)。解释器查找顺序:
`PY` 已设 → `TMM_PY` 环境变量 → PATH 上的 python → 常见安装位置 (3.10~3.13)。
找不到会**明确报错并提示怎么设**, 不会静默用错解释器。

| 启动器 | 作用 |
|--------|------|
| `Tiger.M.M.bat` | 桌面版 (pywebview 原生窗口) |
| `open_web_ui.bat` / `start_web_ui.bat` | 网页版 |
| `open_data_ui.bat` | **数据面**页面 (/dash: 产物/专家/审计, 只读) |
| `tmm.bat` | 命令行 |
| `stop_web_ui.bat` | 优雅停止引擎 |
| `verify.bat` | 跑全部门禁 |

---

## 许可证 (License)

本项目**自有代码**以 **MIT 许可证**发布 —— 见 [`LICENSE`](LICENSE)：
可自由使用、修改、商用，只需保留版权声明。

`tools/` 下的第三方程序（`plantuml.jar` = PlantUML, GPL-2.0；`officecli.exe`）
版权归各自作者，按**各自**许可证使用，不适用本项目的 MIT 许可证。

> 仓库里**不含**任何个人数据与凭据（对话史 / 记忆 / 感知记录 / API Key）；
> 数据边界见 `docs/data_boundary.md`。
