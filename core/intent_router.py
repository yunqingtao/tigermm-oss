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
    "send_msg":     {"patterns": ["发给", "发送给", "通知", "告诉"],                "tool": None,               "action": "send",           "output_type": "text"},
    "file_list":    {"patterns": ["列出", "文件列表", "列目录", "列一下"],            "tool": "file_ops",         "action": "list",           "output_type": "text"},
    "file_find":    {"patterns": ["找一下", "查找文件", "搜索文件"],               "tool": "file_ops",         "action": "search",         "output_type": "list"},
    "file_rename":  {"patterns": ["重命名", "改名", "改成"],                      "tool": "file_ops",         "action": "rename",         "output_type": "text"},
    "file_delete":  {"patterns": ["删除", "删掉", "移除"],                        "tool": "file_ops",         "action": "delete",         "output_type": "text",
                      "slot_map": {"path": "content", "dir_path": "dir_path"}},
    "file_copy":    {"patterns": ["复制到", "拷贝到"],                            "tool": "file_ops",         "action": "copy",           "output_type": "text"},
    "file_move":    {"patterns": ["移动到", "挪到", "移到"],                       "tool": "file_ops",         "action": "move",           "output_type": "text"},
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
    "system_info":  {"patterns": ["系统状态", "系统信息", "电脑状态"],             "tool": "system_info",      "action": "sysinfo",        "output_type": "text"},
    "disk_info":    {"patterns": ["磁盘", "硬盘空间", "磁盘空间"],                  "tool": "system_info",      "action": "disk",           "output_type": "text"},
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
    "check_mail": 3, "weather": 3, "screenshot": 3, "open_browser": 3, "system_info": 3, "disk_info": 3, "kb_search": 3, "kb_list": 3, "kb_delete": 3, "chart": 3,
    "office_analyze": 3, "office_extract": 3, "office_pdf": 3, "office_word": 3,
    "file_read": 2, "file_write": 2, "file_list": 2, "file_find": 2, "file_copy": 2,
    "file_move": 2, "file_delete": 2, "file_rename": 2, "file_organize": 2,
    "send_email": 3,
    "search": 1,
}

DELIVERY_INTENTS = {
    "email": {"patterns": ["发给", "发送给", "告诉", "通知", "发邮件给", "发邮件"], "tool": "send_email"},
    "save":  {"patterns": ["保存到", "保存", "存到", "存", "放到", "导出到", "导出"], "tool": "file_ops"},
    "show":  {"patterns": ["展示", "显示", "看看", "看一下"], "tool": None},
}

ENTITY_SLOTS = {"recipient": {"field": "email"}, "city": {"field": "city"}}
REGEX_SLOTS = {
    "file_path": r'([\w\-\\/:]+\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip))',
    # 2026-08-20: dir_path 排除 "txt文档/word文档" 复合词误捕 (原正则把 "txt文档" 的"文档"当目录 → 桌面被顶替成 Documents)
    "dir_path":  r'(桌面|下载|D盘|C盘|(?<![A-Za-z0-9])文档(?![A-Za-z0-9]))',
    "mail_keyword": r'(?:关于|有没有)(.+?)(?:的邮件|邮件)', 
}

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
    seg = seg.rstrip("\\/").strip()
    return seg if len(seg) >= 3 else ""


def _intent_text(message: str) -> str:
    """意图词匹配用的正文 —— 剔除路径片段。

    只影响**意图词匹配**; 其余 (路径槽提取/扩展名判定) 仍用原消息。
    """
    try:
        return _PATH_SEG_RE.sub(" ", message or "")
    except Exception:
        return message or ""


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
        
        # 1.5 Implicit file detection — only if no action matched
        if re.search(r'\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip)', message):
            if not result["actions"]:
                # Check if this is a create/write intent (2026-08-20: 加 保存/文件名/存到/放到)
                if any(w in msg_lower for w in ["创建", "新建", "写入", "保存", "文件名", "存到", "放到", "写"]):
                    result["actions"] = ["file_write"]
                else:
                    result["actions"] = ["file_read"]
        
        # 1.9 ★ 2026-09-23 加: 「整条消息就是一个文件路径」→ 读它。
        #   实测: 用户从题目目录粘路径过来 (A1 查询.txt), 期望"看这个文件";
        #   原行为要么被文件名里的字劫持成搜索, 要么落到模型闲聊。
        #   判据: 剔除路径后几乎没有可判文本 (<=2 个有效字符) + 消息里确实有路径。
        _pseg = _PATH_SEG_RE.search(message)
        # ★★ 不能加 `not result["actions"]` 前提 —— 实测 1.5 节("Implicit file
        #   detection")已把带扩展名的消息设成 file_read, 加前提会让本块变死代码
        #   (file_path 仍被 REGEX_SLOTS 截成 `查询.txt` → 落桌面读 → File not found)。
        if _pseg:
            _rest = re.sub(r'[\s，。；；、！？?\'"()（）]', '', _intent_text(message))
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
                     "创建", "新建", "写一份", "写一篇")
        # ⚠ 不能用 _pseg (那是**文件**路径正则, 要求末尾带扩展名) ⇒ 纯目录永远不匹配,
        #   第一版就是这么写的 → 一条都没触发 (清缓存重测才发现)。改用 _extract_abs_dir。
        _dir_cand = _extract_abs_dir(message)
        if _dir_cand and not re.search(r"\.[A-Za-z0-9]{1,5}$", _dir_cand) \
                and any(w in msg_lower for w in _LIST_Q) \
                and not any(w in msg_lower for w in _WRITEISH):
            result["actions"] = ["file_list"]
            # build_chain 的 file_list 分支自己会用 _extract_abs_dir(raw) 抽路径, 这里不用写。

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
                elif intent_name == "search":
                    params["query"] = slots["content"]
                elif intent_name == "weather":
                    # Extract city name from content (strip connectors)
                    city = slots["content"]
                    params["city"] = city
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


_router: Optional[IntentRouter] = None
def get_router(ke=None) -> IntentRouter:
    global _router
    if _router is None or ke is not None:
        _router = IntentRouter(ke)
    return _router
