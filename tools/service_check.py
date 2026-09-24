"""
服务/端口存活检查工具 — "引擎还活着吗 / ollama 通不通"。

为什么要有 (2026-09-20 对照 OpenClaw 的 healthcheck 补):
    TMM 依赖几个本地服务 (引擎 8800 / ollama 11434), 但它们挂了的时候
    表现是"某功能突然不好用", 而不是清楚的报错。需要一个能一句话问出
    "谁在、谁不在、多快"的工具。

★ 只读: 只做 TCP 连接测试和本地 HTTP GET, 不改任何东西, 不碰外部网络
  (127.0.0.1 / localhost 白名单语义, 默认只查本机)。
  超时都压得很短 (默认 1.5s), 免得一个不通的服务把整轮对话卡住。
"""
import json
import logging
import socket
import time
import urllib.request

logger = logging.getLogger(__name__)

PLUGIN = {
    "name": "service_check",
    "description": "本机服务/端口存活检查 (引擎 8800 / ollama 11434 等)",
    "version": "1.0",
    "requires": [],
    "trigger": ["服务检查", "端口检查", "服务存活", "引擎活着", "ollama 通不通"],
    "permission": ["safe"],
    "category": "data",
}

# 默认要看的本地服务 (名字, 端口, HTTP 探测路径或 None)
DEFAULT_SERVICES = [
    ("TMM 引擎", 8800, "/stats"),
    ("ollama", 11434, "/api/tags"),
]

LOOPBACK_OK = ("127.0.0.1", "localhost", "::1")


def tcp_probe(port: int, host: str = "127.0.0.1", timeout: float = 1.5):
    """TCP 连接测试 → (是否通, 耗时ms 或 None, 错误文本)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        rc = s.connect_ex((host, port))
        ms = (time.time() - t0) * 1000
        if rc == 0:
            return True, round(ms, 1), ""
        return False, None, f"connect_ex={rc}"
    except Exception as e:
        return False, None, str(e)[:80]
    finally:
        try:
            s.close()
        except Exception:
            pass


def http_probe(port: int, path: str, host: str = "127.0.0.1", timeout: float = 2.0):
    """本机 HTTP GET → (成功?, 摘要文本, 解析出的 dict 或 None)"""
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read(65536).decode("utf-8", "replace")
        try:
            d = json.loads(raw)
        except Exception:
            return True, raw[:120].replace("\n", " "), None
        return True, "", d
    except Exception as e:
        return False, str(e)[:100], None


def _svc_digest(name: str, port: int, path: str, detail: dict):
    """把 HTTP 探测结果压成一句人话 (每个服务只说自己关心的字段)。"""
    if name.startswith("TMM") and detail:
        parts = []
        if "uptime" in detail:
            try:
                parts.append(f"运行 {int(float(detail['uptime']) // 60)} 分钟")
            except Exception:
                parts.append(f"uptime={detail['uptime']}")
        if "errors" in detail:
            parts.append(f"错误 {detail['errors']}")
        if "messages" in detail:
            parts.append(f"消息 {detail['messages']}")
        return " · ".join(parts) if parts else "(有响应)"
    if name == "ollama" and detail:
        models = detail.get("models")
        if isinstance(models, list):
            return f"装着 {len(models)} 个模型"
        return "(有响应)"
    return "(有响应)"


async def run(action: str = "", extra: str = "", timeout: float = 1.5, **kwargs) -> dict:
    """查本机服务。

    action:
      ports (默认) — TCP 存活 + 延迟, 已知服务顺带做 HTTP 探测
      http         — 只看 HTTP 接口内容 (引擎 /stats, ollama /api/tags)
    """
    action = (action or "ports").strip().lower()
    if action not in ("ports", "http", "alive", "check"):
        return {"success": False, "error": f"未知 action: {action} (可用: ports / http)"}
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 1.5
    timeout = max(0.2, min(timeout, 5.0))

    services = list(DEFAULT_SERVICES)
    # extra: "名字:端口,名字:端口" —— 让用户能临时加一个端口
    extra = (extra or kwargs.get("ports") or "").strip()
    if extra:
        for piece in str(extra).split(","):
            piece = piece.strip()
            if not piece:
                continue
            if ":" in piece:
                nm, _, pt = piece.partition(":")
            else:
                nm, pt = f"端口 {piece}", piece
            try:
                services.append((nm.strip(), int(pt.strip()), None))
            except ValueError:
                continue
    # 去重 (端口相同只留第一个)
    seen, uniq = set(), []
    for nm, pt, pth in services:
        if pt in seen:
            continue
        seen.add(pt)
        uniq.append((nm, pt, pth))
    services = uniq

    rows, down, up = [], [], []
    for name, port, path in services:
        ok, ms, err = tcp_probe(port, timeout=timeout)
        note = ""
        if ok:
            up.append(f"{name}({port})")
            if path:
                hok, herr, detail = http_probe(port, path, timeout=timeout + 0.5)
                note = _svc_digest(name, port, path, detail) if hok else f"HTTP 探测失败: {herr}"
        else:
            down.append(f"{name}({port})")
            note = err
        rows.append({"name": name, "port": port, "up": ok, "ms": ms, "note": note})

    lines = [f"本机服务检查: {len(up)} 个在跑" + (f" · {len(down)} 个不通" if down else " · 全部正常")]
    for r in rows:
        mark = "✓" if r["up"] else "✗"
        lat = f"{r['ms']}ms" if r["ms"] is not None else "—"
        lines.append(f"  {mark} {r['name']}(:{r['port']})  {lat}" + (f"  {r['note']}" if r["note"] else ""))
    if down:
        lines.append("提示: 不通的服务如果是引擎, 桌面「虎哥 Tiger.M.M」双击即可拉起。")

    return {"success": True, "output": "\n".join(lines), "services": rows,
            "up": len(up), "down": len(down)}
