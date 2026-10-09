"""
SkillExecutor — executes skill.json steps via ToolGateway.
==========================================================
Resolves $params.X and $steps.Y.Z references, handles on_error,
records success/failure to SkillRegistry.
"""
import re, logging, time, os
from typing import Optional

logger = logging.getLogger("core.skill_executor")

# Regex for variable references
REF_RE = re.compile(r'\$(\w+)\.(\w+)(?:\.(\w+))?')

class SkillExecutor:
    """Execute a skill definition's steps sequentially."""

    def __init__(self, gateway, skill_registry=None, knowledge_engine=None):
        self.gateway = gateway
        self.registry = skill_registry
        self.ke = knowledge_engine
        self._last_result = None
        self._step_outputs = {}

    async def execute(self, skill: dict, message: str = "", **override_params) -> dict:
        """Execute a skill's steps. Returns {success, output, steps, skill_name}."""
        name = skill.get("name", "unknown")
        steps = skill.get("steps", [])
        skill_params = skill.get("params", {})
        t0 = time.time()

        if not steps:
            return {"success": False, "error": f"Skill '{name}' has no steps"}

        # 1. Extract params from message
        params = self._extract_params(message, skill_params)
        params.update(override_params)

        # 2. Execute steps
        self._step_outputs = {}
        all_ok = True
        errors = []

        for i, step in enumerate(steps):
            step_id = step.get("id", f"step_{i}")
            tool = step.get("tool", "")
            action = step.get("action", "")
            on_error = step.get("on_error", "stop")
            raw_input = step.get("input", {})

            # Resolve references
            resolved = self._resolve(raw_input, params)

            # Execute
            try:
                if tool == "knowledge" and action == "query_entity":
                    # Entity lookup via knowledge engine
                    name = resolved.get("name", "")
                    field = resolved.get("field", "email")
                    if self.ke:
                        r = self.ke.lookup_entity_field(name, field)
                        if r and field in r:
                            result = {"success": True, field: r[field]}
                        else:
                            result = {"success": False, "error": f"未找到联系人: {name}"}
                    else:
                        result = {"success": False, "error": "Knowledge engine not available"}
                elif action:
                    result = await self.gateway.call(tool, action=action, **resolved)
                else:
                    result = await self.gateway.call(tool, **resolved)
            except Exception as e:
                result = {"success": False, "error": str(e)}

            ok = isinstance(result, dict) and result.get("success", False)
            self._step_outputs[step_id] = result

            if not ok:
                err = result.get("error", "unknown") if isinstance(result, dict) else str(result)
                errors.append(f"[{step_id}] {tool}: {err[:100]}")
                logger.warning("SkillExecutor: %s step %d (%s) FAILED — %s", name, i, tool, err)
                if on_error == "stop":
                    all_ok = False
                    break
            else:
                logger.debug("SkillExecutor: %s step %d (%s) OK", name, i, tool)

        # 3. Return
        elapsed = round(time.time() - t0, 2)
        final = self._step_outputs.get(steps[-1].get("id", ""), {}) if all_ok else {}

        result = {
            "success": all_ok,
            "skill_name": name,
            "elapsed": elapsed,
            "steps_executed": len(self._step_outputs),
            "errors": errors,
        }

        if all_ok and isinstance(final, dict):
            result["output"] = final.get("output", str(final))

        # Record call
        if self.registry:
            try:
                self.registry.record_call(name, all_ok)
            except Exception:
                pass

        return result

    def _extract_params(self, message: str, schema: dict) -> dict:
        """Extract param values from user message based on skill schema."""
        params = {}
        for pname, pinfo in schema.items():
            ptype = pinfo.get("type", "text")
            if ptype == "entity":
                # Extract known entity name from message
                if self.ke:
                    for ename in self.ke.entities:
                        if ename in message:
                            params[pname] = ename
                            break
            elif ptype == "path":
                # Find file path in message, resolving Chinese directory prefixes
                m = re.search(r'(\S+\.(?:txt|docx?|pptx?|pdf|py|md|xlsx|csv|json|png|jpg|zip|mp3|wav|m4a))', message)
                if m:
                    raw = m.group(1)
                    # Resolve Chinese directory prefixes
                    prefixes = {"桌面": os.path.expanduser("~/Desktop"),
                               "文档": os.path.expanduser("~/Documents"),
                               "下载": os.path.expanduser("~/Downloads")}
                    resolved = raw
                    for cn, real in prefixes.items():
                        if raw.startswith(cn):
                            # Strip prefix and combine
                            fname = raw[len(cn):]
                            resolved = os.path.join(real, fname)
                            break
                    params[pname] = resolved
            elif ptype == "text":
                # Content = everything after recipient and action keywords
                # Simplified: strip known patterns
                content = message
                for kw in ["发给", "发送给", "告诉", "通知", "发邮件给", "附件发给"]:
                    content = content.replace(kw, " ")
                params[pname] = re.sub(r'\s+', ' ', content).strip()
        return params

    def _resolve(self, data: dict, params: dict) -> dict:
        """Resolve $params.X and $steps.Y.Z references in a dict."""
        resolved = {}
        for key, value in data.items():
            if isinstance(value, str):
                resolved[key] = self._resolve_str(value, params)
            else:
                resolved[key] = value
        return resolved

    def _resolve_str(self, value: str, params: dict) -> str:
        """Resolve variable references in a string."""
        def _replace(m):
            scope = m.group(1)  # params or steps
            name = m.group(2)   # param name or step id
            field = m.group(3)  # optional field

            if scope == "params":
                return str(params.get(name, ""))
            elif scope == "steps":
                step_data = self._step_outputs.get(name, {})
                if isinstance(step_data, dict) and field:
                    return str(step_data.get(field, ""))
                return str(step_data)
            return m.group(0)

        return REF_RE.sub(_replace, value)

    async def _query_entity(self, params: dict) -> dict:
        """Lookup entity in knowledge engine via gateway.plugin_mgr."""
        # This is a bridge — knowledge lookups go through the pipeline's ke
        # For now, return the name as email (caller should provide real resolution)
        name = params.get("name", "")
        field = params.get("field", "email")
        # Gateway doesn't have direct knowledge access — return placeholder
        return {
            "success": True,
            field: name,  # Will be resolved by intent_router's ke in pipeline
        }


# Global
_executor: Optional[SkillExecutor] = None

def get_executor(gateway=None, registry=None, ke=None) -> SkillExecutor:
    global _executor
    if _executor is None and gateway is not None:
        _executor = SkillExecutor(gateway, registry, ke)
    elif gateway is not None:
        _executor.gateway = gateway
        if registry:
            _executor.registry = registry
        if ke:
            _executor.ke = ke
    return _executor
