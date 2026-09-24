r"""真实语料脱敏 —— 把**个人标识**换成占位符, 保留说法形态。

为什么需要 (2026-09-22 实测抓到的真泄露):
  用"用户真实原话"当门禁语料是对的方向 (自己编的句子测不出用户的说法),
  但真实原话里**夹着个人标识** —— 实测抓到的:
      "PS C:\\Users\\<真实用户名>\\Desktop> python run.py"     (用户粘的命令提示符)
      "D:\\<工程根>\\MuseTalk\\results\\... 图片去这里找"      (用户的工程目录)
  `nl_real_utterances.json` 是随分发包出门的 ⇒ 这些标识会**公开泄露**。
  (2026-09-22: 已随一次 commit 推上公开站, 属既成事实, 见证据档)

原则
────────────────────────────────────────────────────────────────
  · 只换**标识**, 不动**说法**: "把 X 存到 D:\<work>\out.txt" 仍是同一种说法形态,
    对"路由/路径解析"这类被测行为等价。
  · 占位符写成**看得出来是占位符**的样子 (`<user>` / `<work>`), 便于人眼复查。
  · 同时提供 `LEAK_RE`: 让门禁能**自查**(语料里若又混进真实家目录 → 判红)。
    这条很重要: 光靠"记得脱敏"是纪律, 加正则自查才是护栏。
"""
from __future__ import annotations

import re

#: 家目录: <盘符>:\Users\<名字> —— 名字位置允许 1..N 个字符
_HOME_RE = re.compile(r"([A-Za-z]):[\\/]+Users[\\/]+([A-Za-z0-9_][A-Za-z0-9_.-]*)", re.I)
#: 已知的工程根名 (脱敏时替换; 不写进代码里, 由调用方给)
#: ★ 用**函数**替换而不是 r"\1:..." —— 后者里的 `\U` 会被 re 当成转义而报 bad escape
def _ph(m) -> str:
    return f"{m.group(1)}:\\Users\\<user>"

#: ★ 门禁自查用: 语料里**不该出现**的形态 = 家目录后面跟着**不是占位符**的名字。
#:   (占位符 `<user>` 里有 `<`, 所以下面用负向断言排除掉)
HOME_LEAK_RE = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!<user>)[A-Za-z0-9_][A-Za-z0-9_.-]{1,}", re.I)


def sanitize(text: str, extra_literals: dict[str, str] | None = None) -> str:
    """把个人标识换成占位符。

    extra_literals: {"要替换的真实片段": "占位符"} —— 由调用方提供 (真实工程名/用户名等
    不写进本文件, 否则本文件自己就泄露了)。
    多次替换: 先换 extra_literals, 再兜底换家目录形态。
    """
    if not text:
        return text
    out = text
    for real, ph in (extra_literals or {}).items():
        if real:
            out = out.replace(real, ph)
    out = _HOME_RE.sub(_ph, out)
    return out


def has_leak(text: str) -> bool:
    """还有没有"家目录 + 具体名字"这种形态 (占位符不算)。"""
    return bool(HOME_LEAK_RE.search(text or ""))
