"""
Context compactor — Grok-inspired 85% threshold auto-summarization.
Monitors conversation length and compresses history when approaching context limit.
"""
import time, re
from config.settings import DATA_DIR


# Model context window sizes (tokens)
CONTEXT_WINDOWS = {
    "deepseek": 128000,
    "deepseek-chat": 128000,
    "deepseek-v4-pro": 128000,
    "ollama": 8192,
    "qwen3.5:9b": 32768,
    "mimo": 32768,
    "default": 8192,
}

# How many recent turns to keep intact (not compressed)
KEEP_RECENT_TURNS = 5

# Threshold percentage to trigger compaction
COMPACTION_THRESHOLD = 0.85


def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~1.5 chars per token for Chinese, ~4 for English."""
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


def estimate_total_tokens(messages: list) -> int:
    """Estimate total tokens in a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "") or ""
        total += estimate_tokens(content)
        # Tool calls add overhead
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                total += estimate_tokens(str(tc.get("function", {})))
        if msg.get("role") == "tool":
            total += estimate_tokens(msg.get("content", ""))
    return total


def get_context_window(model_name: str) -> int:
    """Get context window size for a model."""
    for key, size in CONTEXT_WINDOWS.items():
        if key in (model_name or "").lower():
            return size
    return CONTEXT_WINDOWS["default"]


def should_compact(messages: list, model_name: str) -> bool:
    """Check if conversation should be compacted."""
    total = estimate_total_tokens(messages)
    window = get_context_window(model_name)
    ratio = total / window if window > 0 else 1.0
    return ratio >= COMPACTION_THRESHOLD


def build_compaction_prompt(recent_turns: list) -> str:
    """Build a prompt for the model to summarize conversation history."""
    # Extract key turns for summarization
    user_messages = []
    for msg in recent_turns:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if role == "user" and content:
            user_messages.append(f"User: {content[:200]}")
        elif role == "assistant" and content:
            user_messages.append(f"Assistant: {content[:200]}")
    
    history = "\n".join(user_messages[-20:])  # Last 20 turns max
    
    return f"""Summarize this conversation history in 3-5 concise bullet points in Chinese. 
Focus on: what the user asked for, what was done, key decisions, and current state.
Do NOT include greetings or small talk.

{history}

Summary (3-5 bullets, Chinese):"""


def compact_messages(messages: list) -> tuple[list, str]:
    """Split messages: keep recent KEEP_RECENT_TURNS turns, return older ones for compression."""
    user_turns = []
    current_turn = []
    
    for msg in messages:
        current_turn.append(msg)
        if msg.get("role") == "assistant" and msg.get("content"):
            user_turns.append(list(current_turn))
            current_turn = []
    
    if current_turn:
        user_turns.append(current_turn)
    
    if len(user_turns) <= KEEP_RECENT_TURNS + 3:
        return messages, ""  # Not enough to compress
    
    # Split: older turns for compression, recent turns to keep
    older_turns = []
    for turn in user_turns[:-KEEP_RECENT_TURNS]:
        older_turns.extend(turn)
    
    recent_turns = []
    for turn in user_turns[-KEEP_RECENT_TURNS:]:
        recent_turns.extend(turn)
    
    return recent_turns, build_compaction_prompt(older_turns)


def apply_compaction(messages: list, summary: str) -> list:
    """Replace older turns with a summary system message, keep recent turns."""
    _, recent = compact_messages(messages)
    if not recent:
        return messages
    
    # Build new message list: system + summary + recent turns
    result = []
    has_system = False
    for msg in recent:
        if msg.get("role") == "system":
            result.append(msg)
            has_system = True
            break
    
    if not has_system and messages and messages[0].get("role") == "system":
        result.append(dict(messages[0]))
    
    # Add compaction summary
    result.append({
        "role": "system",
        "content": f"[CONTEXT COMPACTION]\nPrevious conversation summary:\n{summary}\n[/CONTEXT COMPACTION]"
    })
    
    # Add recent turns (skip system messages already added)
    for msg in recent:
        if msg.get("role") != "system":
            result.append(msg)
    
    return result
