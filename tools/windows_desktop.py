"""
TMM Windows Desktop Control v3.0 — Pure Python UIA Automation

Design borrowed from:
  - cua-driver (NousResearch): snapshot-index, 3-tier click, CacheRequest
  - Python-UIAutomation-for-Windows (yinkaisheng): @property lazy, pattern matching

Zero new dependencies. Uses comtypes + ctypes + PIL (all already in TMM).
"""

def _home_dir() -> str:
    """取用户主目录 —— **永不让调用方崩** (2026-09-20 分发整改)。

    踩过: 去个人化时把写死的桌面路径换成"用 or 把 USERPROFILE 与 Path.home() 串起来"的写法,
    看着像兜底, 其实不兜底 —— 实测把 USERPROFILE/HOMEDRIVE/HOMEPATH 都清掉后,
    `Path.home()` 直接抛 `RuntimeError: Could not determine home directory.`。
    改成逐级尝试, 最后落到临时目录 (目录不对也得比崩了好)。
    (别在文档里写真实用户名示例 —— 分发门禁会当硬编码个人路径报红)
    """
    for _k in ("USERPROFILE", "HOME"):
        _v = os.environ.get(_k)
        if _v:
            return _v
    _hd, _hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if _hd and _hp:
        return _hd + _hp
    try:
        return str(Path.home())
    except Exception:
        import tempfile
        return tempfile.gettempdir()


def _desktop_dir() -> str:
    return os.path.join(_home_dir(), "Desktop")


# PluginManager TOOL metadata (required for schema export)
TOOL = {
    "name": "windows_desktop",
    "description": "Windows桌面操控。截图保存: action=screenshot_now path=文件名。打开网站: action=open_browser 启动Chrome/Edge访问网页。先显示桌面再截图: action=desktop_screenshot。捕获窗口UI元素(非截图): action=capture。打开文件夹: action=open_folder。等待: action=wait seconds=秒数。列出窗口: action=list_windows。",
    "params": [
        {"name": "action", "type": "string", "required": True,
         "enum": ["capture", "screenshot", "click", "type", "type_text", "list_windows", "find_window", "scroll", "press_key", "key", "launch_app", "open_browser", "desktop_screenshot", "wait", "fullscreen", "save_screenshot", "截图保存", "open_folder", "screenshot_now", "截当前屏幕", "打开文件夹"],
         "description": "screenshot_now=截当前屏幕存PNG, desktop_screenshot=Win+D后截, capture=获取窗口UI元素(非截图)"},
        {"name": "target", "type": "string", "required": False,
         "description": "目标窗口标题(如'微信')或元素索引或输入文本"},
        {"name": "element", "type": "integer", "required": False,
         "description": "元素索引(从capture结果获取)"},
        {"name": "max_elements", "type": "integer", "required": False, "default": 200,
         "description": "最大元素数量"},
        {"name": "direction", "type": "string", "required": False, "enum": ["up", "down", "left", "right"],
         "description": "滚动方向"},
        {"name": "amount", "type": "integer", "required": False, "default": 3,
         "description": "滚动量"},
        {"name": "key", "type": "string", "required": False,
         "description": "按键名: return, tab, escape, space, up, down, left, right"},
    ]
}

import ctypes, os, time, uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path          # ← 2026-09-20 补: 去个人化后用到了 Path.home() 兜底
from typing import Optional, List, Dict, Any, Tuple

import comtypes.client


def _import_uia():
    """拿 comtypes 的 UIAutomationClient 类型库 —— 丢了就**现场再生**。

    ★ 2026-09-20 加 (自愈): gen 缓存会因 Windows 更新 / Python 升级 / comtypes 换版
      而失效。原来直接 `from comtypes.gen.UIAutomationClient import ...` → 抛
      ModuleNotFoundError → **整个 tools/windows_desktop 模块导入失败**
      (生产表现: 截图/窗口/桌面控制功能凭空消失, 工具被网关丢掉只留兜底文案)。
      实测: 本机 Python311 的 comtypes/gen 里只剩 stdole/SpeechLib, 没有
      UIAutomationClient.py → 截图全挂。GetModule 现场生成即可, 用户无感。
    """
    import importlib
    try:
        return importlib.import_module("comtypes.gen.UIAutomationClient")
    except ImportError:
        comtypes.client.GetModule("UIAutomationCore.dll")     # ← 现场再生 gen 缓存
        return importlib.import_module("comtypes.gen.UIAutomationClient")


_uia = _import_uia()
CUIAutomation = _uia.CUIAutomation8
IUIAutomation = _uia.IUIAutomation
TreeScope_Children = _uia.TreeScope_Children
UIA_BoundingRectanglePropertyId = _uia.UIA_BoundingRectanglePropertyId
UIA_NamePropertyId = _uia.UIA_NamePropertyId
UIA_ControlTypePropertyId = _uia.UIA_ControlTypePropertyId
UIA_IsEnabledPropertyId = _uia.UIA_IsEnabledPropertyId
UIA_ClassNamePropertyId = _uia.UIA_ClassNamePropertyId
UIA_AutomationIdPropertyId = _uia.UIA_AutomationIdPropertyId
UIA_InvokePatternId = _uia.UIA_InvokePatternId
UIA_ValuePatternId = _uia.UIA_ValuePatternId
UIA_IsOffscreenPropertyId = _uia.UIA_IsOffscreenPropertyId
UIA_NativeWindowHandlePropertyId = _uia.UIA_NativeWindowHandlePropertyId
UIA_ProcessIdPropertyId = _uia.UIA_ProcessIdPropertyId
UIA_FrameworkIdPropertyId = _uia.UIA_FrameworkIdPropertyId
from comtypes import COMError


def _grab_screen():
    """截屏 → PIL.Image。三级兜底, 缺一个库不再瘫痪。

    ★ 2026-09-20 加: 原来三处截图动作都写死 `import pyautogui`, 本机没装 pyautogui
      → screenshot_now / screenshot / save_screenshot 全报 "No module named 'pyautogui'"
      (与"工具白报缺库"同类的老坑: 只认一个库)。顺序:
        ① pyautogui (装了就用, 行为不变)
        ② Pillow 的 ImageGrab (本机已装 pillow 12.x)
        ③ win32 BitBlt (pywin32 已装) —— 最后兜底
    """
    try:
        import pyautogui
        return pyautogui.screenshot()
    except Exception:
        pass
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(all_screens=True)
    except Exception:
        pass
    import win32gui, win32ui, win32con
    from PIL import Image
    hdesktop = win32gui.GetDesktopWindow()
    left, top, right, bottom = win32gui.GetWindowRect(hdesktop)
    w, h = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hdesktop)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    save_dc.BitBlt((0, 0), (w, h), mfc_dc, (left, top), win32con.SRCCOPY)
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC(); mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hdesktop, hwnd_dc)
    return img


# Win32
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_VSCROLL = 0x0115
WM_HSCROLL = 0x0114
VK_RETURN = 0x0D
VK_TAB = 0x09
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
INPUT_MOUSE = 0

user32 = ctypes.windll.user32

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


# ═══════════════ Data types ═══════════════

@dataclass
class UIElement:
    index: int
    role: str = ""
    label: str = ""
    name: str = ""
    class_name: str = ""
    enabled: bool = True
    offscreen: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    depth: int = 0
    parent_index: int = -1
    _element: any = field(default=None, repr=False)

    @property
    def center(self):
        return (self.x + self.width // 2, self.y + self.height // 2)

    def __repr__(self):
        return f"[{self.index}] {self.role} '{self.label}' @({self.x},{self.y}) {self.width}x{self.height}"


@dataclass
class DesktopState:
    elements: list
    window_title: str
    window_bounds: tuple
    pid: int
    hwnd: int
    snapshot_id: str


@dataclass
class ActionResult:
    success: bool
    message: str = ""
    verified: bool = False


# ═══════════════ Win32 helpers ═══════════════

def _post_message(hwnd, msg, wparam=0, lparam=0):
    return user32.PostMessageW(hwnd, msg, wparam, lparam) != 0

def _make_lparam(x, y):
    return (y << 16) | (x & 0xFFFF)

def _get_deepest_child(hwnd, x, y):
    child = user32.ChildWindowFromPointEx(hwnd, POINT(x, y), 1)
    return child if child else hwnd

def _send_input_mouse(x, y, flags):
    user32.SetCursorPos(x, y)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.union.mi.dwFlags = flags
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


# ═══════════════ UIA Core ═══════════════

UIA_SINGLETON = None

def _uia():
    global UIA_SINGLETON
    if UIA_SINGLETON is None:
        UIA_SINGLETON = comtypes.client.CreateObject(
            CUIAutomation().IPersist_GetClassID(), interface=IUIAutomation)
    return UIA_SINGLETON

def _type_name(ct):
    names = {50000:"Button",50001:"Calendar",50002:"CheckBox",50003:"ComboBox",
             50004:"Edit",50005:"Hyperlink",50006:"Image",50007:"ListItem",
             50008:"List",50009:"Menu",50010:"MenuBar",50011:"MenuItem",
             50017:"StatusBar",50018:"Tab",50019:"TabItem",50020:"Text",
             50023:"Tree",50024:"TreeItem",50025:"Custom",50026:"Group",
             50032:"Window",50033:"Pane",50037:"TitleBar",50038:"Separator"}
    return names.get(ct, f"Ctrl({ct})")

def _get_pattern(el, pid):
    try: return el.GetCurrentPattern(pid)
    except COMError: return None


# ═══════════════ Tree walk ═══════════════

def _walk(element, elements, parent=-1, depth=0, max_d=25, max_e=5000):
    if depth > max_d or len(elements) >= max_e:
        return
    try:
        name = element.CurrentName or ""
        ct = element.CurrentControlType
        role = _type_name(ct)
        cn = element.CurrentClassName or ""
        aid = element.CurrentAutomationId or ""
        en = bool(element.CurrentIsEnabled)
        off = bool(element.CurrentIsOffscreen)
        r = element.CurrentBoundingRectangle
        label = name or aid or cn or role
        idx = len(elements)
        elements.append(UIElement(idx, role, label, name, cn, en, off,
                                   r.left, r.top, r.right-r.left, r.bottom-r.top, depth, parent, element))
        # Walk children via TreeScope
        uia = _uia()
        true_cond = uia.CreateTrueCondition()
        children = element.FindAll(TreeScope_Children, true_cond)
        if children:
            for i in range(children.Length):
                child = children.GetElement(i)
                _walk(child, elements, idx, depth+1, max_d, max_e)
    except COMError:
        pass


# ═══════════════ Controller ═══════════════

class DesktopController:
    def __init__(self):
        self._uia = _uia()
        self._state = None
        self._hwnd = 0

    def capture(self, title=None, pid=None, max_depth=20, max_elements=2000):
        el = self._find_window(title, pid)
        if el is None:
            raise RuntimeError(f"Window not found: {title=} {pid=}")
        self._hwnd = el.CurrentNativeWindowHandle
        p = el.CurrentProcessId
        r = el.CurrentBoundingRectangle
        elements = []
        _walk(el, elements, max_d=max_depth, max_e=max_elements)
        sid = uuid.uuid4().hex[:12]
        self._state = DesktopState(elements, el.CurrentName or "",
                                    (r.left,r.top,r.right-r.left,r.bottom-r.top), p, self._hwnd, sid)
        return self._state

    def _find_window(self, title, pid):
        uia = self._uia
        if title:
            cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, 50032)
            ws = uia.GetRootElement().FindAll(TreeScope_Children, cond)
            for i in range(ws.Length):
                w = ws.GetElement(i)
                try:
                    if title.lower() in (w.CurrentName or "").lower():
                        return w
                except COMError: continue
        if pid:
            cond = uia.CreatePropertyCondition(UIA_ProcessIdPropertyId, pid)
            ws = uia.GetRootElement().FindAll(TreeScope_Children, cond)
            if ws.Length > 0: return ws.GetElement(0)
        try: return uia.GetFocusedElement()
        except COMError: raise RuntimeError("No window found")

    # ── L1+L2+L3 click ladder ──

    def click(self, idx, button="left", double=False):
        el = self._get(idx)
        # L1: InvokePattern
        inv = _get_pattern(el._element, UIA_InvokePatternId)
        if inv and button=="left" and not double:
            try:
                inv.Invoke()
                return ActionResult(True, f"Invoke [{idx}]{el.label}", True)
            except COMError: pass
        # L2: PostMessage pixel
        hw = _get_deepest_child(self._hwnd, *el.center)
        mx, my = el.center
        self._ensure_window_coords(mx, my)
        if not double:
            _post_message(hw, WM_LBUTTONDOWN if button=="left" else WM_RBUTTONDOWN, 0, _make_lparam(mx, my))
            _post_message(hw, WM_LBUTTONUP if button=="left" else WM_RBUTTONUP, 0, _make_lparam(mx, my))
        else:
            _post_message(hw, WM_LBUTTONDBLCLK, 0, _make_lparam(mx, my))
        return ActionResult(True, f"PM click [{idx}]{el.label}")

    def click_foreground(self, idx):
        el = self._get(idx)
        self._ensure_window_coords(*el.center)
        _send_input_mouse(*el.center, MOUSEEVENTF_LEFTDOWN)
        _send_input_mouse(*el.center, MOUSEEVENTF_LEFTUP)
        return ActionResult(True, f"SI click [{idx}]{el.label}")

    # ── Type ──

    def type_text(self, idx, text):
        el = self._get(idx)
        vp = _get_pattern(el._element, UIA_ValuePatternId)
        if vp:
            try:
                vp.SetValue(text)
                if text in vp.CurrentValue:
                    return ActionResult(True, f"Val [{idx}]='{text}'", True)
            except COMError: pass
        try: el._element.SetFocus()
        except COMError: pass
        import time
        time.sleep(0.05)
        for ch in text:
            _post_message(self._hwnd, WM_CHAR, ord(ch), 0)
            time.sleep(0.01)
        return ActionResult(True, f"PM typed to [{idx}]")

    # ── Keys / Scroll ──

    def press_key(self, key):
        km = {"return":VK_RETURN,"enter":VK_RETURN,"tab":VK_TAB,"escape":0x1B,
              "space":0x20,"delete":0x2E,"backspace":0x08,
              "up":0x26,"down":0x28,"left":0x25,"right":0x27}
        vk = km.get(key.lower()) or (ord(key.upper()) if len(key)==1 else None)
        if vk is None: return ActionResult(False, f"Unknown key: {key}")
        _post_message(self._hwnd, WM_KEYDOWN, vk, 0)
        _post_message(self._hwnd, WM_KEYUP, vk, 0)
        return ActionResult(True, f"Key '{key}'")

    def scroll(self, direction="down", amount=3):
        sm = {"down":(WM_VSCROLL,1),"up":(WM_VSCROLL,0),"left":(WM_HSCROLL,0),"right":(WM_HSCROLL,1)}
        msg, wp = sm.get(direction, (WM_VSCROLL,1))
        for _ in range(amount):
            _post_message(self._hwnd, msg, wp, 0)
        return ActionResult(True, f"Scroll {direction}x{amount}")

    def list_windows(self):
        result = []
        cond = self._uia.CreatePropertyCondition(UIA_ControlTypePropertyId, 50032)
        ws = self._uia.GetRootElement().FindAll(TreeScope_Children, cond)
        for i in range(ws.Length):
            w = ws.GetElement(i)
            try:
                n = w.CurrentName
                if n and not w.CurrentIsOffscreen:
                    r = w.CurrentBoundingRectangle
                    result.append({"title":n,"x":r.left,"y":r.top,"width":r.right-r.left,"height":r.bottom-r.top,
                                   "pid":w.CurrentProcessId,"hwnd":w.CurrentNativeWindowHandle})
            except COMError: continue
        return result

    def _get(self, idx):
        if self._state is None: raise RuntimeError("capture() first")
        if idx<0 or idx>=len(self._state.elements):
            raise IndexError(f"idx {idx} out of 0-{len(self._state.elements)-1}")
        return self._state.elements[idx]

    def _ensure_window_coords(self, x, y):
        pass  # coords from UIA are already screen-relative

    @property
    def state(self):
        return self._state

# Singleton
_ctrl = None
def get_controller():
    global _ctrl
    if _ctrl is None: _ctrl = DesktopController()
    return _ctrl


# ═══════════════════════════════════════════
# PluginManager entry point
# ═══════════════════════════════════════════

def execute(**kwargs) -> dict:
    """PluginManager entry: execute(action, ...)."""
    # Ensure COM is initialized for this thread (required in multi-threaded contexts)
    import pythoncom
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass  # already initialized
    
    action = kwargs.get("action", "capture")
    ctrl = get_controller()
    try:
        if action == "capture":
            title = kwargs.get("target") or kwargs.get("window_title")
            max_e = kwargs.get("max_elements", 200)
            state = ctrl.capture(title=title, max_elements=max_e)
            return {
                "ok": True, "success": True,
                "output": f"窗口'{state.window_title}' {len(state.elements)}个元素",
                "window": state.window_title,
                "elements": [
                    {"i": e.index, "role": e.role, "label": e.label,
                     "x": e.x, "y": e.y, "w": e.width, "h": e.height, "en": e.enabled}
                    for e in state.elements[:max_e]
                ],
                "count": len(state.elements)
            }
        elif action == "screenshot":
            # "screenshot" = real screenshot, not UIA capture
            save_path = kwargs.get("path", kwargs.get("target", ""))
            if not save_path:
                save_path = os.path.join(_desktop_dir(), "screenshot.png")
            img = _grab_screen()
            img.save(save_path, "PNG")
            size = os.path.getsize(save_path)
            return {"ok": True, "success": True, "path": save_path, "size": size,
                    "output": f"截图已保存 → {save_path} ({size//1024}KB)",
                    "message": "Screenshot saved: " + save_path}
        elif action == "click":
            idx = kwargs.get("element", kwargs.get("target", 0))
            ctrl.click(int(idx))
            return {"ok": True, "success": True, "output": f"已点击元素{idx}", "message": f"Clicked element {idx}"}
        elif action == "type" or action == "type_text":
            idx = kwargs.get("element", 0)
            text = kwargs.get("text", kwargs.get("target", ""))
            ctrl.type_text(int(idx), text)
            return {"ok": True, "success": True, "output": f"已输入文本", "message": f"Typed to element {idx}"}
        elif action == "list_windows" or action == "find_window":
            wins = ctrl.list_windows()
            target = kwargs.get("target", "")
            if target:
                wins = [w for w in wins if target in w.get("title", "")]
            return {"ok": True, "success": True, "output": f"找到{len(wins[:20])}个窗口", "windows": wins[:20]}
        elif action == "scroll":
            ctrl.scroll(kwargs.get("direction", "down"), int(kwargs.get("amount", 3)))
            return {"ok": True, "success": True, "output": "滚动完成"}
        elif action == "key" or action == "press_key":
            ctrl.press_key(kwargs.get("key", "return"))
            return {"ok": True, "success": True, "output": f"已按键{kwargs.get('key','return')}"}
        elif action in ("screenshot_now", "截当前屏幕"):
            import time as _t
            # If target window specified, try to focus it
            target = kwargs.get("target", "")
            if target:
                hwnd = ctypes.windll.user32.FindWindowW(None, target)
                if hwnd:
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    _t.sleep(0.3)
            save_path = kwargs.get("path", "")
            if not save_path:
                save_path = os.path.join(_desktop_dir(), "screenshot.png")
            img = _grab_screen()
            img.save(save_path, "PNG")
            size = os.path.getsize(save_path)
            return {"ok": True, "success": True, "path": save_path, "size": size,
                    "output": f"截图已保存 → {save_path} ({size//1024}KB)",
                    "message": "Screenshot saved: " + save_path}
        elif action in ("desktop_screenshot", "fullscreen", "save_screenshot", "截图保存"):
            import time as _t
            # Win+D to show desktop
            ctypes.windll.user32.keybd_event(0x5B, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x44, 0, 0, 0)
            ctypes.windll.user32.keybd_event(0x44, 0, 2, 0)
            ctypes.windll.user32.keybd_event(0x5B, 0, 2, 0)
            _t.sleep(0.8)
            save_path = kwargs.get("target", "") or kwargs.get("path", "")
            if not save_path:
                save_path = os.path.join(_desktop_dir(), "test.png")
            img = _grab_screen()
            img.save(save_path, "PNG")
            size = os.path.getsize(save_path)
            return {"ok": True, "success": True, "path": save_path, "size": size,
                    "output": f"截图已保存 → {save_path} ({size//1024}KB)",
                    "message": "Screenshot saved: " + save_path}
        elif action in ("wait", "等待"):
            import time as _t
            secs = float(kwargs.get("target", kwargs.get("seconds", 2)))
            _t.sleep(secs)
            return {"ok": True, "success": True, "output": f"等待{secs}秒", "message": f"Waited {secs}s"}
        elif action in ("open_folder", "打开文件夹"):
            import subprocess, time as _t
            target = kwargs.get("target", kwargs.get("path", "C:\\"))
            subprocess.Popen(["explorer", target])
            # Wait for window to appear and focus it
            for attempt in range(20):
                _t.sleep(0.3)
                hwnd = ctypes.windll.user32.FindWindowW(None, target)
                if hwnd and ctypes.windll.user32.IsWindowVisible(hwnd):
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    _t.sleep(0.5)
                    return {"ok": True, "success": True, "output": f"已打开并聚焦: {target}", "message": f"Opened and focused: {target}"}
            return {"ok": True, "success": True, "output": f"已打开: {target}", "message": f"Opened: {target} (may not be in front)"}
        elif action == "launch_app" or action == "open_browser":
            import subprocess
            target = kwargs.get("target", "")
            url = kwargs.get("url", "https://www.baidu.com")
            if "http" in target:
                url = target
            # Try Chrome first, then Edge
            for browser in [r"C:/Program Files/Google/Chrome/Application/chrome.exe",
                           r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"]:
                if os.path.exists(browser):
                    subprocess.Popen([browser, url])
                    return {"ok": True, "success": True, "output": f"已启动浏览器", "message": f"已启动: {browser} -> {url}"}
            subprocess.Popen(['start', url], shell=True)
            return {"ok": True, "success": True, "output": f"已启动默认浏览器", "message": f"已启动默认浏览器 -> {url}"}
        else:
            return {"ok": False, "success": False, "output": f"未知操作: {action}", "error": f"Unknown action: {action}"}
    except Exception as e:
        return {"ok": False, "success": False, "output": f"操作失败: {e}", "error": str(e)}


async def run(**kwargs):
    """Async entry for PluginManager."""
    return execute(**kwargs)
