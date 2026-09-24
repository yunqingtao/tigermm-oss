"""降级登记器 —— 让"没办成"必须出声 (2026-09-21 立)

为什么要有它 (用血换的)
═══════════════════════════════════════════════════════════════════
本项目全仓有 193 处"完整吞掉异常"的处理器 (`except ...: pass/continue`)。
绝大多数是**正当的**(探测/清理/可选依赖/多级降级链), 所以"禁止吞异常"是错的规矩。

但其中有一类会致命: **吞掉之后, 用户以为办成了**。实测两起:
  · `/voice` 里 TTS 播放失败被吞 → 只回文字, 用户以为语音也回了
  · 门禁离线时 pyflakes 被跳过 → 记成 PASS, "全绿"是假的
这两起都是**人肉翻出来的**, 不是它自己报的。这才是真问题:
  不是"不该容错", 而是"容错了不吭声"。

规矩 (简单到能记住)
───────────────────────────────────────────────────────────────
  容错可以, 但**降级必须留痕**:
      from core.result import degrade
      except Exception as e:
          degrade("语音播报失败", str(e)[:80])      # ← 一行, 替代 pass

  · 探测类/清理类失败仍可直接 pass (那是"没有", 不是"降级")
  · 凡是**用户以为会发生的事**没发生 → 必须 degrade()

它怎么被用
───────────────────────────────────────────────────────────────
  入口处 reset() → 执行中各层 degrade() → 出口 drain() 取走并**追加到回复末尾**
  (用户在回复里直接看到 "⚠ 已降级: ..."), 同时进日志。

为什么用模块级 list 而不是 contextvar
───────────────────────────────────────────────────────────────
引擎是**单进程单会话**模型 (CLI/Web/relay 都串行进同一条 process()),
一次 process() 就是一个完整回合, reset/drain 天然成对。
不引异步上下文, 就不会在并发上出幺蛾子 —— 需要并发时再升级。

自检:  python core/result.py
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("core.result")

_LOCK = threading.Lock()
_DEGRADED: list[dict] = []
_SEEN: set[str] = set()          # 同一回合内同一件事只记一次, 避免刷屏


def reset() -> None:
    """回合开始 —— 清空上一回合的登记。"""
    with _LOCK:
        _DEGRADED.clear()
        _SEEN.clear()


def degrade(what: str, why: str = "", where: str = "") -> None:
    """登记一次降级。what=没做成什么(用户能看懂), why=原因, where=位置(可选)。"""
    key = f"{what}|{why[:40]}"
    with _LOCK:
        if key in _SEEN:
            return
        _SEEN.add(key)
        _DEGRADED.append({"what": str(what)[:120], "why": str(why)[:160], "where": str(where)[:80]})
    logger.warning("degraded: %s%s", what, f" ({why})" if why else "")


def items() -> list[dict]:
    """当前回合已登记的降级 (只读快照)。"""
    with _LOCK:
        return list(_DEGRADED)


def drain() -> list[dict]:
    """取走并清空 —— 出口调用一次。"""
    with _LOCK:
        out = list(_DEGRADED)
        _DEGRADED.clear()
        _SEEN.clear()
    return out


def status() -> str:
    """执行结果三态: ok / degraded / failed(由调用方判定并传)。"""
    return "degraded" if items() else "ok"


def format_marker(mark: str = "⚠") -> str:
    """→ 追加到回复末尾的可见提示; 无降级则返回空串。"""
    ds = items()
    if not ds:
        return ""
    lines = [f"\n\n{mark} 已降级 ({len(ds)} 项 —— 以下步骤没按预期完成, 结果可能不完整):"]
    for d in ds[:5]:
        t = f"  · {d['what']}"
        if d["why"]:
            t += f" —— {d['why']}"
        lines.append(t)
    if len(ds) > 5:
        lines.append(f"  · …还有 {len(ds) - 5} 项")
    return "\n".join(lines)


if __name__ == "__main__":      # 自检
    import sys
    reset()
    assert status() == "ok" and not items()
    degrade("语音播报失败", "缺 playsound")
    degrade("语音播报失败", "缺 playsound")          # 去重
    degrade("证据链重答失败", "模型超时")
    assert status() == "degraded", status()
    assert len(items()) == 2, items()                # 去重生效
    m = format_marker()
    assert "⚠" in m and "语音播报失败" in m and "证据链重答失败" in m
    got = drain()
    assert len(got) == 2 and not items()             # 取走即清
    assert format_marker() == ""                      # 清空后无提示
    print("core/result.py 自检通过 ✓  (三态=%s, 去重生效, drain 即清)" % status())
    sys.exit(0)
