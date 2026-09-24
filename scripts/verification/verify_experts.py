"""角色专家层 — 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

为什么要有这一门 (2026-09-20, 借鉴通用 Agent 平台的 "Experts 面")
═══════════════════════════════════════════════════════════════════
三层分工: 技能=怎么做 · 工具=用什么做 · 专家=以什么身份做。
本门守四件事:
  ① **加性**: 没命中专家 → 不追加任何东西 (系统提示逐字节不变, 既有行为不受影响)
  ② **不静默顶替**: `@很像但不存在` → 诚实标记 unknown (不假装成别的身份)
  ③ **不误伤**: 普通文本里的 `@` (发给 @老板 一封邮件) → 必须 no-match
     (实测踩过: 判据太松时 "老板" 撞上 "老师" 被判成笔误专家)
  ④ **接得通**: /experts 命令已注册 · 人设能渲染

自清理: 全程只读 (专家是纯文本配置, 本门不写任何文件)。
"""
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PY = sys.executable
P, F, S, W = [], [], [], []


def chk(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS " if c else "  FAIL ") + n + (("   <- " + str(d)) if d and not c else ""))


def warn(n, d=""):
    W.append(n)
    print(f"  WARN {n}" + (f"   <- {d}" if d else ""))


def fp_db(p):
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if not tabs:
        c.close()
        return ("err",)
    main = "messages" if "messages" in tabs else tabs[0]
    n = c.execute("SELECT COUNT(*) FROM " + main).fetchone()[0]
    mx = c.execute("SELECT MAX(rowid) FROM " + main).fetchone()[0]
    c.close()
    return (main, n, mx)


def main():
    print("=" * 78)
    print("角色专家层 — 加性 / 不顶替 / 不误伤 / 接得通")
    print("=" * 78)
    db = ROOT / "data" / "chat_sessions.db"
    db_before = fp_db(db)

    from core import experts as E

    # ── [0] 自检: 判据有牙齿 ──
    print("\n[0] 自检 (判据自身的辨别力)")
    chk("★ 专家目录存在", E.EXPERT_DIR.is_dir(), E.EXPERT_DIR)
    experts = E.load_all()
    chk(f"★ 载入了专家 ({len(experts)} 位)", len(experts) >= 1, sorted(experts))
    # ★ 探针自身修正: 原断言写的是"编辑距离"=1/=2 —— **算术就错了**
    #   (老板/老师 共享"老" → 距离其实是 1; 数据控/数据分析 是 2)。
    #   实测证明编辑距离不是好判据, 已改用共同前缀。断言跟着改测**真正在用的规则**。
    chk("★ 笔误判据认得出 @数据控 (与 数据分析 共同前缀 2)",
        E._looks_typo_of("数据控", "数据分析") is True)
    chk("★ 笔误判据不误伤 @老板 (与 老师 共同前缀 0)",
        E._looks_typo_of("老板", "老师") is False)
    chk("★ 短于 2 字不参与笔误判定", E._looks_typo_of("数", "数据分析") is False)

    # ── [A] 显式 @ 命中 ──
    print("\n[A] 显式 @ 命中")
    for name in sorted(experts):
        rec, why = E.match(f"@{name} 帮我看看这个")
        chk(f"@{name} → 命中本尊", rec is not None and str(rec["name"]) == name, f"{rec} {why}")
    any_rec = next(iter(experts.values()))
    alias = (any_rec.get("aliases") or [None])[0]
    if alias:
        rec2, why2 = E.match(f"@{alias} 干活")
        chk(f"别名 @{alias} 也命中", rec2 is not None and why2 == "explicit", why2)
    else:
        warn("没有专家定义别名, 别名分支未覆盖")

    # ── [B] 关键词命中 (无 @) ──
    print("\n[B] 关键词命中 (无 @)")
    kw_cases = [("帮我审一下这段代码", "审代码"), ("讲一道题给孩子听", "教学"),
                ("做条视频发抖音", "做片"), ("写段喊麦词", "写词")]
    for msg, want in kw_cases:
        rec, why = E.match(msg)
        chk(f"「{msg}」→ {want}", rec is not None and str(rec["name"]) == want and why == "keyword",
            f"{rec and rec.get('name')} / {why}")

    # ── [C] 不顶替: @像但不存在 → unknown ──
    print("\n[C] 不顶替 (@很像但不存在 → 诚实)")
    rec3, why3 = E.match("@数据控 帮我看看")
    chk("★ @数据控 → unknown (不是命中别人的)", rec3 is None and str(why3).startswith("unknown:"), why3)
    hint = E.hint_block("数据控")
    chk("★ 诚实提示里点名了「没有这个专家」", "没有" in hint and "数据控" in hint)
    chk("★ 提示里给了现有专家清单", "数据分析" in hint)

    # ── [D] 不误伤: 普通 @ ──
    print("\n[D] 不误伤 (普通文本里的 @)")
    for msg in ("发给 @老板 一封邮件", "抄送给 @张经理", "邮件发到 a@b.com", "今天天气怎么样",
                "把文件复制到桌面"):
        rec4, why4 = E.match(msg)
        chk(f"「{msg}」→ no-match", rec4 is None and why4 == "no-match", f"{rec4} / {why4}")

    # ── [E] 加性: 没命中就不追加 ──
    print("\n[E] 加性 (没命中 → 系统提示不变)")
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    chk("★ pipeline 里是 `if _exp:` 才追加 (纯追加)",
        bool(re.search(r"if _exp:\s*\n\s*system = system \+ ", src)))
    chk("★ 只在 unknown 时才加提示", "elif isinstance(_why, str) and _why.startswith(\"unknown:\")" in src)
    chk("★ 专家层异常被兜住 (不许影响主流程)",
        "expert layer skipped" in src and "except Exception as _ee" in src)
    nomsg = [m for m in ("今天天气怎么样", "帮我读一下 readme", "把文件复制到桌面")
             if E.match(m)[0] is not None]
    chk("★ 无专家语义的请求零追加", not nomsg, nomsg)

    # ── [F] 渲染与命令 ──
    print("\n[F] 渲染与命令")
    for name, rec in experts.items():
        blk = E.system_block(rec)
        bad = not blk or name not in blk
        chk(f"{name} 人设可渲染", not bad, blk[:80])
    chk("★ 渲染含安全兜底 (角色不改事实/安全规则)",
        "不改变事实与安全规则" in E.system_block(any_rec))
    lst = E.render_list()
    chk("★ 列表含全部专家名", all(n in lst for n in experts))
    chk("★ /experts 命令已注册 (route registry)",
        "cmd.experts" in src and "_run_cmd_experts" in src)

    # ── [G] 专家文件卫生 ──
    print("\n[G] 专家文件卫生")
    bad_files = []
    for p in sorted(E.EXPERT_DIR.glob("*.md")):
        t = p.read_text(encoding="utf-8-sig", errors="replace")
        for pat, label in ((r"C:\\Users\\\w+", "硬编码用户目录"),
                           (r"gho_[A-Za-z0-9]{10,}", "GitHub token"),
                           (r"sk-[A-Za-z0-9]{10,}", "API key"),
                           (r"1[3-9]\d{9}", "手机号")):
            if re.search(pat, t):
                bad_files.append(f"{p.name}:{label}")
    chk("★ 专家文件无凭据/个人路径", not bad_files, bad_files)
    yaml_ok = []
    for p in sorted(E.EXPERT_DIR.glob("*.md")):
        head = p.read_text(encoding="utf-8-sig").split("---")[1]
        try:
            import yaml
            yaml.safe_load(head)
        except Exception as e:
            yaml_ok.append(f"{p.name}: {str(e)[:50]}")
    chk("★ 全部 frontmatter YAML 可解析", not yaml_ok, yaml_ok)

    # ── [H] 真跑 /experts (经引擎) ──
    print("\n[H] 真跑 (经引擎)")
    try:
        import asyncio
        env = dict(os.environ, TMM_SESSION_DB=str(ROOT / "tmp" / "_exp_iso.db"),
                   TMM_GAP_DB=str(ROOT / "tmp" / "_exp_gap.db"),
                   TMM_PERCEPTION_FILE=str(ROOT / "tmp" / "_exp_perc.json"),
                   TMM_USAGE_LOG=str(ROOT / "tmp" / "_exp_usage.jsonl"),
                   TMM_ARTIFACTS_FILE=str(ROOT / "tmp" / "_exp_art.jsonl"))
        r = subprocess.run([PY, "-B", "-c",
                            "import sys,os,asyncio,logging;"
                            f"sys.path.insert(0,{str(ROOT)!r});os.chdir({str(ROOT)!r});"
                            "logging.disable(logging.CRITICAL);"
                            "from main import _load_config;from core.model_client import ModelClient;"
                            "from core.pipeline import Level4Pipeline;"
                            "cfg=_load_config();pl=Level4Pipeline(ModelClient(cfg),cfg);"
                            "r=asyncio.run(pl.process('/experts', mode_override='craft'));"
                            "print('OUT|'+(r.get('response') or '')[:200]);"
                            "print('ROUTE|'+str(r.get('route')))"],
                           cwd=str(ROOT), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        chk("★ /experts 真跑出清单 (不是报错)", "OUT|" in out and "专家" in out, out[-160:])
        chk("★ 路由是 cmd.experts", "ROUTE|cmd.experts" in out, out[-120:])
    except Exception as e:
        chk("真跑可执行", False, f"{type(e).__name__}: {e}")
    finally:
        # ★ 用 glob 清: SQLite 还会留 -wal/-shm 边车文件 (实测漏了这两个)
        for f in list((ROOT / "tmp").glob("_exp_*")):
            try:
                f.unlink()
            except Exception:
                pass

    # ── [I] 零副作用 ──
    print("\n[I] 零副作用")
    if db_before is not None:
        chk("★ 真对话库指纹未变", fp_db(db) == db_before, f"{db_before} → {fp_db(db)}")
    chk("专家层只读 (没产生候选技能/没改技能)",
        not list((ROOT / "data" / "skill_candidates").glob("*.md")) if
        (ROOT / "data" / "skill_candidates").is_dir() else True)
    chk("无 tmp 残留 (隔离文件已清)",
        not [p.name for p in (ROOT / "tmp").glob("_exp_*")], [p.name for p in (ROOT / "tmp").glob("_exp_*")])

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(W)} WARN" if W else ""))
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
