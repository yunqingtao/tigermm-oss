"""
Intent Router — 分类引擎。意图+槽位→工具链。
将用户消息拆为"动作"和"结果去向"，运行时组合工具链。

三类任务:
  1. 单向: 动作→结果（展示/保存/发送）
  2. 链式: 动作→动作（批量操作）
  3. 直达: 直接文件操作
"""
import re, logging, time, os
from typing import Optional
from pathlib import Path

logger = logging.getLogger("core.intent_router")

ACTION_INTENTS = {
    "search":       {"patterns": ["搜", "搜索", "查一下", "查", "百度"],          "tool": "web_search",       "action": "search",         "output_type": "text"},
    "file_read":    {"patterns": ["读", "读取", "打开文件"],                      "tool": "file_ops",         "action": "read",           "output_type": "text"},
    "file_write":   {"patterns": ["创建", "新建", "写入", "写文件"],               "tool": "file_ops",         "action": "write",          "output_type": "text"},
    # ★ 2026-09-26 新增 (用户发火那次): 「增加/添加/追加/填入」这类**增补动词**原先
    #   一个都不在表里 ⇒ 带 .csv 路径的消息落进 1.5 节, 而那里只认"创建/新建/写入/…"
    #   ⇒ 判成 file_read ⇒ **把文件原样回显当答案**。用户连问 6 次, 6 次都撞这条。
    #   修法不是把"增加"塞进 file_write 的写词表 —— 那会让它去**整篇覆盖**(原有内容
    #   被冲掉)。这里给它**独立动作**: file_ops 的 append (只追加, 绝不截断)。
    "file_append":  {"patterns": ["增加", "添加", "追加", "填入", "填充", "补充", "增补",
                                  "多加", "添上", "补上", "加进", "再增加", "追加到"],
                     "tool": "file_ops",         "action": "append",         "output_type": "text"},
    # ★★ 2026-09-28 新增: 表格操作统一入口 → table_ops。
    #   为什么必须单开一门: 「续写20条记录」里有**一个"写"字**, 被判成 file_write,
    #   而 file_write 的内容提取是"抠掉路径剩下什么就是内容" ⇒ 真把「续写20条记录」
    #   这句指令写进了 CSV, 原有 20 行被覆盖。而「添加40条人员信息」数字撞上知识库
    #   里一条带 "40" 的罐头答案, 回了"冰的密度小于水"。同一个文件连撞四轮,
    #   每轮走不同的路, 互不知情 —— 根子是**没有一层"表格"的模型**。
    #   行/列/表头/单元格是有结构的, 必须交给懂结构的工具 (table_ops)。
    "table_edit":   {"patterns": ["续写", "添加记录", "增加记录", "添加人员信息", "加行", "加列",
                                  "增列", "删列", "删行", "改表头", "表头", "改列名",
                                  "拆分表格", "拆分表", "合并表格", "合并表", "去重", "查重",
                                  "排序表格", "单元格"],
                     "tool": "table_ops",     "action": "info",          "output_type": "text"},
    # ★★ 2026-09-28 新增 (用户 23:38 原话实测): 图片 ⇒ **看图 (vision)**, 永不走"读文本"。
    #   实测原话 `"<用户目录>\Desktop\TMM_Share_Card_EN_20260928.png"这是什么`
    #   → 1.5 节判 file_read → file_ops 按 utf-8 读 PNG → 回给用户的是
    #     `读文件失败: 'utf-8' codec can't decode byte 0x89 in position 0: invalid start byte`
    #   (同一张图 20:22 裸路径也栽同一条: `✗ 'utf-8' codec…` —— PNG 魔数 0x89)。
    #   而 tools/vision.py **早就**能看图 (本地 qwen2.5vl / gemma4 自报 vision 能力),
    #   是路由表里**一条指向 vision 的路都没有** —— 能力在、门口没开。
    #   本条目给门口: 图片路径的消息 → build_chain 出 vision 步骤 (image=路径, question=原话)。
    "vision":       {"patterns": ["看图", "识图", "识别这张图", "看看这张图", "看看这图",
                                  "这张图里", "图里有什么", "图片里有什么", "这是什么图"],
                     "tool": "vision", "action": None, "output_type": "text",
                     "slot_map": {"image": "image", "question": "content"}},
    "send_msg":     {"patterns": ["发给", "发送给", "通知", "告诉"],                "tool": None,               "action": "send",           "output_type": "text"},
    "file_list":    {"patterns": ["列出", "文件列表", "列目录", "列一下"],            "tool": "file_ops",         "action": "list",           "output_type": "text"},
    "file_find":    {"patterns": ["找一下", "查找文件", "搜索文件"],               "tool": "file_ops",         "action": "search",         "output_type": "list"},
    "file_rename":  {"patterns": ["重命名", "改名", "改成"],                      "tool": "file_ops",         "action": "rename",         "output_type": "text"},
    "file_delete":  {"patterns": ["删除", "删掉", "移除"],                        "tool": "file_ops",         "action": "delete",         "output_type": "text",
                      "slot_map": {"path": "content", "dir_path": "dir_path"}},
    # ★ 2026-09-25 补 pairs (监控日报实测): 模式里只有"复制到/拷贝到"这种**挨着**的说法,
    #   `拷贝 D:\a\b.txt 到桌面` 中间夹着路径 ⇒ 漏判 → 落成 file_read (变成"读", 不是"拷")。
    "file_copy":    {"patterns": ["复制到", "拷贝到"],                            "tool": "file_ops",         "action": "copy",           "output_type": "text",
                     "pairs": [["复制", "到"], ["拷贝", "到"]]},
    "file_move":    {"patterns": ["移动到", "挪到", "移到"],                       "tool": "file_ops",         "action": "move",           "output_type": "text",
                     "pairs": [["移动", "到"], ["挪到", "到"]]},
    "file_organize":{"patterns": ["整理", "分类", "归档"],                        "tool": "file_ops",         "action": "organize",       "output_type": "text"},
    # ★★ 2026-09-24 加 (用户实测): 「今天干了什么」这类**今日汇总** → artifacts 工具。
    #   原来分类对了(domain_override 里有 artifacts) 但 ACTION_INTENTS 没这条 ⇒
    #   build_chain 找不到 tool ⇒ 没执行链 ⇒ 掉 model.fallback 答"我无法感知您的活动"。
    #   实测活引擎: 加之前 route=model.fallback, 加之后应走 ir.chain + artifacts 工具。
    "artifacts":    {"patterns": ["今天做了什么", "今天干了什么", "今天做了啥", "今天干了啥",
                                  "今天都干了啥", "今天都做了什么", "总结今天", "今天的总结",
                                  "产出了什么", "交付物", "产物清单", "成品在哪", "输出的文件清单"],
                     "tool": "artifacts", "action": "list", "output_type": "text"},
    "screenshot":   {"patterns": ["截图", "截屏"],                                "tool": "windows_desktop",  "action": "screenshot_now", "output_type": "file_path"},
    "open_browser": {"patterns": ["打开百度", "打开谷歌", "打开浏览器", "浏览网页"], "tool": "windows_desktop",  "action": "open_browser",   "output_type": "text"},
    # ★ 2026-09-26 加 (真引擎实测, 日报 ① 里的一轮): 「ollama现在加载的哪个大模型」
    #   原来掉兜底 —— 49.9 秒, 模型还编出「根据你提供的命令输出」(其实没有输出)。
    #   现成工具能答 (service_check 的 models action 读 /api/ps), 只是没接线。
    #   只收**明确在问 ollama/本地模型加载情况**的说法, 不收裸"模型"(会抢走别的)。
    "ollama_models": {"patterns": ["ollama现在加载的哪个大模型", "ollama 现在加载的哪个大模型",
                                   "ollama现在加载的哪个模型", "ollama现在加载哪个模型",
                                   "ollama加载的哪个模型", "ollama加载了哪个模型",
                                   "ollama现在加载什么模型", "ollama现在用的哪个模型",
                                   "ollama当前加载的模型", "ollama现在加载的是哪个",
                                   "现在加载的哪个大模型", "本地现在加载的哪个模型",
                                   "本地加载了哪个大模型", "ollama 现在跑的是哪个模型"],
                     "tool": "service_check",    "action": "models",         "output_type": "text"},
    # ★ 2026-09-26 加 (真引擎原话实测): 「你能识别我的电脑吗」掉兜底闲聊。
    #   监控日报里这条**用户重发过** (09-20 13:45 一次, 又是"用户重发"段的头几条),
    #   而 09-20 那次的回复是"不能, 我现在处于问答模式(ASK MODE), 没有任何工具权限"
    #   —— 即**不是能力缺, 是当时模式不对**; 模式正常时本工具答得上来 (sysinfo 有
    #   OS/Host/Python/CWD)。所以补模式把这类"认不认得我的机器"的说法接住。
    #   只收**明确指向本机**的说法 ("我的电脑/我这台机器"), 不收裸"识别"(会抢走
    #   "识别这张图/这段语音")。
    "system_info":  {"patterns": ["系统状态", "系统信息", "电脑状态",
                                  "你能识别我的电脑吗", "能识别我的电脑吗", "识别我的电脑吗",
                                  "认识我的电脑吗", "认得我的电脑吗", "能认得我的电脑吗",
                                  "能看到我的电脑吗", "能看我的电脑吗", "你能看到我的电脑",
                                  "识别一下我的电脑", "识别我的电脑",
                                  "你能识别我这台电脑吗", "能识别我这台电脑吗",
                                  "看看我的电脑配置", "我的电脑什么配置"],
                     "tool": "system_info",      "action": "sysinfo",        "output_type": "text"},
    "disk_info":    {"patterns": ["磁盘", "硬盘空间", "磁盘空间"],                  "tool": "system_info",      "action": "disk",           "output_type": "text"},
    # ★ 2026-09-25 加 (P0, 只读探针实测 "今天几号/现在几点/今天星期几" **三条全部
    #   空路由** ⇒ 掉大模型直答)。system_info 本来就有 action=time, 接上即可。
    #   模式取**具体说法**, 不收裸"几号"(会命中"几号发货"这类)。
    "sys_time":     {"patterns": ["今天几号", "今天几月几号", "今天是几号", "现在几点",
                                  "几点了", "今天星期几", "今天周几", "什么日子",
                                  "今天日期", "当前时间", "现在时间", "今天多少号"],
                     "tool": "system_info",      "action": "time",            "output_type": "text"},
    # ★ 2026-09-25 加 (P0 只读勘查): 干活账**只写不读** —— core/task_ledger.py 早就
    #   在每轮真实对话记账 (本机 1211 轮), 但 tools/ 下**没有任何工具包它** ⇒
    #   用户问「今天干了几轮」「为什么老失败」时路由层无物可接, 掉模型闲聊。
    #   ★ 只接**账本能回答的** (汇总): 轮次/成败/类别/路由/能力。
    #   **不接** "做完了吗""继续跑" —— 那问的是单件任务在途状态, 账本只有聚合、
    #   不存原话, 接过去只能编答案 (反向用例钉在 verify_task_ledger)。
    # ★ 2026-09-25 修 (门禁 verify_task_routing_fixes B 段抓到的**真回归**):
    #   本意图上一版收了 "今天干了什么" —— 而那句是**用户实测缺陷③**的契约句子,
    #   归属 `artifacts`(产出清单)。两处都命中时本意图优先级高, 于是把它抢走了:
    #       "总结今天干了什么" / "今天干了什么" → ledger_progress  ✗ (应为 artifacts)
    #   判据: 账本只认**带"多少/几轮/账"的问法**(问数量), "今天干了什么"是**问产出** ⇒ 归 artifacts。
    "ledger_progress": {"patterns": ["干了多少活", "干了几轮", "干活账", "账本",
                                     "今天做了多少", "成功率多少",
                                     "今天干了多少活", "今天干了几轮"],
                        "tool": "task_ledger",   "action": "today",           "output_type": "text"},
    "ledger_failed":   {"patterns": ["为什么失败", "哪里失败", "什么失败了", "失败了多少",
                                     "失败统计", "老是失败", "最近报错", "报错多吗",
                                     "哪个环节失败", "怎么老出错"],
                        "tool": "task_ledger",   "action": "failed",          "output_type": "text"},
    # ★★ 2026-09-25 加 (用户派的活): 「做完了吗 / 继续跑 / 到哪了」原来**接不了** ——
    #   干活账只有聚合、不存原话 (上一段注释写明了为什么不接)。现在接了**另一本账**:
    #   `core/task_flow.py` (在途台账) 记的是**单件任务的生命周期**(开单/每步/关单),
    #   所以这些问句能老实回答。
    #   边界 (反向用例钉在 verify_task_ledger / verify_task_flow):
    #     · 只认**问那件事做完没**的说法; 问"干了多少活"仍归 ledger_progress (问数量),
    #       问"今天干了什么"仍归 artifacts (问产出) —— 本意图**不许**抢那两个。
    #     · 没在跑就明说没有, 不许拿"最近完成的那件"冒充在跑的。
    "task_status":  {"patterns": ["做完了吗", "做完了嘛", "做完了没", "做完没", "做完没有",
                                 "完成了吗", "完成了没", "完成了？", "完成了没？",
                                 "完了吗", "完了没", "跑完了吗", "跑完了没", "好了吗",
                                 "还在跑吗", "在跑什么", "到哪了", "到哪一步了",
                                 "进行到哪了", "还要多久", "进度怎么样", "继续跑",
                                 "任务状态", "那件事怎么样了", "那个做完了吗"],
                    "tool": "task_flow",     "action": "current",         "output_type": "text"},
    # ★ 2026-09-25 加 (P0, 只读勘查实测): 「你改了什么 / 有什么没提交 / 最近的提交」
    #   10 条真实说法 (含字面 `git status`) **全部空路由** ⇒ 掉兜底(模型闲聊)。
    #   注: 工具层本来就有 shell_exec 能跑 git —— 缺的是**路由**, 不是能力。
    #       但没把这些话接到 shell_exec: 那要由路由层拼命令串 = 注入面;
    #       本工具的命令是写死常量, 用户输入进不去 (见 tools/repo_status.py)。
    "repo_status":  {"patterns": ["未提交", "没提交", "工作树", "仓库状态", "仓库干净",
                                  "git状态", "git 状态", "git status", "你改了什么", "改了啥",
                                  "改了什么", "改了哪些文件", "动了哪些文件", "有什么改动",
                                  "最近的改动", "工作区干净", "几个文件没提交"],
                     # ★ 词对: "仓库现在干净吗" 里 仓库/干净 不挨着, 子串匹配不到 ⇒ 补词对。
                     #   权重 20, 命中即压过泛化意图 (实测该句原来**空路由**)。
                     "pairs": [["仓库", "干净"], ["工作树", "干净"], ["工作区", "干净"],
                               ["仓库", "状态"]],
                     "tool": "repo_status", "action": "status", "output_type": "text"},
    "repo_diff":    {"patterns": ["改动详情", "改动统计", "diff统计", "改了哪些内容",
                                  "改了多少行", "行数改了多少"],
                     "tool": "repo_status", "action": "diff", "output_type": "text"},
    "repo_log":     {"patterns": ["提交记录", "提交历史", "最近的提交", "最近提交",
                                  "最近几次提交", "几次提交", "几个提交", "提交日志",
                                  "commit记录", "commit 记录"],
                     # "仓库有几个提交" = 问**总数**, 别被子串匹配拉去 status
                     "pairs": [["仓库", "几个提交"], ["仓库", "几次提交"], ["一共", "提交"]],
                     "tool": "repo_status", "action": "log", "output_type": "text"},
    # ★★ 2026-09-25 加: 「谁改的 X」—— 这条以前**问不着**, 且有两种坏法 (只读勘查实测):
    #   ① `谁改的 README` (没扩展名) ⇒ **空路由** ⇒ 掉兜底闲聊;
    #   ② `谁改的 core/intent_router.py` ⇒ 被 **file_read 抢走** ⇒ 系统把**文件内容**读出来
    #      当答案 (用户问"谁改的", 收到的是正文) —— 比空路由更坏, 因为它看起来像答了。
    #   打分公式是 `INTENT_PRIORITY + (2 if 名字以 file_ 开头)`, 所以 file_read 实际是 4 分;
    #   本意图给 **5** 才能压过它 (放在 INTENT_PRIORITY 里那行有注释)。
    #   `slot_map` 复用 REGEX_SLOTS 抠出来的 file_path (带扩展名/路径的说法走这条);
    #   没有扩展名的说法由 build_chain 从 content 里挑 (见 `_guess_file_token`)。
    "repo_history": {"patterns": ["谁改的", "谁改过", "谁修改的", "谁动过", "谁动的",
                                  "改动历史", "修改历史", "变更历史", "谁提交的"],
                     # 不挨着的说法: "README 是谁改的" / "这个文件谁改过"
                     "pairs": [["谁", "改的"], ["谁", "改过"], ["谁", "修改"], ["谁", "动的"],
                               ["谁", "动的"], ["文件", "改动历史"], ["文件", "修改历史"]],
                     "slot_map": {"file": "file_path"},
                     "tool": "repo_status", "action": "file_history", "output_type": "text"},
    # ★★ 2026-09-25 加: 双色球 (来自 121 轮兜底复盘 —— 8 轮全掉模型闲聊, 而这两类
    #   **本来就是确定性的**, 本地数据就能答):
    #     ① "去看看双色球2026086-2026095开奖结果" × 4 轮
    #        ⇒ 本地 `lottery/双色球历史数据.txt` 就有 (期号+6红+蓝), 查表即可, 不用联网;
    #     ② "双色球7+1复式多少钱" × 4 轮 (用户还写成"复试")
    #        ⇒ 纯组合数: 注数 = C(红,6) × 蓝, 每注 2 元。7+1 = 14 元 (他常买的正是这个)。
    #   为什么不做 slot_map: 期号区间 (2026086-2026095) 和复式规模 (7+1) **抠错一个数就是
    #   错的号码/错的价钱** ⇒ 参数一律由 build_chain 把**原话**原样递给工具, 工具自己解析
    #   (parse_issues / parse_red_blue), 那边有单测。
    "ssq_draw":    {"patterns": ["开奖结果", "开奖号码", "开奖号", "开奖", "中奖号码", "几号开的"],
                    "pairs": [["双色球", "结果"], ["双色球", "开奖"], ["球", "开奖"]],
                    "tool": "ssq", "action": "draw", "output_type": "text"},
    "ssq_price":   {"patterns": ["复式多少钱", "复试多少钱", "复式几注", "复试几注", "复式多少注",
                                 "复式投注多少", "复式多少钱一注", "复式要多少钱"],
                    # "双色球7+1的复试多少钱" 这种夹着规模的说法靠 pairs (不挨着)
                    "pairs": [["复式", "多少钱"], ["复试", "多少钱"], ["复式", "几注"]],
                    "tool": "ssq", "action": "price", "output_type": "text"},
    "check_mail":   {"patterns": ["收邮件", "查邮件", "看邮件", "收件箱", "邮件"], "tool": "check_mail", "action": "list", "output_type": "text"},
    "kb_search":    {"patterns": ["知识库", "查手册", "查文档", "手册里", "文档里", "说明书", "资料里"], "tool": "kb_search", "action": None, "output_type": "text"},
    "kb_list":      {"patterns": ["知识库里有什么", "知识库有什么", "知识库有哪些", "知识库都有什么", "库里有啥", "知识库列表", "知识库里有啥"], "tool": "kb_list", "action": None, "output_type": "text"},
    "kb_delete":    {"patterns": ["清空知识库", "删掉知识库", "删除知识库", "从知识库删", "知识库删掉", "知识库删除", "移除知识库", "知识库清空"], "tool": "kb_delete", "action": None, "output_type": "text"},
    "weather":      {"patterns": ["天气", "查天气", "气温", "下雨"],                        "tool": "openmeteo",        "action": None,             "output_type": "text"},
    "chart":        {"patterns": ["图表", "比例图", "饼图", "柱状图", "折线图", "画图", "统计图", "占比图", "生成图", "可视化"], "tool": "chart", "action": "pie", "output_type": "file_path"},
    # 🔴 office 工具触发词 (2026-08-19): 明确工具诉求 → tiger_office 专业统计喂模型
    # 不直接返回工具输出, pipeline 里走 _office_preread (工具+喂模型)
    "office_analyze": {"patterns": ["分析表格", "表格分析", "统计表格", "表格统计", "分析一下表格", "表格数据", "表格里", "表里", "表格内容", "表格信息"], "tool": "tiger_office", "action": "analyze_table", "output_type": "text",
                       "pairs": [["分析", "表"], ["统计", "表"], ["表格", "分析"], ["表", "统计"]]},
    "office_extract": {"patterns": ["提取列", "提取某列", "提取.*列", "第.*列.*数据", "列数据", "列内容"], "tool": "tiger_office", "action": "extract_column", "output_type": "text",
                       "pairs": [["提取", "列"], ["第", "列"]]},
    "office_pdf":    {"patterns": ["合并pdf", "合并PDF", "pdf合并", "PDF合并", "提取pdf", "提取PDF", "pdf表格", "PDF表格", "pdf内容", "PDF内容"], "tool": "tiger_office", "action": "merge_pdf", "output_type": "text",
                      "pairs": [["合并", ".pdf"], ["pdf", "合并"], ["提取", ".pdf"], ["pdf", "提取"]]},
    "office_word":   {"patterns": ["提取word", "提取Word", "word内容", "Word内容", "读word", "读Word", "合并word", "合并Word"], "tool": "tiger_office", "action": "extract_word", "output_type": "text",
                      "pairs": [["word", "合并"], ["word", "提取"], ["word", "内容"], ["word", "读"]]},
    # ★ 2026-09-19 修: 原来只有 ["发邮件","发送邮件"], 而 check_mail 的模式里有**裸"邮件"**
    #   → "给涛哥写封邮件" 被判成 check_mail(读收件箱!) 而不是 send_email。
    #   补 creation 形态后: 同优先级(3)下按**匹配长度**比较, "写封邮件"(4) > "邮件"(2) → send_email 胜。
    #   (顺带去掉了重复定义的同名键 —— 复制粘贴遗留)
    "send_email":   {"patterns": ["发邮件", "发送邮件", "写邮件", "写封邮件", "写一封邮件",
                                  "写份邮件", "寄邮件", "发封邮件", "发个邮件", "发一份邮件",
                                  "发一封邮件", "email他", "email她"],
                     "tool": "send_email", "action": "send", "output_type": "text"},
    # 🔴 2026-08-24: "http" 裸子串会误匹配任何含 http 的文本(同 system_info "ps" 事故)。
    # 改精确协议前缀; 具体动作(翻译/搜索)优先级高于 url_shared, 由 _domain_override 覆盖。
    "url_shared":   {"patterns": ["http://", "https://"],                          "tool": None,               "action": None,             "output_type": "text"},
}

# Priority tiers (higher = wins on equal-length match)
# Tier 3: explicit domain actions that must NOT be shadowed by search
# Tier 2: file operations
# Tier 1: generic search (lowest)
INTENT_PRIORITY = {
    "sys_time": 3,
    "ledger_progress": 3, "ledger_failed": 3, "task_status": 3,
    "repo_status": 3, "repo_diff": 3, "repo_log": 3,
    # ★ repo_history 给 **5** (不是 3): 打分是 `priority + (2 if 名字以 file_ 开头)`,
    #   带路径的"谁改的 X.py"会同时命中 file_read(=2+2=4); 3 分会输给它 ⇒ 又被读成文件正文。
    "repo_history": 5,
    "check_mail": 3, "weather": 3, "screenshot": 3, "open_browser": 3, "system_info": 3, "disk_info": 3, "kb_search": 3, "kb_list": 3, "kb_delete": 3, "chart": 3,
    "ssq_draw": 3, "ssq_price": 3,
    "office_analyze": 3, "office_extract": 3, "office_pdf": 3, "office_word": 3,
    "file_read": 2, "file_write": 2, "file_append": 3, "file_list": 2, "file_find": 2, "file_copy": 2,
    "file_move": 2, "file_delete": 2, "file_rename": 2, "file_organize": 2,
    "send_email": 3,
    "search": 1,
}

DELIVERY_INTENTS = {
    "email": {"patterns": ["发给", "发送给", "告诉", "通知", "发邮件给", "发邮件"], "tool": "send_email"},
    "save":  {"patterns": ["保存到", "保存", "存到", "存", "放到", "导出到", "导出"], "tool": "file_ops"},
    "show":  {"patterns": ["展示", "显示", "看看", "看一下"], "tool": None},
}

#: 「谁改的 README」这类句子里挑文件名词: 只剔**整个 token** 等于下列词的,
#: 不做子串剥离 (否则 `改价.py` / `诗.txt` 这种文件名会被削)。
_HIST_STOPWORDS = {
    "谁", "谁改", "谁改的", "改的", "改", "谁改过", "改过", "谁修改的", "修改的",
    "谁动过", "动的", "谁动的", "动过", "改动历史", "修改历史", "变更历史",
    "提交历史", "提交记录", "谁提交的", "文件", "这个", "那个", "这个文件", "那个文件",
    "是", "的是", "这", "那", "到底", "究竟", "一下", "看看", "看", "查", "查一下", "的",
    "是谁", "是谁改", "是谁改的", "是谁动的", "是谁修改的", "是谁提交的",
}


def _strip_stop_suffix(tok: str) -> str:
    """反复剥掉结尾**恰好**是停用词的片段; 剥空或只剩停用词 ⇒ 空串。

    ★ 为什么需要 (实测): `这个文件是谁改的` 的 content 是 `这 文件是` ——
      `文件是` 不是停用词, 整个留下来 ⇒ 工具拿着 "文件是" 去找文件, 回"没找到"。
      剥法只认**整词结尾**, 不做子串替换 ⇒ `改价.py` / `诗.txt` 这种名字不会被削。
    """
    cur = tok
    changed = True
    while changed and cur:
        changed = False
        for w in _HIST_STOPWORDS:
            if len(w) < len(cur) and cur.endswith(w):
                cur = cur[: -len(w)]
                changed = True
                break
    return "" if (not cur or cur in _HIST_STOPWORDS) else cur


def _guess_file_token(content: str) -> str:
    """从 content 里挑文件名词。全被剔掉 ⇒ 空串 (工具会问"查哪个文件")。

    ★ 中英混在一起时 (`谁改README`, 空格被剔除逻辑吃掉) 只剥**开头连续的中文**,
      且剥完必须不像扩展名 (`诗.txt` 剥成 `.txt` ⇒ 保留原样)。
    """
    toks = [t for t in re.split(r"[\s,，。;；?？!！]+", content or "") if t]
    out = []
    for t in toks:
        t = _strip_stop_suffix(t)
        if not t or t in _HIST_STOPWORDS:
            continue
        if re.search(r"[\u4e00-\u9fff]", t) and re.search(r"[A-Za-z]", t):
            cut = re.sub(r"^[\u4e00-\u9fff]+", "", t)
            if cut and not cut.startswith(".") and re.search(r"[A-Za-z]", cut):
                t = cut
        out.append(t)
    return " ".join(out).strip()


ENTITY_SLOTS = {"recipient": {"field": "email"}, "city": {"field": "city"}}
REGEX_SLOTS = {
    "file_path": r'([\w\-\\/:]+\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip))',
    # 2026-08-20: dir_path 排除 "txt文档/word文档" 复合词误捕 (原正则把 "txt文档" 的"文档"当目录 → 桌面被顶替成 Documents)
    "dir_path":  r'(桌面|下载|D盘|C盘|(?<![A-Za-z0-9])文档(?![A-Za-z0-9]))',
    "mail_keyword": r'(?:关于|有没有)(.+?)(?:的邮件|邮件)', 
}

#: ★★ 2026-09-28 加: 图片路径 (含引号包裹 / 中文目录)。用于"看图"路由 —— 见 classify 的 1.42 节。
#:   形状照抄 pipeline 视觉分支那条**已验证过**的写法 (`["\']?` / `[\\/]` / `[^\n"\']`),
#:   不自己发明转义 —— 多一层少一层都会让 `C:\...\a.png` 匹配不上:
#:   写成 `[\\\\/]` 只认正斜杠路径 (实测: 桌面那种反斜杠路径一个都匹配不到)。
_IMG_PATH_RE = re.compile(
    r'["\']?([A-Za-z]:[\\/][^\n"\']*?\.(?:png|jpe?g|gif|webp|bmp))["\']?',
    re.IGNORECASE)
#: 允许被"看图"接管的既有结论 (空串 = 还没定动作)。带写/发/表格类动作的句子**不许抢** ——
#: 实测踩点: 「把这张图 D:\a.png 发给涛哥」里有"发给" ⇒ 必须照旧走发送链。
#: url_shared 也放行: 整条消息就是一张图片链接时, 看图 (vision 吃得下 URL) 比抓网页正文对。
_IMG_READISH = ("", "file_read", "file_list", "file_find", "search", "office_analyze",
                "url_shared")

# 2026-08-20: file_path 前缀剥离表 — 模型/正则把 "名字叫/在桌面创建/读取桌面上的" 等吞进文件名
_FP_PREFIXES = ["名字叫", "文件名称为", "命名为", "名字", "文件名", "名为", "叫做", "名称",
                "读取", "打开", "创建文件", "新建文件", "创建", "新建", "写入", "保存到", "保存", "存到", "放到",
                "在桌面", "桌面上的", "桌面", "在文档里", "在文档", "文档里的", "文档里", "文档", "在下载里", "在下载", "下载里的", "下载",
                "把你好世界写入", "把", "将", "叫", "称"]


#: 路径片段 (Windows 绝对路径 / 带扩展名的文件段) —— 用于**意图词匹配前**剔除。
#: ★ 2026-09-23 立 (实测真 bug): 文件名里的汉字会被当成意图关键词 ——
#:   粘 `D:\tmm-考卷\题目\A1 查询.txt` 时, "查询"里的 **"查"** 命中 search 词表
#:   → 整条路径被当搜索词去搜网 (搜出一堆"字母 d")。
#:   同理 `F1 知识库.txt` 里的"知识库"会命中 kb_search。
#:   判据: 路径是**数据**, 不是指令; 它内部的字不该决定意图。
#: ★ 只剔 **Windows 绝对路径** (带盘符) —— 精确, 不吞前面的指令词。
#:   教训: 曾写成"任意带扩展名的片段", 把 `在桌面新建一个报告.txt` 整段吞掉
#:   → "新建"没了 → file_write 判不出来 (verify_file_write_intent 立刻抓红)。
#: 结构 = 盘符:\ (无空格的目录段\)* 末段文件名(可含空格).扩展名
#:   必须能吃掉 `D:\tmm-考卷\题目\A1 查询.txt` 这种**文件名里带空格**的路径,
#:   否则"查询"会漏出来再次触发搜索意图 (实测踩到)。
_PATH_SEG_RE = re.compile(
    r'[A-Za-z]:[\\/](?:[^\s，。；;、！？?\'"]+[\\/])*'          # 盘符 + 无空格的目录段
    r'[^\\/，。；;、！？?\'"]*\.'                                 # 末段文件名(**允许空格**)
    r'(?:txt|text|docx?|pptx?|pdf|xlsx?|xlsm|csv|json|md|py'
    r'|png|jpe?g|gif|bmp|zip|rar|log|mp3|mp4|wav|db)\b', re.I)



#: ★ 2026-09-26 加: 与 _PATH_SEG_RE 同形, 但**扩展名不设白名单**。
#   为什么要它: 用户粘 `D:/tmm-考卷/题目/C4 技能.tx` (手误少了个 t), 白名单正则
#   不认识 `.tx` ⇒ 1.9 节判不出"这是文件路径" ⇒ 空路由 ⇒ 模型反问"你是要读它还是分析它"。
#   用户粘路径时**不该**因为扩展名拼错就掉兜底。
#   风险控制: 只当**老正则匹配不到时的 fallback**, 所以既有行为零变化;
#   且它要求盘符开头 (`[A-Za-z]:[/\\]`), 不会把 IP/版本号之类误当路径。
_PATH_SEG_ANY_RE = re.compile(
    r'[A-Za-z]:[\\/](?:[^\s，。；;、！？?\'"]+[\\/])*'      # 盘符 + 无空格目录段
    r'[^\\/，。；;、！？?\'"]*\.'                            # 末段文件名(允许空格)
    r'[A-Za-z0-9]{1,5}\b', re.I)


def _extract_abs_dir(message: str) -> str:
    """从消息里抽一个 Windows 绝对路径 (**目录也可**, 不要求扩展名)。

    ★ 2026-09-25 为什么需要: `file_list` 这一支原来**只看 dir_path**(桌面/文档/下载),
      消息里给了 `D:\tmm-考卷` 这种绝对路径也不认 → params 为空 →
      file_ops 落回默认目录(项目根), 把 `.crypto(<主机名>).key` 这类密钥文件名
      摆给用户看 (实测活卷 D1 "列出 D:\tmm-考卷 目录" 就是这个症状)。
    """
    m = re.search(r'[A-Za-z]:[\\/][^\r\n]*', message or "")
    if not m:
        return ""
    # ★ 2026-09-25 又一处 (ad-hoc 验证 C0 抓到): 分割字符类里**不能含 ASCII 冒号** ——
    #   "D:\tmm-考卷" 会在**盘符的冒号**处被切开, 只剩 "D" (实测 len=1 → 被当无效丢弃)。
    #   只按空白 + 中文标点切; 全角 '：' 可以留 (不会出现在盘符里)。
    seg = re.split(r'[\s，。；;、！？：]', m.group(0))[0]
    # ★★ 2026-09-24 扩: 循环剥**尾部中文散文词**。
    #   原来只剥 目录/文件夹/内容/里/中/下/有哪些/有什么 ⇒ 漏了"里面文件"这类说法:
    #   实测用户原话 `打开F:\cs里面文件` → 抽出 "F:\cs里面文件" (不存在的目录)
    #   ⇒ 列目录必失败。现在循环剥, 直到不再变化 (长的先剥, 如"里面文件"先于"文件")。
    _TAIL_WORDS = ("里面文件", "里的文件", "的文件", "里面的", "里面", "里边", "里的",
                   "目录", "文件夹", "的内容", "内容", "里", "中的", "中", "下的", "下",
                   "有哪些", "有什么", "都有啥", "都有什么", "有啥", "文件")
    _chg = True
    while _chg and seg:
        _chg = False
        for _w in _TAIL_WORDS:
            if seg.endswith(_w) and len(seg) > len(_w) + 1:
                seg = seg[: -len(_w)]
                _chg = True
                break
    # ★★ 2026-09-26 修 (用户发火那次抓到的真 bug):
    #   用户粘路径**爱带引号** (`"C:\Users\...\人员信息采集表.csv"`), 而这里原来只
    #   rstrip 斜杠 ⇒ 抽出来的字符串尾巴上**挂着一个引号**。后果不是"差一点", 是
    #   **类型判错**: 调用方用 `\.[A-Za-z0-9]{1,5}$` 判"这是文件还是目录", 尾巴有引号
    #   ⇒ 判成**目录** ⇒ 一条"读这个 csv"的请求变成 `file_list` (列目录)。
    #   实测: `"…采集表.csv" 里有什么` → file_list; 去掉引号才 → file_read。
    #   同一个 bug 还会让"列出 D:\x\y"这类带引号的目录判定连带受影响 —— 一次修干净。
    seg = seg.rstrip("\\/").strip()
    _q = ("\"", "'", "\u201c", "\u201d", "\u2018", "\u2019", "\u300c", "\u300d")
    while seg and seg[0] in _q:
        seg = seg[1:]
    while seg and seg[-1] in _q:
        seg = seg[:-1]
    seg = seg.rstrip("\\/").strip()
    return seg if len(seg) >= 3 else ""


#: 正文载荷起点标记 —— 之后整段只是"用户要写的内容", 不参与意图判定。
#: 少了这条, 用户写什么内容、内容里的词就劫持意图 (实测: 内容写「今天天气不错」
#: ⇒ 一句写文件请求被判成查天气)。
_PAYLOAD_MARK = re.compile(r"(内容|正文|文案|body)\s*[:：]")

#: 所有意图动作词 (从 ACTION_INTENTS / DELIVERY_INTENTS 的 patterns 里筛出来)。
#: 只收**纯文字**词; 带正则元字符的 (如 "提取.*列" / "http://") 一律跳过 ——
#: 它们是模式不是词, 拿来在字符串里找会把普通文本也当命中。
_INTENT_KEYWORDS = tuple(sorted({
    _p for _d in (ACTION_INTENTS, DELIVERY_INTENTS)
    for _v in (_d or {}).values()
    for _p in (_v.get("patterns") or [])
    if _p and not re.search(r"[.*+?\[\](){}|\\]", _p)
}, key=len, reverse=True))


def _intent_text(message: str) -> str:
    """意图词匹配用的正文 —— 剔除路径/文件名/正文载荷。

    只影响**意图词匹配**; 其余 (路径槽提取/扩展名判定) 仍用原消息。

    ★★ 2026-09-25 补 (真数据实测, P0 路由错配):
      原来**只剔带盘符的路径** (`_PATH_SEG_RE`, 要求 `[A-Za-z]:[/\\]`) ⇒ 裸文件名
      原样留在正文里, 于是**文件名里的字劫持意图**:
          "打开 A1 查询.txt"  → 文件名里的「查」命中 search 词 ⇒ 变成**网页搜索**,
                                chain 出 web_search(query='打开 A1 查询.txt'),
                                根本不去读那个本地文件。
          "打开 天气.txt"     → 会被当成查天气。
      修法: 再把**裸文件名那块**剔掉, 但
        ① **保留扩展名** —— "合并 a.pdf 和 b.pdf" 这类意图靠词对 ("合并"+"pdf")
           命中, 把扩展名一起抹掉会让它们全部失效;
        ② **保留块里的动作词** —— "在桌面新建一个报告.txt" 这串没空格, 名字块
           连着动词; 整块抹掉会把「新建」也抹掉 ⇒ 写文件请求反被判成别的意图
           (实测: 被判成 weather, 因为正文里还剩「天气」)。
           判据: 块里出现动作词就补回, 但**该词若属于文件名本身就不补**
           ("天气.txt" 里的「天气」是名字, 不是动作)。
        ③ **正文载荷** (内容:/正文:/文案: 之后) 整段不参与 —— 否则用户写什么
           内容, 内容里的词就劫持意图。
    """
    msg = message or ""
    try:
        t = _PATH_SEG_RE.sub(" ", msg)
    except Exception:
        return msg
    # ③ 正文载荷整段不参与意图判定
    try:
        if _PAYLOAD_MARK.search(t):
            t = _PAYLOAD_MARK.split(t, 1)[0]
    except Exception:
        pass
    try:
        from core.nl_paths import FN_RE as _FN, clean_filename as _cf

        def _keep_actions(m):
            span = m.group(0)
            try:
                ext = span.rsplit(".", 1)[-1]
            except Exception:
                ext = ""
            try:
                _c = _cf(span)
                name = _c.rsplit(".", 1)[0] if "." in _c else _c
            except Exception:
                name = ""
            kept = []
            for kw in _INTENT_KEYWORDS:
                if kw and kw.lower() in span.lower() and kw not in name:
                    kept.append(kw)
            kept = sorted(set(kept), key=lambda x: -len(x))[:3]
            return " ." + ext + ((" " + " ".join(kept)) if kept else "")

        t = _FN.sub(_keep_actions, t)
    except Exception:
        pass
    return t


def _repair_file_path_slot(message: str, current: str) -> str:
    """把 file_path 槽从"被空格截断的裸名"修回**完整路径**。

    ★★ 2026-09-25 修 (真数据 id 646/650/654/656, P0 链路断裂):
      `REGEX_SLOTS["file_path"]` 的字符类 `[\\w\\-\\\\/:]+` 过不了空格 ⇒ 带空格的
      路径/名子在第一个空格处被截断。实测 (改前/改后对照):
          读一下 D:\\tmm-考卷\\题目\\B2 门店清单.csv
              → file_path='门店清单.csv'  ⇒ 落桌面读 ⇒ File not found
          D:\\tmm-考卷\\题目\\A1 查询.txt 这个是什么
              → file_path='查询.txt'      ⇒ 同上
      这里**只做升级, 不做降级** (needs_exist 之外不改动原行为):
        ① 消息里有**完整盘符路径** (`_PATH_SEG_RE`, 它本来就允许名内空格) 且它
           以当前值为尾巴 ⇒ 用完整路径
        ② 否则用共用规则 nl_paths 把裸名左接回被空格切开的碎片
      两条都失败 ⇒ 原样返回 (一个字不改)。
    """
    _cur = (current or "").strip()
    if not _cur:
        return current
    try:
        m = _PATH_SEG_RE.search(message or "")
        if m:
            full = m.group(0)
            # ★ 2026-09-25: 同一条正则也会把动词短语吃进路径 (`…\tmp 新建 x.txt`),
            #   共用 nl_paths 的判据砍掉动词尾巴; `A1 查询.txt` 这类名内空格保住。
            try:
                from core.nl_paths import strip_verb_tail as _svt
                full = _svt(full)
            except Exception:
                pass
            if full != _cur and (full.endswith(_cur) or _cur.endswith(full)):
                return full
    except Exception:
        pass
    if os.path.isabs(_cur):
        return current
    try:
        from core.nl_paths import raw_filename_tokens
        for tok in raw_filename_tokens(message or ""):
            # ★ 只接受**因为空格才接上来的**候选: 候选里必须真有一个空格, 且
            #   当前值正好是"空格之后那一截"。少了这道闸, 纯正文形态会被整句升级
            #   (实测: "打开一首诗.txt" 会被升成 "打开一首诗.txt" —— 正是本文件
            #    反复警告的"整句被当成文件名")。
            if " " not in tok or tok == _cur:
                continue
            _head, _tail = tok.split(" ", 1)
            if _tail != _cur:
                continue
            # ★★ 2026-09-25 补 (常驻门禁 verify_honest_failure 抓到的**回归**):
            #   `在 D:\...\tmp 新建 hv_verify.txt 写入 你好` 这句里, 当前值本来
            #   已经是正确的 `hv_verify.txt`, 但"新建 hv_verify.txt"也满足
            #   `endswith`, 于是**动词被当前缀接到文件名上** → 交付给 file_ops 的
            #   path 变成「新建 hv_verify.txt」→ 参数校验判它"路径后附加描述",
            #   写入直接失败 (门禁红: 指定目录的成功写入)。
            #   判据直接用 nl_paths.space_head_ok —— 规则只留一份, 两路共用
            #   (knowledge.py 那条路也走同一个函数, 谁也别再各写一套)。
            from core.nl_paths import space_head_ok
            if space_head_ok(_head):
                return tok
            continue
    except Exception:
        pass
    return current


def _strip_fp_prefix(v: str) -> str:
    """循环剥 file_path 前缀 (按长度降序), 保护纯文件名 (剥后无扩展名则还原)。

    ★ 2026-09-22 修 (真引擎实跑抓到的): 本函数原来**只从左剥前缀表**, 遇到
      用户真实说法 `写一首诗保存到桌面大哥.txt` 就卡住 —— 表里没有"写一首诗保存到桌面"
      这一整串, 于是**整句被当成文件名**, 写盘时内容为空, 用户看到
      "✗ 写入内容为空" (这正是用户发火的那句原话, 只是走的是 IR 链这条路)。
      修法: 剥完前缀后, 再走**与知识库那条路共用**的规则 (core.nl_paths.clean_filename):
      按最后一个目录词切 → 按最后一个强动词切 → 循环剥弱动词/量词/助词。
      两条路口径必须一致, 否则"同一句话两种结果"。
    """
    _orig = v
    while True:
        _hit = None
        for _pfx in sorted(_FP_PREFIXES, key=len, reverse=True):
            if v.startswith(_pfx):
                _hit = _pfx
                break
        if not _hit:
            break
        v = v[len(_hit):].lstrip()
    # ★ 2026-09-22: 再走**共用规则** —— 按最后一个目录词/强动词切, 再循环剥弱动词/量词。
    #   真实说法里散文和文件名是连着的 ("写一首诗保存到桌面大哥.txt"), 光靠左剥前缀表会卡住。
    try:
        from core.nl_paths import clean_filename as _clean
        _c = _clean(v)
        if _c and "." in _c:
            v = _c
    except Exception:
        pass
    # 保护: 剥过头(中文文件名如 "桌面.txt") → 还原
    if not v or "." not in v or not re.search(r'\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip)$', v):
        return _orig
    return v


class IntentRouter:
    def __init__(self, knowledge_engine=None):
        self.ke = knowledge_engine
    
    def classify(self, message: str) -> dict:
        result = {"actions": [], "delivery": None, "slots": {}}
        result["raw"] = message  # 2026-08-20: 保留原始消息, build_chain 提取文件内容时避免 known 剔除丢字
        msg_lower = message.lower()
        
        # 1. Action intent
        # ★ 2026-09-23: 用**剔除路径后**的文本判意图 —— 路径是数据不是指令
        #   (实测: 粘 `...\A1 查询.txt` 时文件名里的"查"字把整条路径变成搜索词)
        _itext = _intent_text(message).lower()
        scored = []
        for pattern, name in [(p, n) for n, info in ACTION_INTENTS.items() for p in info["patterns"]]:
            if pattern in _itext:
                scored.append((len(pattern), name))
        # 🔴 office 词对匹配 (2026-08-19): "合并这两个pdf" 词不相邻, 子串匹配不到
        # 词对 (a, b) 都出现在消息中 → 命中 (如 "合并"+".pdf" / "pdf"+".xlsx")
        for name, info in ACTION_INTENTS.items():
            for pair in info.get("pairs", []):
                if all(p in _itext for p in pair):
                    scored.append((20, name))  # 词对权重高, 确保命中
        # 🔴 office 词对负向过滤: "分析表面现象/列表里有什么" 含"表"但非表格诉求
        # "表面/列表/表示/表达/表明/表演/表格外的表字" → 不触发 office
        if any(s[1] in ("office_analyze", "office_extract") for s in scored):
            _office_false = ["表面", "列表", "表示", "表达", "表明", "表演", "表白", "发表", "图表之外的"]
            if any(w in msg_lower for w in _office_false):
                scored = [s for s in scored if s[1] not in ("office_analyze", "office_extract")]
        # ★ repo_history 负向过滤 (同 office 那套): 说到桌面/下载/各盘的文件 ⇒ 那不在
        #   git 仓库里, 本工具看不到 ⇒ 别接, 让它走别的路 (接了只能回"仓库里没找到")。
        if any(s[1] == "repo_history" for s in scored):
            _rh_out = ["桌面", "下载", "C盘", "D盘", "E盘", "F盘"]
            if any(w in message for w in _rh_out):
                scored = [s for s in scored if s[1] != "repo_history"]
        if scored:
            # Sort by (priority, length) — explicit intents beat generic search
            # 2026-08-20: file_* 意图 +2 权重, 压过 kb_search("文档里")/check_mail 等
            # ("在文档里创建文件test.md" 的 kb_search 曾压过 file_write)
            scored.sort(key=lambda x: (INTENT_PRIORITY.get(x[1], 0) + (2 if x[1].startswith("file_") else 0), x[0]), reverse=True)
            result["actions"] = [scored[0][1]]
        
        # 🔴 file_list 负向过滤: "列出"命中但带数据语义词 → 不是列目录
        # (如 "列出第一行接单部门所有内容" = 列 Excel 列值, 不是列目录)
        # 注意: 不能用裸"列"(会误伤"列出"), 用"的列/列值"等精确词
        if result["actions"] == ["file_list"]:
            if any(w in msg_lower for w in ["内容", "的值", "行", "数据", "记录", "条目", "字段", "单元格", "的列", "列值"]):
                result["actions"] = []  # 交给模型(基于会话上下文/预读内容回答)
        # 🔴 file_list 负向过滤 #2 (2026-10-09 复盘实测): "列出<非文件名词>" 被"列出"抢成列目录。
        #   实测原话: 用户说"列出技能" → file_list 把**项目根目录**当清单丢回 (他要的是技能清单);
        #   "列出记忆/列出备份/列出插件" 同理 —— "列出"后面的宾语根本不是文件/目录。
        #   判据: 命中非文件名词 **且** 消息里没有路径/目录信号 ⇒ 不是列目录, 交回模型。
        #   (有路径的老话如"列出 core 目录的文件" 原样保留, 不误伤 —— 见 verify_table_phrase_routing。)
        if result["actions"] == ["file_list"]:
            _fl_obj = ("技能", "记忆", "备份", "插件")
            _fl_path = ("/", "\\", ":", "盘", "目录", "文件夹", "夹", "路径", "文件", ".")
            if any(w in message for w in _fl_obj) and not any(p in message for p in _fl_path):
                result["actions"] = []  # 宾语不是文件 ⇒ 交给模型 (别拿目录冒充清单)
        # 🔴 send_msg 负向过滤 (2026-09-28 实测翻车):
        #   起因: "统计 data.csv 几行, 并告诉我第三行" → 命中 send_msg(词表里有"告诉")
        #   → 回"虎哥可以帮你发邮件", **任务根本没到模型手里**。
        #   "查一下X并告诉我Y" 这种日常说法全会中招。
        #   判据: (告诉|通知) 紧跟 我/咱/俺 ⇒ 那是"说给我听"(索取信息), 不是"替我发消息"。
        #   只对这两个模糊词生效: "发给/发送给/发到/转发给" 是明确发送语义, 不动;
        #   消息里带邮箱也不动 (那是真发送, 见 2.2 节邮箱信号)。
        if result["actions"] == ["send_msg"]:
            _sm_info = re.search(r'(告诉|通知)\s*(一下)?\s*(我|咱|俺)', message)
            _sm_clear = any(k in msg_lower for k in
                            ("发给", "发送给", "发到", "发送到", "转发给", "发邮件"))
            _sm_mail = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', message)
            if _sm_info and not _sm_clear and not _sm_mail:
                result["actions"] = []          # 不是发送 → 交回模型照常调工具干活
        
        # Override: explicit domain words override generic search
        # ── KB manage pre-check: 知识库+管理词 → 管理意图 (优先于 kb_search) ──
        # ★ 2026-09-23: 用剔除路径后的文本 —— 文件名里的"知识库"不算指令
        if "知识库" in _itext:
            if any(w in _itext for w in ["删", "移除", "清空", "清掉", "清一下"]):
                result["actions"] = ["kb_delete"]
            elif any(w in _itext for w in ["有什么", "有哪些", "列出", "都有", "几个", "啥"]):
                result["actions"] = ["kb_list"]
        _domain_override = {
            ("邮件", "收件箱", "邮箱", "inbox"): "check_mail",
            ("天气", "气温", "下雨", "下雪"): "weather",
            ("截图", "截屏", "截个图"): "screenshot",
            ("翻译", "translate", "译", "中英", "互译"): "libretranslate",
            ("备份", "备份列表", "备份文件", "创建备份"): "backup",
            ("手册", "说明书", "知识库", "文档里", "资料里"): "kb_search",
            # 2026-09-20 加: 产物/交付清单 (借鉴通用 Agent 平台的"Files 产物面")。
            # 用**具体说法**而不是裸"产物"两个字, 避免误伤 ("这个产品…" 之类)。
            # ★★ 2026-09-24 扩充 (用户实测): 用户说"总结今天干了什么" → 空路由 →
            #   掉大模型直答"我无法感知您的活动"; 而"今天做了什么"本来就有 artifacts 路由。
            #   差一个字就没命中。⇒ 把"干/做/忙/搞"这几种说法都收进来。
            ("产出了什么", "产出了啥", "交付物", "产物清单", "成品在哪", "生成的报告",
             "生成了什么", "输出文件清单",
             "今天做了什么", "今天干了什么", "今天做了啥", "今天干了啥", "今天都干了啥",
             "今天都做了什么", "今天干了些什么", "今天做了些什么", "今天忙了什么",
             "今天忙了啥", "今天搞了什么", "总结今天", "今天的总结", "总结一下今天",
             "今天的工作", "今天做了哪些", "今天干了哪些"): "artifacts",
        }
        for keywords, target in _domain_override.items():
            # ★ 2026-09-23: 改用 _itext (剔除路径) —— 实测 `...\F1 知识库.txt` 里
            #   文件名中的"知识库"把"粘路径"误判成知识库检索。
            if any(w in _itext for w in keywords):
                # 2026-08-20: "文档里/知识库" 等词遇到明确文件操作意图时不覆盖 (创建test.md 被 kb_search 劫持)
                _file_action_kw = ["创建", "新建", "写入", "读取", "打开", "保存", "文件名", "复制", "移动", "删除"]
                if target == "kb_search" and any(w in _itext for w in _file_action_kw):
                    continue
                # Override if no action matched yet, or if current action is generic search
                # 🔴 2026-08-24: url_shared 也算可覆盖的通用意图 — "翻译 https://..." 具体动作优先
                if not result["actions"] or result["actions"][0] in ("search", "file_find", "url_shared"):
                    result["actions"] = [target]
                    import re as _re
                    if target == "check_mail":
                        kw = result.get("slots", {}).get("content", "")
                        if kw:
                            kw = _re.sub(r'(关于|有没有|找|一下|的|相关|邮件|里|看|帮|我)', '', kw).strip()
                            if kw:
                                result["slots"]["keyword"] = kw
                    break  # first matching domain wins
        
        # 1.4 URL detection: if message is/contains URL, let model handle
        # 🔴 2026-08-24: 提取 URL 存 slots — 规则层 firecrawl 抓取后喂模型,
        # 本地模型(ollama/gemma4)不主动调工具, 需 TMM 加持 (与文档预读同套路)
        # 覆盖策略: 消息开头即 URL → 无条件 url_shared (整条消息都是关于链接);
        # URL 在中间 → 仅当无更具体动作(如 翻译/搜索)时才接管, 防劫持。
        _url_m = re.search(r'https?://[^\s，。；、]+', message)
        if _url_m:
            result["slots"]["url"] = _url_m.group(0)
            _starts_with_url = bool(re.match(r'^\s*https?://', message))
            if _starts_with_url or not result["actions"] or result["actions"][0] in ("search", "file_find"):
                result["actions"] = ["url_shared"]
        
        # 1.45 ★★ 2026-09-28 加: 表格文件 + 表格动词 ⇒ table_ops (统一表格操作层)。
        #   必须插在 1.5 前面 —— 那三条老路 (file_read/write/append) 只懂"整篇文本",
        #   不懂表头/列/行。让它们接表格操作, 就是 2026-09-28 同一个 CSV 连撞四轮的根因:
        #     「续写20条记录」→ file_write(把指令当内容写进文件, 原有 20 行被覆盖)
        #     「添加40条人员信息」→ knowledge(回"冰的密度小于水")
        #     「第一行填表头」→ general(模型自己只写了个表头, 数据没了)
        #   表格操作全部交给 table_ops: 一个入口管住行/列/表头/单元格级增删改 + 拆分合并,
        #   原有数据默认不动, 干完重读数核对。
        if re.search(r'\.(?:csv|tsv|xlsx|xlsm|docx)', msg_lower) and _has_table_verb(msg_lower):
            result["actions"] = ["table_edit"]
            result["slots"]["table_op"] = _pick_table_op(msg_lower)

        # 1.42 ★★ 2026-09-28 加: **图片 ⇒ 看图 (vision)**, 位置=表格守卫之后、1.5 之前。
        #   1.5 的扩展名白名单里有 png/jpg, 带路径的消息到了那里一律变 file_read
        #   ("读出来给用户看"), 于是 PNG 被按 utf-8 读 → 回给用户的是 Python 异常
        #   `'utf-8' codec can't decode byte 0x89 in position 0: invalid start byte`
        #   (用户 23:38 原话实测; 同一张图 20:22 也栽过一次)。
        #   ★ 为什么排在 1.45 表格守卫**后面**: `把 a.png 和 b.csv 合并` 这种句子里
        #     表格动作更硬, 不许被看图抢走 (table_edit 不在 _IMG_READISH 里)。
        #   判据: ① 消息里有**图片 URL** 或**图片绝对路径**; ② 当前结论是"没有具体动作"
        #   那一档 (空/读/列/找/搜/打开链接) —— 带写/发/表格动作的句子**不许抢**。
        #   修正后的链: vision 工具 (image=图, question=原话) → 本地 qwen2.5vl / 百炼。
        _img_url_m = re.search(r'https?://[^\s，。；;、]+?\.(?:png|jpe?g|gif|webp|bmp)',
                               message, re.IGNORECASE)
        _img_m = None if _img_url_m else _IMG_PATH_RE.search(message)
        # URL 的中段会被路径正则认成"盘符路径" (`p://a.com/b.png`) → 丢掉, 走上面的 URL
        if _img_m and re.match(r'^[A-Za-z]://', _img_m.group(1)):
            _img_m = None
        _img_src = _img_url_m.group(0) if _img_url_m else (_img_m.group(1) if _img_m else "")
        if _img_src and (not result["actions"] or result["actions"][0] in _IMG_READISH):
            result["actions"] = ["vision"]
            result["slots"]["image"] = _img_src
            result["slots"]["content"] = message

        # 1.5 Implicit file detection — only if no action matched
        if re.search(r'\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip)', message):
            if not result["actions"]:
                # Check if this is a create/write intent (2026-08-20: 加 保存/文件名/存到/放到)
                # ★ 2026-09-26: 增补动词单列一门 (见 ACTION_INTENTS 里 file_append 的注释)。
                #   顺序重要 —— "修改/更新" 也应先归到写类, 不许落到 read。
                if any(w in msg_lower for w in ["增加", "添加", "追加", "填入", "填充", "补充",
                                                "增补", "多加", "添上", "补上", "加进"]):
                    result["actions"] = ["file_append"]
                elif any(w in msg_lower for w in ["创建", "新建", "写入", "保存", "文件名",
                                                  "存到", "放到", "写", "修改", "更新", "改成"]):
                    result["actions"] = ["file_write"]
                else:
                    # ★★★ 硬规矩 (用户发火那次的根因, 写在这里防止再犯):
                    #   带**扩展名路径**的消息落到这一支就是 file_read —— 意思是
                    #   "读出来给用户看"。但如果这条消息里还有**任何改文件的动词**,
                    #   回显内容就等于**答非所问 + 假装办完了**。所以这里补一道
                    #   反向守卫: 有改动词就不许判 read。宁可让上层去问, 不许回显。
                    _MUTATE_ANY = ("增加", "添加", "追加", "填入", "填充", "补充", "增补",
                                   "多加", "添上", "补上", "加进", "创建", "新建", "写入",
                                   "写", "保存", "存到", "放到", "修改", "更新", "改成",
                                   "改成", "删除", "删掉", "替换", "补全")
                    if any(w in msg_lower for w in _MUTATE_ANY):
                        logger.warning("路由守卫: 带路径的消息含改动动词却要被判成 file_read, "
                                       "已拦下改为 file_append (原文=%r)", message[:80])
                        result["actions"] = ["file_append"]
                    else:
                        result["actions"] = ["file_read"]
        
        # 1.9 ★ 2026-09-23 加: 「整条消息就是一个文件路径」→ 读它。
        #   实测: 用户从题目目录粘路径过来 (A1 查询.txt), 期望"看这个文件";
        #   原行为要么被文件名里的字劫持成搜索, 要么落到模型闲聊。
        #   判据: 剔除路径后几乎没有可判文本 (<=2 个有效字符) + 消息里确实有路径。
        _pseg = _PATH_SEG_RE.search(message)
        # ★ 2026-09-26: 老正则(扩展名白名单)匹配不到时, 再用不限扩展名的那个 ——
        #   实测 `.tx` 这种拼错的扩展名会让整条消息空路由 (模型反问"要读还是要分析")。
        if not _pseg:
            _pseg = _PATH_SEG_ANY_RE.search(message)
        # ★★ 不能加 `not result["actions"]` 前提 —— 实测 1.5 节("Implicit file
        #   detection")已把带扩展名的消息设成 file_read, 加前提会让本块变死代码
        #   (file_path 仍被 REGEX_SLOTS 截成 `查询.txt` → 落桌面读 → File not found)。
        # ★ 2026-09-25 修 (真缺陷, 只读探针抓到): 上面 1.5 已按「创建/新建」判成
        #   file_write 的消息, 会被这一块**覆盖回 file_read** —— 实测
        #   `在 D:\tmm-考卷\题目 新建 x.txt` ⇒ actions=[file_read], 变成
        #   "去读一个还不存在的文件"。「有写动词」比「整条消息是个路径」更硬,
        #   不许被覆盖 (1.5 判 file_read 的照旧, 那本来就是同一结论)。
        _cur_acts = result.get("actions") or []
        # ★ 2026-09-26: 守卫原来是 `!= ["file_write"]` —— 只护住了 write, **漏了 append**。
        #   实测 `"…采集表.csv" 增加` ⇒ 1.5 已判 file_append, 走到这里被覆写回 file_read
        #   (又变回"把旧内容回显")。改成"任何写类动作都不许被覆写", 一次修全:
        _WRITE_ACTS = ("file_write", "file_append", "file_delete", "file_copy",
                       "file_move", "file_rename", "table_edit",
                       # ★ 2026-09-28: 已定结论的**看图**也算在内 —— 裸图片路径走到这里
                       #   不许被覆写回 file_read (那又变成"按 utf-8 读 PNG"的旧错)。
                       "vision")
        if _pseg and not any(a in _WRITE_ACTS for a in _cur_acts):
            # ★ 2026-09-26: 余量要用**实际匹配到的那段路径**来算。原来这里又走了一遍
            #   _intent_text() —— 它内部是扩展名白名单的老正则, 判据①用
            #   _PATH_SEG_ANY_RE 匹配上了 `.tx` 它却剔不掉 ⇒ 余量=整条路径 ⇒
            #   白匹配 (实测: 正则修完仍空路由, 就栽在这)。
            _rest_src = (message.replace(_pseg.group(0), " ", 1)
                         if _pseg else _intent_text(message))
            _rest = re.sub(r'[\s，。；；、！？?\'"()（）]', '', _rest_src)
            if len(_rest) <= 2:
                result["actions"] = ["file_read"]
                result["slots"].setdefault("content", message.strip())
                # ★★ 必须把**完整路径**写进 file_path 槽 —— build_chain 的 file_read
                #   分支优先用它; 不写就退回 REGEX_SLOTS 抽到的那个名字(带空格路径
                #   会被截成 `查询.txt`) → 落到桌面去读 → "File not found".
                #   实测: 粘 `D:\...\题目\A1 查询.txt` 就是这个坑。
                result["slots"]["file_path"] = _pseg.group(0)

        # 1.95 ★★ 2026-09-24 加 (用户实测): "有路径 + 问里面有什么" 的自然说法 → 列目录。
        #   实测四种说法**全部空路由** → 掉大模型直答 (12~17 秒, 还可能花钱):
        #     `"F:\cs"有什么` / `F:\cs里有什么` / `打开F:\cs里面文件` / `看看 F:\cs 里面`
        #   而 "列出 F:\cs" 走 file_ops 只要 0.5 秒 ⇒ 差别就是**只认"列出"一个词**。
        #   判据: ① 消息里有 Windows 路径; ② 路径**没有扩展名**(是目录, 不是文件);
        #         ③ 有"看里面"类词。三者同时满足才接管 (防误伤"读这个文件的内容")。
        _LIST_Q = ("有什么", "有哪些", "都有什么", "都有啥", "里面有啥", "里面有什么",
                   "里面是什么", "里面是啥", "有什么文件", "有哪些文件", "里面文件",
                   "里的文件", "的文件", "里面", "里边", "看看里面", "看看里面",
                   "看下里面", "看一下里面", "瞧一眼里面", "列一下", "列出来", "文件列表")
        # ★ 反向守卫: 句子里有"写入类"动词 ⇒ 不是要看里面, 是要往里放 (别误判成列目录)。
        _WRITEISH = ("放到", "放进", "放入", "放在", "存到", "存进", "写入", "写进",
                     "保存到", "另存为", "复制到", "拷贝到", "移动到", "导入", "导出",
                     "创建", "新建", "写一份", "写一篇",
                     # ★ 2026-09-26 补: 增补类动词 (用户"增加20条"那次的用词)
                     "增加", "添加", "追加", "填入", "填充", "补充", "增补", "多加")
        # ⚠ 不能用 _pseg (那是**文件**路径正则, 要求末尾带扩展名) ⇒ 纯目录永远不匹配,
        #   第一版就是这么写的 → 一条都没触发 (清缓存重测才发现)。改用 _extract_abs_dir。
        _dir_cand = _extract_abs_dir(message)
        if _dir_cand and not re.search(r"\.[A-Za-z0-9]{1,5}$", _dir_cand) \
                and any(w in msg_lower for w in _LIST_Q) \
                and not any(w in msg_lower for w in _WRITEISH):
            result["actions"] = ["file_list"]
            # build_chain 的 file_list 分支自己会用 _extract_abs_dir(raw) 抽路径, 这里不用写。

        # 1.97 ★ 2026-09-25 加 (P0, 只读探针实测四条**全部空路由**): 用户把
        #   **目录**路径整条粘进来 —— `D:\tmm-考卷\题目` / `…\题目\` /
        #   `执行D:\tmm-考卷\题目` / `F:\cs`。1.9 只管**文件**路径(要求末尾带
        #   扩展名), 1.95 只管"问里面有什么" ⇒ 纯目录没人接, 掉兜底。
        #   判据: ① 能抽出一个 Windows 绝对路径且**没有扩展名**(是目录);
        #         ② 剔掉路径后剩下的字为空, 或只是"执行/看看/打开"这类指令词;
        #         ③ 没有写入类动词 (往目录里放东西 != 看目录, 复用 _WRITEISH)。
        # ★ 2026-09-26 扩 (真引擎原话实测): 用户粘路径时爱搭一句**指代此处**的口语
        #   —— `D:\tmm-考卷\ 你要的在这` / `…dh_180s_base 这里有` / `… 图片去这里找`。
        #   原表只有"执行/看看/打开"这类指令词, 这三条全不触发 ⇒ 空路由 ⇒ 模型回
        #   "已记录该路径"。判据不变: 仍然是**余量精确等于**表里某项 (不做模糊放宽,
        #   免得把"这里有个人"这类真句子也接管)。
        _BARE_DIR_OK = ("", "执行", "看看", "看下", "看一下", "打开", "列一下", "瞅瞅", "瞧瞧",
                        "这里有", "这里", "在这", "在这呢", "在这里", "你要的在这",
                        "你要的", "你要的在这里", "东西在这", "就在这", "都在这",
                        "图片去这里找", "去这里找", "这找", "这里找", "就这些")
        _PUNCT = ("，", "。", "；", ";", "、", "！", "？", "?", "（", "）", "(", ")",
                  chr(34), chr(39))
        if not result["actions"]:
            _bd = _extract_abs_dir(message)
            if _bd and not re.search(r"\.[A-Za-z0-9]{1,5}$", _bd) \
                    and not any(w in msg_lower for w in _WRITEISH):
                # ★ 必须拿「消息减去目录路径本身」算余量 —— **不能用 _intent_text**:
                #   它用的 _PATH_SEG_RE 是**文件**路径正则(要求末尾带扩展名),
                #   纯目录根本剔不掉 ⇒ 余量=整条路径 ⇒ 四条全不触发 (第一版就栽在这)。
                _bres = message.replace(_bd, "", 1)
                for _p in _PUNCT:
                    _bres = _bres.replace(_p, "")
                _bres = _bres.strip().strip("\\/").strip()
                if _bres in _BARE_DIR_OK:
                    result["actions"] = ["file_list"]
                    # 与 1.95 一致: build_chain 的 file_list 分支自己抽路径, 不写槽。

        # 2. Delivery intent (MUST come before slot extraction)
        d_scored = []
        for pattern, name in [(p, n) for n, info in DELIVERY_INTENTS.items() for p in info["patterns"]]:
            if pattern in msg_lower:
                d_scored.append((len(pattern), name))
        if d_scored:
            d_scored.sort(reverse=True)
            result["delivery"] = d_scored[0][1]

        # 2.2 ★ 2026-09-23 加: 邮箱地址 = 最强收件人信号 (压过一切词表猜测)
        #   实测三条翻车 (用户真实验收):
        #     "给someone@example.com发一份测试邮件" → check_mail (裸"邮件"抢走) → 去读收件箱
        #     "济南旅游攻略 发给someone@example.com" → send_msg, 无收件人 → 拿空 to 硬发
        #   判据: 消息里有合法邮箱 ⇒ 记进 to_addr; 且同时有发送语义 ⇒ 强制 send_email。
        _em_m = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', message)
        if _em_m:
            result["slots"]["to_addr"] = _em_m.group(0)
            _send_kw = ("发给", "发送给", "发到", "发送到", "发邮件", "发一份", "发一封",
                        "发封", "发个", "发一", "寄", "email")
            if any(k in msg_lower for k in _send_kw) or result.get("delivery") == "email":
                result["actions"] = ["send_email"]
        
        # 2.1 Chart type hint (from ORIGINAL message — known-token stripping would erase it)
        if "chart" in result.get("actions", []) or any(p in msg_lower for p in ("图表", "比例图", "饼图", "柱状图", "折线图", "画图", "统计图", "占比图", "生成图", "可视化")):
            if any(k in msg_lower for k in ("柱状", "bar")):
                result["slots"]["chart_type"] = "bar"
            elif any(k in msg_lower for k in ("折线", "line", "趋势")):
                result["slots"]["chart_type"] = "line"
            else:
                result["slots"]["chart_type"] = "pie"

        # 3. Slots (AFTER delivery detection)
        if self.ke:
            # Recipient: find person mentioned in message
            for ename, edata in self.ke.entities.items():
                if ename in message:
                    result["slots"]["recipient"] = ename
                    break
                # Check aliases
                for alias in edata.get("aliases", []):
                    if alias in message:
                        result["slots"]["recipient"] = ename
                        break
                if "recipient" in result["slots"]:
                    break
        
        # Regex slots
        for slot_name, pattern in REGEX_SLOTS.items():
            m = re.search(pattern, message)
            if m and m.lastindex and m.lastindex >= 1:
                try:
                    _v = m.group(1)
                    # 2026-08-20: file_path 剥常见前缀 (原正则 \w 吞中文,
                    # "名字叫agent_test.txt"/"在桌面创建报告.txt" 整个被当文件名)
                    if slot_name == "file_path":
                        _prev = str(result["slots"].get("file_path") or "").strip()
                        # ★ 2026-09-23: 已有**完整绝对路径**(1.9 粘贴路径规则写入) → 不覆盖。
                        #   REGEX_SLOTS 抽不了带空格的路径: `D:\...\A1 查询.txt` 会被截成
                        #   `查询.txt` → 落到桌面去读 → "File not found: 查询.txt".
                        if _prev and os.path.isabs(_prev):
                            continue
                        _v = _strip_fp_prefix(_v)
                    result["slots"][slot_name] = _v
                except IndexError:
                    pass
        
        # Content = remaining text after stripping known tokens
        known = set()
        for info in ACTION_INTENTS.values():
            for p in info["patterns"]:
                if p in msg_lower: known.add(p)
        for info in DELIVERY_INTENTS.values():
            for p in info["patterns"]:
                if p in msg_lower: known.add(p)
        # 2026-08-20 修复: dir_path 取"第一个明确指示", 排除 txt文档/word文档 复合词
        # 原 findall 会命中 "txt文档" 里的"文档"并把桌面覆盖成文档 → 文件写到 Documents
        _dir_hit = None
        for _sp in ("桌面", "下载", "D盘", "C盘"):
            if _sp in message:
                _dir_hit = _sp
                break
        if _dir_hit is None:
            _dm = re.search(r'(?<![A-Za-z0-9])文档(?![A-Za-z0-9])', message)
            if _dm:
                _dir_hit = "文档"
        if _dir_hit:
            known.add(_dir_hit)
            result["slots"]["dir_path"] = _dir_hit
        for filler in ["一下", "一个", "个", "的", "了", "吗", "呢", "吧", "下来", "保存", "然后", "接着", "并且", "之后", "并",
                       "名字叫", "名为", "叫做", "命名为", "新建一个", "一个txt", "txt文档", "word文档", "文档"]:
            known.add(filler)
        # Strip entity names and aliases
        if self.ke:
            for ename, edata in self.ke.entities.items():
                known.add(ename)
                for alias in edata.get("aliases", []):
                    known.add(alias)
        
        remaining = message
        # 保护已提取的 file_path，避免 known token 剔除破坏路径（如 xlsx 里的 "ls"）
        _fp_guard = result["slots"].get("file_path", "")
        for token in sorted(known, key=len, reverse=True):
            if _fp_guard and token in _fp_guard:
                continue  # 路径子串不剔除，防止 content 丢路径
            remaining = remaining.replace(token, " ")
        remaining = re.sub(r'\s+', ' ', remaining).strip()
        if remaining:
            result["slots"]["content"] = remaining
        
        # Auto-extract email keyword for check_mail actions
        if result["actions"] and result["actions"][0] == "check_mail":
            kw = result.get("slots", {}).get("mail_keyword", "")
            if not kw:
                # Fallback: extract from content or message
                kw = result.get("slots", {}).get("content", "")
                if not kw:
                    kw = message
            # Clean: strip common filler prefixes
            kw = re.sub(r'^(关于|有没有|查一下|找一下|帮我|看看|里|有没有|查)', '', kw).strip()
            kw = re.sub(r'(的邮件|相关|的|邮件|给我看)$', '', kw).strip()
            if kw and len(kw) >= 1:
                result["slots"]["keyword"] = kw
        
        # Chain detection: if check_mail + "发给X" → add send_email
        if result["actions"] and result["actions"][0] == "check_mail":
            import re as _re3
            send_m = _re3.search(r'[，,]\s*(发给|发送给|转发给)(.+)', message)
            if send_m:
                result["actions"].append("send_email")
                recipient = send_m.group(2).strip()
                result["slots"]["recipient"] = recipient
                # Clean keyword: remove the send part
                kw = result["slots"].get("keyword", "")
                kw = kw.split('，')[0].split(',')[0].strip()
                kw = _re3.sub(r'(关于|有没有|找|一下|相关)$', '', kw).strip()
                result["slots"]["keyword"] = kw

        # ★★ 2026-09-25 (P0 链路断裂, id 646/650/654/656): file_path 槽在**空格**处
        #   被截断的修复 —— 只升级不降级, 详见 _repair_file_path_slot 的说明。
        _fp_now = result["slots"].get("file_path")
        if _fp_now:
            _fp_fixed = _repair_file_path_slot(message, _fp_now)
            if _fp_fixed != _fp_now:
                logger.debug("[IR] file_path 空格截断修复: %r -> %r", _fp_now, _fp_fixed)
                result["slots"]["file_path"] = _fp_fixed

        return result

    def build_chain(self, classification: dict) -> list[dict]:
        actions = classification.get("actions", [])
        delivery = classification.get("delivery")
        slots = classification.get("slots", {})
        chain = []
        
        # Action tools
        for intent_name in actions:
            info = ACTION_INTENTS.get(intent_name)
            if not info: continue
            if info.get("tool") is None:
                # 🔴 2026-08-24: url_shared + slots.url → 规则层 firecrawl 抓取 (本地模型不主动调工具)
                # 抓取结果在 pipeline IR chain 里喂模型总结 (与 office 工具同套路)
                if intent_name == "url_shared" and slots.get("url"):
                    chain.append({"tool": "firecrawl", "action": None,
                                  "params": {"url": slots["url"]}, "intent": "url_shared"})
                continue  # url_shared etc. — let model handle
            params = {}
            for sk, pk in info.get("slot_map", {}).items():
                if pk in slots: params[sk] = slots[pk]
            # Auto-fill common params from content
            if not params and "content" in slots:
                if intent_name == "file_read":
                    # 优先使用 REGEX 提取的完整路径（含盘符），避免剔除逻辑破坏后的 content 缺路径
                    if slots.get("file_path"):
                        params["path"] = slots["file_path"]
                    else:
                        # Extract filename from content
                        c = slots["content"]
                        fm = re.search(r'(\S+\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip))', c)
                        if fm:
                            params["path"] = fm.group(1)
                        else:
                            params["path"] = c
                    # Prepend dir_path (桌面/文档/下载/D盘/C盘) if present
                    # 2026-08-20: 对齐 file_write, 直接 resolve 绝对路径 (桌面/readme.md → Desktop/readme.md)
                    dir_prefix = slots.get("dir_path", "")
                    if dir_prefix and not params["path"].startswith(dir_prefix):
                        _dir_map = {"桌面": os.path.expanduser(r"~\\Desktop"),
                                    "文档": os.path.expanduser(r"~\\Documents"),
                                    "下载": os.path.expanduser(r"~\\Downloads")}
                        _resolved = _dir_map.get(dir_prefix)
                        if _resolved:
                            params["path"] = str(Path(_resolved) / params["path"])
                        else:
                            params["path"] = dir_prefix + "\\" + params["path"]
                elif intent_name == "repo_history":
                    # 带扩展名/路径的说法已由 slot_map(file_path) 走掉; 这里补**没扩展名**的
                    # ("谁改的 README" → content="谁改 README" → 挑出 "README")。
                    # 挑不出来就留空 —— 工具的 `_resolve_file` 会**诚实问**"查哪个文件".
                    params["file"] = _guess_file_token(slots["content"])
                elif intent_name in ("ssq_draw", "ssq_price"):
                    # ★ 期号区间 (2026086-2026095) 与复式规模 (7+1) 都**不能**靠槽位抠 ——
                    #   抠错一个数就是错的号码/错的价钱。索性把原话整句递进去, 由工具解析。
                    params["text"] = classification.get("raw") or slots.get("content", "")
                elif intent_name == "search":
                    params["query"] = slots["content"]
                elif intent_name == "weather":
                    # Extract city name from content (strip connectors)
                    city = slots["content"]
                    params["city"] = city
                elif intent_name in ("file_copy", "file_move"):
                    # ★ 2026-09-25 修 (监控日报实测): 这两条**原来没有任何参数提取** ⇒
                    #   `把 C:\...\demo.txt 复制到 D:\tmm-考卷` 出来 params={} ⇒
                    #   file_ops 拿不到源和目标, 必然失败 (日报里 chain/plan_exec 的报错就是它)。
                    #   源 = file_path 槽; 目标 = 原话里"复制到/拷贝到/移动到/挪到/移到"后面那段
                    #   (可能是目录也可能是文件, 两种都原样递, 由 file_ops 判)。
                    _raw_c = classification.get("raw") or ""
                    _src = slots.get("file_path", "") or ""
                    if _src:
                        params["path"] = _src
                    _dst = ""
                    _dm = re.search(r"(?:复制到|拷贝到|移动到|挪到|移到)\s*([^\s，,。;；]+)", _raw_c)
                    if _dm:
                        _dst = _dm.group(1).strip().strip('"').strip("'")
                    if not _dst:
                        # 目标写成"到桌面/到下载"这类
                        # ★ 目标写成"…到桌面/到下载"这类, 且**中间可以夹着源路径**
                        #   (`拷贝 D:\a\b.txt 到桌面`) ⇒ 直接找"到 + 位置", 不要求动词挨着。
                        _dm2 = re.search(r"到\s*(桌面|文档|下载)", _raw_c)
                        if _dm2:
                            _dst = _dm2.group(1)
                    if _dst:
                        params["dest"] = _dst
                    _empty_ok_skip = True          # 本分支不涉及空内容
                elif intent_name == "table_edit":
                    # ★★ 2026-09-28: 表格操作全部交给 table_ops (行/列/表头/单元格级)。
                    _raw_t = classification.get("raw") or ""
                    _lo_t = _raw_t.lower()
                    _op = slots.get("table_op") or _pick_table_op(_lo_t)
                    info = dict(info)                      # 局部覆盖, 不动全局表
                    info["action"] = _op
                    params["action"] = _op
                    # ── 目标文件 ──
                    _p_t = slots.get("file_path") or ""
                    if not _p_t or not re.search(r'\.(?:csv|tsv|xlsx|xlsm|docx)$', _p_t, re.I):
                        _fm_t = re.search(r'((?:[A-Za-z]:[\\/])?[^\s"\'，,。]+'
                                          r'\.(?:csv|tsv|xlsx|xlsm|docx))', _raw_t, re.I)
                        if _fm_t:
                            _p_t = _fm_t.group(1)
                    # 候选目录 (用户说了"桌面/文档/下载"就用, 否则桌面)
                    _exts = tuple(x for x in (".csv", ".xlsx", ".docx") if x in _lo_t) \
                            or (".csv", ".xlsx", ".docx")
                    _locs = [os.path.expanduser(s) for k, s in
                             (("桌面", r"~\Desktop"), ("文档", r"~\Documents"),
                              ("下载", r"~\Downloads")) if k in _raw_t] \
                            or [os.path.expanduser(r"~\Desktop")]
                    # ★ 实测坑: "桌面有个人员信息采集表.csv" 里 "有个" 会被正则粘进文件名
                    #   (抽出 "有个人员信息采集表.csv")。所以: 抽出来的裸名字若盘上不存在,
                    #   就去候选目录里找**名字是它后缀**的那个真文件。
                    if _p_t and not re.match(r'^[A-Za-z]:', _p_t) and not os.path.exists(_p_t):
                        _tail = Path(_p_t).name
                        for _d in _locs:
                            _hit_f = ""
                            try:
                                for _f in Path(_d).glob("*"):
                                    if _f.is_file() and (_f.name in _tail or _tail.endswith(_f.name)):
                                        _hit_f = str(_f)
                                        break
                            except Exception:
                                pass
                            if _hit_f:
                                _p_t = _hit_f
                                break
                    # 还是没落到真文件 → 按扩展名在候选目录里找; 唯一命中就用, 多个则诚实反问
                    if not (_p_t and os.path.exists(_p_t)):
                        _cands = []
                        for _d in _locs:
                            for _e in _exts:
                                try:
                                    _cands += [str(_p) for _p in Path(_d).glob("*" + _e)]
                                except Exception:
                                    pass
                        if len(_cands) == 1:
                            _p_t = _cands[0]
                        elif _cands:
                            params["_need_path"] = {
                                "candidates": [Path(c).name for c in _cands[:8]],
                                "looked_in": _locs}
                    if _p_t:
                        params["path"] = _p_t
                    # ★★ 实测坑 (2026-09-28, 门禁 [4](b) 抓到): 文件名里也会含操作关键词 ——
                    #   用户的表就叫「人员信息采集表.csv」, 测试里还有个「无表头.csv」。
                    #   "表头/列/行/删"这些字一旦出现在**文件名**里, 参数正则就会从文件名那个
                    #   位置开始匹配, 抽出 ['无表头.csv"', '第一行填表头', ...] 这种鬼东西,
                    #   工具直接回"表头给了 5 列但表里 3 列, 对不上"。
                    #   处置: 路径先摘掉, 参数抽取只在**指令正文**上做。
                    if _p_t:
                        _raw_t = _raw_t.replace(_p_t, " ")
                    _raw_t = re.sub(r'["\'“”]', " ", _raw_t)
                    # ── 各操作参数 ──
                    if _op == "add_rows":
                        _cm = re.search(r'(\d{1,4})\s*(?:条|行|个|份|人|列|组)', _raw_t)
                        if _cm:
                            params["count"] = int(_cm.group(1))
                        _pl = _raw_t.replace(_p_t, " ") if _p_t else _raw_t
                        _pl = re.sub(r'["\'“”‘’]', " ", _pl)
                        for _v in ("续写", "添加", "增加", "追加", "补充"):
                            _i = _pl.rfind(_v)
                            if _i >= 0:
                                _pl = _pl[_i + len(_v):]
                                break
                        _pl = re.sub(r'(?:条|行|个|份|人|组)\s*(?:记录|信息|数据|人员信息)?', " ", _pl)
                        _pl = re.sub(r'^\s*(?:一下|一些|几条|记录|信息|数据|人员信息|的|个)+',
                                     " ", _pl).strip()
                        # 只有带 ASCII 分隔符才当"用户给了真数据"(中文逗号是散文标点, 不算)
                        if _pl and any(c in _pl for c in (",", "\t", ";", "|")):
                            params["data"] = _pl
                        if not params.get("count") and not params.get("data"):
                            params["count"] = 1
                    elif _op == "del_rows":
                        _rng = re.search(r'第?\s*(\d+)\s*(?:到|至|-|~)\s*第?\s*(\d+)\s*行', _raw_t)
                        if _rng:
                            params["rows"] = f"{_rng.group(1)}-{_rng.group(2)}"
                        else:
                            _ns = re.findall(r'第\s*(\d+)\s*行', _raw_t) \
                                  or re.findall(r'(\d+)\s*行', _raw_t)
                            if _ns:
                                params["rows"] = ",".join(_ns)
                            else:
                                _wc = re.search(r'(?:删|去掉|删除)(?:掉)?\s*'
                                                r'([\u4e00-\u9fa5A-Za-z0-9]{1,10})\s*'
                                                r'(?:是|=|为)\s*([^\s，,。]+)', _raw_t)
                                if _wc:
                                    params["where_col"] = _wc.group(1)
                                    params["where_val"] = re.sub(
                                        r'(?:的)?(?:行|条|记录)$', "", _wc.group(2).strip())
                    elif _op == "set_cell":
                        _sm = re.search(r'第?\s*(\d+)\s*行\s*'
                                        r'([\u4e00-\u9fa5A-Za-z0-9_]{1,12})\s*'
                                        r'(?:改成|改为|换成|设成|填上|写上)\s*'
                                        r'([^\s，,。]{1,30})', _raw_t)
                        if _sm:
                            params["row"] = int(_sm.group(1))
                            params["col"] = _sm.group(2)
                            params["value"] = _sm.group(3)
                    elif _op in ("add_col", "del_col"):
                        _nm = re.search(r'(?:加|添加|增加|新增|添|加个)\s*(?:一|一个|1)?\s*列\s*'
                                        r'[：:]?\s*[「“"\']?'
                                        r'([\u4e00-\u9fa5A-Za-z0-9_（）()·]{1,16}?)[」”"\']?\s*$',
                                        _raw_t)
                        if not _nm:
                            # "删掉技能标签这一列" —— 名字必须**紧跟在动词后**, 且非贪婪
                            _nm = re.search(r'(?:删掉|删去|删除|去掉|移除|减)\s*[「“"\']?'
                                            r'([\u4e00-\u9fa5A-Za-z0-9_]{1,16}?)[」”"\']?\s*'
                                            r'(?:这|该)?一?列\s*[，。]?\s*$', _raw_t.strip())
                        if _nm:
                            params["name"] = _nm.group(1)
                        _df = re.search(r'(?:填|默认值?|值)\s*[:：]?\s*([^\s，,。]{1,20})', _raw_t)
                        if _df:
                            params["default"] = _df.group(1)
                    elif _op == "set_header":
                        _hm = re.search(r'(?:表头|列名)\s*(?:是|为|改成|改为|换成)?\s*[:：]?\s*'
                                        r'[「“"\']?([^\n，。]{2,})[」”"\']?\s*$', _raw_t.strip())
                        if _hm:
                            # ★ 交给工具时切成列表 —— 表头是"多列名字", 不是一整串
                            params["names"] = [x for x in
                                               re.split(r'[\s,，、;；|/]+', _hm.group(1).strip())
                                               if x]
                    elif _op == "sort":
                        _bm = re.search(r'(?:按|根据|以)\s*([\u4e00-\u9fa5A-Za-z0-9_]{1,10}?)\s*'
                                        r'(?:列)?\s*(?:排序|排|升序|降序|从)', _raw_t)
                        if _bm:
                            params["by"] = re.sub(r'(?:升序|降序|排序|排)$', "",
                                                  _bm.group(1)).strip()
                        if any(w in _raw_t for w in ("降序", "倒序", "从大到小", "由高到低")):
                            params["order"] = "desc"
                    elif _op == "dedupe":
                        _bm2 = re.search(r'(?:按|根据)\s*([\u4e00-\u9fa5A-Za-z0-9_]{1,10})\s*列?\s*'
                                         r'(?:去重|查重|去重复)', _raw_t)
                        if _bm2:
                            params["by"] = _bm2.group(1)
                    elif _op == "split":
                        _sr = re.search(r'每\s*(\d+)\s*(?:行|条)', _raw_t)
                        if _sr:
                            params["by_rows"] = int(_sr.group(1))
                        _sc = re.search(r'按\s*([\u4e00-\u9fa5A-Za-z0-9_]{1,10})\s*列?\s*(?:拆分|拆|分)',
                                        _raw_t)
                        if _sc:
                            params["by_col"] = _sc.group(1)
                        if not params.get("by_rows") and not params.get("by_col"):
                            params["by_rows"] = 500
                    elif _op == "merge":
                        _ps = re.findall(r'([^\s"\'，,。]+\.(?:csv|tsv|xlsx|xlsm))', _raw_t, re.I)
                        if _ps:
                            params["paths"] = ";".join(_ps)
                            _d0 = Path(_ps[0]).parent
                            params["target"] = str(_d0 / (Path(_ps[0]).stem + "_合并" + Path(_ps[0]).suffix))
                elif intent_name == "file_append":
                    # ★ 2026-09-26 新增。两件事必须分清, 否则要么丢内容要么编内容:
                    #   ① 用户**给了**要加的东西 (`…追加 张三,男,1990-…`) ⇒ 原样追加;
                    #   ② 用户**只说数量** (`…增加20条人员信息`) ⇒ 他没给数据, 但意思
                    #      很清楚: "你给造 20 条"。这时**不许**空着写 (工具会诚实拒绝,
                    #      但用户的活没干成), 也不许把指令碎片当内容写进去。正解是
                    #      交给执行层调模型生成 N 条, 再追加。
                    #   标注用 `_gen`; 内容留空 ⇒ 执行层看到 _gen 就生成。
                    _raw_a = classification.get("raw") or ""
                    _path_a = slots.get("file_path") or ""
                    if not _path_a:
                        _fm = re.search(r'(\S+\.(?:csv|txt|xlsx|xls|docx|doc|pptx|pdf|md|json))',
                                        _raw_a, re.I)
                        _path_a = _fm.group(1) if _fm else ""
                    if not _path_a:
                        # ★ 2026-09-26: 用户常常**不给文件名**, 只说"桌面有个csv…再增加20条"
                        #   (原话 #837 就是这样)。以前这里没有兜底 ⇒ 空路径一路走到 file_ops
                        #   ⇒ _resolve("") 落到项目根 ⇒ 报 Permission denied (用户看不懂)。
                        #   正解: 去他说的地方**找** —— 唯一命中就用它; 多个/零个则诚实反问。
                        _want_ext = tuple(x for x in (".csv", ".xlsx", ".xls", ".txt", ".md")
                                          if x in _raw_a.lower()) or (".csv", ".xlsx", ".txt")
                        _locs = []
                        for _k, _sw in (("桌面", r"~\Desktop"), ("文档", r"~\Documents"),
                                        ("下载", r"~\Downloads"), ("desktop", r"~\Desktop")):
                            if _k in _raw_a:
                                _locs.append(os.path.expanduser(_sw))
                        if not _locs:
                            _locs.append(os.path.expanduser(r"~\Desktop"))
                        _cands = []
                        for _d in _locs:
                            try:
                                for _e in _want_ext:
                                    _cands += [str(_p) for _p in Path(_d).glob("*" + _e)]
                            except Exception:
                                pass
                        # 用户说过"人员信息/这个文件"之类 → 名字里有这些字的最优先
                        _hint = [w for w in ("人员信息", "采集表", "名单", "表") if w in _raw_a]
                        _hit = [c for c in _cands if any(h in Path(c).name for h in _hint)] if _hint else []
                        _pick = _hit[0] if len(_hit) == 1 else (_cands[0] if len(_cands) == 1 else "")
                        if _pick:
                            _path_a = _pick
                            params["path"] = _pick
                        else:
                            # 多个都像 / 一个都没有 ⇒ 不猜, 让执行层诚实反问
                            params["_need_path"] = {
                                "candidates": [Path(c).name for c in _cands[:8]],
                                "looked_in": _locs,
                            }
                    if _path_a:
                        params["path"] = _path_a
                    # ══ 判定「用户到底给没给数据」—— 用的是**取载荷**法, 不是"剥词看剩什么" ══
                    #   为什么换掉第一版 (两处当场踩的坑, 都记下来):
                    #     坑A: 按残余法, 用户原话「桌面有个csv的文件，里面有人员信息，你再
                    #          增加20个人员信息」**没有路径可剥** ⇒ 残句 "桌面有个csv的文件，
                    #          有一你" 被当成数据, 真写了一行 18 字垃圾进文件。
                    #     坑B: 计数正则写的是 `\d+\s*(条|个|行|列|份|组)?` —— 那个 `?` 让
                    #          计数词变成**可选** ⇒ 把 `1990-01-01` / `110101199001011234`
                    #          里的数字当"数量"啃掉了, 用户给的身份证号被削成 `- -`。
                    #   现在的规矩:
                    #     ① 载荷 = **最后一个增补动词之后**的文本 (数据一般跟在动词后面);
                    #     ② 计数**必须带量词** (20个/20条/5行) 才认, 裸数字一概不动;
                    #     ③ 载荷里还剩东西才算"用户给了内容"; 只剩计数+泛指名词 ⇒ 需生成;
                    #     ④ 表格类目标要求载荷带 **ASCII** 分隔符 (中文逗号是散文标点, 不算)。
                    _APPEND_VERBS = ("增加", "添加", "追加", "填入", "填充", "补充",
                                     "增补", "多加", "添上", "补上", "加进")
                    # _resid = 原文去掉路径与引号 (只用于找"几个/多少条"这种计数)
                    _resid = _raw_a
                    if _path_a:
                        _resid = _resid.replace(_path_a, " ")
                    _resid = re.sub(r'["\'\u201c\u201d\u2018\u2019]', " ", _resid)
                    _cnt_m = re.search(r'(\d{1,4})\s*(?:条|个|行|列|份|组)', _resid)
                    _payload = ""
                    try:
                        _last = -1
                        for _v in _APPEND_VERBS:
                            _p2 = _raw_a.rfind(_v)
                            if _p2 > _last:
                                _last = _p2 + len(_v)
                        if _last >= 0:
                            _payload = _raw_a[_last:]
                    except Exception:
                        _payload = ""
                    # 载荷上剥掉引号 / 前导虚词 / 计数 / 泛指名词
                    _payload = re.sub(r'["\'\u201c\u201d\u2018\u2019]', " ", _payload)
                    # ★ 只剥**方位/介词**类前导词。第一版把 你/我/再/又/多/些/个 也列进去了,
                    #   结果 `追加 你好世界` 被啃成 `好世界` (把"你"当虚词吃了) —— 内容里
                    #   出现的字一个都不许动。虚词要在**剥完之后仍是虚词**才算, 宁可留一点
                    #   噪声也不要吃掉用户写的字。
                    _payload = re.sub(r'^[\s，,。;；、:]*(?:里面|里头|里边|里|内|中间|中|上边|上面|'
                                      r'下边|下面|上|下|到|在|往|向)+\s*', " ", _payload)
                    _payload = re.sub(r'\d{1,4}\s*(?:条|个|行|列|份|组)', " ", _payload)
                    # ★★ 关键分离 (第一版把这两件事混在一起, 于是 `添加 一条记录` 被削成
                    #   `一条` —— 用户写的字被"判定逻辑"啃掉了):
                    #     _payload_raw = 真正要写进文件的内容 (剥到计数为止, **不再动一个字**)
                    #     _payload_key = 只用来判断"这算不算给了内容"的替身 (可随便剥)
                    #   判定用替身, 写入用原样。数据和判据必须分开。
                    _payload_raw = _payload.strip(" \t,，。;；、:：")
                    _key = _payload_raw
                    for _w in ("人员信息", "人员", "信息", "数据", "记录", "条目", "资料",
                               "文本", "内容", "数据行", "示例", "的", "了", "一些", "几"):
                        _key = _key.replace(_w, " ")
                    _key = _key.strip(" \t,，。;；、:：")
                    _payload = _payload_raw
                    _ext_a = Path(_path_a).suffix.lower() if _path_a else ""
                    _tabular = _ext_a in (".csv", ".xlsx", ".xls") or (
                        not _ext_a and any(k in _raw_a.lower() for k in ("csv", "excel", "表格")))
                    _mk = re.search(r'(?:内容|正文|数据|body)\s*[:：]\s*(.+)$', _raw_a, re.S)
                    if _mk and _mk.group(1).strip():
                        params["content"] = _mk.group(1).strip()      # 显式标记最好使
                        logger.info("file_append: 用显式标记后的内容")
                    elif _key and len(_key) >= 2 and \
                            ((not _tabular) or any(c in _payload_raw for c in (",", "\t", ";"))) \
                            and not re.fullmatch(r'[一二三四五六七八九十两半0-9\s]+', _key):
                        params["content"] = _payload_raw              # 用户给了真数据 → **原样**用
                        logger.info("file_append: 用用户给的数据 (%d 字)", len(_payload_raw))
                    else:
                        params["_gen"] = {                            # 只说数量 ⇒ 交给执行层生成
                            "count": int(_cnt_m.group(1)) if _cnt_m else 5,
                            "header_from": _path_a,
                            "source": "用户只说数量, 未给数据",
                        }
                elif intent_name in ("file_write",):
                    # file_write: 正确区分「写入内容」和「文件名」 (2026-08-20 重写)
                    # 带扩展名 → 文件名；否则剩余文字就是要写入的内容，文件名用默认
                    raw_content = slots.get("content", "").strip()
                    # 用原始消息提取内容: known 剔除会丢 "的/了" 等字 (2026-08-20)
                    raw_msg = classification.get("raw") or ""
                    fname = "文档.txt"
                    content = raw_content
                    _bw_hit = False          # "把X写入Y" 句式命中标记 (A① 判据③)
                    # 优先用 REGEX_SLOTS 已提取的 file_path (已剥前缀)
                    _fp_slot = slots.get("file_path", "")
                    if _fp_slot:
                        fname = _fp_slot
                        if raw_msg and _fp_slot in raw_msg:
                            _idx = raw_msg.find(_fp_slot)
                            content = (raw_msg[:_idx] + " " + raw_msg[_idx + len(_fp_slot):]).strip()
                        else:
                            content = raw_content.replace(_fp_slot, "").strip()
                    else:
                        fm = re.search(r'([^\s,，。]+\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip))', raw_content)
                        if fm:
                            fname = _strip_fp_prefix(fm.group(1))
                            content = raw_content.replace(fm.group(1), "").strip()
                    # ── ★ 2026-09-25 修 (监控日报实测): 「在 <绝对目录> 新建 <文件>」──
                    #   旧行为: path 只拿裸文件名, **目录串被当成写入内容**
                    #       `在 <盘>:\<用户>\Desktop 新建 x.txt`
                    #         ⇒ path='x.txt' content='<盘>:\<用户>\Desktop'
                    #       `在 D:\tmm-考卷\题目 新建 笔记.txt 写入 你好`
                    #         ⇒ path='笔记.txt' content='D:\tmm-考卷\题目 你好'
                    #   文件落哪全靠"裸名默认桌面"的侥幸, 而内容里多出一段目录。
                    #   正解: 原话里出现「绝对目录 + 新建/创建/写入/保存到」⇒ 那个目录是**位置**,
                    #   拼进 path 前缀, 并从内容里剥掉。
                    _dirm = re.search(r'([A-Za-z]:[\\/][^\s，,。";|<>]*?)\s*'
                                      r'(?:新建|创建|建立|写入|保存到|存到|放到)', raw_msg)
                    if _dirm:
                        _d = _dirm.group(1).rstrip('\\/')
                        if _d and not fname.lower().startswith(_d.lower()):
                            fname = _d + "\\" + fname.lstrip("\\/")
                        content = content.replace(_d, " ").strip()
                    # content 清理: 剥残留复合词/指令词/标点 (2026-08-20)
                    content = re.sub(r'(?:txt|word|pdf|xlsx?|docx?|pptx?|md|json)\s*文档', ' ', content)
                    content = re.sub(r'(?:写入|新建|新建一个|创建|保存|存为|名字叫|名为|叫做|命名为|正文[:：]?|内容是|内容[:：]?|一行内容[:：]?|一行[:：]?|一个|并)', ' ', content)
                    content = re.sub(r'(?:在桌面|在文档|在下载|到桌面|到文档|到下载|保存到|存到|放到|桌面上的|文档里的|文件名)', ' ', content)
                    content = re.sub(r'[，,、。；;：:\s]+$', '', content).strip()
                    content = re.sub(r'^[，,、。；;：:\s]+', '', content).strip()
                    content = re.sub(r'^\s*(?:在|到|把)\s*[，,、。]?\s*', '', content)  # 介词残留开头
                    content = re.sub(r'\s+', ' ', content).strip()
                    # 触发词本身被当成内容 ("写入文件" → content="文件") → 视为无内容。
                    # 否则会写出一个内容就是"文件"二字的垃圾文件 (实测 path=文档.txt content=文件)。
                    if content in ("文件", "文档", "文本", "内容", "字", "东西", "文件里", "个文件"):
                        content = ""
                    # ★ 纯"新建 x.txt"(原话里没别的内容) ⇒ 意图是建个**空文件**; 标记给 file_ops 放行。
                    #   必须放在**内容清理之后**判断 (清理前 content 可能还是目录串/指令词, 看着非空),
                    #   否则标记不会置位, 用户"新建 x.txt"会被"写入内容为空"防线拦下。
                    if not content and re.search(r"(?:新建|创建|建立)", raw_msg):
                        params["empty_ok"] = True
                    # ── "把X写入Y" 句式: 从 raw_msg 二次拆解 (2026-08-20 v3) ──
                    # "把你好世界写入agent_test.txt" → content=你好世界, fname=agent_test.txt
                    # IR file_path 正则 \w 吞中文, 上面只能剥"把"前缀, 剩"你好世界写入agent_test.txt"
                    if raw_msg:
                        _bw = re.search(r'把(.+?)(?:写入到?|保存到?|存到?|放到?|写成|存为)([^\s，,。]+\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip))', raw_msg)
                        if _bw:
                            _bw_content = _bw.group(1).strip()
                            _bw_fname = _bw.group(2).strip()
                            if _bw_content and _bw_fname:
                                fname = _bw_fname
                                content = _bw_content
                                _bw_hit = True
                    # ── ★ 2026-09-20 修 (A① 垃圾 文档.txt): "存到 X" ≠ "写文件" ──
                    #   pipeline 的 Auto-fill (delivery=="save" 且无动作 → 补 file_write)
                    #   让**任何**带"存到/保存到"的句子都走到这里: 没有文件名 → 默认
                    #   文档.txt, 内容 = 剥完指令词后剩下的**那句话本身**。
                    #   实测 (修前): "服务还活着吗 存到 tmp/_batch2/" → 28B 文档.txt
                    #   (内容就是这句话) · "写入文件" → 0B 文档.txt ·
                    #   "把这段话存到桌面" → 桌面多一个 文档.txt。
                    #   判据 (能区分, 不是禁掉) —— 至少满足一条才算写文件请求:
                    #     ① 显式文件名 (file_path 槽位 / 消息里带扩展名)
                    #     ② 显式内容标记 (内容: / 正文: / 一行内容:)
                    #     ③ "把X写入Y" 句式 (_bw_hit)
                    #   都不满足 → 不生成写步骤 (交给技能层/模型层; 技能自带落盘)。
                    _explicit_name = bool(_fp_slot) or bool(re.search(
                        r'\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip)', raw_msg or ""))
                    _explicit_body = bool(re.search(r'(?:内容|正文|一行内容|文字)\s*[:：]', raw_msg or ""))
                    if not (_explicit_name or _explicit_body or _bw_hit):
                        continue
                    # ★ 2026-09-22 修 (真引擎实跑抓到的最后一块): "写X保存到Y" 是
                    #   **生成 + 落盘**, 不是"把这句话当内容写进文件"。实测
                    #   `写一首诗保存到桌面大哥.txt` 路径对了、但内容被写成 "写一首诗"
                    #   (4 字废话) —— 用户要的是"诗", 不是"写一首诗"这几个字。
                    #   判据: 清理后的内容若是**生成型指令**(以 写/作/生成/做/拟/编/来/画… 开头),
                    #   就不在这里编内容 —— 跳过写步骤, 交给规划链 (llm 生成 → 写盘),
                    #   那里才会真的产出诗/方案/文案。显式正文 ("内容是:"/"把X写入Y") 不受影响。
                    if not _bw_hit and re.match(r'^(?:写|作|生成|做|拟|编|来|画|帮我写|给我写|出一|来个)',
                                                (content or "").strip()):
                        continue
                    dpath = slots.get("dir_path", "")
                    if dpath:
                        import os as _os
                        resolved = {"桌面": _os.path.expanduser(r"~\\Desktop"),
                                    "文档": _os.path.expanduser(r"~\\Documents"),
                                    "下载": _os.path.expanduser(r"~\\Downloads")}.get(dpath, dpath)
                        params["path"] = str(Path(resolved) / fname) if resolved else fname
                    else:
                        params["path"] = fname
                    params["content"] = content
                elif intent_name in ("file_rename",):
                    params["newname"] = slots["content"]
                elif intent_name in ("file_find",):
                    params["pattern"] = slots["content"]
                elif intent_name == "file_list":
                    # ★★ 2026-09-25 修: **绝对路径优先** —— 原来是"只看 dir_path",
                    #   消息里给了绝对路径也不认 → params 为空 → 列了默认目录(项目根),
                    #   把密钥文件名摆给用户 (实测 D1)。
                    # ★ 注意: build_chain 的作用域里**没有** message —— 只有 classification/slots。
                    #   第一版我写了 _extract_abs_dir(message) → 静态门禁当场抓出
                    #   "undefined name 'message'" (NameError)。用 raw 兜 content。
                    _abs = _extract_abs_dir(str(classification.get("raw")
                                               or slots.get("content") or ""))
                    if _abs:
                        params["path"] = _abs
                    # 列出目录: dir_path(桌面/文档/下载) 映射为绝对路径
                    dpath = slots.get("dir_path", "") if not _abs else ""
                    if dpath:
                        import os as _os_ls
                        resolved = {"桌面": _os_ls.path.expanduser(r"~\Desktop"),
                                    "文档": _os_ls.path.expanduser(r"~\Documents"),
                                    "下载": _os_ls.path.expanduser(r"~\Downloads")}.get(dpath, dpath)
                        params["path"] = resolved if resolved else dpath
                elif intent_name == "kb_delete":
                    # 提取要删的文档名 (content 是剥离关键词后的剩余)
                    c = slots.get("content", "").strip()
                    if c:
                        params["source"] = c
                elif intent_name == "chart":
                    # 图表: content → data (自动识别 类别:数值 格式)
                    c = slots.get("content", "").strip()
                    if c:
                        params["data"] = c
                    # 图表类型: 优先用 classify 从原始消息提取的 chart_type
                    params["action"] = slots.get("chart_type", "pie")
                    # 标题
                    _title_m = re.search(r'(?:标题|名为|叫)[:：]?\s*(.+?)(?:[，,。]|$)', c)
                    if _title_m:
                        params["title"] = _title_m.group(1).strip()
                elif intent_name == "send_email":
                    # ★ 2026-09-19 修 (真 bug): 原来**完全不解析**收件人/主题/正文 →
                    #   params={} → IR chain 兜底塞硬编码默认邮箱 → "发邮件给 x@y.com …"
                    #   竟发给虎哥自己的 163, 主题/正文也是整条原始消息。
                    #   这里按消息解析 (与 email.regex_send 路由同一套 regex):
                    #     收件人 = 消息里的邮箱地址; 主题 = "主题:xxx"; 正文 = "内容/正文:xxx"
                    _raw = classification.get("raw") or ""
                    # ★ 2026-09-23 修 (真 bug): 原正则 `[\w.+-]+` 在 Python3 默认
                    #   Unicode 模式下**吞中文** —— 实测 "…攻略发给someone@example.com发邮件"
                    #   整句被吞成收件人 → smtplib 报 SMTPUTF8 (收件人里有中文)。
                    #   改成显式 ASCII 字符类; 并优先用 classify 提取的 to_addr 槽。
                    _to_m = re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', _raw)
                    _to_v = slots.get("to_addr") or (_to_m.group(0) if _to_m else "")
                    if _to_v:
                        params["to"] = _to_v
                    _sj = re.search(r'主题\s*[:：]\s*(.+?)(?:[,，]?\s*(?:内容|正文)\s*[:：]|$)', _raw)
                    if _sj:
                        params["subject"] = _sj.group(1).strip()
                    _bd = re.search(r'(?:内容|正文)\s*[:：]\s*(.+?)$', _raw)
                    if _bd:
                        params["body"] = _bd.group(1).strip()
                    # ★ 2026-09-23 加 (实测): 消息里有**真实存在**的文件 → 当附件发。
                    #   实测 "…攻略.md" 这个攻略发给X发邮件" 只把路径当正文文字,
                    #   用户想发的那个文件根本没带上。
                    _att = ""
                    _full_m = re.search(r'[A-Za-z]:[\\/][^\s"\'，,。；;]+', _raw)
                    if _full_m:
                        _fpc = _full_m.group(0).strip().rstrip('"\'')
                        if os.path.isfile(_fpc):
                            _att = _fpc
                    if _att:
                        params["attachments"] = _att
                        params.setdefault("subject", os.path.basename(_att)[:30])
                        params.setdefault("body", "请查收附件。")
                    # ★ 2026-09-23 加 (实测): subject/body 缺失兜底 —— send_email.run()
                    #   把 subject/body 当必填; "给X发一份测试邮件" 实测只解析出 to,
                    #   真跑会 TypeError (缺必填参数), 邮件根本发不出去。
                    #   兜底内容 = content 槽剥掉邮箱与"给/发"等虚词后的剩余。
                    if not params.get("subject") or not params.get("body"):
                        _c = str(slots.get("content") or "").strip()
                        if _to_v:
                            _c = _c.replace(_to_v, "").strip(" ，,。给发一份寄:：")
                        _c = _c or "来自虎哥的消息"
                        params.setdefault("subject", _c[:30])
                        params.setdefault("body", _c)
                elif intent_name in ("office_analyze", "office_extract", "office_pdf", "office_word"):
                    # 🔴 office 工具 (2026-08-19): 路径从消息提取 (Windows 路径或文件名)
                    # 工具输出不直接返回, pipeline 里 office 意图走 _office_preread 喂模型
                    # build_chain 只有 classification/slots, 用 content(剥离关键词后的剩余)做匹配
                    _of_content = slots.get("content", "") or ""
                    _of_path = ""
                    _of_m = re.search(r'([A-Za-z]:[\\/][^\s"\'，,。]+|[^\s"\'，,。]+\.(?:xlsx|xlsm|docx?|pptx?|pdf))', _of_content)
                    if _of_m:
                        _of_path = _of_m.group(1).strip().strip('"\'')
                    if _of_path:
                        params["path"] = _of_path
                    # extract_column 需要列号/列名
                    if intent_name == "office_extract":
                        _col_m = re.search(r'(?:第|列)[:：]?\s*(\d+|[一二三四五六七八九十]+)', _of_content)
                        if _col_m:
                            _cn_raw = _col_m.group(1)
                            _cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                            params["col"] = _cn_map.get(_cn_raw, _cn_raw)
                    # pdf/word 动作细分
                    if intent_name == "office_pdf":
                        params["action"] = "merge_pdf" if any(w in _of_content for w in ["合并", "拼"]) else "extract_pdf_text"
                    if intent_name == "office_word":
                        params["action"] = "merge_word" if any(w in _of_content for w in ["合并", "拼"]) else "extract_word"
            # Inject mail_keyword if check_mail and keyword in slots
            if intent_name == "check_mail" and "keyword" in slots:
                params["keyword"] = slots["keyword"]
                # Also add a read step after list for mail forwarding chains
                if "send_email" in actions:
                    chain.append({"tool": "check_mail", "params": {"action": "list", "keyword": slots["keyword"]}}) 
                    continue  # skip default append below
            
            if intent_name == "check_mail" and "keyword" in slots:
                params["keyword"] = slots["keyword"]
            if intent_name == "check_mail" and "mail_keyword" in slots:
                params["keyword"] = slots["mail_keyword"]
            
            step = {"tool": info["tool"], "params": params}
            if info.get("action"): step["action"] = info["action"]
            chain.append(step)
        
        # Delivery — only add if there's an action producing output
        if delivery and delivery != "show":
            if not actions:
                # No action = no output to deliver. Likely a question.
                return chain
            d_info = DELIVERY_INTENTS.get(delivery)
            if not d_info: return chain
            
            output_type = ACTION_INTENTS.get(actions[0], {}).get("output_type", "text") if actions else "text"
            
            if delivery == "email":
                params = {}
                # ★ 2026-09-23: 消息里明写的邮箱优先于实体表猜测
                if slots.get("to_addr"):
                    params["to"] = slots["to_addr"]
                recip = slots.get("recipient", "")
                if not params.get("to") and recip and self.ke:
                    r = self.ke.lookup_entity_field(recip, "email")
                    if r: params["to"] = r.get("email", recip)
                # Subject and body
                # Body: use $prev.output if previous step was file_read
                if actions and actions[0] == "file_read":
                    params["subject"] = slots.get("content", "文件")[:30]
                    params["body"] = "$prev.output"
                elif "content" in slots and slots["content"]:
                    params["subject"] = slots["content"][:30]
                    params["body"] = slots["content"]
                else:
                    # Default based on action
                    action_name = actions[0] if actions else "message"
                    params["subject"] = action_name
                    params["body"] = f"来自虎哥的{action_name}"
                # ★ 2026-09-19 修 (真 bug): 意图层命中 send_email 时**已经加了**一个
                #   send_email 步骤; 这里再 append 一个 → 同一封邮件**发两次**。
                #   实测: "发邮件给 x@y.com 主题:嗨 内容:你好" → chain=['send_email','send_email']。
                #   修法: 已有 send_email 步骤时**合并参数**(保留 delivery 解析出的 to/subject/body),
                #   不追加新步骤。
                _ex_send = next((s for s in chain if s.get("tool") == "send_email"), None)
                if _ex_send is not None:
                    # ★ 用 setdefault 而非 update: 意图层已按消息**显式解析**出的
                    #   to/主题/正文 优先; delivery 只补空缺。
                    #   (实测: update 会让通用的 content 槽把"主题:嗨/内容:你好"覆盖成整条原消息)
                    for _k, _v in params.items():
                        _ex_send["params"].setdefault(_k, _v)
                else:
                    chain.append({"tool": "send_email", "params": params})
            
            elif delivery == "save":
                # file_write 已自行 resolve dir_path 到绝对路径并写入内容，
                # save delivery 是多余的（会错误追加 content="$prev.output" 的冗余写步骤）
                if actions and "file_write" in actions:
                    return chain
                save_path = slots.get("save_path", slots.get("dir_path", ""))
                # Default to desktop if no path specified
                if not save_path:
                    import os as _os
                    save_path = _os.path.expanduser(r"~\Desktop")
                if save_path and self.ke:
                    parsed = self.ke.parse(f"保存到{save_path}")
                    paths = parsed.get("paths", []) if parsed else []
                    if paths:
                        pinfo = paths[0]
                        save_path = pinfo.get("path", "") if isinstance(pinfo, dict) else str(pinfo)
                
                if output_type == "file_path":
                    if save_path:
                        chain.append({"tool": "file_ops", "action": "move",
                                       "params": {"source": "$prev.path", "dest": save_path}})
                else:
                    if save_path:
                        fname = f"output_{int(time.time())}.txt"
                        chain.append({"tool": "file_ops", "action": "write",
                                       "params": {"path": str(Path(save_path) / fname),
                                                  "content": "$prev.output"}})
        return chain


_TBL_VERBS = ("续写", "添加", "增加", "追加", "补充", "加行", "增行", "加几行", "补几行",
              "加列", "增列", "添加一列", "增加一列", "加一列", "删列", "删除列", "去掉列",
              "删行", "删除行", "去掉行", "删掉几行", "表头", "改列名", "列名",
              "拆分", "拆表", "拆成", "拆开", "分割", "分开",   # ★ 必须含裸"合并/拆分"
              "合并", "合表", "拼起来", "拼一起",              #   (实测漏掉裸"合并" → "合并成一个表" 没命中)
              "去重", "查重", "去掉重复", "排序", "升序", "降序",
              "单元格", "某一格", "格子里", "填进表", "填入表",
              # ★ 2026-09-28 门禁 verify_claim_guard 的 B13/B15 抓到: 「填入20条模拟数据」
              #   「增补信息」这两种说法漏了 —— 用户说"填入/增补"跟说"添加/补充"是同一件事,
              #   判成两条路就等于同一张表两种待遇。
              "填入", "增补", "补上")


def _has_table_verb(msg: str) -> bool:
    """这句话里有没有**表格级**的动作?

    光靠固定词表不够 —— 真实说法是"删掉技能标签这一列""第3行职位改成X"这种,
    动词和"行/列"之间夹着列名/行号。所以补两条**位置自由**的正则。
    """
    if any(w in msg for w in _TBL_VERBS):
        return True
    if re.search(r'(?:删|去掉|移除|减|挪走)\S{0,12}(?:行|列|条)', msg):
        return True
    if re.search(r'第?\s*\d+\s*行\S{0,14}(?:改成|改为|换成|设成|填上|写上)', msg):
        return True
    return False


def _pick_table_op(text: str) -> str:
    """★ 2026-09-28: 从一句自然话里判出**哪个表格操作**。

    表格的行/列/表头/单元格是四种不同的东西, 一个"改"字什么都指。
    顺序有讲究 —— 越具体的先说, 兜底才是"加行"(续写/添加/补几条这种最常见)。
    """
    t = (text or "").lower()
    # 单元格级最具体, 先判: "第3行职位改成技术总监"
    if re.search(r'第?\s*\d+\s*行\S{0,14}(?:改成|改为|换成|设成|填上|写上)', t) \
            or any(w in t for w in ("单元格", "某一格", "格子里", "这一格", "第几格")):
        return "set_cell"
    if any(w in t for w in ("表头", "列名", "头部", "首行")):
        return "set_header"
    if any(w in t for w in ("加列", "增列", "添加一列", "增加一列", "加一列", "新增一列")) \
            or re.search(r'(?:加|添|新增|增加)\S{0,6}列', t):
        return "add_col"
    if any(w in t for w in ("删列", "删除列", "去掉列", "减列", "移除列")) \
            or re.search(r'(?:删|去掉|移除|减)\S{0,10}列', t):
        return "del_col"
    if any(w in t for w in ("删行", "删除行", "去掉行", "减行", "移除行")) \
            or re.search(r'(?:删|去掉|移除|减)\S{0,12}(?:行|条)', t):
        return "del_rows"
    if any(w in t for w in ("拆分", "拆表", "拆成", "拆开", "分割", "分开成")):
        return "split"
    if any(w in t for w in ("合并", "合表", "拼起来", "拼一起", "汇总成一个")):
        return "merge"
    if any(w in t for w in ("去重", "查重", "去掉重复", "去重复")):
        return "dedupe"
    if any(w in t for w in ("排序", "升序", "降序", "从大到小", "从小到大", "由高到低", "由低到高")):
        return "sort"
    return "add_rows"          # 续写/添加/增加/补 N 条/再加几行 → 都是加行


_router: Optional[IntentRouter] = None
def get_router(ke=None) -> IntentRouter:
    global _router
    if _router is None or ke is not None:
        _router = IntentRouter(ke)
    return _router
