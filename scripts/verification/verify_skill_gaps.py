"""对照 OpenClaw(WorkBuddy) 补充的技能 — 常驻验证器 (2026-09-20)

批次 2 (同日补): session-logs / video-summarize / inbox-triage / pdf-extract /
  healthcheck / model-usage —— 另加三个新工具 (session_logs / service_check /
  usage_stats) 与一个 loader 修复 (文件正好以 `---` 结尾被静默丢弃)。
  新坑: ① 触发词又差字 ("提取这个PDF" vs "提取PDF") ② 文件以 --- 结尾整份丢弃
        ③ 插件安全扫描拒 os import (第一方工具须登记 SAFE_PLUGINS)
        ④ permission 值必须合法 (healthcheck 写了 safe → parse_error)

为什么要有这一份: 这 5 个技能是**照着 OpenClaw 内置技能清单挑的缺口**补的
(网页摘要 / 音频转写 / 视频抽帧 / 双模型审阅 / 电脑体检)。补的过程真踩了 6 类坑,
每一类都值得留住, 否则下次补技能还会踩:

  ★ 坑1 触发词差一个字: 写"给电脑做体检", 用户说"给电脑做个体检" → 技能永远接不到
       → 所以本验证器用**自然说法**测可达性, 不是拿 skills 里的 triggers 自证。
  ★ 坑2 漏写 frontmatter 结束 `---` → 静默跳过 (作者以为生效了)
  ★ 坑3 YAML 标量里裸 `: ` (中文常见) → YAML 炸, 旧日志还报成"没有 frontmatter"
  ★ 坑4 URL/媒体路径抽不到 → 技能拿到空参数 (url 恒空, .mp3/.mp4 不认)
  ★ 坑5 产出守卫过严 → 有素材要成品的请求被挡 ("总结这个网页 https://x")
  ★ 坑6 shell_exec 裸 `format` 一词两处误杀 (ffprobe format=duration / /bin/ 路径)
  ★ 坑7 _skill_match 全局 argmax 后再查触发 → 别的技能 tags 分高就把带触发的挤掉

覆盖: A 加载  B 自然说法可达性(★)  C 结构/工具齐备  D 真跑(无LLM的两个)  E 规则回归  F 零副作用
"""
import asyncio
import importlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

P, F = [], []
PY = sys.executable or "python"
NEW = ["summarize-web", "transcribe-audio", "video-frames", "second-opinion", "pc-checkup"]


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


def _fp(p: Path):
    return (p.stat().st_size, __import__("hashlib").md5(p.read_bytes()).hexdigest()[:12]) \
        if p.is_file() else None


def _db(name, tbl):
    try:
        c = sqlite3.connect(f"file:{ROOT / name}?mode=ro", uri=True)
        try:
            return (c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0],
                    c.execute(f"SELECT COALESCE(MAX(id),0) FROM {tbl}").fetchone()[0])
        finally:
            c.close()
    except Exception:
        return None


# 自然说法 (用户真会这么说的句子) → 期望接管的技能
NATURAL2 = [
    ("我们之前聊过双色球吗", "session-logs"),
    ("查一下聊天记录里有没有提过排期", "session-logs"),
    ("我说过什么关于备份的", "session-logs"),
    ("把这段视频转成文字摘要 D:/v/clip.mp4", "video-summarize"),
    ("这个视频讲了什么 D:/v/a.mp4", "video-summarize"),
    ("视频里说了什么 D:/v/b.mp4", "video-summarize"),
    ("帮我看看邮件", "inbox-triage"),
    ("有什么新邮件", "inbox-triage"),
    ("整理收件箱 D:/out/", "inbox-triage"),
    ("提取这个PDF D:/doc/a.pdf", "pdf-extract"),
    ("这个PDF讲了什么 D:/doc/b.pdf", "pdf-extract"),
    ("读这个PDF D:/doc/c.pdf", "pdf-extract"),
    ("服务还活着吗", "healthcheck"),
    ("服务都好吗", "healthcheck"),
    ("引擎还在吗", "healthcheck"),
    ("模型用量怎么样", "model-usage"),
    ("这个月花了多少", "model-usage"),
    ("哪个模型用得多", "model-usage"),
]

NEW2 = ["session-logs", "video-summarize", "inbox-triage", "pdf-extract",
        "healthcheck", "model-usage"]

# ── 批次 3 (2026-09-20): 本地播报 ──
#   speak-aloud: 把一段文字/一个文本文件用 edge-tts 念出来 (纯产出型, 无 llm 步骤)。
#   同批修掉 voice.py 两个真缺陷: 写死 playsound (本机没装 → 静默退 SAPI 机械音) /
#   不能念文件。详见 scripts/verification/verify_speak_skill.py
NEW3 = ["speak-aloud"]

# ── 批次 3 (2026-09-20): GitHub 巡检 ──
#   github-issues: github_api:digest → llm (查询类 answer_mode)。
#   配套第一方工具 tools/github_api.py (只依赖标准库 urllib, 不用 gh CLI)。
#   详见 scripts/verification/verify_github_skill.py
NEW4 = ["github-issues"]

NATURAL4 = [
    ("我的 github 有什么动静", "github-issues"),
    ("看看我的仓库", "github-issues"),
    ("待办 issue 有哪些", "github-issues"),
]

NATURAL3 = [
    ("念给我听：「今天天气不错」", "speak-aloud"),
    ("播报：会议三点开始", "speak-aloud"),
    ("说出来给我听：明天有雨", "speak-aloud"),
    ("读出来 版本已更新", "speak-aloud"),
]

NATURAL = [
    ("把这篇整理成摘要 https://example.com 存到 D:/x/", "summarize-web"),
    ("网页摘要 https://news.site.cn/a", "summarize-web"),
    ("转写这段录音 D:/rec/m.mp3", "transcribe-audio"),
    ("把录音转成文字 D:/rec/m.wav", "transcribe-audio"),
    ("从视频里抽帧 D:/v/clip.mp4", "video-frames"),
    ("视频取帧 D:/v/clip.mkv", "video-frames"),
    ("双模型审阅 D:/docs/draft.md", "second-opinion"),
    ("让另一个模型看看这份稿子 D:/docs/a.docx", "second-opinion"),
    ("给电脑做个体检", "pc-checkup"),
    ("电脑体检报告", "pc-checkup"),
]


def main() -> int:
    before = {n: _fp(ROOT / n) for n in ("data/chat_sessions.db", "data/gap_ledger.db")}
    sb = Path(tempfile.mkdtemp(prefix="_vfy_gaps_"))
    try:
        from core.skill_loader import SKILLS_DIR, get_skill_index
        eng = get_skill_index()
        idx = eng.get_index()

        # ── A 加载 ──
        print("        —— A 加载 (frontmatter / YAML / 结束标记) ——")
        # ★ 2026-09-24: 排除测试夹具 (下划线开头) —— 门禁自造, 清理失败不该连累本门禁
        disk = sorted(p.parent.name for p in Path(SKILLS_DIR).glob("*/SKILL.md")
                      if not p.parent.name.startswith("_"))
        missing = [n for n in disk if n not in idx]
        chk(f"磁盘 {len(disk)} 个技能全部加载 (无静默跳过)", not missing, f"没加载: {missing}")
        errs = {n: (eng.get_dag(n).parse_errors if eng.get_dag(n) else ["无DAG"]) for n in idx}
        chk("全部 0 解析错误", not {k: v for k, v in errs.items() if v}, str({k: v for k, v in errs.items() if v}))
        for n in NEW + NEW2 + NEW3 + NEW4:
            chk(f"{n} 已加载且有 steps", n in idx and bool(eng.get_dag(n) and eng.get_dag(n).steps))

        # ── B ★ 自然说法可达性 (坑1 的守) ──
        print("        —— B ★ 自然说法可达性 (不是拿 triggers 自证) ——")
        for msg, want in NATURAL:
            got = eng.match_context(msg, max_skills=1) or ""
            chk(f"「{msg[:26]}」→ {want}", want in got, got[:60])
        for msg, want in NATURAL2:
            got = eng.match_context(msg, max_skills=1) or ""
            chk(f"「{msg[:26]}」→ {want}", want in got, got[:60])
        for msg, want in NATURAL3:
            got = eng.match_context(msg, max_skills=1) or ""
            chk(f"「{msg[:26]}」→ {want}", want in got, got[:60])
        for msg, want in NATURAL4:
            got = eng.match_context(msg, max_skills=1) or ""
            chk(f"「{msg[:26]}」→ {want}", want in got, got[:60])
        # 负例: 读类请求不该被这 5 个抢
        for msg in ("总结一下这个文档", "帮我看看这份周报", "这个图是什么意思"):
            got = eng.match_context(msg, max_skills=1) or ""
            chk(f"★ 读类不该被抢: 「{msg}」", not any(n in got for n in NEW), got[:60])

        # ── C 结构 + 工具齐备 ──
        print("        ——— C 结构 / 依赖工具存在 ———")
        for n in NEW + NEW2 + NEW3 + NEW4:
            dag = eng.get_dag(n)
            fm = dag.raw_source or {}
            tools = [s.get("tool") for s in dag.steps]
            # ★ 断言要有辨别力: 每个**非内置**步骤工具都必须声明在 requires_tools 里
            #   (llm/knowledge 是内建步骤, 不需要声明)。原来的 `or` 写法近乎恒真, 已收紧。
            _declared = set(fm.get("requires_tools") or [])
            _needed = {t for t in tools if t not in ("llm", "knowledge")}
            _undeclared = sorted(_needed - _declared)
            chk(f"{n}: steps 用的工具都声明的 requires_tools", not _undeclared,
                f"没声明: {_undeclared} (声明了 {sorted(_declared)})")
            chk(f"{n}: 触发词 ≥5 且无单字", len(fm.get("triggers") or []) >= 5
                and all(len(t) >= 2 for t in (fm.get("triggers") or [])), str(fm.get("triggers")))
            bad_path = [s for s in dag.steps if "~/Desktop" in json.dumps(s, ensure_ascii=False)]
            chk(f"{n}: 无 POSIX 相对路径写法", not bad_path, str(bad_path)[:80])
        from core.skill_factory import SkillFactory
        cat = SkillFactory().catalog()
        for n in NEW + NEW2 + NEW3 + NEW4:
            dag = eng.get_dag(n)
            miss = [s.get("tool") for s in dag.steps
                    if s.get("tool") not in ("llm", "knowledge") and s.get("tool") not in cat]
            chk(f"{n}: 声明的工具都在工具目录里", not miss, f"缺: {miss}")

        # ── D 真跑 (挑不需要 LLM 的两个, 便宜且确定) ──
        print("        —— D ★ 真跑 (抽帧 / 体检采集, 不花模型调用) ——")
        from tools import shell_exec as SE
        clip = sb / "clip.mp4"
        r = asyncio.run(SE.run(command=f"ffmpeg -y -f lavfi -i testsrc=duration=8:size=320x240:rate=10 {clip.as_posix()}"))
        chk("测试视频已造出", clip.is_file() and clip.stat().st_size > 1000, str(r.get("error"))[:80])
        # 用技能里那条真命令 (参数替换成真路径), 验证命令本身能跑
        cmd_tpl = (eng.get_dag("video-frames").steps[0].get("input") or {}).get("command", "")
        real_cmd = cmd_tpl.replace("$params.path", clip.as_posix()).replace("$params.out", "")
        ok, why = SE._check_security(real_cmd)
        chk("★ 抽帧命令过安全闸 (不再被 format//bin/ 误杀)", ok, why)
        rr = asyncio.run(SE.run(command=real_cmd))
        out = str(rr.get("output") or rr.get("error"))
        frames = sorted((sb / "clip_frames").glob("*.jpg")) if (sb / "clip_frames").is_dir() else []
        chk(f"★ 真抽出 6 帧 (实测 {len(frames)})", rr.get("success") and len(frames) == 6, out[:160])
        # 体检: system_info 两个动作真能取到数据
        from core.tool_gateway import ToolGateway
        from core.plugin_manager import PluginManager
        from config.settings import DATA_DIR
        gw = ToolGateway()
        try:
            gw.plugin_mgr = PluginManager(DATA_DIR)
        except Exception:
            pass
        d1 = asyncio.run(gw.call("system_info", action="disk"))
        d2 = asyncio.run(gw.call("system_info", action="sysinfo"))
        chk("★ 体检第一步(磁盘)真取到数据", d1.get("success") and "盘" in str(d1.get("output")) or "GB" in str(d1.get("output")), str(d1)[:120])
        chk("★ 体检第二步(系统)真取到数据", d2.get("success") and len(str(d2.get("output"))) > 20, str(d2)[:120])

        # ── E 规则回归 (坑4/5/6/7) ──
        print("        ——— E 回归: 参数抽取 / 守卫 / shell 规则 ———")
        pl = __import__("core.pipeline", fromlist=["Level4Pipeline"]).Level4Pipeline.__new__(
            __import__("core.pipeline", fromlist=["Level4Pipeline"]).Level4Pipeline)
        pl._ke = None

        class _D:
            params_schema = {"url": {"type": "text"}, "path": {"type": "path"}, "out": {"type": "path"}}

        got = pl._skill_params("把这篇整理成摘要 https://a.b/c 存到 D:/o/", _D())
        chk("★ URL 抽得到 (坑4)", got.get("url") == "https://a.b/c", str(got))
        got2 = pl._skill_params("转写这段录音 D:/rec/a.mp3", _D())
        chk("★ 媒体路径抽得到 (坑4)", got2.get("path") == "D:/rec/a.mp3", str(got2))
        from core.pipeline import Level4Pipeline as LP
        chk("★ 带来源算要产出 (坑5)", LP._has_explicit_source("总结这个网页 https://x.cn/a")
            and not LP._looks_like_creation("总结这个网页 https://x.cn/a"))
        chk("★ 既有负例仍被排除 (坑5 反面)", not LP._has_explicit_source("总结一下这个文档"))
        ok1, _ = SE._check_security("ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 D:/a.mp4")
        chk("★ format=duration 不再被拦 (坑6)", ok1)
        ok2, _ = SE._check_security("format c:")
        chk("★ 格式化磁盘仍被拦 (坑6 反面)", not ok2)
        # 坑7: 触发优先于 tags 分
        hit = pl._skill_match("给电脑做个体检 存到 D:/x/", trigger_only=True)
        chk("★ 带触发的技能不被 tags 挤掉 (坑7)", bool(hit) and hit[0] == "pc-checkup",
            str(hit[0] if hit else None))

        # ── G 批次2 新增工具 (存在/只读/探测真有效) ──
        print("        ——— G 新工具: session_logs / service_check / usage_stats / github_api ———")
        from core.plugin_manager import PluginManager as PM
        from config.settings import SAFE_PLUGINS
        pm = PM(DATA_DIR)
        for t in ("session_logs", "service_check", "usage_stats", "github_api"):
            chk(f"工具文件存在: tools/{t}.py", (ROOT / "tools" / f"{t}.py").is_file())
            chk(f"{t} 已登记 SAFE_PLUGINS (含 os/socket import 的第一方工具须登记)",
                t in SAFE_PLUGINS)
        # session_logs 只读: 查询前后库 md5 必须一致
        import hashlib as _h
        from tools import session_logs as SL
        dep = os.environ.get("TMM_SESSION_DB")
        # ★ 单跑时没有隔离变量 (只有门禁跑才有) → 这里 SKIP 而不是 FAIL。
        #   教训: 验证器要**单跑和门禁跑都成立**; 依赖外部隔离变量的断言在单跑时会假红。
        if dep:
            chk("门禁跑: 对话库已隔离到沙箱 (TMM_SESSION_DB)", True)
            chk("隔离变量指向 tmp/沙箱而非真实 data/", "data" not in str(dep).replace("data\\", "")
                or "tmp" in str(dep).lower() or "Temp" in str(dep), str(dep))
        else:
            print("  SKIP 单跑模式 (无 TMM_SESSION_DB) —— 本验证器不调用 session_logs, 不碰真库")
        # service_check 真探一个自建监听端口
        import socket as _sock
        srv = _sock.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        _port = srv.getsockname()[1]
        try:
            from tools import service_check as SC
            rr = asyncio.run(SC.run(action="ports", extra=f"门禁自检:{_port}"))
            mine = [s for s in rr.get("services", []) if s["port"] == _port]
            chk("service_check 真探到自建监听端口", bool(mine) and mine[0]["up"] is True, str(mine))
            closed = _sock.socket(); closed.bind(("127.0.0.1", 0)); _cp = closed.getsockname()[1]; closed.close()
            rr2 = asyncio.run(SC.run(action="ports", extra=f"空:{_cp}"))
            chk("service_check 对关闭端口报不通",
                any(s["port"] == _cp and not s["up"] for s in rr2["services"]))
        finally:
            srv.close()
        # usage_stats: 探针不记账 (最要紧的一条)
        from core import model_client as _MC
        _utok = _MC.set_probe(True)
        try:
            chk("usage_stats: 探针态下 record_usage 拒记",
                _MC.record_usage("x", {"usage": {"total_tokens": 1}}, 0.1, []) is False)
        finally:
            _MC.reset_probe(_utok)

        # ── H 泛词 tag 守卫 (2026-09-20 实测踩到) ──
        #   tags 是弱线索(+2 分), 但**泛词会抢走无关请求**: pdf-extract 的 tag "整理"
        #   让「帮我把表格整理一下」被它接管 (既有路由基线因此红了)。
        #   这里对所有技能做静态检查: tag 不许是"单概念泛词"。
        print("        ——— H 泛词 tag 守卫 (防止抢走无关请求) ———")
        GENERIC = {"整理", "摘要", "记录", "历史", "统计", "服务", "邮件", "视频", "文字",
                   "提取", "文档", "数据", "信息", "报告", "内容", "文件", "图片", "分析"}
        offenders = []
        for n in (NEW + NEW2):
            fm = (eng.get_dag(n).raw_source or {})
            for t in (fm.get("tags") or []):
                if str(t).strip() in GENERIC:
                    offenders.append(f"{n}:{t}")
        chk("★ 新技能的 tags 无单概念泛词 (会抢无关请求)", not offenders, str(offenders))

        # ── F 零副作用 ──
        print("        ——— F 零副作用 ———")
        for n, v in before.items():
            chk(f"{n} 指纹未变", _fp(ROOT / n) == v, f"{v} -> {_fp(ROOT / n)}")
        chk("验证沙箱无残留", not list(sb.glob("*.docx")))
        chk("项目根没被写下东西", not (ROOT / "文档.txt").exists())
    finally:
        shutil.rmtree(sb, ignore_errors=True)

    print(f"\n结果: {len(P)} PASS / {len(F)} FAIL")
    for f in F:
        print("  -", f)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
