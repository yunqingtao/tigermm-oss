# 数据边界 — 哪些东西**不随包出去**

> 用途：分发/打包/开源前对照本清单。**代码可以出去，你的生活和账本不出去。**
> 门禁：`scripts/verification/verify_distribution_ready.py` 会强制检查本文件存在 + 下面第 1 类不被 git 跟踪。
> 更新日期：2026-09-20

---

## 0. 一句话判据

```text
能重建的 → 可以出去 (代码/技能/模板/门禁/文档)
不能重建的 → 绝不出去 (密钥/对话史/感知到的你的环境/账号/机器标识)
学到的经验 → **净化后**可按需携带 (经验包 —— 见第 6 节)   ← 2026-09-21 新增这一层
```

**为什么要第三层**：原方案是"分发一个**空壳**（data/ 空目录 + 首次运行向导）"。
后果是**学到的能力锁死在一台机器上** —— 换机器 = 从零再学。
经验包就是那座桥：把可携带的**经验**（技能/学到的条目/规则/已解决的缺口）导出来，
个人信息在导出时**强制净化**（净化失败就拒绝产出，不是"警告一下"）。

---

## 1. 绝不出去（凭据类 · 已被 .gitignore 覆盖 · 门禁强制）

| 文件 | 内容 | 现状 |
|---|---|---|
| `keys.json` | 模型 API key（deepseek/mimo/qwen）+ **邮箱账号与授权码** | 已 `git rm --cached`，被 `keys*.json` 忽略 |
| `keys_github.json` | GitHub token | 被忽略（新建即被规则覆盖） |
| `keys(LAPTOP-*).json` | 机器名带出的同名变体 | 已停跟踪 + 被忽略 |
| `relay_tokens*.json` | TMM Link 的 bot / master token | 已停跟踪 + 被忽略 |
| `keys.json.bak_*` | 历史备份 | 被忽略 |
| `.crypto.key` / `*.enc` | 本地加密密钥与密文 | 被忽略 |
| `keys_template.json` | **占位模板**（无真值） | ✅ 允许随包出去（用于首次运行向导） |

⚠️ 历史处置：`keys.json` 的真值在提交 `b8f251a` 里。仓库**当前没有远程**，
   所以现在改只需动本地；一旦 push，历史里的旧密钥会一起出门。
   三个选项：A 只本地用（永不加远程）/ B 重写历史 / C **分发另起干净仓库**（推荐）。

---

## 2. 不出去（个人数据类）

| 路径 | 为什么不能出去 |
|---|---|
| `data/chat_sessions.db` | **全部对话史**（用户原话 + 助手回复），不可重建 |
| `data/perception.json` | 感知引擎学到的环境：项目、路径、人物实体、纠正记录 |
| `data/*.enc` / `data/learned_rules.db` | 加密态的用户偏好 / 学习到的规则 |
| `data/gap_ledger.db` | 能力缺口台账（key 是用户原话） |
| `data/mode.json` / `prefs.json` | 个人使用状态（模式/模型偏好） |
| `data/model_usage.jsonl` | 用量与开销记录 |
| `voice-memos/` | 语音备忘 |
| `docs/memory_node_*.md` | 跨会话记忆节点（含个人项目与决策细节） |
| `reports/` `logs/` `tmp/` | 运行报告与日志（含真实路径与内容） |
| `backups/` | 全量快照（等于把上面全带上） |

**分发做法**：用 `data/` 里**空目录 + 首次运行向导**代替。用户自己跑起来后
这些文件会自然生成，不该继承你的。

---

## 3. 可选出去（示例/模板类，需人工过一眼）

| 路径 | 处理建议 |
|---|---|
| `tmm_skills/*/SKILL.md` | 技能定义可出去；但**检查内容里别夹带个人路径/示例邮箱** |
| `docs/*.md`（ARCHITECTURE / ROADMAP / TOOL_OUTPUT_SPEC） | 可出去；先扫一遍有无个人项目细节 |
| `requirements.txt` | 必须出去（别人靠它装环境） |
| `verify.bat` / `scripts/verification/*` | 必须出去（"装机即验证"是卖点） |
| `_test_mcp_server.py` | 可出去（门禁 verify_mcp_reap 要用） |

---

## 4. 配置层（出去但**值要个人化**的地方）

```text
keys.json          ← 首选配置口 (模型 key / 邮箱)。首次运行向导写它。
环境变量           ← 覆盖口: MARY3_ROOT / TMM_MAIL_USER / TMM_MAIL_PASS / GH_TOKEN
                      / TMM_SESSION_DB / TMM_GAP_DB / TMM_PERCEPTION_FILE / TMM_USAGE_LOG
keys_template.json ← 模板, 复制成 keys.json 再填
```

**已完成的去个人化（2026-09-20）**：桌面路径、主目录、tesseract 路径、项目根
  全部改为**派生**（`Path.home()` / `__file__`），本机派生值与原来的字面量完全一致；
  邮箱账号与授权码移出源码（原来硬编码在 `tools/send_email.py` / `tools/check_mail.py`，
  而这两个文件被 git 跟踪 → 一旦发布就泄露）。
  一次性脚本 `send_test.py`（内含明文密码）已连文件删除，留底在
  `<留底目录>/send_test.py.removed`。

---

## 5. 打包前自检（照做即可）

```bash
cd <项目根>
"python.exe" -B scripts/verification/verify_distribution_ready.py
verify.bat            # 全门禁 (~10 分钟)
```
门禁红了就别打包 —— 它列出的每一条都是"会出门的真问题"。

---

## 6. 经验包（可携带的能力 · 2026-09-21 立）

```bash
python scripts/experience_pack.py export             # 导出 (净化后) → experience/
python scripts/experience_pack.py inspect <pack>.json # 看包里有什么
python scripts/experience_pack.py import  <pack>.json # 合并进本机 (只增不改)
python scripts/experience_pack.py --self-test        # 自检
```

| | 带 | 不带 |
|---|---|---|
| 内容 | 技能定义 · 学到的条目 (`learnings.db`) · 学到的规则 (`learned_rules.db`) · 已解决的缺口 (`gap_ledger.db`) | 对话史 · 感知记录 · 凭据 · 产物台账 · 用量 |

**净化是 fail-closed 的**（两道）：
1. 逐条净化：命中个人信息 → 字段级抹除（留痕"«已抹除: 含个人信息»"）；
   命中在**键名**里或整条几乎全是个人信息 → **整条丢弃**。
2. 成品再扫一遍：**有残留就拒绝写出**（exit 2），不是"提醒一下继续"。
   自检里有一条**变异测试**专门证明这道闸有牙齿（把净化函数改成恒等 → 必须被拦）。

**黑名单来源**：内置基线（`涛哥/虎哥/TMM-PC/LAPTOP-` 等与本人绑定的称呼）
∪ 仓库外 `_personal_denylist.txt`。
⚠ 只靠外部名单**不够** —— 实测那份 18 条里不含"涛哥"，而 `learnings.db` 里就躺着
"默认发给涛哥"；所以内核里必须有内置基线兜底。

**导入只增不改**：已有技能**绝不覆盖**（同名跳过并计数），学到的条目按
`(category,key,value)` 判重。别人给你的经验包不会悄悄改掉你的东西。

**它不随分发包出门**：经验包是**显式**操作，不是自动打包的一部分 ——
`make_dist_repo.py` 不带它。想带就自己 `export` 后手动放进目标机器。

