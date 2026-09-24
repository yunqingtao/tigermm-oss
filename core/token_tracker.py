"""
Token usage tracker — per-model cost estimation.
"""
import time, json
from pathlib import Path
from config.settings import DATA_DIR

# Cost per 1M tokens (USD) — approximate
MODEL_COSTS = {
    "deepseek": {"prompt": 0.27, "completion": 1.10},
    "deepseek-chat": {"prompt": 0.27, "completion": 1.10},
    "deepseek-v4-pro": {"prompt": 0.27, "completion": 1.10},
    "mimo": {"prompt": 0.40, "completion": 1.20},
    "openai": {"prompt": 2.50, "completion": 10.00},
    "ollama": {"prompt": 0, "completion": 0},  # free
    "qwen3.5:9b": {"prompt": 0, "completion": 0},
}


class TokenTracker:
    def __init__(self):
        self._stats = {}  # {model: {"prompt": N, "completion": N, "calls": N}}
        self._load()
    
    def _load(self):
        path = DATA_DIR / "token_stats.json"
        if path.exists():
            try:
                self._stats = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                self._stats = {}
    
    def _save(self):
        path = DATA_DIR / "token_stats.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._stats, ensure_ascii=False, indent=2), encoding='utf-8')
    
    def record(self, model: str, usage: dict):
        """Record token usage from an API call."""
        if not usage or not usage.get("total_tokens"):
            # Estimate: ~2 chars per token for Chinese, ~4 chars for English
            return
        m = model or "unknown"
        if m not in self._stats:
            self._stats[m] = {"prompt": 0, "completion": 0, "calls": 0}
        self._stats[m]["prompt"] += usage.get("prompt_tokens", 0)
        self._stats[m]["completion"] += usage.get("completion_tokens", 0)
        self._stats[m]["calls"] += 1
        self._save()
    
    def cost_estimate(self, model: str) -> float:
        """Estimate cost in USD for a model's usage."""
        s = self._stats.get(model, {})
        if not s:
            return 0
        costs = MODEL_COSTS.get(model, {"prompt": 0.5, "completion": 2.0})
        prompt_cost = (s["prompt"] / 1_000_000) * costs["prompt"]
        completion_cost = (s["completion"] / 1_000_000) * costs["completion"]
        return prompt_cost + completion_cost
    
    def summary(self) -> str:
        """Human-readable summary."""
        if not self._stats:
            return "(no token data yet)"
        lines = []
        total_cost = 0.0
        for model, s in sorted(self._stats.items()):
            cost = self.cost_estimate(model)
            total_cost += cost
            lines.append(
                f"  {model:<18s} {s['calls']:>4d} calls  "
                f"{s['prompt']:>7,d} in  {s['completion']:>7,d} out  "
                f"${cost:.4f}"
            )
        lines.append(f"  {'─'*60}")
        lines.append(f"  Total: ${total_cost:.4f}")
        return "\n".join(lines)


# Singleton
_tracker = None

def get_tracker() -> TokenTracker:
    global _tracker
    if _tracker is None:
        _tracker = TokenTracker()
    return _tracker
