"""
Win32 Input Layer — low-level SendInput for background mouse/keyboard injection.
Uses ctypes + user32.dll. No focus steal. No real cursor movement.
"""
import ctypes
from ctypes import wintypes, Structure, Union, POINTER, byref, sizeof
import time, logging

logger = logging.getLogger("tools.win32_input")

INPUT_MOUSE = 0; INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001; MOUSEEVENTF_LEFTDOWN = 0x0002; MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008; MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800; MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
user32 = ctypes.windll.user32

VK_MAP = {
    'enter':0x0D,'return':0x0D,'tab':0x09,'escape':0x1B,'esc':0x1B,
    'backspace':0x08,'delete':0x2E,'space':0x20,' ':0x20,
    'left':0x25,'up':0x26,'right':0x27,'down':0x28,
    'ctrl':0x11,'alt':0x12,'shift':0x10,'win':0x5B,
    'a':0x41,'c':0x43,'v':0x56,'x':0x58,'z':0x5A,
}

class MOUSEINPUT(Structure):
    _fields_ = [("dx",wintypes.LONG),("dy",wintypes.LONG),("mouseData",wintypes.DWORD),
                ("dwFlags",wintypes.DWORD),("time",wintypes.DWORD),("dwExtraInfo",POINTER(wintypes.ULONG))]

class KEYBDINPUT(Structure):
    _fields_ = [("wVk",wintypes.WORD),("wScan",wintypes.WORD),("dwFlags",wintypes.DWORD),
                ("time",wintypes.DWORD),("dwExtraInfo",POINTER(wintypes.ULONG))]

class _U(Union):
    _fields_ = [("mi",MOUSEINPUT),("ki",KEYBDINPUT)]

class INPUT(Structure):
    _fields_ = [("type",wintypes.DWORD),("union",_U)]

def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), byref(arr), sizeof(INPUT))

def _abs(x, y):
    w = user32.GetSystemMetrics(0); h = user32.GetSystemMetrics(1)
    return int(x * 65535 / max(w,1)), int(y * 65535 / max(h,1))

def click(x, y, button="left"):
    ax, ay = _abs(x, y)
    flags = {"left":(MOUSEEVENTF_LEFTDOWN,MOUSEEVENTF_LEFTUP),
             "right":(MOUSEEVENTF_RIGHTDOWN,MOUSEEVENTF_RIGHTUP)}.get(button)
    if not flags: return False
    down_f, up_f = flags
    mv = INPUT(); mv.type=INPUT_MOUSE; mv.union.mi.dx=ax; mv.union.mi.dy=ay; mv.union.mi.dwFlags=MOUSEEVENTF_MOVE|MOUSEEVENTF_ABSOLUTE
    dn = INPUT(); dn.type=INPUT_MOUSE; dn.union.mi.dx=ax; dn.union.mi.dy=ay; dn.union.mi.dwFlags=down_f|MOUSEEVENTF_ABSOLUTE
    up = INPUT(); up.type=INPUT_MOUSE; up.union.mi.dx=ax; up.union.mi.dy=ay; up.union.mi.dwFlags=up_f|MOUSEEVENTF_ABSOLUTE
    _send(mv, dn, up)
    return True

def type_text(text, delay=0.01):
    for ch in text:
        if ch in ('\n','\r'): _key(0x0D); time.sleep(delay); continue
        if ch == '\t': _key(0x09); time.sleep(delay); continue
        vk = user32.VkKeyScanW(ord(ch))
        if vk == -1: continue
        code = vk & 0xFF; shift = (vk >> 8) & 1
        if shift: _key(0x10)
        _key(code)
        if shift: _key(0x10, False)
        time.sleep(delay)
    return True

def _key(vk, down=True):
    i = INPUT(); i.type=INPUT_KEYBOARD; i.union.ki.wVk=vk
    if not down: i.union.ki.dwFlags=KEYEVENTF_KEYUP
    _send(i)

def hotkey(*keys):
    vks = []
    for k in keys:
        kl = k.lower().strip()
        if kl in VK_MAP: vks.append(VK_MAP[kl])
        elif len(kl)==1:
            v = user32.VkKeyScanW(ord(kl)) & 0xFF
            if v: vks.append(v)
    if not vks: return False
    for vk in vks:
        i=INPUT(); i.type=INPUT_KEYBOARD; i.union.ki.wVk=vk; _send(i); time.sleep(0.02)
    for vk in reversed(vks):
        i=INPUT(); i.type=INPUT_KEYBOARD; i.union.ki.wVk=vk; i.union.ki.dwFlags=KEYEVENTF_KEYUP; _send(i); time.sleep(0.02)
    return True

def scroll(amount, x=0, y=0):
    ax, ay = _abs(x, y) if (x or y) else (0, 0)
    i = INPUT(); i.type=INPUT_MOUSE; i.union.mi.dx=ax; i.union.mi.dy=ay
    i.union.mi.mouseData = amount * 120
    i.union.mi.dwFlags = MOUSEEVENTF_WHEEL
    if x or y: i.union.mi.dwFlags |= MOUSEEVENTF_ABSOLUTE
    _send(i); return True

def click_center(rect, button="left"):
    if not rect or len(rect)<4: return False
    return click((rect[0]+rect[2])//2, (rect[1]+rect[3])//2, button)

# ── 统一入口 (网关约定: run/execute + action 派发) ──────────────────────────
# ★ 2026-09-19 深测修 (真缺陷): 本模块原来只有 click/type_text/hotkey/scroll 平函数,
#   **没有 run/execute** → 网关认不出入口 → Agent 完全调不到 (深测实测)。
#   ⚠ 这些动作会**真的操作鼠标键盘** —— 只在本机、且被显式要求时使用。
_ACTIONS = {
    "click": lambda **kw: click(int(kw.get("x", 0)), int(kw.get("y", 0)),
                                kw.get("button", "left") or "left"),
    "click_center": lambda **kw: click_center(kw.get("rect") or kw.get("bbox") or [],
                                              kw.get("button", "left") or "left"),
    "type_text": lambda **kw: type_text(str(kw.get("text", "")),
                                        float(kw.get("delay", 0.01) or 0.01)),
    "hotkey": lambda **kw: hotkey(*(kw.get("keys") or [kw.get("key", "")])),
    "scroll": lambda **kw: scroll(int(kw.get("amount", 0)), int(kw.get("x", 0)), int(kw.get("y", 0))),
}

ACTIONS = tuple(_ACTIONS)


def _ok(v):
    """把平函数的返回值包成网关契约 (success/output)。"""
    return {"success": True, "output": "" if v is None else str(v), "result": v}


async def run(**kwargs) -> dict:
    """统一入口。action 决定走哪个分支 (与 tiger_office 同约定)。"""
    action = str(kwargs.get("action") or "").strip()
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"success": False, "output": "",
                "error": f"Unknown action: {action or '(空)'}; 可用: {', '.join(ACTIONS)}"}
    try:
        return _ok(fn(**kwargs))
    except Exception as e:
        return {"success": False, "output": "", "error": f"{type(e).__name__}: {e}"}
