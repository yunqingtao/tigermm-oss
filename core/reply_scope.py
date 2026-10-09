# -*- coding: utf-8 -*-
r"""回话归属 —— 这一轮用户的话, **本来就该谁答** (2026-09-25 立)

为什么要这个 (用户派的活: P0 主指标)
═══════════════════════════════════════════════════════════════════
P0 指标是「兜底率 = 掉 model.fallback/general 的轮次 ÷ 总轮次」, 目标 < 40%。
但**兜底 ≠ 没接住**, 它把两种完全不同的东西算成了一个数:

  · 该有专属路由却没接上   ← 这是**真缺口** (文件/任务状态/时间/仓库…)
  · 本来就该由模型答       ← 闲聊 / 创作 / 纯推理 / 知识问答, 走模型是**正确行为**

第一版口径把后者也记成"系统没接住", 于是指标永远压不到 40%, 而**真实的缺口反而被稀释**。
本模块只做一件事: 把每一轮的话按「该谁答」分桶, 让指标能分开报, 且**真缺口不被掩盖**。

判据顺序 (关键: **先问路由**, 再谈归属)
───────────────────────────────────────────────────────────────
  ① `intent_router.classify()` 有 actions ⇒ **routable** (有专属路由的活, 不该算模型该答);
  ② 否则看是不是**明显该模型答**的形态 ⇒ chat(闲聊) / creative(创作) / reason(纯推理) / qa(知识问答);
  ③ 都不是 ⇒ **unknown**(说不清: 既没路由, 也不像"本该模型答")。

★ 保守原则 (写死在代码里, 也是门禁的判据):
   只给**能拿出证据**的形态定桶 —— 一句话只要**可能**是能接的活, 就不许塞进 chat/creative/…
   宁可归 "unknown" (让它在指标里显示为待接), 也不许拿它把指标做好看。
   ⇒ 所以本模块**只减不增**准确率的风险: 它永远不敢把真缺口说成"模型该答"。
"""
from __future__ import annotations

import re

#: 模型该答的六类 (每一类都必须能指着原话说"这本来就是模型的事")
#: ★ discuss / fragment 是 2026-09-25 第二次分桶时补的 —— 第一版只有四类, 结果 121 轮里
#:   55% 落进"说不清", 而那堆里有一大半是**讨论**("赛道可以做个数字人播客"
#:   "我想让看看他这个公众号怎么做到")和**碎片追问**("其他图片呢""不要覆盖我以前的")。
#:   这两类由模型答是**对的** (没有工具能替用户拿主意), 硬说成"系统没接住"同样失真。
BUCKETS = ("chat", "creative", "reason", "qa", "discuss", "fragment")

# ① 闲聊 / 寒暄 / 称呼 / 情绪 / 纯应答
#   ★ 只认**短且没有动作宾语**的说法: "你好"/"聊聊"/"好的"。
#   "帮我看看这个" 这种有宾语的一律不进来 (可能是个活)。
_CHAT_EXACT = {
    "你好", "您好", "hi", "hello", "哈喽", "在吗", "在么", "在不在", "早上好", "中午好",
    "晚上好", "晚安", "再见", "拜拜", "谢谢", "多谢", "辛苦了", "好的", "好", "行", "可以",
    "嗯", "哦", "明白了", "懂了", "收到", "聊聊", "聊天", "在干嘛", "干嘛呢", "你在干嘛",
    "虎哥", "哥", "你说呢", "笑死", "哈哈", "太好了", "不错", "牛逼",
}
_CHAT_RX = re.compile(
    r"^(我(最近|今天)?(遇上|遇到|碰到)了?[一二三两几]?(个|件|些).{0,8}|和(小说|工作|钱)?.{0,4}无关"
    r"|我(琢磨|觉得|感觉|想).{0,12}(挣钱|赚钱|怎么办)|我有个(东西|事|想法))$")

# ② 创作 / 写作: 动词 + 产物 (要说得出"写/编/画/取个名" + 产物)
#   ★ 2026-09-25 修 (门禁当场抓到两个漏): 第一版要求产物名词**紧挨**在量词后面,
#     于是"给我写个姐妹两教英语的剧本"(中间夹了修饰语)漏判成 unknown;
#     "写一首关于青山绿水的七言绝句"同理。⇒ 允许中间夹 <=10 个字的修饰。
_CREATIVE_RX = re.compile(
    r"(写|编|作|填|拟|起|取|想)[一了个篇首段条副]?[^，。；！？]{0,10}"
    r"(诗|词|歌|歌词|文案|故事|小说|剧本|段子|相声|脱口秀|口号|标题|名字|梗|对联|绝句|律诗)"
    r"|(取|起)个?名|帮(我)?(写|编)一(首|段|篇)|给我写一")

# ③ 纯推理 / 算: 数学式 / 逻辑题 / 证明 / 谜题
_REASON_RX = re.compile(
    r"^\s*[0-9]{1,6}\s*[+\-*/x×÷]\s*[0-9]{1,6}\s*(=|等于)?.{0,6}$"
    r"|(证明|推导|推理|逻辑题|脑筋急转弯|谜题|猜谜|解方程|哥德巴赫|费马)"
    r"|(算|计算|求)一?下.{0,10}(等于|多少|是几)"
    r"|各多少字|多少个字|一共有多少(个)?字")

# ④ 知识问答: 问原理/区别/做法 (★ 排除"为什么失败"这类**问系统自己**的)
_QA_RX = re.compile(
    r"(是什么|什么是|为什么|怎么办|如何|区别|原理|意思|定义|解释|介绍一下)"
    r"|(怎么|怎样)(做|才能|才)(成|到|好)")
_QA_NOT = re.compile(r"(失败|报错|出错|没反应|不灵|卡住|不能|打不开|连不上|没成功)")

# ⑤ 讨论 / 征求意见: 陈述一个想法、聊赛道、问"你觉得" —— **没有动作宾语**的活儿
#   ★ 两条都要满足才不会误伤真活: ① 有讨论词或长句陈述; ② **没有**工具能办的宾语
#     (路径/文件/时间/仓库/邮件/坐标…)。少一条都退化成 unknown。
_DISCUSS_RX = re.compile(
    r"(我在思考|我在琢磨|我琢磨|我想做|我想让|我想(要)?做|我觉得|你认为|你觉得|你看呢"
    r"|赛道|方向|规划|思路|方案|应该怎么|值得(吗|么)|好不好|行不行|靠谱吗|建议"
    r"|聊聊|谈谈|出个主意|给点意见)")
#: 工具能办的宾语 (出现这些 ⇒ 不许当"讨论", 它是个活)
_TOOLISH_RX = re.compile(
    r"[A-Za-z]:\\|/[a-z]+/|桌面|下载|文件夹|目录|文件|文档|表格|日志|提交|仓库|分支"
    r"|今天|明天|昨天|几号|几点|坐标|地图|路线|天气|邮件|收件箱|内存|磁盘|下载|链接|网址")

# ⑥ 碎片追问 / 承接上文的短话: 极短且没有工具线索, 或含指代但没有动作
#   ("其他图片呢" / "不要覆盖我以前的" / "这个呢") —— 由模型结合上文答是**对的**。
_FRAGMENT_RX = re.compile(r"^(那个|这个|它|他|她|其他|其余|剩下|别的|还有).{0,10}(呢|吗|了)?$|^再.{0,6}$")


def _router():
    """懒加载路由 (避免模块循环 import; 拿不到就当"判不了", 退化成 unknown)。"""
    try:
        from core.intent_router import get_router
        return get_router()
    except Exception:                                       # noqa: BLE001
        return None


def scope(text: str) -> dict:
    """这一轮的话该谁答。返回 {"bucket": "routable|chat|creative|reason|qa|unknown", "why": str}。"""
    t = (text or "").strip()
    if not t:
        return {"bucket": "unknown", "why": "空话"}

    # ① 先问路由: 有专属路由 ⇒ 这是能接的活 (绝不算"模型该答")
    rt = _router()
    cl = None
    if rt is not None:
        try:
            cl = rt.classify(t)
            acts = [a for a in (cl.get("actions") or []) if a]
        except Exception:                                   # noqa: BLE001
            acts = []
        if acts:
            return {"bucket": "routable", "why": "路由能接: %s" % ",".join(acts[:3])}

    low = t.lower()
    # ★★ 2026-09-25 修 (门禁当场抓到): "有工具能办的宾语"> **一切模型类桶**。
    #   第一版只给 discuss/fragment 加了这道闸, 于是"帮我写个剧本保存到桌面"(创作外形
    #   + 落盘目标 = 真活)被判成 creative; "这个文件里的东西是什么意思"(读文件)被判成 qa。
    #   ⇒ 提到最前面统一判一次: 带路径/时间/仓库/邮件… 一律归 unknown(待接), 不许算模型该答。
    toolish = bool(_TOOLISH_RX.search(t))

    # ② 模型该答的四类 (只在**没路由**时才看)
    if low in _CHAT_EXACT or (len(t) <= 12 and _CHAT_RX.search(t)):
        return {"bucket": "chat", "why": "闲聊/寒暄/纯应答"}
    if not toolish:
        if _CREATIVE_RX.search(t):
            return {"bucket": "creative", "why": "创作: 要写/编一份东西 (无落盘/文件宾语)"}
        if _REASON_RX.search(t):
            return {"bucket": "reason", "why": "纯推理/算"}
        if _QA_RX.search(t) and not _QA_NOT.search(t):
            return {"bucket": "qa", "why": "知识问答 (问原理/做法)"}
        # ⑤ 讨论/征求意见 (没有工具能办的宾语)
        if _DISCUSS_RX.search(t):
            return {"bucket": "discuss", "why": "讨论/征求意见 (没有工具能办的宾语)"}
        # ⑥ 碎片追问: 短 + 指代
        if _FRAGMENT_RX.search(t) and len(t) <= 14:
            return {"bucket": "fragment", "why": "碎片追问 (承接上文, 无工具线索)"}

    # ③ 说不清 —— 既没路由, 也不像"本该模型答"。**不许**硬塞进上面任何一桶。
    return {"bucket": "unknown", "why": "既没路由, 也不像本来就该模型答"}


def summarize(items) -> dict:
    """给一批原话算分布。items: 可迭代的原话。"""
    from collections import Counter
    cnt = Counter()
    samples: dict = {}
    for t in items:
        s = scope(t)
        cnt[s["bucket"]] += 1
        samples.setdefault(s["bucket"], []).append(t)
    total = sum(cnt.values()) or 1
    return {
        "total": sum(cnt.values()),
        "counts": dict(cnt),
        "pct": {k: round(100.0 * v / total, 1) for k, v in cnt.items()},
        "samples": {k: v[:8] for k, v in samples.items()},
        "model_territory": sum(cnt.get(b, 0) for b in BUCKETS),
        "unknown": cnt.get("unknown", 0),
        "routable": cnt.get("routable", 0),
    }
