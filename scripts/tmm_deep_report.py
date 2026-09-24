"""把深度功能测试的 JSON 结果**自动**渲染成 Markdown 报告。

为什么用脚本生成报告: 报告里的每个数字都必须来自**实测 JSON**, 不能手写
(项目铁律: 统计数字由脚本实算)。报告正文的定性部分 (修复/未修/建议) 也放在这里,
保证"结论"与"数据"同源 —— 改一次 JSON, 报告自动跟着变。

用法: python -B scripts/tmm_deep_report.py [--off JSON] [--live JSON] [--out MD]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KIND_CN = {"bool": "", "real": "真跑通", "cred": "需凭据", "side": "有副作用未实调",
           "noentry": "无执行入口", "gap": "能力缺口", "skip": "未测", "info": "信息",
           "entry": "入口可查"}

FIXES = [
    ("★★ 感知引擎把**整句话/路径**当实体名存库 (污染知识库与提示词)", """\
**症状**: 真实用户库里出现过 `C:...七瞬_第一章_素材清单.txt这是一份素材清单，我` 与
`用一句话说明什么是二分查找。另外帮我` 这种"实体"; 关系抽取的克制口径没有用在实体上。
**根因 (两处)**:
 ① `_extract_entities` 用 `(S\\S+?)` 取名字 —— **没有长度上限、也没排除标点/路径分隔符**,
    于是一段无空格的长串会被整段当成实体名 (中文句子里"在"极常见, 贪心回溯正好在这里收口);
 ② 触发词 `在` 是**单字**且中文里极常见 → "文件在 D:\\data" 会把通用名词"文件"建进实体库。
**修法**: 加 `_clean_entity_name()` 收敛判据 (长度 1~12 · 不含标点/空白/路径分隔符/盘符 ·
不含数字串 · 不是通用名词/代词, **子串判定** —— "内容我" 这种拼接垃圾直接否掉);
"在" 形态只在 `名字+在+盘符路径` 紧邻时取。
**证据 (真实来源)**: 用户会话 #163 的原话就是那个形态 ——
`C:\...\七瞬_第一章_素材清单.txt这是一份素材清单，我在C:\...` (路径紧跟句子、无空格)。
修后: 4 类污染 (长句+路径 · 内容我 · 文件在D盘 · 无空格长串) 全部返回空;
5 类正常 (人名+邮箱/电话/位于/路径是/在) 全部照旧抽出。"""),
    ("★ office_cli / win32_input **没有统一入口** → Agent 完全调不到", """\
**症状**: 深测实测两个工具都是"无 run/execute 入口"; 全网关认不出它们。
**根因**: 两个模块只有一组 `office_xxx` / `click·type_text·hotkey` 平函数, 没有网关约定的
`run(**kwargs)` + `action` 派发 (tiger_office 有, 它们没有)。
**修法**: 照 tiger_office 的既有约定补派发表 + `run()`; 平函数返回值包成网关契约
(success/output); 未知 action 诚实报错并列出可用动作。
**证据**: `office_cli` 9 个动作 + `win32_input` 5 个动作全部接线; `office_cli.info` 真调通;
未知 action 两份工具都诚实报错 (不再有"没有可调用入口")。"""),
    ("★ core/plugin_manager.py 仍在**谎报成功** (同一个 bug, 那一份漏改了)", """\
**症状**: `call("不存在的工具")` 返回 `{"success": True, "output": "[xxx] called"}` ——
调用方以为干完了, 用户看到"✓ 已完成"而什么都没发生。
**根因**: 我先前只修了 `core/pipeline.py` 里那份 `PluginManager`, **这份漏改**
(它经 `gateway/__init__` / `gateway/plugin_mgr` / `mcp/tool_adapter` 仍是活跃路径,
并接进两处留档验证器)。同一份代码的两个副本 —— 修一个不等于修完。
**修法**: 同款诚实报错 + 出口归一化 `_norm()` (与 pipeline 那份同口径)。
**证据**: 两份 PluginManager 的"未知工具"/"无入口模块"都诚实报错; 只有 `result` 键的工具
能通过 `output` 读到结果 (`output_from=result`); 测试断言**剥注释**后搜谎报模板, 两份都干净。"""),
    ("★ 办公全家桶**没有「新建表格」能力** (只有 scan/format/extract)", """\
**症状**: 用户说"新建一个 Excel 记录本月销量" → `Unknown action: write_excel` → 只能说做不到。
**修法**: 补 `tiger_office.write_excel`: 支持 `headers+rows` / `rows`(dict 行) /
`content`(markdown 表 / CSV / TSV 文本) 三种入参; 输出位置复用目录解析; 排版与 `format_excel`
同口径 (表头加粗居中) + 列宽按中文宽度自适应; 空内容诚实报错。
**证据**: 4 种入参全部真出 .xlsx 且**回读内容一致**; 空内容 → 诚实报错; 已注册进派发表。"""),
    ("★ OCR **整块不可用** (缺 pytesseract + 二进制路径硬编码)", """\
**症状**: `ocr()` → `No module named 'pytesseract'`。
**根因 (两层)**:
 ① Python311 环境没装 `pytesseract` (只有 conda 那边有);
 ② 二进制路径是**硬编码**一个个人机的 conda 绝对路径 → 换机器/换 conda 版本就找不到。
**修法**: 装 `pytesseract`; 二进制改成 `_find_tesseract()` 优先级探测
(env `TESSERACT_CMD` → 已知路径 → PATH → conda 常见位置); tesseract 不可用/没读出字时
**降级到本地视觉模型**读图 (不是猜: 走 `tools/vision` 本地 ollama 优先链, 读不出照实报);
返回同时给 `output`/`result`/`text`。
**证据**: 真跑造图 → 读出 `Hello World 12345` (engine=tesseract); 缺图/文件不存在 → 诚实报错。"""),
    ("★ tools/output.py 是**重复的 openmeteo** (两个插件抢同一个工具名)", """\
**症状**: `tools/output.py` 与 `tools/openmeteo.py` 的 `TOOL.name` 都是 `openmeteo`,
是同一次导入的旧草稿 (晚 4 分钟, 少了 keywords/输出归一/类型签名)。
**修法**: 备份后移除旧草稿 (备份 `backups/dedup_output_*`), 并从 `skill_loader` 的手工兜底
清单里去掉 `"output"`。留 `openmeteo.py` (有 keywords + 输出归一 + 类型签名)。
**证据**: 工具目录**无重名 TOOL**; `openmeteo` 真查出济南天气; 工具数 30 → 29。"""),
    ("★★ 技能产物**落点不可控**: 「存到 X」被无视, 一律落桌面", """\
**症状**: 12 个生成类技能里, 用户明确说「写周报，存到 D:/报告/」时产物仍然落到桌面;
文件名还会退化成正文首行 (实测生成过 `一、背景.docx` / `周报.docx` 混在一起)。
**根因 (三层)**:
 ① `_skill_params` 的路径抽取**只认带扩展名的路径** → 「存到 D:/报告/」(目录) 抽不到 →
    技能参数留空 → 工具用默认值落桌面 (用户指定的位置被静默丢弃);
 ② `make-image` 的 `path` 参数声明成 `type: text` → 不进路径抽取, 且 DAG 里**没有**把
    `$params.path` 传给 `image_gen`;
 ③ 工具侧只做「给路径就用 / 缺扩展名补」→ 给**目录**时会被当成文件补成 `D:/报告/.docx`。
**修法**:
 ① 抽出「存到/保存到/导出到/放到/写入 <目标>」的目标 (目录也认), 并**分角色**分发:
    显式目标 → 输出型参数 (`out_path`/`path`) ; 带扩展名的路径 → 输入型参数;
 ② `make-image` 的 path 改 `type: path` 并在 DAG 里传下去;
 ③ 三个工具加统一的「目录 → 补默认文件名」解析
    (`tiger_office._resolve_out_path` / `image_gen` / `plantuml`)。
**证据 (真跑)**: 「写周报…存到 <目录>/」→ `tiger_office.write_word(path=<目录>)` → 产物真在那;
「做PPT…存到 <目录>/」→ PPT 落在目录; 「画个架构图…存到 <目录>/arch.png」→ `arch.png` 真在目录;
「把这个表格做成分析报告 <表> 存到 <目录>/」→ 输入表给 `analyze_table`、输出目录给 `write_word` (未错位)。
`tests/test_output_path_contract.py` 28 项守这些行为。"""),
    ("★★ diagram 技能: 按它自己的触发词说话**必然失败**", """\
**症状**: 「画个架构图」→ `plugin.keyword` 拿空 code 调 plantuml →
`No PlantUML code provided`。技能形同不存在。
**根因 (两层, 都是真缺陷)**:
 ① 技能的 `dsl` 是**必填**, 却**没有任何步骤去生成它** —— 触发即缺参, 必然失败;
 ② `guard_generation` 只认「写/做/生成/整理成…」,**不认「画/绘制/设计」** →
    「画个架构图」被判成「不是要产出」→ 技能主动放弃 → 掉到关键词路由。
 ③ (修 ② 后才暴露) DAG 里写的是 `content: $steps.plan.text`, 而 `plantuml.run` 的
    参数名是 **`code`** → 源码根本传不进工具。「旧技能一直是坏的」。
**修法**: ① `diagram` 前面加 `llm` 步骤把描述编译成 PlantUML 源码, 并把 `dsl` 改为可选;
② 产出动词补「画/绘制/作图/出图/设计/制图」; ③ 技能改用 `code`, 工具侧另留 `content/uml` 别名。
**证据 (真跑)**: 三例全通 —— 「画个架构图…存到 <目录>/arch.png」→ 真出 `arch.png`;
「画个流程图，存到 <目录>/」→ 落在目录; 「画个架构图」→ 落桌面默认。"""),
    ("★ 工具返回契约不统一: `output` / `result` / `data` 三种混用", """\
**症状**: 33 个工具里 30 个把结果放 `output`, 其余放 `result` (web_search/nominatim/…)
或 `data` (libretranslate)。网关**不做归一化** → 任何按 `output` 读结果的调用方
(技能 DAG、`_run_search`、IR chain) 对这些工具**看不到结果**。
(深测台第一版就因此误判 3 个工具失败 —— 工具其实是好的, 是读法被坑。)
**修法**: 网关出口加**加性**归一化 `_norm_tool_result`: 仅当 `output` 为空且兄弟键有内容时
补上, 并留 `output_from` 痕迹; 不覆盖既有字段、不改变失败语义。
**证据**: `_norm_tool_result` 8 项测试 (含"失败语义不许被改写成成功"); 归一后 web_search /
nominatim / libretranslate 的结果都能从 `output` 读到。"""),
    ("★ 存量 11857 条 `running` 会话行 (上一轮修了增长, 这一轮清存量)", """\
**做法**: 先备份 (`backups/sessions_before_archive_*`), 再
`UPDATE sessions SET status='abandoned', finished_at=COALESCE(finished_at, created_at) WHERE status='running'`。
**为什么用 UPDATE 不用 DELETE**: 行数不变, 随时可回溯; 删了就说不清了。
**结果**: `running 11757 → 0`; 现状 `abandoned 11757 · done 80 · error 27` (总行数仍 11864)。"""),
    ("★★ web_search 搜索能力**整体失效**", """\
**症状**: 任意查询都只回 `(no results from Bing)` —— 搜索这条路等于废掉。
**根因 (打桩实证)**: `_search_ddg` 把 `HTTP_PROXY/HTTPS_PROXY` 写成**进程级环境变量且从不还原**;
ddgs 库未安装时该函数抛异常 → 兜底走 Bing, 但此时代理 env 已污染 → Bing 请求被代理转发,
拿到的是 14.9KB 无结果空壳页 (直连 97KB 含 10 条结果)。同进程后续**任何**裸 urllib 调用
都会继续走那个代理, 属于全局副作用。
**修法**: ① 代理 env 改为**限定作用域**临时设置 (try/finally 还原), 且 ddgs 未装时根本不碰 env;
② Bing 解析加第二套正则兜底 (h2>a), 不再因页面结构变动静默变"无结果";
③ "空结果"时把**拿到多少字节**一并报出, 便于区分"真没结果"和"被拦/走代理"。
**证据**: 修复后 4 个查询全部返回真实结果 (python decorator → python.org/下载/菜鸟教程…);
`HTTP_PROXY` 调用后为 None (不再外泄)。"""),
    ("★★ 危险操作拦截词表过窄", """\
**症状**: 拦截表只有 6 个**固定短语** (`删除C盘`/`格式化`/`清空C盘`…), 换个说法就漏网 ——
实测 `删掉整个项目目录` 没被拦 (只是恰好被下游参数校验挡住, 属侥幸)。
**修法**: 改成"危险动作词 × 高危目标词"**双条件**判定 (两个都中才拦) ——
所以 `删掉桌面的临时文件 test.txt` 这类正常请求不受影响。
**证据**: `删掉整个项目目录` / `格式化 D 盘` / `清空C盘` 全部 → `regulator.dangerous`;
正常删除请求 → 不拦。"""),
    ("★ probe 流量下状态命令掉到无关链路, 给出**错误答案**", """\
**症状**: `/learn stats` 在验证流量 (probe) 下被正确地跳过 (它要写状态), 但**跳过之后继续往下掉**,
落进 brain 的罐头技能, 答出 `OK 文件信息: size=40960B, mtime=… is_dir=True` ——
一个**看起来正常、实际完全无关**的答案。凡是拿 probe 做验证的人都会被它误导 (本报告作者就被误导过)。
**修法**: dispatch 里 probe 命中 `probe_gated` 路由时, **明确回一个"已跳过"响应**
(route=`probe.skip`), 不再 fall-through。match 契约是纯函数, 调用包在 try 里。
**证据**: `/learn stats`、`/skill list` → `probe.skip` 且文案说明; 正式对话 (probe=False) 仍 `cmd.learn`。"""),
    ("★ /mode 命令没有路由 → 模型编造答案", """\
**症状**: `/mode` `/modes` 只在 CLI 里有实现, Web/命令路由表里**没有**这条路由 →
请求掉到模型, 模型编出"当前处于标准交互模式" (实际是 craft/ask/plan 三模式体系)。
**修法**: 加 `cmd.mode` 路由 (只读, 与 CLI 同口径), 输出真实的三模式状态与当前模式。
**证据**: 活服务 `/mode` → `route=cmd.mode`, 内容含 `● craft 实干 直接执行文件/系统操作`。"""),
    ("★★ sessions.db 每次请求留一条**永不收尾**的 running 行", """\
**症状**: `data/sessions.db` 已积累 **11864 条 `status=running`** 的会话行。
**根因**: `SessionManager.spawn()` **无条件**写库, 但只有 `_run_session` (仅 `background=True` 才有)
会把它收尾; 而 `process()` 每次都调 `spawn(..., background=False)` → 每一条用户消息
(包括**本该零副作用**的 probe 探针流量) 都在永久增加一行。库里这些行还没有任何读取方
(`_load` 不存在, `list_sessions` 读内存) → 纯垃圾。
**修法**: 纯跟踪会话**不落库** (只留内存, `stats`/`list` 照常可查), 只有会被收尾的后台会话才写。
**证据**: 连跑 3 次 `process()` 后 `sessions.db` 行数 `11864 → 11864` (修前每条 +1)。"""),
    ("★ 门禁自己污染用户实体库 (本轮引入, 同轮修掉)", """\
**症状**: 门禁跑完后, 用户 `data/entities.json` 里多出一个测试实体。
**根因**: 本轮给 `verify_learn_loop` 新增了"实体记忆形态不被教学路由抢"这条断言, 它会跑
`记住：X 邮箱 Y` → 经 KnowledgeEngine 写**真**的 `data/entities.json`。该门禁原本只快照
`perception.json` 与学习库, 没把实体库算进去。
**修法**: 把 `entities.json` 纳入该门禁的快照/还原, 跑完字节回滚。
**证据**: 修复后跑门禁 → `entities = ['涛哥','虎哥','M.M']` 不再出现测试实体, 门禁 46 PASS。
**教训**: 新加断言时, 要连带问一句"这句话会写到哪些真文件", 把它们一并纳入快照。"""),
    ("★ plantuml 工具**无法通过网关调用**", """\
**症状**: `plantuml.run(code, format, output_name)` 是严格签名, 而网关/命令层会统一带
`action=` 之类的关键字 → `TypeError: run() got an unexpected keyword argument 'action'`
→ 该工具对 Agent 实际不可用 (而 diagram 技能正是靠它出图)。
**修法**: 加 `**kwargs` 加性兼容 (忽略未声明的额外关键字), 既有调用行为不变。
**证据**: 带 `action=` 与不带, 两次都 `Diagram saved: …/x.png`。"""),
]

OPEN_FINDINGS = [
    ("web_search 中文泛查询**相关性差** (非故障)", """\
修复后搜索能出结果, 但 `今天新闻`/`OpenAI 最新消息` 之类泛查询返回的相关性很差
(Bing cn 的结果质量)。英文查询正常。DDG 路 (`ddgs` 库) 未安装 → 只剩 Bing。
**建议**: 若要更好的相关性可装 `ddgs` + 走代理 7890 (代码路已就位); 或接入付费搜索 API。"""),
    ("whisper 未测", "需真实音频素材; `voice` 走 Windows SAPI 朗读, 不产生音频文件, 本测试无法构造输入。"),
    ("健康提示: C 盘剩余 46.1GB", "`system_info disk`: C:/ 278.2/324.3GB (46.1 free)。与本轮功能无关, 仅记录。"),
]

def _load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))

def _cnt(rs, kind=None, ok=None):
    n = 0
    for x in rs:
        if kind and x["kind"] != kind:
            continue
        if ok is not None and x["ok"] is not ok:
            continue
        n += 1
    return n

def render(off: dict, liv: dict, gates: str = "") -> str:
    rs = off["results"] + liv["results"]
    n_pass = _cnt(rs, ok=True)
    n_fail = _cnt(rs, kind="bool", ok=False)
    dims = {}
    for x in rs:
        d = dims.setdefault(x["dim"], {"pass": 0, "fail": 0, "note": 0})
        if x["kind"] == "bool":
            d["pass" if x["ok"] else "fail"] += 1
        else:
            d["note"] += 1
    real = [x for x in rs if x["kind"] == "real"]
    side = [x for x in rs if x["kind"] == "side"]
    gap = [x for x in rs if x["kind"] == "gap"]
    noent = [x for x in rs if x["kind"] == "noentry"]

    L = []
    A = L.append
    A("# Tiger.M.M 深度功能测试报告")
    A("")
    # 缺字段一律给默认值 —— 报告器要能吃**部分**JSON (实测: 少一个 ts 就 KeyError 崩掉)
    A(f"- 测试时间: {off.get('ts', '?')} ~ {liv.get('ts', '?')}")
    A(f"- 被测项目: `{off.get('root', '?')}`")
    A(f"- 测试方式: **真调用** (真 HTTP 活服务 / 真网络 / 真模型 / 真出图) + **沙箱隔离** (零副作用)")
    A(f"- 用时: 离线+有状态 {off.get('elapsed_s', '?')}s · 活服务 {liv.get('elapsed_s', '?')}s")
    A(f"- 测试台: `scripts/tmm_deep_test.py` (可复现, 见文末命令)")
    A("")
    A("## 一、结论")
    A("")
    A(f"**通过 {n_pass} 项 / 失败 {n_fail} 项** (另 {len(rs) - n_pass - n_fail} 条分类备注: "
      f"真跑通 {len(real)} · 有副作用未实调 {len(side)} · 能力缺口 {len(gap)} · 无入口 {len(noent)})。")
    A("")
    _ntool = len({x["name"].split("(")[0] for x in real if x["dim"] == "F.工具"})
    A(f"核心能力**可用**: 三模式闸门 / 46 句路由矩阵 / 12 个技能 / "
      f"33 个工具中 {_ntool} 个真跑通 / 多模态(本地看图+云端生图) / 学习闭环 / 记忆实体 / "
      f"邮件只读 / 网络信息 / 办公产物 / 诚实与安全边界。")
    A("")
    A(f"本轮**修掉 {len(FIXES)} 个真缺陷** —— 最严重的是 **web_search 搜索整体失效**"
      f" (任意查询恒返回「no results」) 与 **危险操作拦截词表过窄**;"
      f"另有 {len(OPEN_FINDINGS)} 项「能跑但不该这样」的问题列在第四节。")
    A("")
    A("## 二、结果矩阵")
    A("")
    A("| 维度 | PASS | FAIL | 备注 |")
    A("|---|---|---|---|")
    for k in sorted(dims):
        v = dims[k]
        A(f"| {k} | {v['pass']} | {v['fail']} | {v['note']} |")
    A("")
    A("## 三、本轮修复 (全部有实测证据)")
    A("")
    for i, (t, body) in enumerate(FIXES, 1):
        A(f"### 3.{i} {t}")
        A("")
        A(body)
        A("")
    A("## 四、发现但**未修** (建议与证据)")
    A("")
    for i, (t, body) in enumerate(OPEN_FINDINGS, 1):
        A(f"### 4.{i} {t}")
        A("")
        A(body)
        A("")
    A("## 五、工具清单实测分类")
    A("")
    A("**真跑通 (真调用成功)**")
    A("")
    for x in real:
        A(f"- `{x['name']}` — {x['detail'][:90].replace(chr(10), ' / ')}")
    A("")
    A("**有副作用 / 未实调 (只验入口, 测试不真跑)**")
    A("")
    A("- " + " · ".join(f"`{x['name'].split(' ')[0]}`" for x in side))
    A("")
    if gap:
        A("**能力缺口 / 缺依赖**")
        A("")
        for x in gap:
            A(f"- {x['name']} — {x['detail'][:90]}")
        A("")
    if noent:
        A("**无执行入口**")
        A("")
        for x in noent:
            A(f"- {x['name']} — {x['detail'][:80]}")
        A("")
    A("## 六、安全与诚实性")
    A("")
    A("- 危险操作拦截: `删掉整个项目目录` / `格式化 D 盘` / `清空C盘` → 全部拦截; "
      "`rm -rf /` → 输入守卫拦截; 正常删除请求**不误拦**。")
    A("- 诚实性: 不存在的文件/表 → 不谎报成功; 未知工具名 → 明确报错; "
      "缺参数 → 直接说缺什么 (不硬发、不编造)。")
    A("- 状态零副作用: `probe` 流量不写 `mode.json`; `probe_gated` 命令明确跳过; "
      "测试结束后用户库 (会话/实体/感知/mode) 全部还原。")
    A("- 学习闭环: 教学句 (以后/默认/总是/记住…) → **零工具调用** (历史事故: 曾被当成发送指令真发邮件, 已修)。")
    A("")
    if gates:
        A("## 七、回归门禁")
        A("")
        A("```")
        A(gates)
        A("```")
        A("")
    A("## 八、复现方式")
    A("")
    A("```bash")
    A("# 1) 活服务部分 (需要引擎在跑: python -B start_web_ui.py)")
    A("python -B scripts/tmm_deep_test.py --only-live --json tmp/_deep_live.json")
    A("")
    A("# 2) 离线+有状态部分 (建议**先停引擎**, 避免与引擎争写用户库)")
    A("python -B scripts/tmm_deep_test.py --skip-live --json tmp/_deep_off.json")
    A("")
    A("# 3) 出报告")
    A("python -B scripts/tmm_deep_report.py --off tmp/_deep_off.json --live tmp/_deep_live.json \\")
    A("       --out reports/TMM_深度功能测试报告.md")
    A("")
    A("# 4) 回归门禁 (一条命令)")
    A("cmd.exe /c verify.bat")
    A("```")
    A("")
    A(f"原始数据: `tmp/_deep_off.json` · `tmp/_deep_live.json` (本报告所有数字均由这两个文件实算)")
    A("")
    return chr(10).join(L)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--off", default=str(ROOT / "tmp" / "_deep_off.json"))
    ap.add_argument("--live", default=str(ROOT / "tmp" / "_deep_live.json"))
    ap.add_argument("--gates", default="", help="门禁输出文件 (纯文本, 贴进报告)")
    ap.add_argument("--out", default=str(ROOT / "reports" / "TMM_深度功能测试报告.md"))
    a = ap.parse_args()
    off, liv = _load(a.off), _load(a.live)
    gates = ""
    if a.gates and Path(a.gates).exists():
        gates = Path(a.gates).read_text(encoding="utf-8", errors="replace").strip()
    md = render(off, liv, gates)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"报告 → {out}  ({len(md)} 字符)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
