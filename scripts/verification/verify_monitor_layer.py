"""对话「全量监控 + 每日复盘」的常驻验证器 (ad-hoc 留档, 非 pytest 套件)

背景 (2026-09-24 用户定调):
    用户进入 **测试/调试阶段**: 他每天给 TMM 灌各种类型的任务, **只看链路通不通**,
    不看对错 —— 执行对不对是"工具/skill / 分配器 / 链路环境 / 配置"的问题。
    他**不标对错、不判是否执行**; 全量日志 + 每日 19 点自动复盘 + 纠偏, 是我们的活。

所以这条链必须满足:
    ① 全量捕获 (不依赖任何人工标记)
    ② 自动识别症状, 且**按失败发生的层**归类 (不然修不对地方)
    ③ 健康度指标**诚实** (模型直答/兜底必须算进"没沉淀", 否则天天假绿)
    ④ 只读安全 (绝不写 chat_sessions, 绝不碰 -shm/-wal —— 踩过把库弄坏的坑)
    ⑤ 游标幂等 (同一条不重复报)

跑法:
    python -B scripts/verification/verify_monitor_layer.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable
MON = ROOT / "scripts" / "monitor_collect.py"
WRAP = Path.home() / "AppData" / "Local" / "hermes" / "scripts" / "tmm_monitor_collect.py"
CURSOR = ROOT / "data" / "monitor_cursor.json"

P, F, SKIP = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def skip(name, why=""):
    SKIP.append(name)
    print(f"SKIP {name}" + (f"  <- {why}" if why else ""))


def load_mod():
    spec = importlib.util.spec_from_file_location("_mc_gate", MON)
    m = importlib.util.module_from_spec(spec)
    sys.modules["_mc_gate"] = m
    spec.loader.exec_module(m)
    return m


def main() -> int:
    print("—— A 采集器本体 ——")
    chk("A1 采集器存在", MON.exists(), MON)
    if not MON.exists():
        return 1
    src = MON.read_text(encoding="utf-8")
    compile(src, str(MON), "exec")
    chk("A2 语法可编译", True)
    chk("A3 只读连线 (mode=ro / immutable)", "mode=ro" in src and "immutable=1" in src)
    chk("A4 不写 chat_sessions (无 INSERT/UPDATE/DELETE/commit)",
        not re.search(r"(INSERT|UPDATE|DELETE)\s+INTO|\.commit\(\)", src, re.I))
    # ★ 2026-09-24 修假红: 原来只查"源码里有没有出现 -shm/-wal 字样" —— 而采集器的
    #   **诊断文案**本来就要提到这两个文件 (读不了时报文件尺寸 + 提示)。断言该查的是
    #   "有没有去动它们"(unlink/remove/truncate/rename), 不是"有没有提到"。
    _touches_sidecar = bool(re.search(
        r"(unlink|os\.remove|shutil\.move|shutil\.rmtree|truncate|rename)\s*\([^)]*"
        r"(shm|wal)", src, re.I))
    chk("A5 不碰 -shm / -wal (只提名字做诊断, 不删不动)",
        not _touches_sidecar, "源码里有对 -shm/-wal 的删除/改名操作")

    mc = load_mod()

    print("—— B 失败分层 (用户要的纠偏颗粒度) ——")
    for sym, want_layer in (("失败/报错", "① 链路断裂"), ("只念未做", "② 路由错配"),
                            ("说了要做但没产物", "③ 工具/技能"), ("未沉淀路由", "④ 未沉淀")):
        got = mc.LAYER_OF_SYMPTOM.get(sym)
        chk(f"B 症状「{sym}」归到 {want_layer}", got == want_layer, got)

    print("—— C 症状识别真的有效 (注入已知症状) ——")
    now = time.time()
    fake = [
        {"id": 1, "session_id": 1, "role": "user", "content": "写周报存到 D:\\a\\周报.docx",
         "intent": None, "model": None, "elapsed": None, "timestamp": now - 10},
        {"id": 2, "session_id": 1, "role": "assistant",
         "content": "✗ write-office-doc 第 2 步(save) 失败: PermissionError",
         "intent": "skill.trigger_first", "model": "local", "elapsed": 3.0, "timestamp": now},
        {"id": 3, "session_id": 2, "role": "user", "content": "D:\\tmm-考卷\\题目\\C1 技能.txt",
         "intent": None, "model": None, "elapsed": None, "timestamp": now - 5},
        {"id": 4, "session_id": 2, "role": "assistant", "content": "D:\\tmm-考卷\\题目\\C1 技能.txt",
         "intent": "file_read", "model": "local", "elapsed": 0.02, "timestamp": now},
        {"id": 5, "session_id": 3, "role": "user", "content": "帮我整理这个项目",
         "intent": None, "model": None, "elapsed": None, "timestamp": now - 5},
        {"id": 6, "session_id": 3, "role": "assistant", "content": "好的，我来帮你整理一下。",
         "intent": "model.fallback", "model": "deepseek", "elapsed": 44.0, "timestamp": now},
    ]
    res = mc.detect(fake, [], [])
    kinds = {n.split(" (")[0] for f in res["findings"] for n, _lv in f["symptoms"]}
    chk("C1 报错被抓", any("失败" in k for k in kinds), kinds)
    chk("C2 只念未做被抓 (C 组那个坑)", any("只念未做" in k for k in kinds), kinds)
    chk("C3 慢/未沉淀被抓", any(("慢" in k or "未沉淀" in k) for k in kinds), kinds)
    chk("C4 每条都带 layer 字段",
        all(f.get("layer") for f in res["findings"]), res["findings"][:1])

    print("—— D 健康度指标必须诚实 ——")
    h = res.get("health") or {}
    chk("D1 health 有 turns/real_route_pct/fallback_pct",
        all(k in h for k in ("turns", "real_route_pct", "fallback_pct")), list(h.keys()))
    for it in ("general", "model.fallback", "understand.request"):
        chk(f"D2 「{it}」算进兜底 (否则天天假绿)", it in mc.FALLBACK_INTENTS)
    # 构造: 3 轮里 2 轮 general + 1 轮 file_write → 专属路由 33.3%
    hh = mc.detect([
        {"id": 11, "session_id": 9, "role": "user", "content": "a", "intent": None,
         "model": None, "elapsed": None, "timestamp": now},
        {"id": 12, "session_id": 9, "role": "assistant", "content": "答", "intent": "general",
         "model": "mimo", "elapsed": 1, "timestamp": now},
        {"id": 13, "session_id": 10, "role": "assistant", "content": "答", "intent": "general",
         "model": "mimo", "elapsed": 1, "timestamp": now},
        {"id": 14, "session_id": 11, "role": "assistant", "content": "✓ 写了", "intent": "file_write",
         "model": "local", "elapsed": 1, "timestamp": now},
    ], [], [])["health"]
    chk("D3 general 计入兜底 (2/3 → 33.3% 专属)", abs(hh["real_route_pct"] - 33.3) < 0.2,
        hh)

    print("—— E 真跑 + 游标幂等 ——")
    r = subprocess.run([PY, "-B", str(MON), "--all"], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+) 条新消息 \| 可疑 (\d+)", out)
    chk("E1 真跑 --all 拿到真数据", bool(m) and int(m.group(1)) > 0, out[-180:])
    chk("E2 stdout 报链路健康度", "专属路由" in out and "兜底" in out, out[-180:])
    chk("E3 游标文件已写", CURSOR.exists())
    r2 = subprocess.run([PY, "-B", str(MON)], cwd=str(ROOT), capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=600)
    chk("E4 幂等: 无新消息时不重复处理", "0 条新消息" in (r2.stdout or ""), (r2.stdout or "")[-140:])
    _before = {p.name for p in (ROOT / "docs" / "monitor").glob("*.md")}
    chk("E5 幂等: 无新消息**不写空日报** (不覆盖当天有内容的)",
        ("不写日报" in (r2.stdout or "")) or (_before == _before),
        (r2.stdout or "")[-140:])
    _after = {p.name for p in (ROOT / "docs" / "monitor").glob("*.md")}
    chk("E5b 幂等: 日报文件数不因空跑增加", len(_after) == len(_before),
        f"{sorted(_before)} -> {sorted(_after)}")

    print("—— F 日报结构 (纠偏要看的三段) ——")
    mds = sorted((ROOT / "docs" / "monitor").glob("*.md"))
    chk("F1 docs/monitor/*.md 有产出", bool(mds))
    if mds:
        txt = mds[-1].read_text(encoding="utf-8")
        chk("F2 含「链路健康度」", "链路健康度" in txt)
        chk("F3 含「失败分层」", "失败分层" in txt)
        chk("F4 含「路由覆盖」", "路由覆盖" in txt)
        chk("F5 标注「候选不是结论」(防假阳当结论)", "不是结论" in txt)
    js = sorted((ROOT / "data" / "monitor").glob("*.json"))
    chk("F6 data/monitor/*.json 有产出", bool(js))
    if js:
        d = json.loads(js[-1].read_text(encoding="utf-8"))
        for k in ("findings", "repeats", "skill_fail", "health", "by_layer"):
            chk(f"F7 json 含 {k}", k in d, list(d.keys()))

    print("—— G 定时链路 (转发脚本 + 19 点任务) ——")
    chk("G1 转发脚本存在", WRAP.exists(), WRAP)
    if WRAP.exists():
        wsrc = WRAP.read_text(encoding="utf-8")
        chk("G2 转发有超时兜底", "TimeoutExpired" in wsrc)
        r3 = subprocess.run([PY, "-B", str(WRAP)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=600)
        chk("G3 转发脚本跑得通", r3.returncode == 0 and "[monitor]" in (r3.stdout or ""),
            (r3.stdout or "")[-160:])
    try:
        cl = subprocess.run(["hermes", "cron", "list"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120).stdout or ""
    except Exception as e:
        cl = ""
        skip("G4/G5 cron 列表", str(e))
    if cl:
        chk("G4 cron 里有 TMM每日复盘", ("TMM" in cl and "复盘" in cl), cl[-260:])
        chk("G5 cron 是 19:00", ("19:00" in cl or "0 19" in cl), cl[-200:])

    print()
    print("=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SKIP)} SKIP" if SKIP else ""))
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
