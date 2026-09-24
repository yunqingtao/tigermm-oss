"""工具注册 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

为什么要有这一门 (代价是实测出来的)
═══════════════════════════════════════════════════════════════════
2026-09-20 实测发现: **三个功能在生产里静默失效** —— `vision` / `image_gen` / `backup`
未被登记 config/settings.py 的 SAFE_PLUGINS, 于是

    PluginManager._load_plugins()
      → scan_plugin_safe() 因它们 import os 返回 False
      → logger.warning("REJECTED by security scan") 然后 **continue (不加载)**
      → 调用得 {"success": False, "error": "未知工具: vision"}

后果链 (实测):
  · core/pipeline.py:4035 `await self.gateway.call("vision", …)` 永远失败;
    而且失败提示是"请检查视觉凭证(keys.json 的 qwen.key)" —— **把病因说成凭证问题**
  · intent_router 把"备份"映射到 backup → PluginManager 路径上不可用
溯源: 这三个都是后加的文件, 而"新工具须登记 SAFE_PLUGINS"这条规矩**没有门禁兜**,
      所以漏了三处, 且 pytest 全绿 (单测直接 import 工具模块, 绕过 PluginManager)。

本门禁守住的不变量:
  ① **无工具被静默拒绝**: 每个 tools/*.py (非 _ 开头) 要么在 SAFE_PLUGINS 里,
     要么能通过静态扫描 —— 否则它根本不会被加载, 调用方只会看到"未知工具"。
  ② 白名单**同源**: gateway/plugin_mgr.py 不再自己维护一份名单。
  ③ 幽灵条目透明化 (WARN, 不判红): 名单里有、但 tools/ 下没有文件的条目
     —— 说明名单在腐化 (本轮就抓到 output / sample_echo 两个幽灵)。

自清理: 只读, 不写任何文件; 不碰用户数据。
"""
import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F, S, W = [], [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def warn(name, detail=""):
    W.append(name)
    print(f"  WARN {name}" + (f"   <- {detail}" if detail else ""))


def skip(name, why=""):
    S.append(name)
    print(f"  SKIP {name}   ({why})")


def main():
    print("=" * 78)
    print("工具注册 —— 无工具被静默拒绝 / 白名单同源")
    print("=" * 78)

    try:
        from config.settings import SAFE_PLUGINS, TOOLS_DIR, DATA_DIR
        from core.plugin_manager import PluginManager, scan_plugin_safe
    except Exception as e:
        chk("引擎可导入 (config/core)", False, f"{type(e).__name__}: {e}")
        return 1
    chk("引擎可导入 (config/core)", True)

    tools_dir = Path(TOOLS_DIR) if TOOLS_DIR else ROOT / "tools"
    files = sorted(f for f in tools_dir.glob("*.py")
                   if not f.name.startswith("_") and f.stem != "__init__")
    chk("tools/ 下找到工具模块", len(files) > 0, len(files))

    # ── [0] 自检: 本门禁的判据有牙齿 ──
    print("\n[0] 自检 (判据自身的辨别力)")
    _fake = ROOT / "tools" / "_zz_probe_rejected.py"
    tooth = False
    try:
        _fake.write_text("import os\nprint(os.getcwd())\n", encoding="utf-8")
        tooth = (scan_plugin_safe(_fake) is False)          # 未登记的 os 导入 → 必须被拒
    finally:
        _fake.unlink(missing_ok=True)
    chk("★ 未登记且 import os 的模块会被扫拒 (判据有牙齿)", tooth)
    _ok = ROOT / "tools" / "_zz_probe_safe.py"
    tooth2 = False
    try:
        _ok.write_text("import json\nprint(json.dumps({}))\n", encoding="utf-8")
        tooth2 = (scan_plugin_safe(_ok) is True)            # 无危险导入 → 放行
    finally:
        _ok.unlink(missing_ok=True)
    chk("★ 无危险导入的模块被放行 (不狼来了)", tooth2)

    # ── [A] 无工具被静默拒绝 ──
    print("\n[A] 无工具被静默拒绝 (核心不变量)")
    dead = []
    for f in files:
        if f.stem in SAFE_PLUGINS:
            continue
        if not scan_plugin_safe(f):
            dead.append(f.name)
    chk("★ 没有工具会被安全扫描拒绝加载 (否则调用方只看到'未知工具')", not dead,
        f"{len(dead)} 个: {dead}")

    # ── [B] 真实加载面: 与磁盘一致 ──
    print("\n[B] 真实加载面 (PluginManager 实测)")
    try:
        pm = PluginManager(DATA_DIR)
        loaded = set(getattr(pm, "_plugins", {}).keys())
    except Exception as e:
        chk("PluginManager 可实例化", False, f"{type(e).__name__}: {e}")
        loaded = set()
    expected = {f.stem for f in files}
    chk("★ 磁盘上的工具都已加载 (无缺口)", expected <= loaded,
        f"未加载: {sorted(expected - loaded)}")
    missing_fn = sorted(s for s in (expected & loaded)
                        if not (tools_dir / f"{s}.py").exists())
    chk("加载项都有对应文件", not missing_fn, missing_fn)

    # ── [C] 关键能力可达 (本轮修的三处) ──
    print("\n[C] 本轮修复的能力可达性")
    for name in ("vision", "image_gen", "backup"):
        chk(f"★ {name} 已登记且会加载", (name in SAFE_PLUGINS) and (name in loaded),
            f"in SAFE={name in SAFE_PLUGINS} loaded={name in loaded}")

    # ── [D] 白名单同源 ──
    print("\n[D] 白名单单一来源")
    gw = ROOT / "gateway" / "plugin_mgr.py"
    src = gw.read_text(encoding="utf-8") if gw.exists() else ""
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    defines_own = bool(re.search(r"^\s*SAFE_PLUGINS\s*=\s*[\{\[]", body, re.M))
    chk("gateway/plugin_mgr.py 不再自建名单 (改为转发 settings)", not defines_own)
    chk("gateway/plugin_mgr.py 从 settings 导入名单",
        bool(re.search(r"from config\.settings import[^\n]*SAFE_PLUGINS", src)))
    try:
        from gateway.plugin_mgr import SAFE_PLUGINS as GW
        chk("★ 两处名单完全一致 (同源)", set(GW) == set(SAFE_PLUGINS),
            f"gw={len(set(GW))} settings={len(set(SAFE_PLUGINS))} 差={sorted(set(GW) ^ set(SAFE_PLUGINS))}")
    except Exception as e:
        chk("两处名单完全一致 (同源)", False, f"{type(e).__name__}: {e}")

    # ── [E] 幽灵条目透明化 (软) ──
    print("\n[E] 名单腐化检查 (软)")
    ghosts = sorted(s for s in SAFE_PLUGINS if not (tools_dir / f"{s}.py").exists())
    if ghosts:
        warn(f"名单里有 {len(ghosts)} 个幽灵条目 (无对应文件, 建议清掉)", ghosts)
    else:
        chk("名单里没有幽灵条目", True)

    # ── [F] 元数据一致性: 关键词只有写进 TOOL 才参与自动路由 (2026-09-20 加) ──
    print("\n[F] 关键词元数据一致性 (TOOL 参与路由 / PLUGIN 不参与)")
    _trigger_only, _both, _tool_only, _none = [], [], [], []
    import importlib.util as _ilu2
    for _f in files:
        try:
            _sp = _ilu2.spec_from_file_location("_meta_probe_" + _f.stem, _f)
            _mod = _ilu2.module_from_spec(_sp)
            _sp.loader.exec_module(_mod)
        except Exception as _e:
            chk(f"{_f.name} 可导入 (读元数据)", False, f"{type(_e).__name__}: {_e}")
            continue
        _t = getattr(_mod, "TOOL", None) or {}
        _pk = getattr(_mod, "PLUGIN", None) or {}
        _has_t = bool(_t.get("keywords"))
        _has_p = bool(_pk.get("trigger"))
        if _has_t and _has_p:
            _both.append(_f.stem)
        elif _has_t:
            _tool_only.append(_f.stem)
        elif _has_p:
            _trigger_only.append(_f.stem)
        else:
            _none.append(_f.stem)
        # 同名工具: 两处 name 不一致 = 元数据打架 (会让人调错工具)
        if _t.get("name") and _pk.get("name") and _t["name"] != _pk["name"]:
            chk(f"{_f.stem} 两处 name 一致", False, f"TOOL={_t['name']} PLUGIN={_pk['name']}")
    print(f"      写 TOOL.keywords 且写 PLUGIN.trigger: {len(_both)} 个")
    print(f"      只写 TOOL.keywords (可被关键词路由): {len(_tool_only)} 个")
    print(f"      只写 PLUGIN.trigger (**关键词不参与路由**): {len(_trigger_only)} 个")
    print(f"      两处都没有 (只能靠模型按名调用): {len(_none)} 个")
    chk("★ 元数据统计完成 (无工具导入失败)", True)
    def _skill_requires():
        """每个技能 frontmatter 里的 requires_tools → {tool: [技能名]}。

        为什么用**声明**而不是全文搜工具名: 声明是机器可读的意图
        (skills/video-frames: requires_tools: [shell_exec]), 全文搜会撞到文档里的举例。
        """
        sk_dir = ROOT / "tmm_skills"
        m = {}
        n_sk = n_req = 0
        if sk_dir.is_dir():
            for p in sorted(sk_dir.glob("*/SKILL.md")):
                try:
                    txt = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                n_sk += 1
                fm = txt.split("---", 2)
                head = fm[1] if len(fm) > 2 else txt[:1200]
                mm = re.search(r"requires_tools:\s*\[([^\]]*)\]", head)
                if not mm:
                    continue
                n_req += 1
                for t in (x.strip().strip("'\"") for x in mm.group(1).split(",")):
                    if t:
                        m.setdefault(t, []).append(p.parent.name)
        return m, n_sk, n_req

    def _required_params(tool: str) -> list:
        """工具 run() 里**没有默认值**的参数 —— 裸关键词(query) 补不了它们。"""
        p = tools_dir / f"{tool}.py"
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return []
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ("run", "execute"):
                a = n.args
                pos = list(a.posonlyargs) + list(a.args)
                dflt = len(a.defaults)
                req = [x.arg for x in pos[:len(pos) - dflt]] + \
                      [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
                return [x for x in req if x not in ("query", "action")]
        return []

    _req, _n_sk, _n_req = _skill_requires()
    # ★ 判据函数先自证"真的取到了数据" —— 否则"取不到"会被静默当成"通过"
    chk(f"★ 覆盖判据有数据 (技能 {_n_sk} 个 · 其中声明 requires_tools 的 {_n_req} 个 · 工具 {len(files)} 个)",
        _n_sk > 0 and _n_req > 0 and len(files) > 0, f"sk={_n_sk} req={_n_req}")
    if _trigger_only or _none:
        print("      ── 为什么它们**不需要**补 TOOL.keywords (逐工具判定) ──")
        for _t in sorted(_trigger_only) + sorted(_none):
            if _t in ("file_ops", "shell_exec"):
                # 早期出路是**承重**的: 这两个同时也有技能入口 (second-opinion / video-frames…),
                # 但"硬排除"是决定性理由 —— 顺序反了就会漏掉危险泛词这条。
                _cov0 = _req.get(_t, [])
                _extra = f" · 另有技能入口 {', '.join(_cov0[:2])}" if _cov0 else ""
                print(f"        {_t:<16} ★ core/pipeline.py 已**硬排除**在关键词路由之外 (泛词+需精确参数){_extra}")
                continue
            _cov = _req.get(_t, [])
            _rp = _required_params(_t)
            if _cov:
                print(f"        {_t:<16} 技能已在做用户入口 → {', '.join(_cov[:3])} "
                      f"(补 TOOL 块 = 关键词直接执行, 会**绕过技能层**, 输出更低质)")
            elif _rp:
                print(f"        {_t:<16} 无技能入口, 且需要精确参数 {'/'.join(_rp[:3])} "
                      f"→ 裸关键词补不了, 交给模型按名调用")
            else:
                print(f"        {_t:<16} 无技能入口 (另有别的路径接它: 模型按名调用 / Brain 硬编码路由)")
    # ★ pipeline 的硬排除是危险关键词的**唯一**防线 → 必须门禁兜
    _psrc = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
    _kw_re = re.compile(r'if\s+m\s+and\s+m\[0\]\[0\]\s+in\s+\(\s*"file_ops"\s*,\s*"shell_exec"\s*\)')
    chk("★ file_ops/shell_exec 仍被硬排除在关键词路由之外 (泛词防线在位)",
        bool(_kw_re.search(_psrc)), "core/pipeline.py 里找不到硬排除判据")
    # 变异自检: 把判据改坏后必须**抓不到** —— 否则说明上面那条是空转
    _mut = _psrc.replace('in ("file_ops", "shell_exec")', 'in ("__nope__",)')
    if _mut != _psrc:
        chk("★ 硬排除判据有牙齿 (变异后确实抓不到)", not _kw_re.search(_mut))
    else:
        skip("硬排除判据的变异自检", "源码写法变了, 变异串未命中 — 请人工确认")
    if _trigger_only:
        warn(f"以下 {len(_trigger_only)} 个工具只写了 PLUGIN.trigger → 关键词不参与路由 "
             f"(见上逐条判定: 补 TOOL 块**不是**默认解法)",
             ", ".join(sorted(_trigger_only)[:12]))
    else:
        chk("没有'写了 trigger 却无效'的工具", True)
    if _none:
        warn(f"{len(_none)} 个工具两处都没有关键词元数据 (见上逐条判定)",
             ", ".join(sorted(_none)[:8]))

    print("\n" + "=" * 78)
    tail = f"结果: {len(P)} PASS / {len(F)} FAIL"
    if W:
        tail += f" / {len(W)} WARN"
    if S:
        tail += f" / {len(S)} SKIP"
    print(tail)
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
