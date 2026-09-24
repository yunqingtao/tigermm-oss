"""
LCS (Longest Common Subsequence) similarity engine (from config/settings).
Unified API for AutoGuide, PluginManager, and tool chain matching.
"""

from functools import lru_cache
import re


def lcs_length(a: str, b: str) -> int:
    """Return raw LCS length between two strings."""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    # Optimize: only keep current and previous row
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, prev
    return prev[n]


@lru_cache(maxsize=2000)
def calc_similarity(s1: str, s2: str) -> float:
    """Return 0~1 similarity score. Results cached for high-frequency pairs."""
    if not s1 or not s2:
        return 0.0
    l = lcs_length(s1, s2)
    max_len = max(len(s1), len(s2))
    return round(l / max_len, 3)


def match_top_k(target: str, pool: list, threshold: float = 0.6, k: int = 3):
    """Batch match against pool, return top-k items above threshold.
    Each pool item can be a string or (string, metadata) tuple."""
    res = []
    for item in pool:
        if isinstance(item, tuple):
            text, meta = item
        else:
            text, meta = item, None
        sim = calc_similarity(target, text)
        if sim >= threshold:
            res.append((sim, text, meta))
    res.sort(key=lambda x: x[0], reverse=True)
    return [(text, meta) for _, text, meta in res[:k]]


def match_best(target: str, pool: list, threshold: float = 0.6):
    """Return single best match (text, score) or (None, 0)."""
    best_score = 0.0
    best_text = None
    for item in pool:
        text = item if isinstance(item, str) else item[0]
        sim = calc_similarity(target, text)
        if sim > best_score:
            best_score = sim
            best_text = text
    if best_score >= threshold:
        return best_text, best_score
    return None, 0.0
