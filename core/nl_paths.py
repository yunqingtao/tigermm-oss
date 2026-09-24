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

#: 文件名候选 (允许中文名/名内空格; 排除标点与括号)
#: ★ 右边界用 (?![A-Za-z0-9_]) 而不是 (?!\w) —— `\w` 在 Unicode 下**含中文**,
#:   否则 "写入F:\data.txt文件" 这类"扩展名后紧跟中文"的说法一个都匹配不到。
FN_RE = re.compile(r"([^\s，。、；：,;！!？?\"'（）()\[\]]{1,60}?\." + EXTS + r")(?![A-Za-z0-9_])", re.I)


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
    for m in FN_RE.finditer(message or ""):
        c = clean_filename(m.group(1))
        if c and "." in c and len(c) <= 45 and not re.search(r"[的了呢吗啊呀吧嘛]", c):
            out.append(c)
    out.sort(key=len)
    return out
