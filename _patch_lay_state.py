"""Patch pipeline.py: implement _lay_state + fix caller."""
from pathlib import Path

pipeline_path = Path(__file__).parent / "core" / "pipeline.py"
with open(pipeline_path, 'r', encoding='utf-8') as f:
    content = f.read()

# ── 1. Replace _lay_state method ──
old_state = '''    def _lay_state(self, message, ext_model=None):
        """Silent per user preference."""
        return None, False'''

new_state = '''    def _lay_state(self, message, ext_model=None):
        """Pre-execution understanding — shows 虎哥理解 before acting, logs to tmm_output.log."""
        msg = message.strip()
        output_log = Path(__file__).parent.parent / "tmm_output.log"
        intent = self._classify_intent(message)
        model_name = ext_model or "auto"
        
        # 构建理解摘要
        if msg.startswith('/'):
            understanding = f"执行内置命令: {msg.split()[0]}"
        elif intent == 'task':
            summary = msg[:100].replace('\\n', ' ').strip()
            if len(msg) > 100:
                summary += "…"
            understanding = f"任务模式 — {summary}（模型: {model_name}）"
        else:
            summary = msg[:100].replace('\\n', ' ').strip()
            if len(msg) > 100:
                summary += "…"
            understanding = f"对话/指令 — {summary}（模型: {model_name}）"
        
        state_msg = f"{GOLD}[虎哥理解]{RST} {understanding}"
        needs_confirm = (intent == 'task')  # 任务模式需确认
        
        # 写入 tmm_output.log
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{ts}] 虎哥理解 | intent={intent} | model={model_name} | msg={msg[:200]}\\n"
        try:
            with open(output_log, 'a', encoding='utf-8') as f:
                f.write(log_line)
        except Exception:
            pass
        
        return state_msg, needs_confirm'''

if old_state not in content:
    print("ERROR: old _lay_state not found!")
    print("Looking for:", repr(old_state[:60]))
    # Try to find it
    idx = content.find('def _lay_state')
    if idx >= 0:
        print("Found at offset:", idx)
        print(content[idx:idx+200])
    exit(1)

content = content.replace(old_state, new_state)
print("[1/2] _lay_state replaced ✓")

# ── 2. Fix caller: show state_msg always ──
old_caller = '''        _state_msg, _needs_confirm = pipeline_obj._lay_state(user_input, _ext_model)
        # Silent — only show if needs confirmation
        # if _state_msg: print(_state_msg)
        if _needs_confirm:'''

new_caller = '''        _state_msg, _needs_confirm = pipeline_obj._lay_state(user_input, _ext_model)
        if _state_msg:
            print(_state_msg)
        if _needs_confirm:'''

if old_caller not in content:
    print("ERROR: old caller block not found!")
    exit(1)

content = content.replace(old_caller, new_caller)
print("[2/2] Caller fixed — state_msg always shown ✓")

with open(pipeline_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done. pipeline.py patched successfully.")
