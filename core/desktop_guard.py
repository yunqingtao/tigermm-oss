# -*- coding: utf-8 -*-
r"""桌面动作安全闸 —— 策划红线第 2 条。

    「桌面自动化必须有安全白名单 —— 涉及钱/删除/发送的动作, 一律停下等确认。」
                                docs/策划_能力提升_v1.md 第四节

为什么必须有 (P3 的**前置**, 不是附加项)
═══════════════════════════════════════════════════════════════════
`tools/windows_desktop.py` 能截图 / 点任意坐标或元素 / 打字 / 按键, 而且**已经**
在模型的工具表里 (改前就在)。它身上没有任何"哪些窗口不能碰"的概念 ——
即: 模型被诱导一句, 就能在用户的网银页面点一个按钮、在回收站里按 Del、
在一个"确认转账"?上回车。桌面自动化里**最贵的失败就是误操作**,
而这类失败是**不可撤销**的 (钱走了、文件删了、消息发出去了)。

这一层只做判断, 不碰桌面:
    evaluate(action, kwargs) -> {"decision": "allow"|"confirm"|"deny",
                                 "allowed": bool, "reason": str, "token": str}
规则 (可被 data/desktop_policy.json 覆盖):
    · 只读动作 (capture/screenshot/list_windows/...)            → allow
    · 改动动作 (click/type/key/hotkey/drag/launch):
        - 目标是"钱/删除/发送/凭据"类窗口 (网银·支付·钱包·订单·密码…)
          ⇒ confirm (要人签字才动)
        - 按键/文本里带不可逆语义 (Ctrl+Shift+Del · 格式化 · 转账 · 卸载…)
          ⇒ confirm
        - 目标窗口在策略的 allow_windows 里 ⇒ allow
        - 其余 ⇒ allow (正常办公窗口不该被拦住, 否则工具就废了)
    · 明令禁止的语义 (格式化磁盘 / 关机 / 清空回收站 / 支付确认)
      ⇒ **deny** (连确认都不给, 直接拒绝并说明)

确认怎么走 (不做"弹窗确认", 走**令牌**):
    第一次调用 → 返回 decision=confirm + needs_confirm=True + token;
    调用方(人)看过后, 用同一个 action 参数 + confirm_token=<token> 再调一次才真执行。
    token 与 (action + 关键参数 + 目标窗口) 绑定 ⇒ 换了参数旧 token 失效,
    避免"确认了一次, 之后一路放行"。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "desktop_policy.json"

#: 只读动作 —— 看看不动手, 永远放行
READONLY_ACTIONS = {
    "capture", "screenshot", "screenshot_now", "list_windows", "find_window",
    "scroll", "move", "show", "info", "elements", "get_state", "截当前屏幕",
}

#: 会改动外部状态的动作 —— 过策略
MUTATING_ACTIONS = {
    "click", "type", "type_text", "key", "press_key", "hotkey", "drag",
    "launch", "launch_app", "open_browser", "double_click", "right_click", "set_value",
}

#: 语义上明令禁止 —— 连确认都不给。宁可不动, 不赌。
DENY_PATTERNS = [
    (r"格式化|format\s*[/\\]?\s*[a-z]:|diskpart|clean\s+all", "磁盘格式化/清空"),
    (r"关机|shutdown\s*/s|shutdown\s*-s", "关机"),
    (r"重启|shutdown\s*/r|reboot", "重启系统"),
    (r"清空回收站|empty\s+recycle|Clear-RecycleBin", "清空回收站"),
    (r"卸载|uninstall", "卸载软件"),
]

#: 需要人签字的窗口特征 (钱 / 删除 / 发送 / 凭据)
SENSITIVE_WINDOW_PATTERNS = [
    (r"支付|付款|收银|结算|买单|扫码付|pay|checkout|billing", "涉及付款"),
    (r"银行|网银|转账|汇款|钱包|余额|理财|证券|基金|股票|交易", "涉及资金"),
    (r"订单|下单|购买|购物车|确认订单|充值", "涉及下单/充值"),
    (r"密码|凭据|密钥|password|credential|vault|1password|bitwarden|keepass", "涉及凭据"),
    (r"删除|移除|清空|回收站|格式化|卸载|delete|remove|trash|recycle", "涉及删除"),
    (r"发送|发送给|提交|发布|上传|send|submit|publish|upload", "涉及发送/提交"),
    (r"授权|同意|签署|合同|签约|授权书|agree|consent|sign", "涉及授权/签署"),
]

#: 危险按键组合 / 文本语义
SENSITIVE_KEY_PATTERNS = [
    (r"(?i)ctrl\s*\+\s*shift\s*\+(del|delete)", "Ctrl+Shift+Del (删除不可撤销)"),
    (r"(?i)shift\s*\+\s*del", "Shift+Del (绕过回收站)"),
    (r"(?i)ctrl\s*\+\s*(a)\s*$", "Ctrl+A 全选 (紧接着常跟删除)"),
    (r"(?i)alt\s*\+\s*f4", "Alt+F4 (会关闭正在编辑的东西)"),
    (r"(?i)win\s*\+\s*(l|x)", "Win+L/X (锁屏/关机菜单)"),
]

#: 内置默认策略 (data/desktop_policy.json 缺省时用它, 有则合并覆盖)
DEFAULT_POLICY = {
    "allow_windows": [],
    "confirm_windows": [],
    "deny_windows": [],
    "allow_actions": sorted(READONLY_ACTIONS),
    "never_confirm_actions": [],
    "note": "confirm_windows / allow_windows 里写窗口标题的子串; deny_windows 一票否决",
}


def load_policy() -> dict:
    pol = dict(DEFAULT_POLICY)
    try:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, list):
                    pol[k] = list(v)
                else:
                    pol[k] = v
    except FileNotFoundError:
        pass
    except Exception:
        # 策略文件坏了 ⇒ 用**更严**的默认, 绝不因为读不到就放行
        pol["note"] = "策略文件读取失败, 已退回严格默认"
    return pol


def active_window_title() -> str:
    """取当前前台窗口标题 (判断"这一下会落在谁身上")。取不到就返回空串。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return ""
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        u.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value or ""
    except Exception:
        return ""


def _norm_keys(kwargs: dict) -> str:
    keys = kwargs.get("keys") or kwargs.get("key") or kwargs.get("hotkey") or ""
    if isinstance(keys, (list, tuple)):
        keys = "+".join(str(k) for k in keys)
    return str(keys)


def _token(action: str, kwargs: dict, window: str) -> str:
    """确认令牌 —— 与 (动作 + 关键参数 + 目标窗口) 绑定。"""
    parts = [action, window, _norm_keys(kwargs),
             str(kwargs.get("text") or ""), str(kwargs.get("element") or ""),
             str(kwargs.get("target") or "")]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def evaluate(action: str, kwargs: dict | None = None,
             window_title: str | None = None) -> dict:
    """判断这个桌面动作能不能做。**只判断, 不执行。**

    返回: {"decision": "allow"/"confirm"/"deny",
           "allowed": bool,             # confirm 时为 False (要先签字)
           "needs_confirm": bool,
           "token": str,                # confirm 时给出, 复调用时带上
           "reason": str}
    """
    kwargs = kwargs or {}
    act = (action or "").strip().lower()
    pol = load_policy()
    win = window_title if window_title is not None else ""

    # ① 明令禁止的语义 (优先级最高, 覆盖一切 allow)
    hay = " ".join([act, _norm_keys(kwargs), str(kwargs.get("text") or ""),
                    str(kwargs.get("target") or "")])
    for pat, why in DENY_PATTERNS:
        if re.search(pat, hay, re.I):
            return {"decision": "deny", "allowed": False, "needs_confirm": False,
                    "token": "", "reason": "拒绝执行: 命中禁止动作 —— %s" % why}

    # ② 只读动作直接放行
    if act in (pol.get("allow_actions") or []) or act in READONLY_ACTIONS:
        if act not in MUTATING_ACTIONS:
            return {"decision": "allow", "allowed": True, "needs_confirm": False,
                    "token": "", "reason": "只读动作"}

    # ③ 政策里一票否决的窗口
    for w in (pol.get("deny_windows") or []):
        if w and w in win:
            return {"decision": "deny", "allowed": False, "needs_confirm": False,
                    "token": "", "reason": "拒绝执行: 窗口 %r 在 deny_windows 里" % win}

    # ④ 危险按键 —— ★ 刻意放在「策略免确认 / 白名单」**之前**。
    #   白名单的含义是"这个窗口里的常规点击不用问", 不是"这里的全局危险热键随便按"。
    #   (P3 门禁第一次跑就抓到这个洞: 把 Visual Studio Code 加入 allow_windows 后,
    #    Ctrl+Shift+Del 一路放行。)
    k = _norm_keys(kwargs)
    if k:
        for pat, why in SENSITIVE_KEY_PATTERNS:
            if re.search(pat, k):
                return {"decision": "confirm", "allowed": False, "needs_confirm": True,
                        "token": _token(act, kwargs, win),
                        "reason": "按键 %r 属于不可逆操作 (%s), 需要确认" % (k, why)}

    # ⑤ 政策里明确免确认的
    if act in (pol.get("never_confirm_actions") or []):
        return {"decision": "allow", "allowed": True, "needs_confirm": False,
                "token": "", "reason": "动作在 never_confirm_actions 里"}
    for w in (pol.get("allow_windows") or []):
        if w and w in win:
            return {"decision": "allow", "allowed": True, "needs_confirm": False,
                    "token": "", "reason": "窗口 %r 在 allow_windows 里" % win}

    # ⑥ 敏感窗口 (钱/删除/发送/凭据) —— 要人签字
    #   ★ 必须 re.I: "1Password" 里是 "Password", 大小写敏感就漏了 (门禁实测抓到的洞)。
    reasons = [why for pat, why in SENSITIVE_WINDOW_PATTERNS if win and re.search(pat, win, re.I)]
    for w in (pol.get("confirm_windows") or []):
        if w and w in win:
            reasons.append("策略要求确认 (%s)" % w)
    if reasons and act in MUTATING_ACTIONS:
        return {"decision": "confirm", "allowed": False, "needs_confirm": True,
                "token": _token(act, kwargs, win),
                "reason": "目标窗口 %r %s ⇒ 需要确认后才动" % (win, "、".join(sorted(set(reasons))))}

    # ⑦ 其余正常窗口: 放行 (普通办公窗口不该被拦, 否则工具就废了)
    return {"decision": "allow", "allowed": True, "needs_confirm": False,
            "token": "", "reason": "常规窗口/动作"}


def verify_token(action: str, kwargs: dict | None = None,
                 window_title: str | None = None) -> bool:
    """复调用时校验 confirm_token 是否对得上当前这组参数。"""
    kwargs = kwargs or {}
    given = str(kwargs.get("confirm_token") or kwargs.get("_confirm_token") or "").strip()
    if not given:
        return False
    return given == _token((action or "").strip().lower(), kwargs,
                           window_title if window_title is not None else "")
