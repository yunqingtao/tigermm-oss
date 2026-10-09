r"""文件名/路径「说法」解析 —— **两条路共用的一份规则**。

为什么单独成模块 (2026-09-22 用真引擎实跑抓到的):
    同一句 `写一首诗保存到桌面大哥.txt`
      · 知识库那条路 (core/knowledge.py) → 正确得到 `大哥.txt`
      · IR 链那条路 (core/intent_router.py) → 得到**整句**
        `写一首诗保存到桌面大哥.txt` ⇒ 且内容为空 ⇒ 用户看到"写入内容为空"
    两根因都是"文件名候选里混着散文": 正则 `[\w\-\\/:]+\.(ext)` 里 `\w` **包含中文**,
    于是整句被当成一个 token。旧的剥前缀表**只从左剥**, 遇到"写一首诗保存到桌面"就卡住。

规则 (按**用户真实说法**总结, 不是我们文档里那种"桌面/文件名"格式):
    ① 先按**最后一个目录词**切 —— 用户总在说"到桌面X"/"存到文档X"
    ② 再按**最后一个强动词**切 —— "写入/保存到/另存为/命名为…"
    ③ 再循环剥弱动词/量词/助词 —— "把/写一首/一个/的…"
    ④ 折叠连续空白, 去首尾分隔符

共用理由: 两条路口径必须一致, 否则"同一句话两种结果"—— 这正是本模块存在的起因。
"""
from __future__ import annotations

import re

#: 目录词 (命中就切; 含英文)
DIR_WORDS = ("桌面", "文档", "下载", "desktop", "documents", "downloads")

#: 强动词 (散文与文件名之间的分界; 命中就切)
STRONG_VERBS = ("写入到", "保存到", "另存为", "命名为", "取名为", "写到", "写入", "写进",
                "存到", "存进", "放进", "放到", "放在")

#: 弱动词/量词/助词 (从左循环剥)
WEAK_PREFIXES = ("写入到", "保存到", "另存为", "命名为", "取名为", "写到", "写入", "写进",
                 "存到", "存进", "放进", "放到", "放入", "放在", "创建", "新建", "生成",
                 "写一首", "写一篇", "写一份", "写一段", "写个", "写",
                 "叫", "叫做", "帮我", "请", "把", "将", "给",
                 "一个", "一首", "一篇", "一份", "一段", "个", "条", "句", "张", "份")

#: 支持的扩展名 (各正则共用)
EXTS = (r"(?:txt|text|py|json|md|markdown|pdf|docx?|xlsx?|pptx?|csv|png|jpe?g|gif"
        r"|log|mp3|mp4|zip|rar|html?)")

#: 文件名候选 (允许中文名; 排除标点与括号)
#: ★ 右边界用 (?![A-Za-z0-9_]) 而不是 (?!\w) —— `\w` 在 Unicode 下**含中文**,
#:   否则 "写入F:\data.txt文件" 这类"扩展名后紧跟中文"的说法一个都匹配不到。
FN_RE = re.compile(r"([^\s，。、；：,;！!？?\"'（）()\[\]]{1,60}?\." + EXTS + r")(?![A-Za-z0-9_])", re.I)

# ═══ 名内空格 ═══════════════════════════════════════════════════════════════
# ★★ 2026-09-25 修 (真数据 id 646/650/654/656 实测, P0 链路断裂):
#   上面 FN_RE 的字符类里有 `\s` —— 于是带空格的文件名**在第一个空格处被切断**:
#       D:\tmm-考卷\题目\A1 查询.txt   → 抽出的名字是 `查询.txt`
#       D:\tmm-考卷\题目\B2 门店清单.csv → `门店清单.csv`, 而 B2 被吃成了目录
#   接着拿 `查询.txt` 去桌面找 ⇒ `✗ File not found: 查询.txt`, 用户重发 3 次。
#   (本模块第 41 行的注释一直写着"允许名内空格", 与实现不一致 —— 属于实现漏了意图, 不是设计选择。)
#
# 修法: 保持 FN_RE 不动 (它对"纯名"的匹配是准的), 命中后**向左接回**被空格切开的部分。
#   只接满足全部条件的碎片, 宁可少接也不能把散文吃进文件名 ——
#   语料里真实存在的反例必须保住:
#       "把这首诗写入桌面 大哥.txt"                → 前面是目录词「桌面」  ⇒ 不接
#       "帮我在桌面生成…文件，叫 tmm活路测试.xlsx"   → 前面是「叫」        ⇒ 不接
#       "打开 A1 查询.txt"                        → 接成 `A1 查询.txt`; 再往前是「打开」⇒ 停
_MAX_NAME_TOKENS = 4          # 名字最多几个空格分隔的碎片 (超过就不是文件名了)
_MAX_FRAG_LEN = 8             # 单个碎片的长度上限 (长碎片基本是散文)

#: 这些词出现在空格左边 ⇒ 说明左边是**指令散文**, 不该并进文件名
_LEFT_STOPWORDS = frozenset(
    [w.lower() for w in (DIR_WORDS + STRONG_VERBS + WEAK_PREFIXES)] +
    ["打开", "开启", "开启", "读", "读取", "阅读", "看", "查看", "瞧", "运行", "执行", "跑",
     "发送", "发给", "发", "寄", "给", "文件", "文档", "表格", "内容", "里面", "里", "上", "下",
     "的", "了", "这", "那", "它", "到", "在", "把", "请", "帮", "我", "你", "他", "她", "和",
     "与", "或", "然后", "再", "就", "去", "来", "一下", "其中", "关于", "这个", "那个",
     "路", "该", "此", "以下", "如下", "共", "第"]
)

#: 碎片里出现这些字 ⇒ 是散文不是名字碎片
_PROSE_CHARS = ("的", "了", "是", "把", "和", "与", "在", "就", "都", "也", "很", "请", "让")

#: ★ 2026-09-25 加: 片段里出现**动作字** ⇒ 它是用户的话(动词), 不是名字碎片。
#:   起因 (常驻门禁 verify_honest_failure 抓到的回归): `在 D:\...\tmp 新建
#:   hv_verify.txt 写入 你好` 里, "新建" 被当成名字碎片左接到文件名上 ⇒ 交付给
#:   file_ops 的 path 成了「新建 hv_verify.txt」⇒ 参数校验判"路径后附加描述"
#:   ⇒ 指定目录的写入直接失败。
#:   判据是"宁可少接": 漏接只是退回改前行为(拿 tail), 误接会把一条本来正确的路弄坏。
_VERB_CHARS = ("新", "建", "开", "打", "写", "读", "保", "存", "生", "成", "做", "创",
               "目", "录", "名", "叫", "称", "封", "份", "放", "移", "复", "删", "改")

#: 纯 ASCII 标识符 (A1 / B2 / v2 …) —— 这类碎片一定是名字的一部分
_FRAG_ASCII = re.compile(r"^[A-Za-z0-9_\-\.]+$")


def space_head_ok(head: str) -> bool:
    """空格左边那一截**像不像名字碎片** (两路共用的唯一判据)。

    用途: raw_filename_tokens 的"空格左接"修复会不会把**用户的话**(动词/量词)
    接到文件名上。ASCII 标识符直接放行; 中文片段则要短且不含动作字。
    ★ 规则只留这一份 —— core/intent_router.py 也 import 本函数, 不许各写一套。
    """
    head = (head or "").strip()
    if not head:
        return False
    if _FRAG_ASCII.match(head):
        return True
    if len(head) > 4:
        return False
    return not any(ch in head for ch in _VERB_CHARS)


def strip_verb_tail(s: str) -> str:
    """路径尾部若挂着**动词短语**就砍掉 (两路共用的唯一判据)。

    起因 (常驻门禁 verify_honest_failure 抓到的回归, 真数据同族):
        `在 D:\\...\\tmp 新建 hv_verify.txt 写入 你好`
        "允许名内空格"的绝对路径正则从 `D:\\...\\tmp` 一路吃到 `.txt`, 把**动词
        「新建」**也吞进路径 → 交付给 file_ops 的 path 成了「…tmp 新建
        hv_verify.txt」→ 参数校验判"路径后附加描述", 指定目录的写入失败。

    判据 (按空格切, 看**倒数第二片**的最后一个路径分量):
        · 它像名字碎片 (A1 / B2 / 我的…) ⇒ 空格是**名内空格**, 保留原样
          (`D:\\tmm-考卷\\题目\\A1 查询.txt` 必须保住)
        · 它是动词/量词 (新建 / 打开 …) ⇒ 那是用户的话, 从它处砍断
          (留下 `D:\\...\\tmp` 当目录, 文件名交给候选逻辑给)
    ★ 宁可少接: 砍错只是退回"目录 + 文件名分开给", 不砍错会把一条正确的路弄坏。
    """
    s = s or ""
    if " " not in s:
        return s
    parts = s.split(" ")
    if len(parts) < 2:
        return s
    comp = parts[-2].replace("\\", "/").rsplit("/", 1)[-1]
    if space_head_ok(comp):
        return s
    return " ".join(parts[:-2])


def _extend_left_start(message: str, start: int) -> int:
    """候选名的起点从 `start` 向左接回被空格切开的碎片, 返回**新的起点**。

    判据 (全部满足才接):
        ① 紧邻左边**恰好一个空格** (多个空格 = 明显分隔, 不接)
        ② 空格左边那个碎片长度 1..8, 且只由 字母/数字/下划线/连字符/汉字 组成
        ③ 该碎片不在 _LEFT_STOPWORDS 里, 且不含 _PROSE_CHARS 里的散文字
        ④ 该碎片里没有路径分隔符 / 引号 / 括号
        ⑤ 接完总碎片数 <= _MAX_NAME_TOKENS
    """
    pos = start
    frags = 0
    while frags < _MAX_NAME_TOKENS - 1:
        j = pos
        spaces = 0
        while j > 0 and message[j - 1] == " ":
            j -= 1
            spaces += 1
        if spaces != 1 or j == 0:
            break
        k = j
        while k > 0 and message[k - 1] not in " \t，。、；：,;！!？?\"'（）()[]/\\":
            k -= 1
        frag = message[k:j]
        if not (1 <= len(frag) <= _MAX_FRAG_LEN):
            break
        if frag.lower() in _LEFT_STOPWORDS:
            break
        if any(ch in frag for ch in _PROSE_CHARS):
            break
        # ★ 2026-09-25: 动作字 (新建/打开/写入…) 是**用户的话**, 不是名字碎片。
        #   少了这条, 「新建 hv_verify.txt」整串被当成文件名 (门禁实测抓到的回归)。
        if not space_head_ok(frag):
            break
        if not re.fullmatch(r"[\w\u4e00-\u9fff\-]+", frag):
            break
        pos = k
        frags += 1
    return pos


def raw_filename_tokens(message: str) -> list[str]:
    """FN_RE 的全部命中, 已做「空格左接」修复, **未清洗**。

    ★ 两条路共用这一份 (core/knowledge.py 与 core/intent_router.py) —— 本模块
      存在的理由就是"规则只留一份", 别再各写一套正则。
    """
    out = []
    for m in FN_RE.finditer(message or ""):
        start = _extend_left_start(message, m.start())
        out.append(message[start:m.end()])
    return out


#: 在**第一个**扩展名处切断 (尾部散文很常见: "大哥.txt里面" → "大哥.txt")
_EXT_TAIL = re.compile(r"^(.*?\." + EXTS + r")(?![A-Za-z0-9_])", re.I)


def clean_filename(tok: str) -> str:
    """从候选里切出**用户真正想要的文件名** (剥目录词/强动词/弱动词/量词/分隔符)。

    例:
        "写一首诗保存到桌面大哥.txt" → "大哥.txt"
        "把这首诗写入桌面 大哥.txt里面" → "大哥.txt"
        "桌面/C:\\Users\\x\\Desktop\\doc.txt" → "doc.txt"  (目录词在, 取其后)
        "my_note.md" → "my_note.md"                        (已是纯名, 不动)
    """
    _orig = (tok or "").strip()
    t = _orig
    _peeled = False          # ★ 是否已经剥掉过散文成分 (目录词/强动词/弱动词)
    for w in DIR_WORDS:                      # ① 最后一个目录词之后才是文件名
        i = t.lower().rfind(w.lower())
        if i >= 0:
            t = t[i + len(w):]
            _peeled = True
    t = t.strip().lstrip("/\\").strip()
    for w in STRONG_VERBS:                   # ② 按最后一个强动词切断
        i = t.rfind(w)
        if i > 0:
            t = t[i + len(w):]
            _peeled = True
    t = t.lstrip("/\\").strip()
    # ★★ 2026-09-24 修 (用户实测): 原来③**无条件**剥量词 ⇒ `打开一首诗.txt` 里的
    #   "一首" 被当填充词吃掉, 得到 `诗.txt`, 然后去桌面找 ⇒ File not found。
    #   实测: 打开一首诗.txt→诗.txt · 打开一个文件.txt→文件.txt (而 两/三 不在词表 ⇒ 正常)。
    #   修法: **量词**只有在"已经在剥散文了"(_peeled)时才算填充词; 纯文件名形态不动
    #   (量词开头的文件名是合法的, 如"一首诗.txt""一个文件.txt")。
    #   剥掉过任何动词 → _peeled 置真, 后续量词照旧可剥 (保住 "写一个报告.txt" 这类)。
    _QUANT = ("一个", "一首", "一篇", "一份", "一段", "个", "条", "句", "张", "份")
    changed = True
    while changed and t:                     # ③ 循环剥弱动词/量词/助词
        changed = False
        for w in WEAK_PREFIXES:
            if t.lower().startswith(w.lower()) and len(t) > len(w):
                if w in _QUANT and not _peeled:
                    continue                 # 纯文件名形态 → 不剥量词
                t = t[len(w):].lstrip("/\\").strip()
                if w not in _QUANT:
                    _peeled = True           # 剥过动词 → 之后量词可剥
                changed = True
                break
        if t[:1] in ("的", "了", "这", "那", "它"):
            t = t[1:].lstrip("/\\").strip()
            changed = True
    # ④ 尾部散文截断: 在**第一个**扩展名处切断。
    #    实测 (ad-hoc 验证抓出): "把这首诗写入桌面 大哥.txt里面" 会得到 "大哥.txt里面" ——
    #    用户真实说法常在文件名后跟"里面/的内容/那份"这类散文。
    _m = _EXT_TAIL.match(t)
    if _m:
        t = _m.group(1)
    # ⑤ 防剥过头 (ad-hoc 验证抓出): 名子的"主干"空了 (如 "桌面.txt" 被目录词剥成 ".txt")
    #    ⇒ 这名字本身就叫"桌面", 不是目录指示 ⇒ 还原原值。
    _stem = t.rsplit(".", 1)[0] if "." in t else ""
    if not _m or not _stem:
        return _orig
    return t.strip()


def filename_candidates(message: str) -> list[str]:
    """从一句话里抽**所有**像文件名的候选 (已清洗), 短的优先 (更像纯文件名)。"""
    out = []
    for tok in raw_filename_tokens(message or ""):
        c = clean_filename(tok)
        if c and "." in c and len(c) <= 45 and not re.search(r"[的了呢吗啊呀吧嘛]", c):
            out.append(c)
    out.sort(key=len)
    return out
