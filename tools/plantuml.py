"""
PlantUML diagram generator — generates architecture, sequence, class, ER diagrams.
Uses local plantuml JAR (Java required). Saves result to desktop.
"""
import subprocess, os, time, tempfile, re

TOOL = {
    "name": "plantuml",
    "version": "2.1",
    "description": "Generate PlantUML diagrams locally (architecture/sequence/class/ER/flowchart). Saves PNG to desktop.",
    "requires": ["java"],
    "permission": ["system"],
    "category": "automation",
    "keywords": ["架构图", "流程图", "时序图", "类图", "ER图", "画图", "uml", "diagram", "puml", "生成图"],
    "params": [
        {"name": "code", "type": "str", "description": "PlantUML source code, e.g. '@startuml\\nAlice -> Bob: hello\\n@enduml'", "required": True},
        {"name": "format", "type": "str", "description": "Output format: png/svg/txt", "required": False},
        {"name": "output_name", "type": "str", "description": "Optional output filename (without extension). Default: tmm_puml_<timestamp>", "required": False},
    ],
}

JAR = os.path.join(os.path.dirname(__file__), "plantuml.jar")
OUTPUT_DIR = os.path.expanduser("~/Desktop")

async def run(code: str = "", format: str = "png", output_name: str = "", path: str = "", **kwargs):
    """生成 PlantUML 图。

    **kwargs 是**加性兼容**: 网关/命令层会统一带 `action=` 之类的关键字, 严格签名会让
    工具在统一调用约定下直接 TypeError (实测: "run() got an unexpected keyword argument
    'action'") → 该工具对 Agent 实际不可调用。未声明的额外关键字忽略, 不改变既有行为。
    """
    if not os.path.exists(JAR):
        return {"success": False, "error": f"plantuml.jar not found at {JAR}"}
    
    # ★ 别名兼容: 技能/调用方可能写成 content / uml (实测 diagram 技能就写成 content →
    #   源码传不进去, 工具报 "No PlantUML code provided")。加性别名, 不改变既有行为。
    if not code:
        code = str(kwargs.get("content") or kwargs.get("uml") or "").strip()
    if not code:
        return {"success": False, "error": "No PlantUML code provided (参数名应为 code)"}

    # ★ 输出位置: 支持用户给**目录**或**文件路径** (深测: 原来永远落 ~/Desktop, 无视用户指定)
    out_dir = OUTPUT_DIR
    out_name_from_path = ""
    _raw = (path or "").strip().strip('"\'“”')
    if _raw:
        if _raw.endswith(("/", "\\")) or os.path.isdir(_raw):
            out_dir = _raw
        else:
            _d, _f = os.path.split(_raw)
            out_dir = _d or OUTPUT_DIR
            out_name_from_path = os.path.splitext(_f)[0]
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            out_dir = OUTPUT_DIR
    
    # Ensure @startuml/@enduml
    code = code.strip()
    if not code.startswith("@startuml"):
        code = "@startuml\n" + code
    if not code.endswith("@enduml"):
        code = code + "\n@enduml"
    
    # Write temp file
    t = int(time.time())
    tmp_uml = os.path.join(tempfile.gettempdir(), f"tmm_puml_{t}.puml")
    with open(tmp_uml, 'w', encoding='utf-8') as f:
        f.write(code)
    
    try:
        # ★★ 2026-09-21 三处必修 (用户实测: "架构图全是乱码"):
        #  ① -syntax 预检: 语法错时 PlantUML **返回码 200 且照样生成一张"错误页 PNG"** ——
        #     老代码看到文件存在就报 "Diagram saved", 用户收到一张错误页却被告知已保存
        #     (典型的静默失败)。先用 -syntax 预检(不产图、快), 拿到**行号**再决定要不要画。
        #  ② -charset UTF-8: .puml 按 UTF-8 写, 但本机 JVM 默认 GBK
        #     (实测 java -XshowSettings:properties → file.encoding=GBK) →
        #     中文被按 GBK 解 → "涓绘絵鍦夊亾" 式乱码。必须显式告诉 PlantUML 源编码。
        #  ③ -failfast2: 真渲染时若仍出错, 不产错误页 (兜底, 防"文件存在=成功"的老逻辑复活)。
        _JAVA = ["java", "-Dfile.encoding=UTF-8", "-jar", JAR, "-charset", "UTF-8"]
        _chk = subprocess.run(_JAVA + ["-syntax"], input=code, capture_output=True,
                              text=True, timeout=30, encoding="utf-8", errors="replace")
        _cout = ((_chk.stdout or "") + (_chk.stderr or "")).strip()
        if _chk.returncode != 0 or _cout.upper().startswith("ERROR"):
            # -syntax 的 ERROR 输出形如: ERROR\n<行号>\n<原因>
            _lines = [x.strip() for x in _cout.splitlines() if x.strip()]
            _ln = next((x for x in _lines[1:2] if x.isdigit()), "")
            _why = _lines[2] if len(_lines) > 2 else (_lines[-1] if _lines else "")
            _where = f"第 {_ln} 行" if _ln else "解析阶段"
            return {
                "success": False,
                "error": f"PlantUML 语法错误 ({_where}): {_why}",
                "degrade_reason": f"没画出图 —— 生成的 PlantUML 源码在{_where}有语法错误: {_why}",
                "diag": _cout[:800],
                "syntax_error_line": int(_ln) if _ln.isdigit() else 0,
            }

        result = subprocess.run(
            _JAVA + ["-failfast2", "-t" + format, "-output", out_dir, tmp_uml],
            capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace',
            cwd=out_dir
        )
        _diag = ((result.stdout or "") + (result.stderr or "")).strip()
        # 真渲染仍报错 (预检漏掉的) → 一样说实话, 不假装成功
        _m = re.search(r"Error line (\d+)", _diag)
        if result.returncode != 0 or _m:
            _where = f"第 {_m.group(1)} 行" if _m else "渲染阶段"
            return {
                "success": False,
                "error": f"PlantUML 语法/解析错误 ({_where}): {_diag[:400]}",
                "degrade_reason": f"没画出图 —— 生成器在{_where}被卡住",
                "diag": _diag[:800],
            }
        
        # Find generated file
        base = os.path.splitext(os.path.basename(tmp_uml))[0]
        generated_path = os.path.join(out_dir, base + "." + format)
        output_path = generated_path
        # 名字优先级: path 里的文件名 > output_name (都在 out_dir 内, 不再写回桌面)
        _want = out_name_from_path or (os.path.splitext(output_name)[0] if output_name else "")
        if _want:
            target = os.path.join(out_dir, _want + "." + format)
            # ★ 用户**明确给的文件名** → 覆盖 (他说"存到 arch.png"就是要那个文件);
            #   只有"给目录 + 默认名冲突"时才加时间戳避让 (不静默改名用户指定的文件)。
            if os.path.exists(target) and not out_name_from_path:
                target = os.path.join(out_dir, f"{_want}_{t}.{format}")
            if os.path.exists(generated_path):
                import shutil
                shutil.move(generated_path, target)
            output_path = target
        
        if os.path.exists(output_path):
            size = os.path.getsize(output_path)
            os.remove(tmp_uml)
            return {
                "success": True,
                "output": f"Diagram saved: {output_path} ({size//1024}KB)",
                "local_path": output_path,
                "format": format,
            }
        else:
            # rc=0 但没产物 (如 "No diagram found": 内容里没有图) → 也是失败, 不假装成功
            return {
                "success": False,
                "error": f"未生成图: {(_diag or '无输出')[:300]}",
                "diag": (_diag or "")[:800],
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "PlantUML generation timed out (30s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if os.path.exists(tmp_uml):
            try: os.remove(tmp_uml)
            except Exception: pass
