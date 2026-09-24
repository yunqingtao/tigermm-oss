"""PlantUML 工具门禁 (2026-09-21 立, 用户实测"架构图全是乱码")

挡的是什么
═══════════════════════════════════════════════════════════════════
两个真 bug, 都是用户在真机上撞到的:

① 乱码: .puml 按 UTF-8 写, 但本机 JVM 默认 GBK
   (实测 `java -XshowSettings:properties` → file.encoding=GBK)
   → 中文被按 GBK 解 → "涓绘絵鍦夊亾" 式乱码。
   修法: 调用必须带 `-charset UTF-8` (+ `-Dfile.encoding=UTF-8` 让报错也可读)。

② ★静默失败: 语法错误时 PlantUML **返回码 200 且照样生成一张"错误页 PNG"** ——
   老代码只判"文件在不在", 于是报 "Diagram saved": 用户拿到一张错误页,
   却被告知已保存。修法: `-syntax` 预检 (拿到行号) + `-failfast2` 兜底,
   失败就返回 success=False 并说清第几行, 由降级机制告诉用户。

本门禁钉住这两条, 并用**变异测试**证明守卫是承重的 (去掉预检 → 立刻回到"撒谎"行为)。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
PY = sys.executable
JAR = ROOT / "tools" / "plantuml.jar"
SRC = ROOT / "tools" / "plantuml.py"

P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"   <- {detail}" if detail and not cond else ""))


GOOD = ("@startuml\nskinparam shadowing false\n"
        "[用户服务] as UserSvc\n[订单服务] as OrderSvc\n"
        "UserSvc --> OrderSvc: 下单\ndatabase \"主库\" as MainDB\nOrderSvc --> MainDB\n@enduml")
BAD = "@startuml\n[用户服务] as A\n[订单服务 as B\nB --> C\n@enduml"


def main() -> int:
    print("=" * 78)
    print("PlantUML 门禁 —— 中文不乱码 + 语法错不撒谎")
    print("=" * 78)

    print("\n[A] 源码: 两处守卫都在")
    src = SRC.read_text(encoding="utf-8")
    chk("★ 带 -charset UTF-8 (否则本机 GBK 读 UTF-8 源 → 乱码)",
        re.search(r'"-charset",\s*"UTF-8"', src) is not None)
    chk("★ 带 -Dfile.encoding=UTF-8 (报错信息也可读)",
        "-Dfile.encoding=UTF-8" in src)
    chk("★ 有 -syntax 预检 (拿行号 + 不产错误页)", '"-syntax"' in src)
    chk("★ 有 -failfast2 兜底 (出错不产错误页)", '"-failfast2"' in src)
    chk("★ 失败路径返回 success=False + 行号", "syntax_error_line" in src and '"success": False' in src)
    chk("★ 不再有'文件存在即成功'的老逻辑 (要判返回码)",
        re.search(r"if result\.returncode != 0 or _m:", src) is not None)

    if not JAR.exists():
        print("\n★ plantuml.jar 不在 → 跳过真跑 (但上面的静态断言已生效)")
    else:
        sbx = Path(tempfile.mkdtemp(prefix="_vfy_puml_"))
        try:
            import asyncio
            import importlib.util
            spec = importlib.util.spec_from_file_location("tmm_puml_gate", SRC)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            print("\n[B] 真跑: 正常图 (中文必须能画出来)")
            r = asyncio.run(mod.run(code=GOOD, path=str(sbx / "架构图.png")))
            png = sbx / "架构图.png"
            chk("★ 画成功", r.get("success") is True, r)
            chk("★ 产物存在且非空", png.is_file() and png.stat().st_size > 2000,
                png.stat().st_size if png.is_file() else "无")

            print("\n[C] 真跑: 语法错误 (必须诚实报错, 不许撒谎)")
            r2 = asyncio.run(mod.run(code=BAD, path=str(sbx / "坏图.png")))
            chk("★★ 语法错 → success=False", r2.get("success") is False, r2)
            chk("★★ 报出了行号 (对用户可定位)", int(r2.get("syntax_error_line") or 0) > 0, r2.get("syntax_error_line"))
            chk("★★ 不留错误页 PNG (老逻辑就是靠'文件在'谎报成功)",
                not (sbx / "坏图.png").exists())
            chk("★ 带 degrade_reason (降级机制能用它告诉用户)", bool(r2.get("degrade_reason")))

            print("\n[D] ★★ 变异测试: 去掉预检 → 必须复现'撒谎'行为 (证明守卫承重)")
            mut_dir = sbx / "mut"; mut_dir.mkdir()
            mut_src = src.replace('_JAVA + ["-syntax"]', '_JAVA + ["-__disabled__"]')
            mut_src = mut_src.replace('if _chk.returncode != 0 or _cout.upper().startswith("ERROR"):',
                                      'if False:')
            mut_src = mut_src.replace('"-failfast2", ', '')
            mut_src = re.sub(r'if result\.returncode != 0 or _m:', 'if False:', mut_src)
            mut_src = mut_src.replace(f'JAR = os.path.join(os.path.dirname(__file__), "plantuml.jar")',
                                      f'JAR = r"{JAR}"')
            mp = mut_dir / "plantuml_mut.py"
            mp.write_text(mut_src, encoding="utf-8")
            spec2 = importlib.util.spec_from_file_location("tmm_puml_mut", mp)
            mut = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(mut)
            r3 = asyncio.run(mut.run(code=BAD, path=str(mut_dir / "坏图.png")))
            lied = (r3.get("success") is True) and (mut_dir / "坏图.png").exists()
            chk("★★ 去掉守卫后: 谎报成功 + 留下错误页 (即本门禁挡的正是这个东西)",
                lied, f"success={r3.get('success')} png={(mut_dir/'坏图.png').exists()}")
        finally:
            shutil.rmtree(sbx, ignore_errors=True)

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for x in F:
        print("  -", x)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
