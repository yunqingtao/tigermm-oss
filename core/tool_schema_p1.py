# -*- coding: utf-8 -*-
r"""P1 工具可见性收口 —— 11 条插件工具的 schema (2026-09-25)。

为什么单独一个模块 (而不是塞在 core/tool_schema.py 里)
═══════════════════════════════════════════════════════════════════
原本这些直接写在 `tool_schema.py` 的 `BUILTIN_SCHEMAS` 字典里。加完之后那个文件
从 ~376 行长到 597 行 —— 而 `tests/test_architecture_hardening.py` 的
`TestFileOpsCompleteSignal::test_read_returns_complete_flag` 正拿它当"小文件"
样本 (读完整 ⇒ `complete=True`)。file_ops 一次默认只读 500 行, 于是这个样本文件
自己越线, 七道门一起红。

**处置是挪走加量, 不是去改测试** —— 改测试等于把门禁编软。拆出来之后:
    · `tool_schema.py` 回到 ~378 行, 样本假设重新成立;
    · 功能一模一样 (tool_schema.py 里一句 `BUILTIN_SCHEMAS.update(...)` 合并);
    · 顺带把"P1 收口"这块从主文件里分出去, 主文件只留解释器 + 分组逻辑。

内容与判据见 scripts/verification/verify_tool_visibility.py
(每个工具声明了哪些参数、哪些故意不暴露、为什么)。
"""

#: 11 个原来只有 PLUGIN 声明 (没有 params) 的插件工具。
#: 它们原来被 get_all_schemas 的"必须有 params"规则**全挡在模型视野外** ——
#: 模型自己想不到用这些能力, 只能指望技能触发器命中。
#: ★ 每条 params 都对着 tools/*.py 里 run() 的真实入参/真实 kwargs 键抄的。
#:   参数名对不上会变成 09-24 那种 "No command provided" 甩脸。
P1_PLUGIN_SCHEMAS = {
    # ══ 2026-09-25 P1 工具可见性收口 ══════════════════════════════════════
    # 这 11 个插件原来是 PLUGIN-only 声明 (没有 params), 于是 get_all_schemas
    # 只收 TOOL+params 的规则把它们**全挡在模型视野外** —— 办公/语音/翻译/推送
    # 这类高频能力模型自己想不到用, 只能靠技能触发器命中。
    # 为什么写在这里而不是改工具文件: 本文件是 BUILTIN_SCHEMAS 的唯一 owner,
    # 纯加性、零回归 (工具文件另有测试钉着 "PLUGIN 约定", 动它会红)。
    # ★ 每条 params 都对着 tools/*.py 里 run() 的真实入参/真实 kwargs 键抄的,
    #   不是照猜的 —— 参数名对不上会变成 09-24 那种 "No command provided" 甩脸。
    "tiger_office": {
        "name": "tiger_office",
        "description": "办公全家桶 (生成/读取 Office 文件). action 选功能: "
                       "write_word=生成Word(.docx) · write_ppt=生成PPT(.pptx) · "
                       "write_excel=新建Excel(.xlsx) · analyze_table=分析Excel出统计 · "
                       "extract_word=读Word正文 · merge_word=合并多个Word · "
                       "extract_pdf_text=读PDF文字 · extract_pdf_table=读PDF表格 · merge_pdf=合并PDF · "
                       "scan_excel=扫Excel · extract_column=抽某列 · format_excel=美化Excel · "
                       "import_cards/export_cards=实体卡片双向 · file_sort=按类型分拣文件 · batch_rename=批量改名. "
                       "★ 要产出 Word/PPT/Excel 就用这个, 不要用 shell_exec 拼脚本.",
        "params": [
            {"name": "action", "type": "string", "required": True,
             "enum": ["write_word", "write_ppt", "write_excel", "analyze_table",
                      "extract_word", "merge_word", "extract_pdf_text", "extract_pdf_table",
                      "merge_pdf", "scan_excel", "extract_column", "format_excel",
                      "import_cards", "export_cards", "file_sort", "batch_rename"],
             "description": "要做的办公动作 (见工具说明)"},
            {"name": "path", "type": "string", "required": False,
             "description": "输入文件或输出位置 (可以是文件, 也可以是目录 — 目录则自动补文件名; 不给 → 桌面)"},
            {"name": "folder", "type": "string", "required": False,
             "description": "目录 (scan_excel / file_sort / batch_rename 用)"},
            {"name": "title", "type": "string", "required": False,
             "description": "文档标题 (write_word / write_ppt / write_excel)"},
            {"name": "content", "type": "string", "required": False,
             "description": "正文. markdown 风格: '# 标题' / '- 要点' / 普通段落 (write_word); '## 页标题' 开新页 (write_ppt)"},
            {"name": "sections", "type": "string", "required": False,
             "description": "结构化章节 [{heading, body}] (write_word, 与 content 二选一)"},
            {"name": "subtitle", "type": "string", "required": False,
             "description": "副标题 (write_ppt)"},
            {"name": "slides", "type": "string", "required": False,
             "description": "PPT 页 [{title, bullets:[...], body}] (write_ppt, 与 content 二选一)"},
            {"name": "headers", "type": "string", "required": False,
             "description": "Excel 表头 list[str] (write_excel)"},
            {"name": "rows", "type": "string", "required": False,
             "description": "Excel 数据行 list[list] 或 list[dict] (write_excel)"},
            {"name": "sheet", "type": "string", "required": False,
             "description": "工作表名 (默认 Sheet1)"},
            {"name": "col", "type": "string", "required": False,
             "description": "列名 (extract_column 抽列用)"},
            {"name": "files", "type": "string", "required": False,
             "description": "待合并的文件路径列表 (merge_word / merge_pdf)"},
            {"name": "output", "type": "string", "required": False,
             "description": "输出文件路径 (merge_pdf / merge_word)"},
            {"name": "prefix", "type": "string", "required": False,
             "description": "批量改名前缀 (batch_rename)"}
        ]
    },
    "office_cli": {
        "name": "office_cli",
        "description": "按元素路径精确读写 docx (底层 Office CLI, officecli.exe 驱动). "
                       "action: create=新建 · get=按路径读元素 · set=按路径改值 · add=按路径插元素 · "
                       "query=选择器查 · view=看文本 · info=文档结构 · read=读全文 · write_text=写纯文本. "
                       "★ 生成/整篇改写优先用 tiger_office, 这个用于**精确定位到某个段落/单元格**的改动.",
        "params": [
            {"name": "action", "type": "string", "required": True,
             "enum": ["create", "get", "set", "add", "query", "view", "info", "read", "write_text"],
             "description": "要做的动作"},
            {"name": "path", "type": "string", "required": True,
             "description": "docx 文件路径"},
            {"name": "element_path", "type": "string", "required": False,
             "description": "元素路径, 如 '/body/p[1]' (get/set/add 用), 默认 '/', 即文档根"},
            {"name": "value", "type": "string", "required": False,
             "description": "要写入的值 (set)"},
            {"name": "parent_path", "type": "string", "required": False,
             "description": "插入位置的父路径 (add), 默认 '/', 即文档根"},
            {"name": "element_type", "type": "string", "required": False,
             "description": "要插入的元素类型 (add), 默认 'paragraph'"},
            {"name": "selector", "type": "string", "required": False,
             "description": "查询选择器 (query)"},
            {"name": "mode", "type": "string", "required": False,
             "description": "'text' (默认) 或其它视图模式 (view)"},
            {"name": "doc_type", "type": "string", "required": False,
             "description": "文档类型 (create, 可选)"}
        ]
    },
    "voice": {
        "name": "voice",
        "description": "语音输入输出 (TTS 朗读 / 麦克风录音转文字). "
                       "action: speak=把 text 读出来 · record=录麦克风并转文字 · "
                       "transcribe=转录已有音频文件 · full=录完转文字. "
                       "用户说'读出来/念一下/朗读' → speak; 说'我说你听/录音' → record.",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["speak", "record", "transcribe", "full"],
             "description": "speak=朗读 text · record=录音转文字 · transcribe=转录 file · full=录制并转录"},
            {"name": "text", "type": "string", "required": False,
             "description": "要朗读的文字 (action=speak / 不给 action 但给了 text 时自动朗读)"},
            {"name": "file", "type": "string", "required": False,
             "description": "音频文件路径 (action=transcribe)"},
            {"name": "duration", "type": "integer", "required": False,
             "description": "录音秒数 (action=record, 默认 5)"},
            {"name": "lang", "type": "string", "required": False,
             "description": "语言, 默认 'zh'"}
        ]
    },
    "whisper": {
        "name": "whisper",
        "description": "本地 Whisper 把音频文件转成文字 (mp3/wav/m4a/ogg/flac/mp4). "
                       "user 说'把这个录音转成文字/音频转文字' 时用。只转录, 不朗读。",
        "params": [
            {"name": "path", "type": "string", "required": True,
             "description": "音频文件路径 (也接受 audio / file 参数名)"},
            {"name": "lang", "type": "string", "required": False,
             "description": "语言, 默认 'zh'"},
            {"name": "model", "type": "string", "required": False,
             "description": "模型规格, 默认 'base'"}
        ]
    },
    "github_api": {
        "name": "github_api",
        "description": "GitHub REST 操作 (需 token: GH_TOKEN 或 keys_github.json). "
                       "action: whoami=验身份 · repos=列仓库 · issues=列 issue · pulls=列 PR · "
                       "create_issue=开 issue · digest=巡检汇总.",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["whoami", "repos", "issues", "pulls", "create_issue", "digest"],
             "description": "要做的动作, 默认 whoami"},
            {"name": "repo", "type": "string", "required": False,
             "description": "仓库全名, 如 'owner/repo' (issues/pulls/create_issue 用)"},
            {"name": "state", "type": "string", "required": False,
             "description": "issue/PR 状态: 'open' (默认) / 'closed' / 'all'"},
            {"name": "title", "type": "string", "required": False,
             "description": "issue 标题 (create_issue)"},
            {"name": "body", "type": "string", "required": False,
             "description": "issue 正文 (create_issue)"},
            {"name": "limit", "type": "integer", "required": False,
             "description": "最多几条, 默认 10 (上限 50)"}
        ]
    },
    "sms_send": {
        "name": "sms_send",
        "description": "发短信 (腾讯云 SMS, 需配置 secret_id/app_id). "
                       "action: send=发短信 (要 phones + params 模板参数) · status=看是否配置好.",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["send", "status"], "description": "默认 send"},
            {"name": "phones", "type": "string", "required": False,
             "description": "手机号, 多个用逗号分隔 (也接受 to); 缺 +86 会自动补"},
            {"name": "params", "type": "string", "required": False,
             "description": "短信模板参数, 多个用逗号分隔 (也接受 template_params)"}
        ]
    },
    "im_notify": {
        "name": "im_notify",
        "description": "发群机器人消息 (钉钉 / 飞书 webhook). "
                       "action: dingtalk=发钉钉 · feishu=发飞书 · status=看配置了哪个.",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["dingtalk", "feishu", "status"], "description": "默认 dingtalk"},
            {"name": "content", "type": "string", "required": False,
             "description": "消息正文 (也接受 text)"},
            {"name": "title", "type": "string", "required": False,
             "description": "消息标题"},
            {"name": "msg_type", "type": "string", "required": False,
             "description": "消息类型, 默认 'text'"},
            {"name": "at_all", "type": "boolean", "required": False,
             "description": "是否 @ 所有人"}
        ]
    },
    "push_notify": {
        "name": "push_notify",
        "description": "手机推送 (Server酱 / PushPlus). "
                       "action: serverchan=Server酱 · pushplus=PushPlus · broadcast=两个都发 · status=看配置.",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["serverchan", "pushplus", "broadcast", "status"], "description": "默认 serverchan"},
            {"name": "title", "type": "string", "required": False,
             "description": "推送标题"},
            {"name": "desp", "type": "string", "required": False,
             "description": "推送正文 (也接受 body)"}
        ]
    },
    "service_check": {
        "name": "service_check",
        "description": "查本机服务活着没 (TCP 存活+延迟, 已知服务顺带 HTTP 探测). "
                       "action: ports (默认) 查端口 · http 只看 HTTP 接口内容. "
                       "user 说'引擎在跑吗/服务挂了没/端口通不通' 时用。",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["ports", "http"], "description": "默认 ports"},
            {"name": "extra", "type": "string", "required": False,
             "description": "临时加的端口, 格式 '名字:端口,名字:端口'"},
            {"name": "timeout", "type": "number", "required": False,
             "description": "单个探测超时秒数, 默认 1.5 (上限 5)"}
        ]
    },
    "session_logs": {
        "name": "session_logs",
        "description": "查自己的历史对话记录 (哪些轮次、说了什么、路由到哪). "
                       "user 说'今天聊了什么/翻一下记录' 时用。",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "description": "动作, 如 'list' / 'stats'"},
            {"name": "keyword", "type": "string", "required": False,
             "description": "按关键词过滤"},
            {"name": "limit", "type": "integer", "required": False,
             "description": "最多几条, 默认 20"},
            {"name": "days", "type": "integer", "required": False,
             "description": "看最近几天, 0 = 不限"}
        ]
    },
    "usage_stats": {
        "name": "usage_stats",
        "description": "统计自己的用量 (模型调用次数/token/耗时). action: summary 汇总 · today 只看今天.",
        "params": [
            {"name": "action", "type": "string", "required": False,
             "enum": ["summary", "today"], "description": "默认 summary"},
            {"name": "days", "type": "integer", "required": False,
             "description": "统计最近几天, 0 = 不限"},
            {"name": "limit", "type": "integer", "required": False,
             "description": "扫描条数上限, 默认 20000"}
        ]
    },
    # ══ 2026-09-28: 统一表格操作层 ══════════════════════════════════════════
    # 为什么要有它 (用户实测连撞四轮): 「续写20条记录」被 file_write 当成**文件内容**
    # 写进了 CSV (原有 20 行被覆盖); 「添加40条人员信息」数字撞上知识库一条带 "40" 的
    # 罐头答案, 回了"冰的密度小于水"; 「第一行填表头」模型自己只写了个表头, 数据没了。
    # 根子不是某条路写错了 —— 是**没有一层"表格"的模型**: file_ops 只有 read/write/append
    # (append 只懂纯文本, 不懂表头列), tiger_office 只会整篇 write_excel (不会"在第 20 行
    # 后面加 20 行"), 而"加一列""改表头""填某个格"根本没人认领。
    # 表格的行/列/表头/单元格是有结构的东西, 交给这个懂结构的工具。
    # ★ 一条铁律写在工具里: 原有数据默认不动, 干完重读一遍数行数列数并回报。
    "table_ops": {
        "name": "table_ops",
        "description": "统一表格操作 (CSV/Excel/Word 的表格). action 选功能: "
                       "info=看结构 · read=读几行 · add_rows=加行(给 count 或 data; 只说条数就按表头生成) · "
                       "del_rows=删行(rows=\"2,5\" 或 \"2-5\" / where_col+where_val) · "
                       "add_col=加一列(name) · del_col=删一列(name) · "
                       "set_cell=改某个格子(row+col+value) · set_header=设/改表头(names 或 map) · "
                       "sort=排序(by+order) · dedupe=去重(by) · split=拆分(by_rows 或 by_col) · "
                       "merge=合并(paths+target). "
                       "★ 往表里加记录/加行列/改表头/拆分合并一律用这个, 不要用 file_ops 的 "
                       "write/append (它只懂整篇文本, 会把指令当内容写进文件、把表头冲掉). "
                       "原有数据默认不动; 干完会重读磁盘核对行数列数。",
        "params": [
            {"name": "action", "type": "string", "required": True,
             "enum": ["info", "read", "add_rows", "del_rows", "add_col", "del_col",
                      "set_cell", "set_header", "sort", "dedupe", "split", "merge"],
             "description": "要做的表格操作"},
            {"name": "path", "type": "string", "required": True,
             "description": "表格文件路径 (.csv/.xlsx/.docx)"},
            {"name": "count", "type": "integer", "required": False,
             "description": "add_rows: 加几行 (只说条数、没给数据时按表头生成)"},
            {"name": "data", "type": "string", "required": False,
             "description": "add_rows: 用户给的**真数据** (CSV/TSV 文本, 每行一条, 列顺序同表头)"},
            {"name": "rows", "type": "string", "required": False,
             "description": "del_rows: 行号 (不含表头, 从 1 数), 如 \"2,5\" 或 \"2-5\""},
            {"name": "where_col", "type": "string", "required": False,
             "description": "del_rows: 按列值删, 列名"},
            {"name": "where_val", "type": "string", "required": False,
             "description": "del_rows: 按列值删, 要匹配的值"},
            {"name": "name", "type": "string", "required": False,
             "description": "add_col/del_col: 列名"},
            {"name": "col", "type": "string", "required": False,
             "description": "列名或 1 起的列号 (set_cell/sort 等)"},
            {"name": "row", "type": "integer", "required": False,
             "description": "set_cell: 行号 (不含表头, 从 1 数)"},
            {"name": "value", "type": "string", "required": False,
             "description": "set_cell: 要写入的值"},
            {"name": "names", "type": "array", "required": False,
             "description": "set_header: 整行表头 (字符串数组); 表里第一行若是数据会自动插入而不是替换"},
            {"name": "map", "type": "object", "required": False,
             "description": "set_header: {旧列名: 新列名} 只改名字"},
            {"name": "default", "type": "string", "required": False,
             "description": "add_col: 新列默认值"},
            {"name": "by", "type": "string", "required": False,
             "description": "sort/dedupe: 按哪一列"},
            {"name": "order", "type": "string", "required": False,
             "enum": ["asc", "desc"], "description": "sort: 升序 asc / 降序 desc"},
            {"name": "by_rows", "type": "integer", "required": False,
             "description": "split: 每份几行"},
            {"name": "by_col", "type": "string", "required": False,
             "description": "split: 按哪一列的值分开"},
            {"name": "outdir", "type": "string", "required": False,
             "description": "split: 输出目录 (默认跟源文件同目录)"},
            {"name": "paths", "type": "string", "required": False,
             "description": "merge: 源文件列表, 用 ; 或换行分隔"},
            {"name": "target", "type": "string", "required": False,
             "description": "merge: 输出文件路径"},
            {"name": "sheet", "type": "string", "required": False,
             "description": "Excel 工作表名"},
            {"name": "table_index", "type": "integer", "required": False,
             "description": "Word: 第几个表格 (从 1 数)"},
            {"name": "encoding", "type": "string", "required": False,
             "description": "CSV 编码覆盖 (如 gbk / utf-8-sig), 只在自动识别不对时才用"},
            {"name": "dry_run", "type": "boolean", "required": False,
             "description": "只预演不落盘"}
        ]
    },
}
