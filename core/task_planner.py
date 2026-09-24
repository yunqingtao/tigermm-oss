"""
TaskPlanner + PlanExecutor — autonomous task decomposition & execution.
=====================================================================
TaskPlanner: fuzzy goal → deepseek → structured plan (JSON)
PlanExecutor: plan → step-by-step execution → result summary

Together they enable: "整理这周邮件，重要的回复，垃圾的删掉"
→ 6 steps → executed → done.
"""
import json, time, logging, re, os
from typing import Optional
from pathlib import Path

logger = logging.getLogger("core.task_planner")

# Plan step schema for LLM
PLAN_SCHEMA = """
[
  {
    "step": 1,
    "tool": "tool_name",
    "action": "action_name",
    "params": {"key": "value"},
    "depends_on": null,
    "description": "what this step does in Chinese"
  }
]
"""

PLANNER_PROMPT = """You are Tiger.M.M's task planner. Decompose user requests into tool-calling steps.

{tools}

IMPORTANT: Every step MUST include "action" and "params" fields.
- For "to" field in send_email, just use the person's name (e.g. "涛哥") — the system auto-resolves names to email addresses. Do NOT add a separate step to look up email addresses.
- check_mail is ONLY for reading actual emails, NOT for looking up people.
- check_mail: action="list" (count=N) or action="read" (mail_id=N) or action="stat"
- send_email: to, subject, body (no action needed)  
- file_ops: action="read" (path) or action="write" (path, content) or action="list" or action="delete" (path)
- openmeteo: city="北京" (no action needed)
- web_search: action="search" (query)
- system_info: action="sysinfo" or action="disk"
- windows_desktop: action="screenshot_now"

ORIGINAL CONTENT RULE:
- When the user asks you to WRITE something (诗/故事/文案/报告…), write ORIGINAL content yourself.
- NEVER reproduce a famous existing work. (Observed failure: returning 《静夜思》 verbatim when asked to 写一首诗.)
- Give it a fresh title and your own lines; keep the length the user asked for.

PATH RULES:
- Use "桌面/filename.txt" for desktop files (NOT just "桌面")
- Use "文档/filename.txt" for documents
- The system automatically resolves "桌面" to the real Desktop path

EXAMPLES:
"查邮件" →
[{{"step":1,"tool":"check_mail","action":"list","params":{{"count":5}},"depends_on":null,"description":"查看最近5封邮件"}}]

"查Agent邮件然后保存到桌面" →
[{{"step":1,"tool":"check_mail","action":"list","params":{{"count":10,"keyword":"Agent"}},"depends_on":null,"description":"查找Agent相关邮件"}},
 {{"step":2,"tool":"check_mail","action":"read","params":{{"mail_id":3}},"depends_on":null,"description":"读取匹配的邮件全文"}},
 {{"step":3,"tool":"file_ops","action":"write","params":{{"path":"桌面/email.txt","content":"$prev.output"}},"depends_on":2,"description":"保存到桌面"}}]

User request: {message}

NEVER use empty object as placeholder — always use $stepN.field references for unknown values. Always reference previous steps with $stepN.field syntax.
Return ONLY a JSON array of steps:"""


class TaskPlanner:
    """Decompose fuzzy goals into executable plans using LLM."""

    def __init__(self, model_client, plugin_manager, mcp_client=None):
        self.model_client = model_client
        self.plugin_manager = plugin_manager
        self.mcp_client = mcp_client  # MCP 外部工具(MCP工具名 mcp_<server>__<tool>)

    def _get_tool_list(self) -> str:
        """Build tool catalog with output format hints for the prompt."""
        catalog = {
            "check_mail": {
                "actions": "list (returns emails array), read (returns single email with id/subject/from/body), stat (returns count)",
                "output": 'list: {success, count, emails: [{id, subject, from, date, body_preview}]}; read: {success, email: {id, subject, from, body}}',
            },
            "send_email": {
                "actions": "send (to, subject, body, attachments)",
                "output": "{success, output}",
            },
            "file_ops": {
                "actions": "read (path), write (path, content), list, delete (path), search (pattern)",
                "output": "read: {success, content}; write: {success, output}; list: {success, output}",
            },
            "openmeteo": {
                "actions": "query (city)",
                "output": "{success, output: Chinese weather text}",
            },
            "web_search": {
                "actions": "search (query)",
                "output": "{success, result}",
            },
            "system_info": {
                "actions": "sysinfo, disk",
                "output": "{success, output}",
            },
            "windows_desktop": {
                "actions": "screenshot_now, capture, click, type_text",
                "output": "screenshot_now: {success, path}",
            },
        }
        lines = []
        for name in sorted(self.plugin_manager._plugins.keys()):
            info = catalog.get(name, {})
            if info:
                lines.append(f"  {name}: {info.get('actions', '')}")
                lines.append(f"    returns: {info.get('output', '')}")
            else:
                plugin = self.plugin_manager._plugins.get(name, {})
                mod = plugin.get("module")
                desc = ""
                if mod and mod.__doc__:
                    desc = mod.__doc__.strip().split('\n')[0]
                lines.append(f"  {name}: {desc}")
        # ── MCP 外部工具追加 (2026-08-18): 让规划器知道 MCP 工具, 拆步骤时优先用而非 shell 命令 ──
        if self.mcp_client is not None:
            try:
                mcp_tools = self.mcp_client.get_all_tools()
                for t in mcp_tools:
                    desc = (t.description or "").split('\n')[0][:80]
                    params = list((t.input_schema or {}).get("properties", {}).keys())
                    lines.append(f"  {t.full_name}: {desc} (参数: {', '.join(params)})")
            except Exception:
                pass
        return '\n'.join(lines)

    async def plan(self, message: str, model: str = "deepseek") -> dict:
        """Decompose message into a plan. Returns {plan, raw, error}."""
        # ── MCP 工具同步刷新: 后台线程可能未完成, 确保规划器看得到 MCP 工具 (2026-08-18) ──
        if self.mcp_client is not None:
            try:
                if not self.mcp_client.get_all_tools() and getattr(self.mcp_client, "servers", {}):
                    await self.mcp_client.refresh_all()
            except Exception:
                pass
        tools = self._get_tool_list()
        prompt = PLANNER_PROMPT.replace("{tools}", tools).replace("{message}", message)

        try:
            raw = await self.model_client.generate(
                model,
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=4000
            )
            text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
        except Exception as e:
            return {"plan": None, "raw": "", "error": f"Model call failed: {e}"}

        if not text:
            return {"plan": None, "raw": "", "error": "Empty response from model"}

        # Extract JSON array — handle truncation
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if not m:
            # Try to salvage: close unclosed array
            if text.strip().startswith('['):
                # Remove last incomplete object and close array
                last_complete = text.rfind('},')
                if last_complete > 0:
                    salvaged = text[:last_complete+1] + '\n]'
                    m = re.search(r'\[.*\]', salvaged, re.DOTALL)
            if not m:
                return {"plan": None, "raw": text[:500], "error": "No valid JSON array in response (possibly truncated)"}

        try:
            plan = json.loads(m.group())
        except json.JSONDecodeError as e:
            return {"plan": None, "raw": text[:500], "error": f"JSON parse error: {e}"}

        # Validate
        if not isinstance(plan, list) or len(plan) == 0:
            return {"plan": None, "raw": text[:500], "error": "Plan is empty or not a list"}

        for i, step in enumerate(plan):
            tool = step.get("tool", "")
            if tool not in self.plugin_manager._plugins:
                return {"plan": None, "raw": text[:500],
                       "error": f"Step {i+1}: unknown tool '{tool}'"}

        return {"plan": plan, "raw": text, "error": None}


#: 本次执行里出现过的"引用回退"说明 (字段名对不上等) —— 供调用方如实告知, 不静默吞。
_FIELD_NOTES: list = []


class PlanExecutor:
    """Execute a plan step by step with dependency resolution."""

    def __init__(self, gateway, skill_executor=None, knowledge_engine=None):
        self.gateway = gateway
        self.skill_executor = skill_executor
        self.ke = knowledge_engine
        self._step_outputs = {}
        self.field_notes: list = []          # ★ 本次执行的引用回退说明

    async def execute(self, plan: list, max_retries: int = 1, step_timeout: int = 30) -> dict:
        """Execute all plan steps. Returns {success, results, summary, errors}."""
        self._step_outputs = {}
        self.field_notes = []
        _FIELD_NOTES.clear()

        # Validate plan before execution
        for step in plan:
            tool = step.get("tool", "")
            if not tool:
                return {"success": False, "steps_total": 0, "steps_ok": 0,
                        "steps_failed": 0, "results": [],
                        "errors": ["Plan step missing tool name"], "elapsed": 0}
            params = step.get("params")
            if params is not None and not isinstance(params, dict):
                _err = "Step " + str(step.get("step", "?")) + ": params must be dict, got " + type(params).__name__
                return {"success": False, "steps_total": 0, "steps_ok": 0,
                        "steps_failed": 0, "results": [],
                        "errors": [_err], "elapsed": 0}
        results = []
        errors = []
        t0 = time.time()

        # ★★ 2026-09-25 改 (真缺陷, 用户实测): 原来是"某步失败**且有人依赖它** → 整个计划停"。
        #   实测证据: 26 题清单被规划成**一条线性链**, 在第 10 步 (EOF) / 第 19 步
        #   (代码编造的 plan_output.txt 读不到) 挂掉 → **后面全部作废**。
        #   用户原话概括得准: "TMM 容易卡在某个点上不继续执行剩余工作"。
        #   现在改成**依赖感知跳过**: 只有"传递依赖该失败步"的步骤被跳过, 其余照跑;
        #   末尾如实汇总跳过了哪几步 —— 不再因为一处失败丢掉整份工作。
        _skipped: dict = {}          # step_num -> 跳过原因

        def _dependents(root):
            """传递闭包: 所有直接/间接依赖 root 的步号。"""
            out, frontier = set(), [root]
            while frontier:
                cur = frontier.pop()
                for _s in plan:
                    try:
                        _sn = int(_s.get("step", 0))
                    except Exception:
                        continue
                    if _sn in out or _sn == root:
                        continue
                    if _s.get("depends_on") == cur:
                        out.add(_sn)
                        frontier.append(_sn)
            return out

        for step in plan:
            step_num = step.get("step", len(results) + 1)
            # (中文前缀解析见下, 内含 2026-09-22 修的双重拼接)
            tool = step.get("tool", "")
            action = step.get("action", "")
            params = dict(step.get("params", {}))
            desc = step.get("description", "")

            # ★ 依赖感知跳过: 前面某步失败且本步(传递)依赖它 → 跳过, **不中断整份计划**
            if step_num in _skipped:
                results.append({"step": step_num, "tool": tool, "success": False,
                                "description": str(desc) + " [已跳过: " + _skipped[step_num] + "]",
                                "result": "", "skipped": True})
                continue

            # Resolve Chinese path prefixes
            for k, v in list(params.items()):
                if k in ("path", "file") and isinstance(v, str) and not (v.startswith("C:") or v.startswith("D:") or v.startswith("/")):
                    prefixes = {"桌面": os.path.expanduser("~/Desktop"),
                               "文档": os.path.expanduser("~/Documents"),
                               "下载": os.path.expanduser("~/Downloads")}
                    for cn, real in prefixes.items():
                        if v.startswith(cn):
                            rest_raw = v[len(cn):]
                            rest = rest_raw.lstrip("/").lstrip("\\")
                            # ★ 2026-09-22 修 (用户实测): 前缀后面**已经是绝对路径**时不许再拼 ——
                            #   实测模型会写成「桌面/<盘符>:\…\Desktop\文件名」(前缀 + 绝对路径叠写,
                            #   因为提示词同时教了"用 桌面/文件名"和"桌面绝对路径是 …");
                            #   旧代码 os.path.join 会拼成「<盘符>:\…\Desktop\<盘符>:\…\Desktop\文件名」
                            #   → 报 Access denied。此时 rest 就是用户要的路径, 直接用。
                            #   (注释用占位符描述: 源码随包出门, 真实路径属个人标识)
                            # ★ 只处理"盘符绝对路径"这一种叠加; **不要**把 "桌面/诗.txt" 里的
                            #   分隔符 "/" 误当成 posix 绝对路径 (我第一版就这么错了, 被自己的
                            #   验证器当场抓出: "桌面/诗.txt" 被解析成 "/诗.txt")。
                            if re.match(r"^[A-Za-z]:[\\/]", rest):
                                params[k] = rest
                            elif rest:
                                params[k] = os.path.join(real, rest)
                            else:
                                # ★★ 2026-09-24 修 (真 bug, 用户实测): 路径只有目录名
                                #   (如 "桌面") 时, 旧代码**自己编一个文件名**
                                #   `plan_output.txt` —— 若这步是**读**, 必然 File not found:
                                #   实测 "把这些题挨个跑一遍" → `Step 19 (file_ops):
                                #   File not found: …\Desktop\plan_output.txt`。
                                #   编造文件名本身就是不可接受的行为(用户没见过这个文件)。
                                #   修法: 写类操作才用默认名(至少产物在预期目录里);
                                #   读/列类操作**不编路径**, 只给目录 —— 下游会如实地报
                                #   "这是目录/缺文件名", 而不是骗人的 File not found。
                                _act = str(action or "").lower()
                                if _act in ("write", "save", "append", "create",
                                            "mkdir", "patch", "rename", "move", "copy"):
                                    params[k] = os.path.join(real, "plan_output.txt")
                                else:
                                    params[k] = real
                            break

            # Normalize list/dict params to strings
            for k, v in list(params.items()):
                if isinstance(v, list):
                    params[k] = ','.join(str(x) for x in v)
                elif isinstance(v, dict):
                    params[k] = str(v)

            # Resolve $prev / $stepN references
            for k, v in list(params.items()):
                if isinstance(v, str) and ('$prev' in v or '$step' in v):
                    refs = re.findall(r'\$(prev|step\d+)(?:\.(\w+))?(?:\[(\d+)\])?(?:\.(\w+))?', v)
                    resolved_val = v
                    for ref in refs:
                        src, field, idx, subfield = ref
                        if src == 'prev':
                            dep = step.get("depends_on")
                            src_key = f"step{dep}" if dep else f"step{step_num - 1}"
                        else:
                            src_key = src
                        src_data = self._step_outputs.get(src_key, {})
                        if not isinstance(src_data, dict):
                            resolved_val = str(src_data)
                            break
                        # ★ 2026-09-22 修: 字段名对不上时, 回退到**工具结果的主文本**,
                        #   而不是整个结果字典。原行为 `src_data.get(field, src_data)`
                        #   会把 {'success': True, 'output': '...'} 整串塞给下游 ——
                        #   实测产出的文件内容就是这坨字典文本 (规划器按提示词写了
                        #   $step1.content, 而该工具真实返回的是 output)。
                        if field and field not in src_data:
                            _fb = None
                            for _k in ("output", "result", "data", "content", "text"):
                                _v = src_data.get(_k)
                                if _v not in (None, "") and not isinstance(_v, (dict, list)):
                                    _fb = _v
                                    break
                            if _fb is not None:
                                note = f"字段 {field} 不存在, 已回退到主文本"
                                val = _fb
                            else:
                                note = f"字段 {field} 不存在, 无主文本可回退 (退回整个结果)"
                                val = src_data
                            if note not in _FIELD_NOTES:
                                _FIELD_NOTES.append(note)
                            if note not in self.field_notes:
                                self.field_notes.append(note)
                        else:
                            val = src_data.get(field, src_data) if field else src_data
                        if idx and isinstance(val, list) and int(idx) < len(val):
                            val = val[int(idx)]
                        if subfield and isinstance(val, dict):
                            val = val.get(subfield, val)
                        resolved_val = str(val) if not isinstance(val, (dict, list)) else str(val)
                    params[k] = resolved_val

            # Auto-fix: if mail_id is empty, extract from depends_on step
            mid = params.get("mail_id")
            if tool == "check_mail" and action == "read":
                mid = params.get("mail_id")
            if mid is None or isinstance(mid, dict) or (isinstance(mid, str) and not str(mid).strip()):
                    dep = step.get("depends_on")
                    if dep:
                        prev = self._step_outputs.get(f"step{dep}", {})
                        logger.warning("auto-fix: dep=%s prev_keys=%s emails_count=%s",
                                      dep,
                                      list(prev.keys()) if isinstance(prev, dict) else "NOT_DICT",
                                      len(prev.get("emails",[])) if isinstance(prev, dict) else "N/A")
                        if isinstance(prev, dict):
                            emails = prev.get("emails", [])
                            if emails and isinstance(emails, list) and len(emails) > 0:
                                params["mail_id"] = emails[0].get("id")
                                logger.warning("auto-fix: SET mail_id=%s (type=%s)", 
                                             params["mail_id"], type(params["mail_id"]).__name__)

            # Default body for send_email
            if tool == "send_email" and "body" not in params:
                params["body"] = params.get("subject", "TMM转发") + "\n\n(请查看附件)"

            # Resolve entity names for send_email
            if self.ke and tool == "send_email":
                for pk in ("to",):
                    if pk in params and params[pk] and "@" not in str(params[pk]):
                        r = self.ke.lookup_entity_field(params[pk], "email")
                        if r and "email" in r:
                            params[pk] = r["email"]

            # Strip .send action for tools that don't accept it
            no_action_tools = {"send_email", "openmeteo", "system_info"}
            if tool in no_action_tools and action in ("send", ""):
                action = ""

            # Execute
            ok = False
            result = None
            for attempt in range(max_retries + 1):
                try:
                    if action:
                        result = await self.gateway.call(tool, action=action, timeout=step_timeout, **params)
                    else:
                        result = await self.gateway.call(tool, timeout=step_timeout, **params)
                except Exception as e:
                    result = {"success": False, "error": str(e)}

                ok = isinstance(result, dict) and result.get("success", False)
                if ok:
                    break
                if attempt < max_retries:
                    logger.info("PlanExecutor: step %d retry %d", step_num, attempt + 1)

            self._step_outputs[f"step{step_num}"] = result or {}
            results.append({
                "step": step_num,
                "tool": tool,
                "success": ok,
                "description": desc,
                "result": str(result)[:200] if result else "",
            })

            if not ok:
                err = result.get("error", "unknown") if isinstance(result, dict) else str(result)
                errors.append(f"Step {step_num} ({tool}): {err[:100]}")
                # ★ 旧行为: `if has_dep: break` —— 线性计划里每步都被依赖 ⇒ 一挂全停。
                #   新行为: 只让**传递依赖它**的步骤跳过, 不相干的步骤继续跑完。
                for _dn in _dependents(step_num):
                    _skipped.setdefault(_dn, f"依赖的第 {step_num} 步失败")

        elapsed = round(time.time() - t0, 2)
        all_ok = len(errors) == 0

        return {
            "success": all_ok,
            "steps_total": len(plan),
            "steps_ok": sum(1 for r in results if r["success"]),
            "steps_failed": sum(1 for r in results
                                if not r["success"] and not r.get("skipped")),
            "steps_skipped": sum(1 for r in results if r.get("skipped")),
            "results": results,
            "errors": errors,
            "elapsed": elapsed,
            # ★ 引用回退如实带出 (字段名对不上时不再静默): 调用方可在响应里提示
            "field_notes": list(self.field_notes or _FIELD_NOTES),
        }

    def format_summary(self, result: dict) -> str:
        """Format execution result as readable text."""
        _sk = result.get("steps_skipped", 0)
        _tail = f" (另跳过 {_sk} 步)" if _sk else ""
        lines = [f"\n=== 执行结果: {result['steps_ok']}/{result['steps_total']} 步成功{_tail} ==="]
        for r in result.get("results", []):
            icon = "⏭️" if r.get("skipped") else ("✅" if r["success"] else "❌")
            lines.append(f"  {icon} [{r['step']}] {r['tool']}: {r['description']}")
        if result.get("errors"):
            lines.append(f"\n错误:")
            for e in result["errors"]:
                lines.append(f"  - {e}")
        return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════
# 清单拆分 (2026-09-25 加)
# ═══════════════════════════════════════════════════════════════════════
# 为什么需要 (用户实测 + 用户自己总结的):
#   用户把"一份清单"(26 道题)整段粘进来 → 规划器把它编成**一条线性链** →
#   第 10 步 (EOF) / 第 19 步 (读一个代码自己编出来的文件) 挂掉 → 后面全部作废。
#   用户原话: "你把题目单独拿出来 TMM 会做。你给他一个文档, 他执行困难,
#              这是不是分析拆解执行的问题, 就算执行 TMM 还容易卡在某个点上
#              不继续执行剩余工作。"  —— 两点都对。
#   这些条目**彼此互不依赖** ⇒ 正确做法是拆成 N 个独立任务, 各自走自己的路由
#   (该走技能的走技能), 一个失败不影响其他。
#
# ★ 判据保守: 宁可判成"不是清单"(退回原行为), 也别把普通长文误拆成一堆垃圾任务。

#: 明显是"说明/元信息/装饰"的行 —— 不算任务
_META_LINE = re.compile(
    r"(^\s*[>#]|`|→|-->|／"
    r"|跑前须知|跑完|注意事项|必须|说明[:：]|注意[:：]|规则[:：]"
    r"|预检|结论|分布|当时的|不会白跑|真引擎|兜底"
    r"|哪题不对|粘回来|原文输出"
    r"|用什么|模式必须)"
)
#: 看着像"一件事"的迹象
_TASK_HINT = re.compile(
    r"([A-Za-z]:[\\/]"                       # 带盘符路径
    r"|写|做|画|查|列|搜|分|生成|保存|存到|发给|导入|导出|分析|统计"
    r"|看|帮我|把|给|现在|几点|天气|多少|怎么|多少|复式)"
)
#: 列表标记 (只剥纯符号/纯数字编号; **保留** A1/B2 这类带字母的标签 —— 便于汇报)
_LIST_MARK = re.compile(r"^\s*(?:[-*·•—]|\(?\d+\s*[.)、])\s*")


def split_independent_tasks(message: str) -> list:
    """把"一份清单"拆成多个独立任务。

    返回 [{"label": "A1", "text": "北京天气"}, ...];
    **不像清单就返回空列表** (空 = 调用方按原行为处理)。
    """
    if not message or len(message) < 40:
        return []
    items, seen = [], set()
    for raw_line in message.splitlines():
        s = raw_line.strip()
        # ★ 先剥掉行尾的"← 注释" —— 清单里常用 ← 写备注,
        #   而备注里可能带"跑完/说明"等词, 会误触发下面的元信息过滤 (实测 B3 被误滤)。
        if "←" in s:
            s = s.split("←", 1)[0].strip()
        if not s or s in seen:
            continue
        if "|" in s:                       # markdown 表格行 (含 | 一律不算任务)
            continue
        if s.rstrip().endswith(("：", ":")):   # "当时的分布：" 这类引出行
            continue
        if _META_LINE.search(s):
            continue
        if set(s) <= set("-=*#_—–~ `"):        # 纯装饰线 / 代码围栏
            continue
        c = _LIST_MARK.sub("", s).strip()
        if len(c) < 4:
            continue
        # 提取 "A1"/"1."/"C6" 这类前缀做标签 (执行时剥掉, 汇报时用它)
        m = re.match(r"^([A-Za-z]\d{1,2})\s*[.、:：]?\s+(.+)$", c)
        if m:
            label, text = m.group(1).upper(), m.group(2).strip()
        else:
            label, text = "", c
        if len(text) < 4:
            continue
        seen.add(s)
        items.append({"label": label, "text": text})
    if len(items) < 3:
        return []                              # 少于 3 条不当清单 (太容易误判)
    like = sum(1 for it in items
               if _TASK_HINT.search(it["text"]) or len(it["text"]) >= 12)
    if like < max(3, int(len(items) * 0.6)):
        return []
    return items


def format_batch_plan_text(items: list) -> str:
    """给用户看的"将拆成 N 个独立任务"方案。"""
    lines = [f"[规划模式] 检测到 **{len(items)} 项独立任务**。",
             "这些条目彼此不依赖 —— 我会**逐个独立执行**(各自走自己的路由),",
             "某一项失败**不会**影响其余项。确认后开始:", ""]
    for it in items:
        tag = f"{it['label']}  " if it.get("label") else ""
        lines.append(f"  {tag}{it['text']}")
    lines.append("")
    lines.append("回「执行」/「好的」开始; 想改就直说。")
    return "\n".join(lines)

