# 分发版计划（C 项）· 2026-09-20 第二刀完成

目标：把 `<项目根>` 从"只在我这台机器上能跑"变成"别人拿到能跑"。

**做法（大国师路数）**：先立门禁 `verify_distribution_ready.py`，让它把真问题列出来，
再按它列的逐条修 —— 不凭印象改代码。**门禁现在 12 PASS / 0 FAIL。**

---

## ✅ 第一刀（前次完成）

| 项 | 结果 |
|---|---|
| `requirements.txt` | 从零生成：扫全部源码 import → 映射到本机实际安装的发行版+版本 → 30 条，逐条回验版本一致 |
| 凭据停跟踪 | `keys.json` / `keys(LAPTOP-*).json` / `relay_tokens*.json` 4 个真密钥文件 `git rm --cached`（磁盘文件未动）+ `.gitignore` 补齐 |

## ✅ 第二刀（本次完成）

### ① 分发门禁 `scripts/verification/verify_distribution_ready.py`（新增，第 25 门）

```text
证据面 = git ls-files  ← 关键设计: 查"真正会出门的东西", 不是工作树(工作树有一堆开发残留会狼来了)
[0] 自检 —— 验证器自己有没有辨别力 (真 key 抓得到 / 单测夹具放过), 防门禁变噪音
[1] 明文凭据 (BLOCKING)
[2] 运行时个人路径, core/tools/config 的**代码**里, tokenize 跳过注释 (BLOCKING)
[3] requirements.txt 存在且覆盖全部 47 个第三方 import (BLOCKING)
[4] PROJECT_ROOT 不依赖 cwd (BLOCKING)
[5] 数据边界清单存在 / 开发残留 (WARN)
```

**门禁首次运行时抓到的真问题**（全部已修）：

```text
★ 硬编码邮箱授权码: tools/send_email.py:27 · tools/check_mail.py:24
  —— 这两个文件**被 git 跟踪**, 一旦 push 就泄露。这是本次最要紧的一条。
★ 运行时个人路径/账号 13 处:
  core/pipeline.py  系统提示里写死 <你的用户目录>\Desktop (换机器就把模型引到不存在的路径)
  core/pipeline.py  发信收件人**硬编码兜底** <收件人邮箱> (收件人解析不到时真会发给这个地址!)
  core/cli.py       两处日志/快照目录写死 <项目根>\...
  tools/ocr.py      tesseract 路径写死个人 conda 目录
  tools/windows_desktop.py  ×3 USERPROFILE 兜底写死 C:/Users/<你的用户名>
  tools/check_mail.py ×3 账号/显示名写死
★ PROJECT_ROOT = Path(os.environ.get("MARY3_ROOT", os.getcwd())) —— 从别的目录启动会指错根
```

### ② 去个人化（全部**加性/等价**替换，本机行为字节一致）

```text
桌面/主目录  → Path.home() / "Desktop"        本机派生值 == 原字面量 ✓
tesseract    → Path.home()/"miniconda3"/...   本机同址, 实测仍探测到 ✓
PROJECT_ROOT → Path(__file__).resolve().parent.parent  从 C:\ 启动也正确 ✓
邮箱账号     → keys.json["mail"] / TMM_MAIL_USER·TMM_MAIL_PASS 环境变量
               (读不到就**诚实报错**, 不再退回写死账号)
收件人兜底   → 删掉 (解析不到就不发, 交给守卫去问用户要)
一次性脚本 send_test.py (含明文密码) → 连文件删除, 留底 TMM_depersonalize_20260920_192315/
```

### ③ 首次运行向导 `install.py`（重写）

```text
旧版问题: 自己写死 14 个 pip 包 (与 requirements 漂移: 装了没用的 playsound/whisper,
          漏了 fastapi/pywin32/comtypes), 且自己内联一份 keys 模板 (两套模板会漂)
新版: ① Python 检查 ② 直接 pip install -r requirements.txt (单一来源)
      ③ 从 keys_template.json 生成 keys.json (已存在**绝不覆盖**)
      ④ 收尾跑分发门禁, 让门禁说话 + 打印五步下一步
      (踩过并修: _report_keys 的占位过滤把空串放进 startswith 元组 → 恒真 → 把已配置的
       4 个模型全报成"还没有"。已修, 现在如实报 ['deepseek','mimo','qwen','ollama'])
```

### ⑥ 数据边界清单 `docs/data_boundary.md`（新增）

```text
判据: 能重建的可以出去 (代码/技能/模板/门禁/文档); 不能重建的绝不出去 (凭据/对话史/
      感知到的环境/账号/机器标识)。
三类清单: ① 绝不出去(凭据, 已被 gitignore) ② 不出去(个人数据: 对话库/perception/
      entities/mode/prefs/usage/memory_node/backups…) ③ 可选出去(技能/文档/模板)。
另附"打包前自检"两条命令。
```

### ⑦ 又抓出三处真泄露（都是"未跟踪文件"漏网）

```text
tools/sms_send.py            腾讯云 secret_id/secret_key 明文 (被 git 跟踪)
core/relay_client.py         relay 凭据字面量 (开发仓库里**未跟踪** → 主仓库门禁扫不到,
                             但生成分发仓库时从工作树复制 → 会跟着出门)
core/cli.py:57               CLI **登录口令**明文 `if user == "TMM" and pwd == "<口令>"`
                             (被跟踪; 而且是比较式, 我的正则只认赋值式 → 又漏了一轮)
scripts/verify_modes_cli.py  同一口令的副本 (未跟踪)
全部处理: 移出源码 → 环境变量 → keys.json["mail"/"sms"/"cli"]; 拿不到就诚实报错/跳过登录。
原值已搬进 keys.json(不进 git), 本机行为不变。
⚠ 这些值都在 git 历史里 → 视为已暴露 → **建议轮换**: 163 授权码 / 腾讯云 secret / CLI 口令
   / relay token。
★ 关键教训: "扫 git 跟踪文件"会漏掉**未跟踪但在工作树里**的文件 —— 而分发打包是复制工作树。
  所以门禁必须在**生成出来的分发包上再跑一遍**(make_dist_repo.py 已内置这一步), 那才是终检。
```

### ⑧ ★ 门禁自身的四个真缺陷（靠"牙齿测试 + 在包上再跑一遍"才发现）

```text
① 凭据正则**大小写敏感** → `SMTP_PASSWORD = "…"` 大写写法漏抓
② 没有**通用密钥变量** → `API_KEY` / `SECRET_KEY` / `ACCESS_TOKEN` 全漏
③ 只认**赋值式**, 不认**比较式** → `pwd == "<口令>"` 漏抓 (CLI 登录口令就是这样漏的)
④ 门禁**自扫自己**: 自检里的样例凭据字面量被算成命中 → 文件一旦被跟踪就永久假红
   (开发仓库里它未被跟踪所以没暴露; 分发仓库 git add -A 后立刻假红 6 处)
另一头 (豁免过宽 → 漏真值): 曾把 `123456` 当夹具标记, 结果含 1234567890 的真 token 被放过;
  又曾漏 `your-api-key-here` 这类英文占位 → 假红。
现在: 正则 re.I + 键名全变体 + `(?:[:=]=?|!=)`; 夹具标记靠 abc123/qwerty/dummy;
     占位按 "key/token/secret + here"; 门禁把自己排除出待检清单。
     ★ 这些盲点全部写进 [0] 自检段, 防回退。
```

### ⑨ 门禁的"牙齿"证据 + 分发包终检

```text
牙齿测试 (开发仓库): 注入 _teeth_probe_config.py (SMTP_PASSWORD + 个人路径) → git add -f
  门禁: FAIL ★ 仓库内无明文 API key / 授权码 / token
        <- ['_teeth_probe_config.py:3 硬编码口令/授权码']   ← 精确定位, exit 1
  移除 → 转绿。  自检矩阵: 6 个"该抓的"全抓到 · 7 个"该放的"全放过 · 真仓库 14 PASS / 0 FAIL

★ 选项 C 落地: scripts/make_dist_repo.py 生成干净分发仓库 <分发包目录>
  (395 个文件, 跳过 37 个个人/调试文件; git init + 单个提交, 不带开发史)
  在**新包上再跑门禁** → 14 PASS / 0 FAIL   ← 这一步抓出了 relay_client / verify_modes_cli 两处漏网
  在新包里真跑 install.py → 从模板生成 keys.json, 如实报已配置模型 ['ollama'] ✓
```

### ⑤ 模板纳入仓库

`keys_template.json` 之前**没被 git 跟踪**（分发包里就没有模板 → 别人无从下手）。
已 `git add`（内容只有占位，无真值）。

---

## ⚠️ 唯一还剩的"要你拍板"：git 历史里的旧密钥

`keys.json` 的真值在提交 `b8f251a` 里。仓库**当前没有远程** → 还没出去。三选一：

- **A 只本地用** —— 永不给这个仓库加远程（零操作，但等于永远不能开源这个仓库）
- **B 重写历史** —— `git filter-repo` 清掉，本地仓库可控，但要重写全部提交
- **C 分发另起干净仓库（我荐）** —— 只提交当前工作树，不带开发史；分发包本来也不该带历史

## 命令备忘

```bash
cd <项目根>
"python.exe" -B install.py     # 首次运行向导
"python.exe" -B scripts/verification/verify_distribution_ready.py
verify.bat      # 全门禁 (~10 分钟)
```
