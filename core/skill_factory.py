"""技能工厂 — 把"一句话需求"编译成**可执行 DAG 技能**, 经质检线 + 人工确认门才生效。

与 Hermes/WorkBuddy 的技能生态的关键差异:
    · 它们的技能是**散文**, 行为由大模型现场决定 → 原理上**没法在装机前静态验证**。
    · 本工厂产出的是**声明式 DAG**(tool + input + on_error) → 可校验、可自测、可审计。
    这就是"技能工厂"的价值立足点: **可编译 = 可验证**。

流水线 (四道门, 缺一不可):
    ① 编译  propose()   LLM 依据**真实工具目录**生成 DAG 草案 (JSON), 不许凭空造工具
    ② 质检  validate()  结构/工具存在性/占位符一致/触发词冲突/重名
    ③ 自测  self_test() 用 stub 工具**真跑一遍** execute_dag → 每步都被调用 (证明链路可达)
    ④ 确认  promote()   ★ 人工门 —— 候选**不会**自动生效; 用户点头才移入 tmm_skills/

设计要点:
    · 候选池在 data/skill_candidates/, **绝不放进 tmm_skills/** ——
      零干扰: 未确认的技能不会污染技能发现/路由/reachability 门禁。
    · generate 依赖注入 → 离线可测 (pytest 不打网络)。
    · thinking 模型/JSON 围栏都要容忍 (LLM 会 ```json 包起来或加解释)。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "tmm_skills"
TOOLS_DIR = PROJECT_ROOT / "tools"
CANDIDATE_DIR = PROJECT_ROOT / "data" / "skill_candidates"

# 内建步骤 (不走 ToolGateway, 由 pipeline 直接实现) —— 与 skill_loader 保持一致
BUILTIN_STEPS = {"knowledge", "llm"}

_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_PROSE_KEYS = ("desc", "description")


# ────────────────────────────── 数据 ──────────────────────────────

@dataclass
class Proposal:
    name: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    triggers: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    requires_tools: list = field(default_factory=list)
    raw: str = ""                      # LLM 原始输出 (排查用)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_frontmatter(self) -> dict:
        fm = {"name": self.name, "version": "1.0.0",
              "description": self.description,
              "tags": list(self.tags), "triggers": list(self.triggers),
              "platforms": ["windows", "linux"]}
        if self.requires_tools:
            fm["requires_tools"] = list(self.requires_tools)
        if self.params:
            fm["params"] = self.params
        fm["steps"] = self.steps
        return fm


# ────────────────────────────── 工厂 ──────────────────────────────

class SkillFactory:
    """generate: async (model, messages, **kw) -> {"text": str, "error": ...}
    engine:   SkillWorkflowEngine (复用它的 _validate_dag / _KNOWN_TOOLS)
    """

    def __init__(self, generate=None, engine=None, tools_dir: Path = None,
                 skills_dir: Path = None, candidate_dir: Path = None, model: str = "deepseek",
                 gateway=None, smoke_dir: Path = None):
        self.generate = generate
        self.engine = engine
        # 真实质检用: 真 gateway (含真工具) + 实跑沙箱目录
        self.gateway = gateway
        self.smoke_dir = Path(smoke_dir or (PROJECT_ROOT / "tmp" / "_factory_smoke"))
        self.tools_dir = Path(tools_dir or TOOLS_DIR)
        self.skills_dir = Path(skills_dir or SKILLS_DIR)
        self.candidate_dir = Path(candidate_dir or CANDIDATE_DIR)
        self.model = model
        self._catalog = None

    # ── ① 可用积木 (真实工具目录, 不许凭空造) ──
    def catalog(self) -> dict:
        if self._catalog is None:
            cat = {}
            try:
                from core.tool_schema import load_tool_metadata
                cat = dict(load_tool_metadata(self.tools_dir))
            except Exception:
                cat = {}
            # ★ 兜底: 运行时"已知工具"里若有目录没收录的 (模块没声明 TOOL/PLUGIN),
            #   也补个名字条目 —— 否则工厂会误判"用了未知工具"而拒掉合法技能。
            #   ⚠ 这里只能用 _engine_known(), **不能**调 known_tools() ——
            #     后者会回头调 catalog() → 无限递归 (踩过)。
            for nm in sorted(set(BUILTIN_STEPS) | self._engine_known()):
                if nm not in cat:
                    _p = self.tools_dir / f"{nm}.py"
                    cat[nm] = {"name": nm, "description": "(模块未声明描述)",
                               "keywords": [], "params": [],
                               "path": str(_p) if _p.is_file() else "",
                               "declared_as": "-"}
            # ★ 标注"是否真有可调用入口 (run/execute)" —— 没有入口的模块 (office_cli /
            #   win32_input / 导入失败的 windows_desktop) 被调用时只会**谎报成功**,
            #   绝不能让它们进编译提示词或过质检。
            for nm, meta in cat.items():
                meta["callable"] = self._is_callable(nm, meta.get("path") or "")
                meta["actions"] = self._extract_actions(meta.get("path") or "")
            self._catalog = cat
        return self._catalog

    def _tool_callable(self, tool: str):
        if tool in BUILTIN_STEPS:
            return True
        meta = self.catalog().get(tool)
        return meta.get("callable") if meta else False

    @staticmethod
    def _extract_actions(path: str) -> list:
        """从工具模块里抽出它支持的 **action** 名。

        为什么需要: 多数工具的 PLUGIN/TOOL 声明**不写动作**, 而动作是真实入口
        (tiger_office 有 16 个: scan_excel / write_word / ...)。
        不告诉 LLM 动作名, 它就会**编**——实测编出 "excel_read"(真名 scan_excel),
        一路"成功"到实跑门才被拦。两种历史分发形态都要认:
          · handlers = {"scan_excel": fn, ...}   (字典分发)
          · if action == "read": ...             (if 链)
        """
        if not path:
            return []
        try:
            import ast as _ast
            src = Path(path).read_text(encoding="utf-8", errors="replace")
            acts = set()
            tree = _ast.parse(src)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Dict):
                    for tgt in node.targets:
                        if isinstance(tgt, _ast.Name) and tgt.id.upper() in (
                                "HANDLERS", "_ACTIONS", "ACTIONS"):
                            acts |= {k.value for k in node.value.keys
                                     if isinstance(k, _ast.Constant) and isinstance(k.value, str)}
                if isinstance(node, _ast.Compare) and isinstance(node.left, _ast.Name) \
                        and node.left.id == "action":
                    for op, comp in zip(node.ops, node.comparators):
                        if isinstance(op, _ast.Eq) and isinstance(comp, _ast.Constant) \
                                and isinstance(comp.value, str):
                            acts.add(comp.value)
                        if isinstance(op, _ast.In) and isinstance(comp, (_ast.Tuple, _ast.List, _ast.Set)):
                            acts |= {e.value for e in comp.elts
                                     if isinstance(e, _ast.Constant) and isinstance(e.value, str)}
            return sorted(a for a in acts if a)
        except Exception:
            return []

    def _is_callable(self, name: str, path: str):
        """三态: True 有入口 / False 明确没有入口 / None 无法判定 (导入失败=环境问题)。

        ★ 区分 False 与 None 很重要:
          · False (模块导入了但没有 run/execute) → **硬拒**: 调它只会谎报成功
          · None  (导入失败, 如 windows_desktop 缺 comtypes.gen) → 只提示, 不误拒 ——
            这是环境问题, 换台机器/装好依赖就能用, 不该在编译期判死刑。
        """
        if name in BUILTIN_STEPS:
            return True                                  # llm / knowledge 由 pipeline 实现
        if not path:
            return False                                 # 连模块都没有 = 不可调用
        try:
            import importlib.util as _iu
            spec = _iu.spec_from_file_location(name, path)
            if not (spec and spec.loader):
                return False
            mod = _iu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return bool(hasattr(mod, "run") or hasattr(mod, "execute"))
        except Exception:
            return None                                  # 导入失败 → 无法判定

    def _engine_known(self) -> set:
        """引擎(或运行时)知道的工具名 —— 不含 catalog, 避免递归。"""
        if self.engine is None:
            return set()
        return set(getattr(self.engine, "_KNOWN_TOOLS", set()) or set())

    def known_tools(self) -> set:
        return set(BUILTIN_STEPS) | set(self.catalog().keys()) | self._engine_known()

    def existing_skills(self) -> dict:
        """{技能名: [triggers]} —— 查重名/抢触发词。"""
        out = {}
        if not self.skills_dir.is_dir():
            return out
        import yaml
        for entry in sorted(self.skills_dir.iterdir()):
            f = entry / "SKILL.md"
            if not f.is_file():
                continue
            try:
                m = re.match(r"---\s*\n(.*?)\n---", f.read_text(encoding="utf-8"), re.S)
                fm = yaml.safe_load(m.group(1)) if m else {}
                out[fm.get("name") or entry.name] = list(fm.get("triggers") or [])
            except Exception:
                out[entry.name] = []
        return out

    # ── ② 编译 ──
    def _prompt(self, description: str, name_hint: str) -> str:
        cat = self.catalog()
        lines = []
        for nm, meta in sorted(cat.items()):
            if not meta.get("callable"):
                continue     # ★ 没有 run/execute 入口的工具只会谎报成功, 不能给 LLM 当积木
            ps = ", ".join(f"{p.get('name')}({p.get('type','str')}{'' if p.get('required') else '?'})"
                           for p in (meta.get("params") or [])[:8])
            ac = meta.get("actions") or []
            ac_s = f" | 动作(必须逐字用这些): {', '.join(ac)}" if ac else ""
            lines.append(f"- {nm}: {(meta.get('description') or '')[:70]} | 参数: {ps or '(见工具签名)'}{ac_s}")
        tools_block = "\n".join(lines) or "(工具目录读取失败 —— 只用 llm / knowledge)"
        return f"""你是技能编译器。把用户需求编译成一个**声明式 DAG 技能**(可执行, 非散文)。

## 可用工具 (只能用这些; tool 必须逐字来自下表)
{tools_block}
- llm: 调模型生成文字 (内建步骤, input: prompt, system?, temperature?, max_tokens?; output: text)
- knowledge: 查实体表 (内建步骤, input: query; output: text)

## 输出格式 (严格 JSON, 不要 markdown 围栏, 不要解释)
{{
  "name": "小写连字符名",
  "description": "一句话说明这个技能做什么",
  "tags": ["检索用词", "..."],
  "triggers": ["用户可能说的短语", "..."],
  "params": {{"参数名": {{"type": "text|path|entity|number|bool|list", "required": false, "desc": "说明"}}}},
  "steps": [
    {{"id": "step1", "tool": "工具名", "action": "动作名(可选)", "input": {{"参数": "值"}}, "output": "text", "on_error": "stop"}}
  ]
}}

## 硬规则
1. 每个 step 的 tool 必须来自上表 (或 llm / knowledge)。**不许造工具名**。
2. 占位符: `$params.参数名` (取用户参数) · `$steps.<前一步id>.<字段>` (取前一步输出) · `$message` (用户原话)。
   用 `$params.X` 时, X **必须**出现在 params 里。
3. params 尽量都写 `"required": false` —— 技能层会把整句原话透传, 必填会直接报错。
4. id 唯一; 后一步用 `$steps.前一步id.text` 串联。on_error 用 "stop" 或 "skip"。
5. triggers 写 3~8 个**中文短语**, 要具体 (别写单字"画/看/查", 会抢走别的技能的句子)。
6. ★ 路径不要用 `~/Desktop`、`./x` 这类 POSIX/相对写法 —— 本机工具**不认**
   (实测踩过: file_ops 把 "~/Desktop" 解析成项目下的 "~\Desktop" → File not found)。
   要用户给的路径就用 `$params.path`; 要固定桌面就用绝对路径。
7. 只输出 JSON。

## 用户需求
{description}
{f'## 名字偏好{chr(10)}{name_hint}' if name_hint else ''}"""

    @staticmethod
    def _extract_json(text: str) -> dict:
        """容忍 ```json 围栏 / 前后解释 / thinking 残留。"""
        if not text:
            return {}
        s = str(text).strip()
        m = re.search(r"```(?:json)?\s*(.+?)```", s, re.S)
        if m:
            s = m.group(1).strip()
        try:
            return json.loads(s)
        except Exception:
            pass
        # 退一步: 抓最外层花括号
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                pass
        return {}

    async def propose(self, description: str, name_hint: str = "") -> Proposal:
        prop = Proposal(raw="")
        if self.generate is None:
            prop.errors.append("未注入 generate (无法调模型编译)")
            return prop
        try:
            r = await self.generate(self.model, [
                {"role": "system", "content": "你是技能编译器, 只输出严格 JSON。"},
                {"role": "user", "content": self._prompt(description, name_hint)}],
                temperature=0.2, max_tokens=2000)
        except Exception as e:
            prop.errors.append(f"模型调用异常: {type(e).__name__}: {e}")
            return prop
        if not isinstance(r, dict) or r.get("error"):
            prop.errors.append(f"模型返回错误: {(r or {}).get('error')}")
            return prop
        text = r.get("text") or r.get("think") or ""
        prop.raw = str(text)[:4000]
        d = self._extract_json(text)
        if not d:
            prop.errors.append("模型输出不是可解析的 JSON (见 raw)")
            return prop
        prop.name = str(d.get("name") or name_hint or "").strip()
        prop.description = str(d.get("description") or description)[:300]
        prop.tags = [str(x) for x in (d.get("tags") or []) if str(x).strip()]
        prop.triggers = [str(x).strip() for x in (d.get("triggers") or []) if str(x).strip()]
        prop.params = d.get("params") if isinstance(d.get("params"), dict) else {}
        prop.steps = d.get("steps") if isinstance(d.get("steps"), list) else []
        prop.requires_tools = sorted({str(s.get("tool")) for s in prop.steps
                                      if isinstance(s, dict) and s.get("tool")
                                      and str(s.get("tool")) not in BUILTIN_STEPS})
        return prop

    # ── ③ 质检 ──
    def validate(self, prop: Proposal) -> Proposal:
        errors, warns = [], []
        known = self.known_tools()
        existing = self.existing_skills()

        if not prop.name:
            errors.append("缺少 name")
        elif not _NAME_RE.match(prop.name):
            errors.append(f"name '{prop.name}' 不合规 (要求小写字母开头, 只含 a-z0-9 和连字符)")
        elif prop.name in existing:
            errors.append(f"name '{prop.name}' 与现有技能重名")

        if not prop.description:
            errors.append("缺少 description")
        if not prop.steps:
            errors.append("没有 steps")
        if not prop.triggers:
            errors.append("没有 triggers (技能层靠它命中)")
        else:
            # 触发词冲突: 与现有技能完全相等, 或互为子串 (会抢句子)
            for t in prop.triggers:
                if len(t) <= 1:
                    errors.append(f"触发词 '{t}' 太短 (单字会抢走别的技能的句子)")
                for sk, trs in existing.items():
                    for et in trs:
                        if t == et:
                            errors.append(f"触发词 '{t}' 与技能 '{sk}' 完全相同 → 会互相抢")
                        elif len(t) >= 2 and len(et) >= 2 and (t in et or et in t):
                            warns.append(f"触发词 '{t}' 与 '{sk}' 的 '{et}' 互为子串 (可能抢句子)")

        pnames = set((prop.params or {}).keys())
        for pname, pspec in (prop.params or {}).items():
            if not isinstance(pspec, dict):
                errors.append(f"参数 '{pname}' 的声明必须是 dict")
            elif pspec.get("type") and pspec["type"] not in ("text", "path", "entity",
                                                             "number", "bool", "list", ""):
                errors.append(f"参数 '{pname}' 类型 '{pspec['type']}' 不认识")

        sids = set()
        for i, st in enumerate(prop.steps or []):
            if not isinstance(st, dict):
                errors.append(f"step {i} 不是 dict")
                continue
            sid = str(st.get("id") or f"step_{i}")
            if sid in sids:
                errors.append(f"step id '{sid}' 重复")
            sids.add(sid)
            tool = str(st.get("tool") or "")
            if not tool:
                errors.append(f"step '{sid}' 缺 tool")
            elif tool not in known:
                errors.append(f"step '{sid}' 用了未知工具 '{tool}' (不在工具目录里)")
            elif self._tool_callable(tool) is False:
                # ★ 模块导入了但没有 run/execute 入口 → 调了只会"谎报成功"
                #   (实测: office_cli 存 Word 全流程报成功但文件不存在) → 编译期拒掉。
                errors.append(f"step '{sid}' 的工具 '{tool}' 没有可调用入口 (需 run/execute), 不能用")
            elif self._tool_callable(tool) is None:
                warns.append(f"step '{sid}' 的工具 '{tool}' 无法判定可用性 (导入失败, 可能是环境缺依赖)")
            inp = st.get("input") or {}
            if not isinstance(inp, dict):
                errors.append(f"step '{sid}' 的 input 必须是 dict")
                continue
            for k, v in inp.items():
                if isinstance(v, str) and v.startswith("$params."):
                    ref = v[8:]
                    if ref not in pnames:
                        errors.append(f"step '{sid}' 引用 $params.{ref}, 但 params 里没有 '{ref}'")
                elif isinstance(v, str) and v.startswith("$steps."):
                    ref = v[7:].split(".", 1)[0]
                    if ref not in sids and ref != sid:
                        errors.append(f"step '{sid}' 引用 $steps.{ref} —— 该 id 不存在或在其后")

        # 用引擎自己的校验再兜一层 (结构/权限)
        if self.engine is not None and prop.steps:
            try:
                from core.skill_loader import SkillDAG
                dag = SkillDAG(name=prop.name, steps=prop.steps,
                               params_schema=prop.params or {}, requires_tools=prop.requires_tools)
                self.engine._validate_dag(dag)
                errors.extend(getattr(dag, "parse_errors", []) or [])
            except Exception as e:
                warns.append(f"引擎校验未跑成 ({type(e).__name__}) — 已由本工厂规则覆盖")

        prop.errors = errors
        prop.warnings = warns
        return prop

    # ── ④ 自测: stub 跑一遍, 每步都必须被调用 ──
    async def self_test(self, prop: Proposal, stub_ok: bool = True) -> dict:
        """用 stub 工具真跑 execute_dag。
        目的: 证明**链路可达** (每个 step 都被执行到), 而不是"看起来像 DAG"。
        """
        if self.engine is None:
            return {"ran": False, "reason": "未注入 engine, 跳过自测", "called": [], "missing": []}
        if prop.errors or not prop.steps:
            return {"ran": False, "reason": "质检未过, 不自测", "called": [], "missing": []}
        calls = []

        class _StubMgr:
            async def call(self, tool, action="", **kw):
                calls.append([str(tool), str(action or "")])
                return {"success": stub_ok, "output": "[stub]", "text": "[stub]",
                        "path": str(kw.get("path", ""))}

        from core.skill_loader import SkillDAG
        dag = SkillDAG(name=prop.name, steps=prop.steps, params_schema=prop.params or {},
                       requires_tools=prop.requires_tools)
        params = {k: "测试值" for k in (prop.params or {})}
        # ★ execute_dag 用的是 engine 自己的 _gateway (不是传入的 plugin_mgr) ——
        #   所以必须临时换掉它, 且 **finally 还原**: 忘了还原 = 真管线被 stub 永久接管,
        #   所有真实工具调用都变成 "[stub]" 且不报错 (最阴的污染)。
        prev = getattr(self.engine, "_gateway", None)
        prev_gen = getattr(self.engine, "_generate", None)

        async def _stub_gen(model, messages, **kw):
            return {"text": "[stub]", "error": None}

        try:
            if hasattr(self.engine, "set_gateway"):
                self.engine.set_gateway(_StubMgr())
            else:
                self.engine._gateway = _StubMgr()
            # ★ llm 步骤走 generate (不走 gateway) —— 不注入就会当"失败"提前中止
            if hasattr(self.engine, "set_generate"):
                self.engine.set_generate(_stub_gen)
            res = await self.engine.execute_dag(dag, params, _StubMgr())
        except Exception as e:
            return {"ran": True, "ok": False, "error": f"{type(e).__name__}: {e}",
                    "called": calls, "missing": [str(s.get("id")) for s in prop.steps]}
        finally:
            if hasattr(self.engine, "set_gateway"):
                self.engine.set_gateway(prev)
            else:
                self.engine._gateway = prev
            if hasattr(self.engine, "set_generate"):
                self.engine.set_generate(prev_gen)

        # 逐步核对 (按**多重集**比 —— 多步共用同一工具时, 少跑一步也能被发现)
        expected = [str(s.get("tool")) for s in prop.steps
                    if isinstance(s, dict) and str(s.get("tool")) not in BUILTIN_STEPS]
        called = [c[0] for c in calls]
        missing = []
        pool = list(called)
        for tname in expected:
            if tname in pool:
                pool.remove(tname)
            else:
                missing.append(tname)
        return {"ran": True, "ok": bool(res.get("success")) and not missing,
                "steps": [str(s.get("id") or f"step_{i}") for i, s in enumerate(prop.steps)],
                "expected_tools": expected, "called": calls, "missing": missing,
                "result": {k: str(v)[:150] for k, v in (res or {}).items()}}

    # ── ⑤ 实跑门: 真工具跑一遍, 任何一步失败 → 拒 ──
    async def smoke_test(self, prop: Proposal) -> dict:
        """用**真 gateway** 真跑一遍 DAG。

        为什么 stub 自测不够: stub 永远成功, 验不出"选错了工具"。
        实测踩到的三类"看起来成功其实没干活":
          ① 选了没有 run/execute 入口的工具 (office_cli) → 旧代码谎报成功
          ② 选了写文本的工具去存 .docx (file_ops write) → Access denied / 格式不对
          ③ DAG 层硬编码 success=True → 工具失败也上报成功
        这三类只有**真跑**才拦得住, 所以实跑是编译期的最后一道门。
        """
        if self.gateway is None or self.engine is None:
            return {"ran": False, "reason": "未注入 gateway/engine, 跳过实跑"}
        if prop.errors or not prop.steps:
            return {"ran": False, "reason": "质检未过, 不实跑"}
        import shutil as _sh
        sbx = self.smoke_dir / prop.name
        _sh.rmtree(sbx, ignore_errors=True)
        sbx.mkdir(parents=True, exist_ok=True)
        # 给 path 类参数喂沙箱路径, 其余喂短文本
        params = {}
        for k, spec in (prop.params or {}).items():
            params[k] = str(sbx / "out.txt") if (spec or {}).get("type") == "path" else "测试内容"
        from core.skill_loader import SkillDAG
        dag = SkillDAG(name=prop.name, steps=prop.steps, params_schema=prop.params or {},
                       requires_tools=prop.requires_tools)
        prev = getattr(self.engine, "_gateway", None)
        prev_gen = getattr(self.engine, "_generate", None)
        try:
            if hasattr(self.engine, "set_gateway"):
                self.engine.set_gateway(self.gateway)
            else:
                self.engine._gateway = self.gateway
            if hasattr(self.engine, "set_generate") and self.generate is not None:
                self.engine.set_generate(self.generate)     # llm 步骤要用
            res = await self.engine.execute_dag(dag, params, self.gateway)
        except Exception as e:
            return {"ran": True, "ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            if hasattr(self.engine, "set_gateway"):
                self.engine.set_gateway(prev)
            else:
                self.engine._gateway = prev
            if hasattr(self.engine, "set_generate"):
                self.engine.set_generate(prev_gen)          # ★ 也必须还原
            _sh.rmtree(sbx, ignore_errors=True)          # 不留调试痕迹
        bad = [k for k, v in (res.get("results") or {}).items()
               if isinstance(v, dict) and not v.get("success")]
        return {"ran": True, "ok": bool(res.get("success")) and not bad,
                "failed_steps": bad, "error": str(res.get("error") or "")[:200]}

    # ── 候选池 (人工确认前不生效) ──
    def save(self, prop: Proposal) -> Path:
        d = self.candidate_dir / prop.name
        d.mkdir(parents=True, exist_ok=True)
        fm = prop.to_frontmatter()
        import yaml
        body = (f"# {prop.name}\n\n{prop.description}\n\n"
                f"> 本技能由**技能工厂**编译, 状态: 候选 (未生效)。\n"
                f"> 确认: `/skill approve {prop.name}` · 丢弃: `/skill reject {prop.name}`\n")
        (d / "SKILL.md").write_text("---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
                                    + "---\n\n" + body, encoding="utf-8")
        meta = {"name": prop.name, "description": prop.description, "triggers": prop.triggers,
                "steps": len(prop.steps), "errors": prop.errors, "warnings": prop.warnings,
                "requires_tools": prop.requires_tools, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return d / "SKILL.md"

    def list_candidates(self) -> list:
        out = []
        if not self.candidate_dir.is_dir():
            return out
        for d in sorted(self.candidate_dir.iterdir()):
            mf = d / "meta.json"
            if d.is_dir() and mf.is_file():
                try:
                    out.append(json.loads(mf.read_text(encoding="utf-8")))
                except Exception:
                    out.append({"name": d.name, "description": "(meta 读取失败)", "errors": ["meta 损坏"]})
        return out

    def load_candidate(self, name: str) -> Proposal:
        import yaml
        f = self.candidate_dir / name / "SKILL.md"
        if not f.is_file():
            p = Proposal(name=name)
            p.errors.append(f"候选不存在: {name}")
            return p
        txt = f.read_text(encoding="utf-8")
        m = re.match(r"---\s*\n(.*?)\n---", txt, re.S)
        fm = yaml.safe_load(m.group(1)) if m else {}
        return Proposal(name=fm.get("name", name), description=fm.get("description", ""),
                        tags=list(fm.get("tags") or []), triggers=list(fm.get("triggers") or []),
                        params=dict(fm.get("params") or {}), steps=list(fm.get("steps") or []),
                        requires_tools=list(fm.get("requires_tools") or []), raw=txt)

    def reject(self, name: str) -> bool:
        d = self.candidate_dir / name
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        return True

    def promote(self, name: str) -> dict:
        """★ 人工确认门: 候选 → 正式技能 (先复检, 再移入, 最后 reload 引擎)。"""
        prop = self.load_candidate(name)
        if prop.errors and any("不存在" in e for e in prop.errors):
            return {"ok": False, "error": f"候选不存在: {name}"}
        self.validate(prop)
        if prop.errors:
            return {"ok": False, "error": "复检未过, 拒绝生效", "errors": prop.errors}
        dest = self.skills_dir / prop.name
        if dest.exists():
            return {"ok": False, "error": f"目标技能已存在: {prop.name} (先手动处理)"}
        src = self.candidate_dir / name
        (src / "meta.json").unlink(missing_ok=True)          # 不把候选元数据带进正式技能
        shutil.move(str(src), str(dest))
        reloaded = None
        if self.engine is not None:
            try:
                from core import skill_loader as SL
                reloaded = bool(SL.reload_skills())
            except Exception as e:
                reloaded = f"reload 失败: {type(e).__name__}: {e}"
        return {"ok": True, "name": prop.name, "path": str(dest / "SKILL.md"),
                "reloaded": reloaded, "steps": len(prop.steps), "triggers": prop.triggers}

    # ── 全流程 ──
    async def build(self, description: str, name_hint: str = "", smoke: bool = True) -> dict:
        prop = await self.propose(description, name_hint)
        if prop.errors:
            return {"ok": False, "stage": "编译", "errors": prop.errors, "proposal": asdict(prop)}
        self.validate(prop)
        if prop.errors:
            return {"ok": False, "stage": "质检", "errors": prop.errors,
                    "warnings": prop.warnings, "proposal": asdict(prop)}
        st = await self.self_test(prop)
        if st.get("ran") and not st.get("ok"):
            return {"ok": False, "stage": "自测", "errors": [f"链路不可达, 缺调用: {st.get('missing')}"],
                    "self_test": st, "proposal": asdict(prop)}
        # ★ 实跑门: stub 自测验不出"选错工具", 必须用真工具跑一遍
        if smoke:
            sm = await self.smoke_test(prop)
            if sm.get("ran") and not sm.get("ok"):
                return {"ok": False, "stage": "实跑",
                        "errors": [(sm.get("error") or "实跑失败") +
                                   (f" · 失败步骤: {sm.get('failed_steps')}" if sm.get("failed_steps") else "")],
                        "self_test": st, "smoke": sm, "proposal": asdict(prop)}
        else:
            sm = {"ran": False, "reason": "smoke=False"}
        path = self.save(prop)
        return {"ok": True, "stage": "候选已就绪 (待人工确认)", "name": prop.name,
                "path": str(path), "errors": [], "warnings": prop.warnings,
                "self_test": st, "smoke": sm, "proposal": asdict(prop)}
