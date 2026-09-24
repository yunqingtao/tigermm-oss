"""WeChat Sender v7 — pyautogui, no SendInput"""
import pyautogui, time, subprocess, ctypes, sys

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Find WeChat
wx_hwnd = None
def callback(hwnd, lparam):
    global wx_hwnd
    if user32.IsWindowVisible(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == '微信':
            wx_hwnd = hwnd
    return True

user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)(callback), 0)

if not wx_hwnd:
    print("ERROR: WeChat not found")
    sys.exit(1)

import ctypes.wintypes as w
rect = w.RECT()
user32.GetWindowRect(wx_hwnd, ctypes.byref(rect))
wx, wy = rect.left, rect.top

if user32.IsIconic(wx_hwnd):
    print("ERROR: WeChat minimized")
    sys.exit(1)

print(f"WeChat: ({wx},{wy}) {rect.right-rect.left}x{rect.bottom-rect.top}")
SX, SY = wx + 100, wy + 60
print(f"Search: ({SX},{SY})")

# Give time to ensure WeChat is front
print("3...")
time.sleep(1)
print("2...")
time.sleep(1)
print("1...")
time.sleep(1)

# Use pyautogui instead of SendInput
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

# Esc
pyautogui.press('esc')
time.sleep(0.3)

# Click search box
print(f"Clicking ({SX},{SY})...")
pyautogui.click(SX, SY)
time.sleep(0.5)

# Copy to clipboard
subprocess.run(["powershell", "-Command", "Set-Clipboard -Value '黑山'"], capture_output=True, encoding='utf-8', errors='replace')
time.sleep(0.1)

# Ctrl+V
pyautogui.hotkey('ctrl', 'v')
time.sleep(1.5)

# Enter
pyautogui.press('enter')
time.sleep(1.0)

# Paste message
subprocess.run(["powershell", "-Command", "Set-Clipboard -Value '晚上吃饭'"], capture_output=True, encoding='utf-8', errors='replace')
time.sleep(0.1)
pyautogui.hotkey('ctrl', 'v')
time.sleep(0.5)

# Send
pyautogui.press('enter')

print("DONE")
