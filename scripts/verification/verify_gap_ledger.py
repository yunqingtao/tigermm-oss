"""能力缺口台账 (core/gap_ledger.py + 接线) — 常驻验证器

为什么要有这一份: 这个模块管的是「TMM 知不知道**自己缺什么**」。
它是"缺口 → 找办法 → 拿回来"这条链的第一步 —— 没有账, 出门找工具就没有关键词、
评估没有标准、装完也不知道值不值。

单元测试 (tests/test_gap_ledger.py) 覆盖了分类/聚合/状态流转; 这一份专测
**接线与不变量** (单元测不到的部分):

  A 路由声明: cmd.gap 在 P_COMMAND 段 · writes_state 且 probe_gated · requires 可解析
  B ★★ 接线: 真跑 process → 真缺口被记下 (不是"模块能跑但没人调用")
  C ★ 不误报: 正常问答 / 技能正常追问用户 / 裸文件不存在 → **不得**进账
  D ★★ 硬不变量: probe 流量不记账、不改台账 (连 /gap drop 都拦)
  E 聚合: 同类缺口重复出现 → count 累加 (值得造阈值的依据)
  F 出口: /gap 看账 · why 细节 · plan/done/drop 状态流转
  G ★ 隔离: TMM_GAP_DB 生效 (门禁/验证不写用户台账)
  H 零副作用: 本验证器不写真台账 (行数指纹不变)
  I ★ 变异: 拆掉接线/守卫 → 对应断言必须变红 (有辨别力)

全离线: 不联网、不打模型 (只用命令行 + 记账路径)。
"""
import asyncio
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F = [], []
REAL_GAP_DB = ROOT / "data" / "gap_ledger.db"


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def goal_fingerprint():
    """真台账指纹 (行数, 最大 id, 总次数) —— 本验证器跑完必须不变。"""
    try:
        c = sqlite3.connect(f"file:{REAL_GAP_DB}?mode=ro", uri=True)
        try:
            return (c.execute("SELECT COUNT(*) FROM gaps").fetchone()[0],
                    c.execute("SELECT COALESCE(MAX(id),0) FROM gaps").fetchone()[0],
                    c.execute("SELECT COALESCE(SUM(count),0) FROM gaps").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


def _chat_fp():
    """真对话库指纹 (行数, 最大 id) —— 本验证器跑完必须不变。"""
    try:
        c = sqlite3.connect(f"file:{ROOT / 'data' / 'chat_sessions.db'}?mode=ro", uri=True)
        try:
            return (c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    c.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


def build(sandbox: Path):
    """真 pipeline + 沙箱台账 (不碰真库)。"""
    import types
    import core.mcp_client as _mc
    _mc.get_mcp_client = lambda *a, **k: types.SimpleNamespace(
        load_config=lambda *a, **k: None, list_tools=lambda *a, **k: [],
        tools=[], get_tool=lambda *a, **k: None)
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline
    from core.knowledge import KnowledgeEngine
    from config.settings import DATA_DIR
    cfg = _load_config()
    pl = Level4Pipeline(ModelClient(cfg), cfg)
    try:
        pl._ke = KnowledgeEngine(DATA_DIR)
    except Exception:
        pass
    # 沙箱台账 (含模块单例, 防别处 get_gap_ledger 写真库)
    import core.gap_ledger as _gl
    sb_led = _gl.GapLedger(db_path=sandbox / "gaps.db", data_dir=sandbox)
    _gl._ledger = sb_led
    pl.gap_ledger = sb_led
    # ★★ 沙箱对话库 —— 必须的! 本验证器会真跑 N 轮 process(), 而 process() 现在
    #    统一落库 → 不沙箱就会把 "/gap"/"/gap why 1" 这些验证消息写进**用户的真实
    #    对话历史** (实测踩到: 第一版就是这么污染了 22 行, 已清理)。
    from storage.session_store import SessionStore
    pl.sessions = SessionStore(sandbox / "sessions.db")
    return pl, sb_led


def main() -> int:
    SB = Path(tempfile.mkdtemp(prefix="_vfy_gap_"))
    fp_before = goal_fingerprint()
    chat_fp_before = _chat_fp()
    try:
        pl, led = build(SB)

        # ── A 路由声明 ──
        print("        —— A 路由声明 ——")
        r = pl.routes.get("cmd.gap")
        chk("cmd.gap 已注册", r is not None)
        if r:
            chk("优先级在 1000 命令段", r.priority == 1000, str(r.priority))
            chk("声明 writes_state", bool(r.writes_state))
            chk("probe_gated (probe 下明确跳过, 不 fall-through)", bool(getattr(r, "probe_gated", False)))
            chk("requires 可解析 (gap_ledger 在位)", r.available(pl))
            chk("match 是纯函数 (同输入同结果)",
                all(r.match("/gap", {}) == r.match("/gap", {}) for _ in range(3)))
            chk("match 认 /gap 与 /gap xxx", r.match("/gap", {}) and r.match("/gap why 1", {}))
            chk("match 不认别的命令", not r.match("/stats", {}) and not r.match("/gapx", {}))
        aud = pl.routes.audit()
        chk("路由表无审计问题", not aud["issues"], str(aud["issues"]))

        # ── B 接线: 真跑 process ──
        print("        —— B 接线 (真跑) ——")
        out = asyncio.run(pl.process("/gap", mode_override="craft"))
        chk("★ /gap 真被 cmd.gap 接手", out.get("route") == "cmd.gap", str(out.get("route"))[:60])
        chk("空账文案正确", "缺口台账" in (out.get("response") or ""), (out.get("response") or "")[:60])

        gaps_before = led.stats()["total"]
        # 造真缺口: 缺依赖型 (走 tool 路径最稳, 用 _on_route_done 同款调用)
        hit = led.record("识别这张图的文字", "No module named 'pytesseract'", "tool")
        chk("★ 记账通路可用 (created)", bool(hit) and hit.get("action") == "created", str(hit))
        chk("★ 分类正确 = missing_dep", (hit or {}).get("category") == "missing_dep", str(hit))
        chk("★ _on_route_done 也接线 (真跑一轮后账有变化)",
            led.stats()["total"] >= gaps_before)

        # ── C 不误报 ──
        print("        —— C 不误报 (账本不能全是噪音) ——")
        n0 = led.stats()["total_hits"]
        for msg, resp, rt in [
            ("1+1等于几", "1+1=2。", "model.fallback"),
            ("你好", "你好！虎哥在此。", "brain.route"),
            ("写份周报", "已写入 D:/r.docx", "skill"),
            ("帮我识别这张图", "✗ look-at-image 第 1 步(look) 失败: 没找到图片。请给出图片路径, 例如: D:/a.png", "skill"),
            ("转写音频", "文件不存在: D:\\tmp\\v.mp3", "tool"),
            ("看看那个文件", "没找到相关文件, 请确认路径", "tool"),
        ]:
            led.record(msg, resp, rt)
        chk("★ 5 类非缺口全部未记账", led.stats()["total_hits"] == n0,
            f"多了 {led.stats()['total_hits'] - n0} 次: {[g['sample'] for g in led.list_gaps()]}")

        # ── D 硬不变量: probe ──
        print("        —— D 硬不变量 (probe 不记账) ——")
        t0 = led.stats()["total"]
        h0 = led.stats()["total_hits"]
        asyncio.run(pl.process("帮我识别图片里的文字", probe=True, mode_override="craft"))
        chk("★ probe 下不记账", led.stats()["total"] == t0 and led.stats()["total_hits"] == h0)
        pr = asyncio.run(pl.process("/gap drop 1", probe=True, mode_override="craft"))
        chk("★ probe 下 /gap 被拦 (probe.skip)", pr.get("route") == "probe.skip", str(pr.get("route"))[:60])
        chk("★ 台账未被 probe 改动", led.stats()["total"] == t0)

        # ── E 聚合 ──
        print("        —— E 聚合 (值得造的依据) ——")
        for _ in range(3):
            led.record("帮我做个视频", "抱歉，我无法制作视频。", "model.fallback")
        wb = led.worth_building()
        chk("★ 重复出现 → 进『值得造』", len(wb) >= 1, str([(g['sample'], g['count']) for g in wb]))
        chk("★ count 累加不插新行",
            len([g for g in led.list_gaps() if g["sample"] == "帮我做个视频"]) == 1)

        # ── F 出口 ──
        print("        —— F 出口 (/gap 各子命令) ——")
        txt = asyncio.run(pl.process("/gap", mode_override="craft")).get("response") or ""
        chk("看账含分类统计", "分类:" in txt, txt[:80])
        chk("看账含『值得造』段", "值得造" in txt)
        why = asyncio.run(pl.process(f"/gap why {wb[0]['id']}", mode_override="craft")).get("response") or ""
        chk("why 显示原话+卡在哪+签名", "原话:" in why and "卡在哪:" in why and "签名" in why, why[:80])
        gid = wb[0]["id"]
        chk("done 标记成功", "已标记" in (asyncio.run(pl.process(f"/gap done {gid}", mode_override="craft")).get("response") or ""))
        chk("done 后不再算值得造", not any(g["id"] == gid for g in led.worth_building()))
        bad = asyncio.run(pl.process("/gap why 99999", mode_override="craft")).get("response") or ""
        chk("不存在的 id 诚实回", "没有" in bad, bad[:50])
        usage = asyncio.run(pl.process("/gap 乱写", mode_override="craft")).get("response") or ""
        chk("用法提示 (不乱执行)", "用法" in usage, usage[:50])

        # ── G 隔离 ──
        print("        —— G 隔离 (门禁/验证不写用户账) ——")
        import core.gap_ledger as _gl
        from core.gap_ledger import resolve_gap_db
        gate = SB / "gate.db"
        os.environ["TMM_GAP_DB"] = str(gate)
        try:
            chk("★ 默认取值 → 重定向", resolve_gap_db().resolve() == gate.resolve())
            chk("★ 显式真实路径 → 重定向 (pipeline 的写法)",
                resolve_gap_db(REAL_GAP_DB).resolve() == gate.resolve())
            own = SB / "own.db"
            chk("★ 显式别的路径 → 不动", resolve_gap_db(own).resolve() == own.resolve())
        finally:
            os.environ.pop("TMM_GAP_DB", None)
        chk("无隔离变量 → 就是真实台账", resolve_gap_db().resolve() == REAL_GAP_DB.resolve())
        chk("门禁 runner 会注入 TMM_GAP_DB",
            "TMM_GAP_DB" in (ROOT / "scripts" / "hermes_verify.py").read_text(encoding="utf-8", errors="replace"))

        # ── I 变异 ──
        print("        —— I 变异 (有辨别力) ——")
        tgt = ROOT / "core" / "gap_ledger.py"
        orig = tgt.read_bytes()
        h_orig = hashlib.md5(orig).hexdigest()
        try:
            txt_src = orig.decode("utf-8").replace("\r\n", "\n")
            nl = "\r\n" if b"\r\n" in orig else "\n"
            # 变异1: 拆掉"追问用户不算缺口"的过滤 → 正常追问会被误记
            m1 = txt_src.replace(
                '            if asking and cat in ("missing_arg", "skill_failed"):\n                return None\n',
                "", 1)
            chk("变异锚点1命中", m1 != txt_src)
            tgt.write_bytes(m1.replace("\n", nl).encode("utf-8"))
            import importlib
            import core.gap_ledger as _m
            importlib.reload(_m)
            mis = _m.classify("帮我识别这张图",
                              "✗ look-at-image 第 1 步(look) 失败: 没找到图片。请给出图片路径, 例如: D:/a.png",
                              "skill")
            chk("★ 变异1 被抓: 正常追问被误记", mis is not None, f"classify 返回 {mis}")
            # 变异2: missing_dep 退回**宽松模式** (裸"不存在/找不到"也算) ——
            #   实测踩过: 文件不存在 → 被误记成缺依赖 (那是**用户给错路径**, 不是能力缺口)。
            #   宽松正则看着更"能用", 所以这是很容易复发的回归, 必须钉住。
            _old_sig = [ln for ln in txt_src.split("\n") if "不是内部或外部命令" in ln]
            chk("变异2 锚点命中", len(_old_sig) == 1, str(_old_sig)[:80])
            _i0 = txt_src.index('("missing_dep", re.compile(')
            _i1 = txt_src.index(")),", _i0) + 3
            _loose = txt_src[_i0:_i1]
            tgt.write_bytes((txt_src[:_i0] + '("missing_dep", re.compile('
                            + '    r"No module named|未安装|没有安装|请先安装|not found|不存在|找不到", re.I)),'
                            + txt_src[_i1:]).replace("\n", nl).encode("utf-8"))
            importlib.reload(_m)
            _bare = _m.classify("转写音频", "文件不存在: D:" + chr(92) * 2 + "tmp" + chr(92) * 2 + "v.mp3", "tool")
            chk("★ 变异2 被抓: 裸『文件不存在』被误记成缺依赖", _bare is not None, f"classify 返回 {_bare}")
            tgt.write_bytes(orig)
            importlib.reload(_m)
            tgt.write_bytes(orig)
            importlib.reload(_m)
        finally:
            tgt.write_bytes(orig)
        chk("变异后已还原 (md5 一致)", hashlib.md5(tgt.read_bytes()).hexdigest() == h_orig)

        # ── H 零副作用 ──
        print("        —— H 零副作用 ——")
        chk("本验证器未动真台账 (指纹不变)", goal_fingerprint() == fp_before,
            f"{fp_before} -> {goal_fingerprint()}")
        chk("★ 本验证器未动真对话库 (落库也走沙箱)",
            _chat_fp() == chat_fp_before, f"{chat_fp_before} -> {_chat_fp()}")
    finally:
        shutil.rmtree(SB, ignore_errors=True)

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
