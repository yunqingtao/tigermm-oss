"""
SkillWorkflowEngine -- unified Skill parser + Workflow runner.
=============================================================
v4.2 merged form: internal two-layer isolation (parser + runner), single external entry.

ARCHITECTURE:
  SkillParser (static, cold-start)
    - scan SKILL.md/skill.json, validate syntax/params/permissions/deps
    - convert to SkillDAG (standardized task definition)
    - NEVER calls tools or models (boundary rule, enforced by _assert_not_parser)
  WorkflowRunner (runtime, on trigger)
    - consume SkillDAG, execute steps: loops, branches, retries, state
    - call ToolGateway and model inference
    - Verify/Fix loop per Level4Pipeline

DATA FLOW (merged form):
  scan() -> SkillIndex + SkillDAG (cached)
  match_context() -> finds SkillIndex by tags/triggers
  execute_workflow() -> reads SkillDAG, runs steps

SPLIT TRIGGERS (when any ONE is met, decouple into separate engines):
  1. Cross-skill nested calls, parallel branches, conditional jumps, sub-workflows
  2. Open Skill ecosystem: 3rd-party workflows needing sandbox + timeout
  3. Multi-device: remote scheduling from mobile to PC Skill execution

POST-SPLIT COMMUNICATION:
  SkillLoader -> SkillDAG JSON -> WorkflowEngine
"""
import os, re, json, yaml, logging, time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("core.skill_loader")

SKILLS_DIR = Path(__file__).parent.parent / "tmm_skills"
CACHE_TTL = 300  # seconds before re-parse

# ================================================================
# BOUNDARY RULE
# ================================================================
PARSER_BOUNDARY_VIOLATION = (
    "BOUNDARY VIOLATION: SkillParser must not call tools or trigger model inference. "
    "Only WorkflowRunner may invoke tools/models."
)


@dataclass
class SkillIndex:
    """Lightweight index entry for matching/tag search."""
    name: str
    path: Path
    description: str = ""
    tags: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    body: str = ""


@dataclass
class SkillDAG:
    """Standardized task definition -- parser output, runner input.
    
    This is the contract between SkillParser and WorkflowRunner.
    Post-split, this becomes the JSON serialization format for IPC.
    """
    name: str
    version: str = "1.0"
    description: str = ""
    params_schema: dict = field(default_factory=dict)   # {name: {type, required, default, field}}
    permissions: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    requires_skills: list[str] = field(default_factory=list)
    timeout: int = 60
    steps: list[dict] = field(default_factory=list)      # [{id, tool, action, input, on_error, depends_on}]
    raw_source: dict = field(default_factory=dict)        # original JSON for debugging
    parse_errors: list[str] = field(default_factory=list)

    def is_valid(self) -> bool:
        return len(self.parse_errors) == 0 and len(self.steps) > 0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "version": self.version,
            "description": self.description,
            "params_schema": self.params_schema,
            "permissions": self.permissions,
            "requires_tools": self.requires_tools,
            "requires_skills": self.requires_skills,
            "timeout": self.timeout,
            "steps": self.steps,
        }


    # 内建步骤 (不走 ToolGateway, 由 pipeline 直接实现)
_BUILTIN_STEPS = {"knowledge", "llm"}

# ★ 2026-09-19: 原为**手写死表** —— 后果实测: 新增 tools/vision.py 与 tools/image_gen.py
#   后忘了加进来 → DAG 判"未知工具" → **整条技能被静默丢弃**(技能匹配返 None),
#   用户问"看看这张图"时落到旧的 ollama→deepseek 兜底, 纯文本模型编造图片内容。
#   改为**自动发现**: 扫 tools/*.py 取模块名 ∪ 内建步骤。新增工具无需再改这张表。
def _discover_known_tools() -> set:
    names = set(_BUILTIN_STEPS)
    try:
        from pathlib import Path as _P
        for _d in (_P(__file__).resolve().parent.parent / "tools",
                   _P(__file__).resolve().parent.parent / "plugins"):
            if _d.is_dir():
                for _f in _d.glob("*.py"):
                    if not _f.stem.startswith("_"):
                        names.add(_f.stem)
    except Exception:
        pass
    # 手工兜底 (目录扫描失败时仍可用; 含历史别名)
    names |= {
        "file_ops", "send_email", "check_mail", "shell_exec", "system_info",
        "windows_desktop", "web_search", "openmeteo", "nominatim",
        "libretranslate", "plantuml", "browser", "ocr", "whisper",
        "voice", "push_notify", "im_notify", "firecrawl", "sms_send",
        "office_cli", "tiger_office", "ntfy", "chart", "vision", "image_gen",
        "kb_search", "kb_list", "kb_delete", "kb_import", "backup",
        "win32_input", "hermes_bridge",
    }
    return names


_KNOWN_TOOLS = _discover_known_tools()


class SkillWorkflowEngine:
    """Unified engine: SkillParser (static) + WorkflowRunner (runtime).
    
    Internal boundary: parser section reads/validates SKILL.md/skill.json only.
    Runner section executes DAG steps via ToolGateway.

    SPLIT TRIGGERS (decouple SkillLoader/WorkflowEngine when any met):
      1. Cross-skill nested calls, parallel branches, conditional jumps, sub-workflows
      2. Open Skill ecosystem: 3rd-party workflows need sandbox + timeout
      3. Multi-device: remote scheduling from mobile to PC Skill execution
    """

    # 工具集: 指向上方模块级自动发现的结果 (方法里用 self._KNOWN_TOOLS)
    _KNOWN_TOOLS = _KNOWN_TOOLS

    _CONNECTORS = [
        (r'(.+?)(?:然后|接着|再|之后|再然后|完了)(.+)', 2),
        (r'先(.+?)(?:再|然后|接着)(.+)', 2),
        (r'(.+?)(?:并且|同时|顺便|外加)(.+)', 2),
    ]

    # -- Known tools for validation --
    # 步骤可用的工具。`knowledge` / `llm` 是**内建步骤**(不走 ToolGateway, 由
    # pipeline._skill_dag_exec 直接实现): knowledge=实体查表, llm=调模型生成内容。

    def __init__(self, skills_dir: Path = None):
        self.skills_dir = skills_dir or SKILLS_DIR
        self._index: dict[str, SkillIndex] = {}
        self._dag_cache: dict[str, SkillDAG] = {}  # parse-once cache
        self._cache_time: dict[str, float] = {}
        self._loaded = False
        self._ke = None
        self._workflows: dict = {}
        self._gateway = None
        self._generate = None
        self._in_parser = False

    # ================================================================
    # PARSER GUARDS
    # ================================================================

    def _enter_parser(self):
        self._in_parser = True

    def _exit_parser(self):
        self._in_parser = False

    def _assert_not_parser(self, caller: str = ""):
        if self._in_parser:
            raise RuntimeError(
                f"{PARSER_BOUNDARY_VIOLATION}\n"
                f"  Caller: {caller or 'unknown'}\n"
                f"  Hint: Move this logic to WorkflowRunner section."
            )

    # ================================================================
    # SECTION 1: SkillParser -- static, no tools/models
    # ================================================================

    def scan(self) -> list[str]:
        """[PARSER] Scan skills directory, parse all SKILL.md/skill.json into index + DAG cache."""
        self._enter_parser()
        try:
            self._index.clear()
            self._dag_cache.clear()
            self._cache_time.clear()

            if not self.skills_dir.exists():
                logger.info("Skills dir not found: %s", self.skills_dir)
                return []

            loaded = []
            for entry in sorted(self.skills_dir.iterdir()):
                result = self._scan_entry(entry)
                if result:
                    loaded.append(result)

            self._loaded = True
            logger.info("SkillParser: %d skills loaded, %d DAGs cached",
                       len(self._index), len(self._dag_cache))
            return loaded
        finally:
            self._exit_parser()

    def _scan_entry(self, entry: Path) -> str | None:
        """[PARSER] Scan one skill directory or file."""
        if entry.is_dir():
            # Prefer skill.json over SKILL.md in directory
            json_file = entry / "skill.json"
            md_file = entry / "SKILL.md"
            if json_file.exists():
                return self._parse_json_skill(json_file)
            elif md_file.exists():
                return self._parse_md_skill(md_file)
        elif entry.suffix.lower() == ".json":
            return self._parse_json_skill(entry)
        elif entry.suffix.lower() == ".md":
            return self._parse_md_skill(entry)
        return None

    def _parse_json_skill(self, path: Path) -> str | None:
        """[PARSER] Parse skill.json into SkillIndex + SkillDAG."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("SkillParser: bad JSON %s: %s", path.name, e)
            return None

        name = raw.get("name", path.parent.name if path.parent != self.skills_dir else path.stem)
        dag = self._build_dag(name, raw, path)

        # Cache DAG
        self._dag_cache[name] = dag
        self._cache_time[name] = time.time()

        # Build SkillIndex
        triggers_raw = raw.get("trigger", {})
        patterns = triggers_raw.get("patterns", []) if isinstance(triggers_raw, dict) else []
        tags = raw.get("tags", [])
        if not tags and patterns:
            tags = patterns[:5]

        self._index[name] = SkillIndex(
            name=name,
            path=path,
            description=raw.get("description", ""),
            tags=tags,
            triggers=patterns,
            platforms=raw.get("platforms", []),
            requires_tools=dag.requires_tools,
            body=raw.get("description", ""),
        )

        if dag.parse_errors:
            logger.warning("SkillParser: %s has %d parse errors: %s",
                         name, len(dag.parse_errors), dag.parse_errors[:3])
        else:
            logger.debug("SkillParser: %s OK (%d steps)", name, len(dag.steps))

        return name

    def _parse_md_skill(self, path: Path) -> str | None:
        """[PARSER] Parse SKILL.md (YAML frontmatter + markdown body)."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("SkillParser: cannot read %s: %s", path.name, e)
            return None

        frontmatter, body = self._parse_frontmatter(text)
        if not frontmatter:
            # 静默跳过 = 技能作者以为生效了其实没加载 (实测踩过: 忘写 --- 分隔符 → 无声无息)
            if getattr(self, "_last_fm_error", None):
                logger.warning(
                    "SkillParser: %s 的 YAML frontmatter **语法错误** → 已跳过。真因: %s"
                    "   (提示: 值里含 ': ' / '#' / 以引号开头 等请整体加引号)",
                    path.name, self._last_fm_error)
            else:
                logger.warning(
                    "SkillParser: %s 无有效 YAML frontmatter → 已跳过。"
                    "SKILL.md 必须以 '---' 开头、以 '---' 结束, 中间放 name/description/params/steps",
                    path)
            return None

        name = frontmatter.get("name", path.parent.name if path.parent != self.skills_dir else path.stem)
        tags = frontmatter.get("tags", [])
        triggers = frontmatter.get("triggers", [])

        # Build lightweight DAG from frontmatter
        dag = SkillDAG(
            name=name,
            version=frontmatter.get("version", "1.0"),
            description=frontmatter.get("description", ""),
            params_schema=frontmatter.get("params", {}),
            permissions=[frontmatter.get("permission", "read")],
            requires_tools=frontmatter.get("requires_tools", []),
            timeout=frontmatter.get("timeout", 60),
            steps=frontmatter.get("steps", []),
            raw_source=frontmatter,
        )

        # Validate
        self._validate_dag(dag)

        self._dag_cache[name] = dag
        self._cache_time[name] = time.time()

        self._index[name] = SkillIndex(
            name=name, path=path,
            description=frontmatter.get("description", ""),
            tags=tags, triggers=triggers,
            platforms=frontmatter.get("platforms", []),
            requires_tools=dag.requires_tools,
            body=body,
        )

        if dag.parse_errors:
            logger.warning("SkillParser: %s has %d parse errors", name, len(dag.parse_errors))
        return name

    # -- DAG builder + validators --

    def _build_dag(self, name: str, raw: dict, source_path: Path) -> SkillDAG:
        """[PARSER] Convert raw skill.json into validated SkillDAG."""
        dag = SkillDAG(
            name=name,
            version=raw.get("version", "1.0"),
            description=raw.get("description", ""),
            params_schema=raw.get("params", {}),
            permissions=[raw.get("permission", "read")],
            requires_tools=raw.get("requires", []),
            timeout=raw.get("timeout", 60),
            steps=raw.get("steps", []),
            raw_source=raw,
        )
        self._validate_dag(dag)
        return dag

    def _validate_dag(self, dag: SkillDAG):
        """[PARSER] Validate DAG: params, permissions, tool refs, step structure."""
        errors = []

        # 1. Required fields
        if not dag.name:
            errors.append("Missing 'name'")
        if not dag.steps:
            errors.append("No steps defined")
        else:
            if not isinstance(dag.steps, list):
                errors.append("'steps' must be a list")

        # 2. Param schema validation
        if dag.params_schema:
            if not isinstance(dag.params_schema, dict):
                errors.append("'params' must be a dict of {name: {type, required}}")
            else:
                for pname, pspec in dag.params_schema.items():
                    if not isinstance(pspec, dict):
                        errors.append(f"Param '{pname}' spec must be dict, got {type(pspec).__name__}")
                        continue
                    ptype = pspec.get("type", "")
                    if ptype not in ("text", "path", "entity", "number", "bool", "list", ""):
                        errors.append(f"Param '{pname}': unknown type '{ptype}'")

        # 3. Step structure validation
        if isinstance(dag.steps, list):
            step_ids = set()
            for i, step in enumerate(dag.steps):
                if not isinstance(step, dict):
                    errors.append(f"Step {i}: must be dict, got {type(step).__name__}")
                    continue
                sid = step.get("id", f"step_{i}")
                if sid in step_ids:
                    errors.append(f"Step '{sid}': duplicate id")
                step_ids.add(sid)

                tool = step.get("tool", "")
                if not tool:
                    errors.append(f"Step '{sid}': missing 'tool'")
                elif tool not in self._KNOWN_TOOLS and tool != "knowledge":
                    errors.append(f"Step '{sid}': unknown tool '{tool}' (not in known tools)")

                # Check input references against params
                step_input = step.get("input", {})
                if isinstance(step_input, dict):
                    for key, val in step_input.items():
                        if isinstance(val, str) and val.startswith("$params."):
                            ref_name = val[8:]
                            if dag.params_schema and ref_name not in dag.params_schema:
                                errors.append(
                                    f"Step '{sid}': input '{key}' references '$params.{ref_name}' "
                                    f"but '{ref_name}' not in params schema"
                                )

                # depends_on validation
                deps = step.get("depends_on", [])
                if isinstance(deps, list):
                    for dep in deps:
                        if dep not in step_ids and dep != sid:
                            errors.append(f"Step '{sid}': depends_on '{dep}' not found in step ids")

        # 4. Permission validation
        valid_perms = {"read", "write", "execute", "system", "file_read", "file_write", "network", "all"}
        for perm in dag.permissions:
            if perm not in valid_perms:
                errors.append(f"Unknown permission '{perm}'. Valid: {sorted(valid_perms)}")

        # 5. Tool check against requires
        if dag.requires_tools:
            for tool in dag.requires_tools:
                if tool not in self._KNOWN_TOOLS and tool != "knowledge":
                    errors.append(f"Required tool '{tool}' not in known tools")

        dag.parse_errors = errors

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        """[PARSER] Extract YAML frontmatter and body from SKILL.md."""
        # ★ 2026-09-20: 记录本轮的 YAML 错误 (供调用方带着文件名报**真因**)。
        #   原来这里吞掉异常只留 fm={} → 调用方报"无有效 frontmatter(必须以 --- 开头/结束)"
        #   → **误导**: 实测作者明明写了 ---, 真因是标量里有裸 `: ` (中文很常见,
        #   如 `只做三件事: 认同哪些`) 让 YAML 炸了。
        self._last_fm_error = None
        # ★ 2026-09-20 修: 原来要求结束标记后**必须有换行和正文** (`\n---\s*\n(.*)`)。
        #   于是"手写成 <frontmatter>--- 就到底"的技能文件**整份被静默丢弃** ——
        #   实测我照 OpenClaw 风格补的 5 个技能全中招 (--- 个数=2、YAML 也合法,
        #   但正则不匹配 → 报"无有效 frontmatter", 作者完全看不出问题在哪)。
        #   现在: 结束标记后是文件末尾也接受 (用 \Z 锚定, 正文可选)。
        m = re.match(r'^---\s*\n(.*?)\n---\s*(?:\n(.*))?\Z', text, re.DOTALL)
        if m:
            try:
                fm = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError as e:
                self._last_fm_error = " ".join(str(e).split())[:260]
                fm = {}
            return fm, (m.group(2) or "").strip()
        m2 = re.match(r'^```json\s*\n(.*?)\n```\s*\n(.*)', text, re.DOTALL)
        if m2:
            try:
                fm = json.loads(m2.group(1))
            except json.JSONDecodeError:
                fm = {}
            return fm, m2.group(2).strip()
        return {}, text

    # -- Public parser API --

    def get_index(self) -> dict[str, SkillIndex]:
        """[PARSER] Return loaded skill index. Auto-scans on first call."""
        if not self._loaded:
            self.scan()
        return dict(self._index)

    def get_dag(self, skill_name: str, force_reload: bool = False) -> SkillDAG | None:
        """[PARSER] Get cached SkillDAG for a skill. Re-parses if cache expired."""
        if not self._loaded:
            self.scan()

        if skill_name in self._dag_cache and not force_reload:
            age = time.time() - self._cache_time.get(skill_name, 0)
            if age < CACHE_TTL:
                return self._dag_cache[skill_name]
            logger.debug("SkillParser: cache expired for %s (%.0fs old), re-parsing", skill_name, age)

        # Re-scan to refresh
        if skill_name in self._index:
            idx = self._index[skill_name]
            if idx.path.suffix.lower() == ".json":
                self._parse_json_skill(idx.path)
            else:
                self._parse_md_skill(idx.path)
            return self._dag_cache.get(skill_name)
        return None

    def match_context(self, message: str, max_skills: int = 3) -> str:
        """[PARSER] Match message against skill triggers, return context for model injection."""
        if not self._loaded:
            self.scan()
        matched = []
        msg_lower = message.lower()
        for name, skill in self._index.items():
            score = 0
            for tag in skill.tags:
                if tag.lower() in msg_lower:
                    score += 2
            for trigger in skill.triggers:
                if trigger.lower() in msg_lower:
                    score += 3
            for tool in skill.requires_tools:
                if tool.lower() in msg_lower:
                    score += 1
            if score > 0:
                matched.append((score, skill))
        matched.sort(key=lambda x: x[0], reverse=True)
        selected = matched[:max_skills]
        if not selected:
            return ""
        lines = ["[Skills matched]"]
        for score, skill in selected:
            lines.append(f"  - {skill.name}: {skill.description}")
            if skill.body:
                lines.append(f"    {skill.body[:300]}")
        return "\n".join(lines)

    def build_context(self, message: str, max_skills: int = 3) -> str:
        """[PARSER] Alias for match_context."""
        return self.match_context(message, max_skills)

    def validate_skill(self, skill_name: str) -> dict:
        """[PARSER] Validate a skill definition, return {valid, errors, dag}."""
        dag = self.get_dag(skill_name, force_reload=True)
        if dag is None:
            return {"valid": False, "errors": [f"Skill '{skill_name}' not found"], "dag": None}
        return {
            "valid": dag.is_valid(),
            "errors": dag.parse_errors,
            "dag": dag.to_dict() if dag.is_valid() else None,
        }

    # ================================================================
    # SECTION 2: WorkflowRunner -- runtime execution
    # ================================================================

    def set_generate(self, generate):
        """注入模型生成函数 (供 llm 步骤使用) —— 与 set_gateway 同源的可插拔设计。"""
        self._generate = generate

    def set_gateway(self, gateway):
        """[RUNNER] Set ToolGateway for tool calls."""
        self._gateway = gateway

    # -- Task splitting --

    def split_tasks(self, text: str) -> list:
        """[RUNNER] Split text into sub-tasks using Chinese connectors."""
        self._assert_not_parser("split_tasks")
        for pat, n in self._CONNECTORS:
            m = re.search(pat, text)
            if m:
                tasks = [g.strip() for g in m.groups() if g.strip()]
                if len(tasks) >= 2:
                    return tasks
        return [text]

    def match_workflow(self, message: str) -> dict | None:
        """[RUNNER] Match message against learned workflow patterns."""
        self._assert_not_parser("match_workflow")
        msg_lower = message.lower()
        best_key, best_score = None, 0
        for key, wf in self._workflows.items():
            for pat in wf.get("patterns", []):
                if pat.lower() in msg_lower:
                    score = len(pat) / max(len(msg_lower), 1)
                    if score > best_score:
                        best_score = score
                        best_key = key
        if best_key and best_score > 0.1:
            return self._workflows[best_key]
        return None

    def learn_workflow(self, message: str, steps: list) -> bool:
        """[RUNNER] Learn a workflow from successful execution."""
        self._assert_not_parser("learn_workflow")
        if not steps or len(steps) < 2:
            return False
        first = steps[0].get("skill", "?")
        last = steps[-1].get("skill", "?")
        key = f"{first}+{last}"
        existing = self._find_similar_wf(key)
        if existing:
            self._workflows[existing]["success_count"] = (
                self._workflows[existing].get("success_count", 0) + 1
            )
            self._save_workflows()
            return True
        words = re.findall(r'[\u4e00-\u9fff]{2,6}', message)
        patterns = [message[:30]]
        if len(words) >= 2:
            patterns.append(words[0] + words[-1])
        self._workflows[key] = {
            "patterns": patterns,
            "steps": [{"skill": s["skill"], "tool": s.get("tool", s["skill"])} for s in steps],
            "learned_at": time.time(),
            "success_count": 1,
            "source_message": message[:200],
        }
        self._save_workflows()
        logger.info("Workflow learned: %s", key)
        return True

    def _find_similar_wf(self, key: str) -> str | None:
        for k in self._workflows:
            if key in k or k in key:
                return k
        return None

    def _save_workflows(self):
        fp = self.skills_dir.parent / "data" / "workflows.json"
        try:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps(self._workflows, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save workflows: %s", e)

    def _load_workflows(self):
        fp = self.skills_dir.parent / "data" / "workflows.json"
        if fp.exists():
            try:
                self._workflows = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                self._workflows = {}

    async def execute_workflow(self, workflow: list, plugin_mgr) -> dict:
        """[RUNNER] Execute a parsed workflow, passing context between steps."""
        self._assert_not_parser("execute_workflow")
        if self._ke is None:
            return {"_steps": [], "_success": False, "error": "KnowledgeEngine not set"}
        ctx = {"_steps": [], "_success": True}
        for task in workflow:
            try:
                parsed = task.get("parsed", {})
                sk, sd, ss = parsed.get("skill", (None, None, 0))
                if not sk or ss < 0.02:
                    task["result"] = {"success": False, "error": f"No skill for: {task.get('text', '?')}"}
                    ctx["_success"] = False
                    break
                params = dict(parsed.get("params", {}))
                result = await self._ke.execute(parsed, plugin_mgr)
                task["result"] = result
                ctx["_steps"].append({"skill": sk, "tool": sd.get("tool", ""), "result": result})
                if isinstance(result, dict):
                    ctx["prev"] = result
                if not result.get("success", False):
                    ctx["_success"] = False
                    break
            except Exception as e:
                logger.error("execute_workflow step failed: %s", e)
                task["result"] = {"success": False, "error": str(e)}
                ctx["_steps"].append({"skill": "?", "tool": "?", "error": str(e)})
                ctx["_success"] = False
                break
        return ctx

    async def execute_dag(self, dag: SkillDAG, params: dict, plugin_mgr) -> dict:
        """[RUNNER] Execute a SkillDAG directly with resolved params.
        
        This is the clean path: parser -> DAG -> runner.
        """
        self._assert_not_parser("execute_dag")
        if not dag.is_valid():
            return {"success": False, "error": f"DAG invalid: {dag.parse_errors}"}

        results = {}
        step_outputs = {}
        t0 = time.time()

        for i, step in enumerate(dag.steps):
            step_id = step.get("id", f"step_{i}")
            tool = step.get("tool", "")
            action = step.get("action", "")
            on_error = step.get("on_error", "stop")
            step_input = step.get("input", {})

            # Resolve $params.X references
            resolved_input = {}
            if isinstance(step_input, dict):
                for key, val in step_input.items():
                    if isinstance(val, str) and val.startswith("$params."):
                        ref_name = val[8:]
                        resolved_input[key] = params.get(ref_name, "")
                    elif isinstance(val, str) and val.startswith("$steps."):
                        parts = val[7:].split(".", 1)
                        sid = parts[0]
                        field = parts[1] if len(parts) > 1 else "output"
                        prev = step_outputs.get(sid, {})
                        resolved_input[key] = prev.get(field, prev) if isinstance(prev, dict) else prev
                    else:
                        resolved_input[key] = val

            # Check depends_on
            deps = step.get("depends_on", [])
            if deps:
                for dep in deps:
                    if dep not in step_outputs:
                        if on_error == "skip":
                            continue
                        return {
                            "success": False,
                            "error": f"Step '{step_id}': dependency '{dep}' not completed",
                            "step": step_id,
                        }

            try:
                if tool == "knowledge":
                    result = {"success": True, "output": f"knowledge:{action}"}
                elif tool == "llm":
                    # ★ 2026-09-19 修 (真缺陷): 原来只特判 knowledge → `llm` 步骤
                    #   被当成普通工具发给 gateway → 报"未知工具: llm"。
                    #   生产路径 pipeline._skill_dag_exec 是支持 llm 的, 这里对齐。
                    if self._generate is None:
                        result = {"success": False, "error": "llm 步骤需要 generate (未注入)"}
                    else:
                        _pr = str(resolved_input.get("prompt") or "").strip()
                        if not _pr:
                            result = {"success": False, "error": "llm 步骤缺 prompt"}
                        else:
                            _msgs = []
                            if resolved_input.get("system"):
                                _msgs.append({"role": "system", "content": str(resolved_input["system"])})
                            _msgs.append({"role": "user", "content": _pr})
                            _r = await self._generate(
                                str(resolved_input.get("model") or "deepseek"), _msgs,
                                temperature=float(resolved_input.get("temperature", 0.7)),
                                max_tokens=int(resolved_input.get("max_tokens", 3000)))
                            _txt = str((_r or {}).get("text") or (_r or {}).get("think") or "")
                            if (_r or {}).get("error") or not _txt.strip():
                                result = {"success": False,
                                          "error": f"llm 失败: {str((_r or {}).get('error') or '空回复')[:160]}"}
                            else:
                                result = {"success": True, "output": _txt, "text": _txt}
                elif self._gateway:
                    result = await self._gateway.call(tool, action=action, **resolved_input)
                else:
                    result = {"success": False, "error": "No ToolGateway set"}

                step_outputs[step_id] = result
                # ★ 2026-09-19 修 (真缺陷): 原来这里**硬编码 success=True** —— 工具明明
                #   返回 success=False (如 file_ops "Access denied"、无入口工具的诚实报错),
                #   DAG 仍向上报"成功"。后果: 调用方以为干完了, 用户拿到"✓"但什么都没发生。
                #   生产路径 pipeline._skill_dag_exec 是正确判的 (ok = res.get("success")),
                #   这里对齐它。
                _ok = bool(isinstance(result, dict) and result.get("success", result.get("ok", True)))
                results[step_id] = {"success": _ok, "output": result}
                if not _ok:
                    _e = (result or {}).get("error") or (result or {}).get("output") or "执行失败"
                    logger.warning("DAG step '%s' returned failure: %s", step_id, str(_e)[:200])
                    if on_error == "stop":
                        return {
                            "success": False,
                            "error": f"step '{step_id}': {_e}",
                            "step": step_id,
                            "results": results,
                            "elapsed": round(time.time() - t0, 2),
                        }

            except Exception as e:
                logger.error("DAG step '%s' failed: %s", step_id, e)
                results[step_id] = {"success": False, "error": str(e)}
                if on_error == "stop":
                    return {
                        "success": False,
                        "error": str(e),
                        "step": step_id,
                        "results": results,
                        "elapsed": round(time.time() - t0, 2),
                    }

        return {
            "success": True,
            "results": results,
            "step_outputs": {k: v for k, v in step_outputs.items()},
            "elapsed": round(time.time() - t0, 2),
        }

    # -- Reference resolver --

    def _resolve_refs(self, text: str, params: dict, step_outputs: dict) -> str:
        """[RUNNER] Resolve $params.X and $steps.Y.Z references in text."""
        _re = re

        def _resolve(match):
            ref = match.group(1)
            if ref.startswith("params."):
                key = ref[7:]
                val = params.get(key, "")
                return str(val) if not isinstance(val, dict) else json.dumps(val, ensure_ascii=False)
            elif ref.startswith("steps."):
                parts = ref[6:].split(".", 1)
                step_id = parts[0]
                field = parts[1] if len(parts) > 1 else None
                step_data = step_outputs.get(step_id, {})
                if field:
                    val = step_data.get(field, step_data)
                else:
                    val = step_data
                if isinstance(val, dict):
                    val = val.get("output", val.get("content", val.get("data", val)))
                return str(val) if val else ""
            return match.group(0)

        return _re.sub(r'\$([a-z_.]+)', _resolve, text)


# ================================================================
# Backward compat aliases
# ================================================================
SkillLoader = SkillWorkflowEngine

_skill_engine: Optional[SkillWorkflowEngine] = None


def get_skill_engine(skills_dir: Path = None) -> SkillWorkflowEngine:
    global _skill_engine
    if _skill_engine is None:
        _skill_engine = SkillWorkflowEngine(skills_dir)
    return _skill_engine


def get_skill_index(skills_dir: Path = None) -> SkillWorkflowEngine:
    """[DEPRECATED] Use get_skill_engine() instead."""
    return get_skill_engine(skills_dir)


def reload_skills():
    global _skill_engine
    _skill_engine = None
    return get_skill_engine()
