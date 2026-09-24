"""门禁单入口 (scripts/hermes_verify.py) 自身 — 常驻验证器

为什么要有这一份: 门禁是"改动即验证"的地基 —— 如果**门禁本身**会把红当绿、
会漏掉状态污染、或门禁清单被写死成硬编码, 那所有"全绿"都不可信 (garbage in)。
本验证器就是防这个的: 用临时探针门禁把 runner 的判红路径逐条走一遍。

跑法:
    python -B scripts/verification/verify_gate_runner.py

技术要点 (可复用于任何"验证器 runner"的验证):
  ① 用 **verify_zz_probe_*** 前缀造临时门禁, finally 里删除, 不污染真门禁目录
  ② 嵌套调用 runner 时**必须带 --only <probe 名>** —— 否则 runner 会再跑本验证器 →
     本验证器又调 runner …**无限递归** (这是本文件最大的坑)
  ③ D 项会故意污染 data/mode.json 来验"状态守卫" → 必须**自己先备份、finally 还原**
     (验证禁污染用户数据)
  ④ 退出码 / 超时标志 / 污染判定 都**真跑子进程**看输出, 不看源码猜
"""
import hashlib
import json
import os
import re
import subprocess
import subprocess as _sp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PY = sys.executable
RUNNER = ROOT / "scripts" / "hermes_verify.py"
BAT = ROOT / "verify.bat"
V_DIR = ROOT / "scripts" / "verification"
# ★ 2026-09-22: 本门自己就**逐条真跑子进程**, 墙钟 ~480s > runner 的全局预算 420s
#   ⇒ 一直被记成 [超时] 假红 (不是代码坏)。这里声明自己的预算下限, runner 自动读取;
#   别去改 runner 的全局值 (那会让真卡死的门禁也能一起拖 15 分钟)。
GATE_TIMEOUT = 900
STATE = [ROOT / "data/mode.json", ROOT / "data/prefs.json"]
#: 本门禁自己创建的临时探针前缀 (与 hermes_verify._PROBE_PREFIX 同一约定);
#: 计数/清理时都要排除它 —— 它永远只是临时件。
_PROBE_PREFIX = "verify_" + "zz_probe_"

P, F, S = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <- {detail}" if detail and not cond else ""))


def skip(name: str, why: str = "") -> None:
    S.append(name)
    print(f"SKIP {name}" + (f"   <- {why}" if why else ""))


def md5(p: Path) -> str:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except Exception:
        return "-"


def run_runner(*args, timeout=300):
    """★ 永远只传 --only <probe 名> / 便宜的单门禁 —— 禁止无参调用 (会递归)"""
    r = subprocess.run([PY, "-B", str(RUNNER), *args], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


_BAK = {p: (p.read_bytes() if p.exists() else None) for p in STATE}
_made: list[Path] = []


def mk_probe(name: str, body: str) -> Path:
    p = V_DIR / f"verify_zz_probe_{name}.py"
    p.write_text(body, encoding="utf-8")
    _made.append(p)
    return p


def main() -> int:
    try:
        # ★ 2026-09-23 加固: 先清掉上次**崩溃/被 kill** 留下的临时探针。
        #   实测踩到: 一次被 kill 的运行留下 verify_zz_probe_*.py →
        #   下面 on_disk 数成 46 而 --list 是 45 ⇒ 假 FAIL (不是产品坏, 是残渣)。
        #   这些文件永远只由本门禁创建、也永远在 finally 里删 —— 启动时清理是安全的。
        for _stale in V_DIR.glob(f"{_PROBE_PREFIX}*.py"):
            try:
                _stale.unlink()
            except Exception:
                pass
        print("=" * 74)
        print("A. 门禁清单必须是**自动发现**的 (写死清单 = 加一条就假 FAIL)")
        print("=" * 74)
        # ★ 计数时也排除探针前缀 (双保险: 万一有半成品残留在 glob 窗口内)
        on_disk = len([p for p in V_DIR.glob("verify_*.py")
                       if not p.name.startswith(_PROBE_PREFIX)])
        rc, out = run_runner("--list")
        listed = len(re.findall(r"^  verify_", out, re.M))
        chk("--list 退出 0", rc == 0, f"exit={rc}")
        chk(f"--list 条数 == 磁盘上的 verify_*.py ({on_disk})", listed == on_disk, f"listed={listed}")
        src = RUNNER.read_text(encoding="utf-8")
        hard = [n for n in re.findall(r"verify_[a-z_]+", src) if n not in ("verify_zz_probe_",)]
        # 允许 SLOW_HINTS 里的"慢门禁标注"(不是清单本身); 其余具体门禁名出现 = 写死信号
        slow = tuple(re.findall(r'SLOW_HINTS = \(([^)]*)\)', src)[0].replace('"', "").split(", ")) if "SLOW_HINTS = (" in src else ()
        hard = [n for n in hard if n not in slow]
        chk("★ 源码里没有写死的门禁名", not hard, f"写死了: {hard}")
        mk_probe("alpha", 'print("结果: 1 PASS / 0 FAIL")\n')
        rc, out = run_runner("--list")
        chk("★ 新验证器自动纳入 (不用改 runner)", "verify_zz_probe_alpha" in out and rc == 0)
        os.unlink(_made.pop())

        print()
        print("=" * 74)
        print("B. 退出码契约: 全绿 0 / 有红 1, 且不许把红当绿")
        print("=" * 74)
        rc_ok, _ = run_runner("--only", "skill_format")
        chk("★ 全绿 → exit 0", rc_ok == 0, f"exit={rc_ok}")
        mk_probe("red", 'print("FAIL 故意红")\nprint("结果: 1 PASS / 1 FAIL")\nimport sys; sys.exit(1)\n')
        rc_bad, out_bad = run_runner("--only", "zz_probe_red")
        chk("★ 有红 → exit 1", rc_bad == 1, f"exit={rc_bad}")
        chk("★ 有红时不得出现'全绿'", "全绿" not in out_bad)
        chk("★ 具体失败项被打出", "故意红" in out_bad)
        os.unlink(_made.pop())
        mk_probe("boom", 'raise RuntimeError("崩")\n')
        rc_b, out_b = run_runner("--only", "zz_probe_boom")
        chk("★ 门禁崩溃 → 判红 (0 PASS / 1 FAIL), 不当绿", rc_b == 1 and "0 PASS /  1 FAIL" in out_b, out_b[-200:])
        os.unlink(_made.pop())

        print()
        print("=" * 74)
        print("C. 超时: 判红 **且** 在汇总里可辨认 (曾只有'红', 分不出是超时)")
        print("=" * 74)
        mk_probe("slow", 'import time; time.sleep(30)\nprint("结果: 1 PASS / 0 FAIL")\n')
        rc_t, out_t = run_runner("--only", "zz_probe_slow", "--timeout", "3")
        chk("★ 超时 → exit 1", rc_t == 1, f"exit={rc_t}")
        chk("★ 超时可辨认 ([超时] 标签 or TIMEOUT 字样)", "[超时]" in out_t or "TIMEOUT" in out_t, out_t[-260:])
        os.unlink(_made.pop())

        print()
        # ★ 2026-09-23: 交付包里没有 data/mode.json (运行时状态, 不进包)。
        #   此时"污染检测"无从谈起 ⇒ 整段 SKIP, 不许因此报红
        #   (否则用户拿到的包一跑就是红, 分不清"没首次运行"和"功能坏")。
        if not STATE[0].exists():
            skip("D 段状态守卫", "本机没有 data/mode.json (交付包未首次运行) —— 非缺陷")
        else:
            print("D. ★★ 状态守卫: 门禁污染用户数据 → 即使自报全绿也必须判红")
            print("=" * 74)
            mk_probe("dirty", (
                'import json\n'
                f'p = r"{ROOT / "data" / "mode.json"}"\n'
                'd = json.load(open(p, encoding="utf-8"))\n'
                'd["mode"] = "ask"\n'
                'json.dump(d, open(p, "w", encoding="utf-8"))\n'
                'print("结果: 1 PASS / 0 FAIL")\n'
            ))
            m_before = md5(STATE[0])
            # ★ 前提必须**独立**证明: 直接跑一次探针 (不经 runner) —— 经 runner 跑的话,
            #   runner 自己会在退出时还原 mode.json, 于是事后比 md5 就"看不出被改过",
            #   前提检查会假红 (实测: 加了"跑前钉 craft+跑完还原"之后就是这样红的)。
            _probe_path = _made[-1]
            _sp.run([sys.executable, _probe_path], capture_output=True, text=True)
            chk("(前提) 探针确实改了 mode.json", md5(STATE[0]) != m_before, "探针无效, 后面结论不可信")
            if _BAK[STATE[0]] is not None:
                STATE[0].write_bytes(_BAK[STATE[0]])          # 手动还原, 保持后续前提干净
            rc_d, out_d = run_runner("--only", "zz_probe_dirty")
            chk("★ 污染被检出 (报'状态污染')", "状态污染" in out_d, out_d[-360:])
            chk("★ 自报全绿仍判红 → exit 1", rc_d == 1, f"exit={rc_d}")
            os.unlink(_made.pop())
            if _BAK[STATE[0]] is not None:
                STATE[0].write_bytes(_BAK[STATE[0]])
            chk("★ mode.json 已还原 (验证不污染用户数据)",
                md5(STATE[0]) == hashlib.md5(_BAK[STATE[0]]).hexdigest(), md5(STATE[0])[:8])

        print()
        print("=" * 74)
        print("E. verify.bat: 纯 ASCII + 真跑无 cmd 解析报错 + 退出码透传")
        print("=" * 74)
        raw = BAT.read_bytes()
        non_ascii = [b for b in raw if b > 127]
        chk("★ 纯 ASCII (中文注释会被 cmd.exe 按 GBK 曲解并当命令执行)",
            not non_ascii, f"{len(non_ascii)} 个非 ASCII 字节")
        chk("定位项目用 pushd %~dp0", b"%~dp0" in raw)
        chk("退出码透传 (exit /b %RC%)", b"exit /b %RC%" in raw)
        r = subprocess.run(["cmd.exe", "/c", "verify.bat --only skill_format"], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        bat_out = (r.stdout or "") + (r.stderr or "")
        chk("★ 真跑 bat 无 cmd 报错", "不是内部或外部命令" not in bat_out, bat_out[:160])
        chk("★ bat 全绿时退出码 0", r.returncode == 0, f"exit={r.returncode}")

        print()
        print("=" * 74)
        print("F. 过滤生效 + runner 自身零副作用")
        print("=" * 74)
        rc, out = run_runner("--only", "skill_format")
        chk("--only 只跑命中项 (+canonical)", "门禁 2 个" in out, out[-160:])
        rc, out = run_runner("--quick", "--only", "office_generation")
        chk("--quick 跳过慢门禁", "门禁 1 个" in out, out[-160:])
        same = all(md5(p) == (hashlib.md5(_BAK[p]).hexdigest() if _BAK[p] else "-") for p in STATE)
        chk("★ 跑完用户状态文件与跑前一致", same, str([(p.name, md5(p)[:8]) for p in STATE]))
    finally:
        for p in _made:
            try:
                os.unlink(p)
            except Exception:
                pass
        for p, b in _BAK.items():
            try:
                if b is not None:
                    p.write_bytes(b)
            except Exception:
                pass
        left = [p.name for p in V_DIR.glob("verify_zz_probe_*")]
        print()
        print(f"[cleanup] 临时探针残留: {left or '无'}")
        # ★ 2026-09-23 修 (交付包内复核实测): 这里原来**直接读 mode.json**,
        #   而交付包里没有这个文件 (它是运行时状态, 不进包) ⇒ FileNotFoundError
        #   ⇒ 门禁全部跑完却在收尾崩掉: 没有"结果:"行、只剩 exit 1 —— 看起来像功能坏。
        #   收尾打印**绝不许**让门禁判红: 一律 try 包住。
        try:
            _mj = STATE[0]
            _cur = json.loads(_mj.read_text(encoding="utf-8")).get("mode") if _mj.exists() else "(无 mode.json)"
            print(f"[cleanup] 用户 mode = {_cur}")
        except Exception as _ce:
            print(f"[cleanup] 用户 mode 读取跳过: {type(_ce).__name__}")

    print()
    print("=" * 74)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(S)} SKIP" if S else ""))
    if F:
        print("失败: " + " · ".join(F))
    print("=" * 74)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
