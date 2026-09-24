"""
AuditAgent -- Tiger.M.M 推理链审计层
=====================================
独立于 verify_fix(错误分类/重试) 的第三方审计: 检查整个推理链条是否存在
逻辑谬误或幻觉, 而不只是"结果是否正确"。

两级审计:
  1. 规则审计 (零成本, 每次有工具轨迹的响应都跑):
     - 声明-工具一致性: 响应声称做了X, 但轨迹里没有对应工具调用
     - 结果-声明矛盾: 响应说"成功"但工具全失败 / 响应说"失败"但工具全成功
     - 幻觉路径: 响应给出具体文件路径/命令, 但轨迹里无任何文件操作
  2. LLM 审计 (可选, 仅任务类响应且轨迹 >= 2 步时触发):
     - 把 用户消息 + 工具轨迹 + 响应 发给轻量模型, 让模型审推理链谬误
     - 走 ollama (免费) 或当前模型, temperature 低, 输出结构化 JSON

审计发现不阻断主流程, 以中文警告形式追加到响应尾部。
"""
import re, json, logging, time

logger = logging.getLogger("core.audit_agent")

# 声称完成/操作的关键词 → 需要轨迹支撑
CLAIM_TOOLS = {
    "发": ["send_email", "wechat_send", "send_msg"],
    "邮件": ["check_mail", "send_email"],
    "搜": ["web_search", "kb_search", "search"],
    "查": ["check_mail", "system_info", "file_ops", "web_search"],
    "写": ["file_ops", "code_engine"],
    "创建": ["file_ops", "code_engine"],
    "删除": ["file_ops"],
    "截图": ["windows_desktop"],
    "天气": ["openmeteo"],
    "知识库": ["kb_search", "kb_import", "kb_list"],
    "文件": ["file_ops"],
    "下载": ["browser", "firecrawl"],
    "网页": ["browser", "web_search", "firecrawl"],
}

# MCP 工具声称检测: 响应声称调用了 mcp_xxx 工具但轨迹无该工具 → 幻觉 (2026-08-18)
MCP_CLAIM_RE = re.compile(r'mcp_[A-Za-z0-9_]+__[A-Za-z0-9_]+')

SUCCESS_WORDS = ["成功", "完成", "搞定", "done", "completed", "已发送", "已保存", "已创建"]
FAIL_WORDS = ["失败", "无法", "错误", "error", "failed", "不能", "没找到", "不存在"]


def _extract_trace_tools(trace) -> set:
    """从工具轨迹提取实际执行过的工具名集合。"""
    tools = set()
    if not trace:
        return tools
    for t in trace:
        if isinstance(t, dict):
            name = t.get("tool") or ""
            if name:
                tools.add(name)
            # 兼容 result 嵌套结构
            r = t.get("result")
            if isinstance(r, dict) and r.get("tool"):
                tools.add(r["tool"])
    return tools


def _trace_has_error(trace) -> bool:
    for t in trace or []:
        if isinstance(t, dict):
            if t.get("success") is False:
                return True
            r = t.get("result")
            if isinstance(r, dict) and (r.get("success") is False or r.get("error")):
                return True
    return False


def audit_response(user_msg: str, response_text: str, trace, limit: int = 3) -> list:
    """规则审计: 返回 findings 列表 [{type, severity, detail}]。"""
    findings = []
    tools_used = _extract_trace_tools(trace)
    has_error = _trace_has_error(trace)
    text = response_text or ""
    if not text.strip():
        return findings

    # ── 规则1: 声明-工具一致性 ──
    # 响应声称"已发邮件/已创建文件/已截图"但轨迹里没有对应工具 → 疑似幻觉
    for kw, expected_tools in CLAIM_TOOLS.items():
        if kw in text:
            claimed_ok = any(w in text for w in SUCCESS_WORDS)
            if claimed_ok and not (tools_used & set(expected_tools)):
                # 排除"建议/应该/可以"等非完成式表达
                if re.search(r'(建议|应该|可以|请|让我|将|准备)', text):
                    continue
                findings.append({
                    "type": "unsupported_claim",
                    "severity": "high",
                    "detail": f"响应声称「{kw}」成功, 但本轮工具轨迹中无 {expected_tools[0]} 调用 — 可能为幻觉。",
                })
                if len(findings) >= limit:
                    return findings

    # ── 规则2: 结果-声明矛盾 ──
    if tools_used:
        says_ok = any(w in text for w in SUCCESS_WORDS)
        says_fail = any(w in text for w in FAIL_WORDS)
        # 2026-08-20: 自检模板 "失败项：无/失败项: 没有" 是完成任务的标准表述, 不算"称失败"
        if re.search(r'失败项\s*[:：]\s*(无|没有|0|零)', text):
            says_fail = False
        if says_ok and has_error:
            findings.append({
                "type": "contradiction",
                "severity": "medium",
                "detail": "响应称操作成功, 但工具轨迹中存在失败记录 — 结果与声明矛盾。",
            })
        elif says_fail and not has_error and tools_used:
            # 工具全成功但响应说失败 (可能为悲观表述, 低危)
            findings.append({
                "type": "contradiction",
                "severity": "low",
                "detail": "工具执行未见失败, 但响应称失败 — 请核实表述是否准确。",
            })
    if len(findings) >= limit:
        return findings

    # ── 规则4: MCP 声称幻觉 (2026-08-18) ──
    # 响应声称调用了 mcp_xxx__yyy 但轨迹里没有该工具 → 模型编造
    for m in MCP_CLAIM_RE.finditer(text):
        claimed_tool = m.group(0)
        if claimed_tool not in tools_used:
            findings.append({
                "type": "mcp_hallucination",
                "severity": "high",
                "detail": f"响应声称调用了 {claimed_tool}, 但本轮工具轨迹中无此调用 — MCP 工具幻觉。",
            })
            if len(findings) >= limit:
                return findings

    # ── 规则3: 幻觉路径 ──
    # 响应中出现具体文件路径, 但轨迹里无 file_ops/browser 等可产出路径的工具
    path_m = re.findall(r'[A-Za-z]:[\\/][^\s，。；、"]+', text)
    if path_m and tools_used and not (tools_used & {"file_ops", "windows_desktop", "browser", "firecrawl", "kb_import"}):
        findings.append({
            "type": "unsupported_path",
            "severity": "medium",
            "detail": f"响应给出具体路径 {path_m[0][:60]}, 但本轮轨迹无文件类工具 — 路径可能来自记忆而非本次操作。",
        })
    return findings[:limit]


AUDIT_PROMPT = """你是审计员。判断虎哥(Agent)的回答是否存在推理谬误或幻觉。

用户问题: {user_msg}

工具执行轨迹:
{trace_text}

虎哥的回答:
{response}

只输出 JSON: {{"verdict": "ok"|"suspicious"|"hallucination", "reason": "不超过50字的中文原因"}}
不要输出其他内容。"""


async def audit_with_llm(model_client, model_name: str, user_msg: str,
                         response_text: str, trace) -> dict:
    """LLM 审计: 审推理链谬误/幻觉。返回 {verdict, reason}。"""
    try:
        trace_text = "\n".join(
            f"  step{t.get('round','?')} {t.get('tool','?')}({t.get('args','')})"
            for t in (trace or [])[:10]
        ) or "  (无工具调用)"
        prompt = AUDIT_PROMPT.replace("{user_msg}", str(user_msg)[:300]) \
            .replace("{trace_text}", trace_text) \
            .replace("{response}", str(response_text)[:1500])
        r = await model_client.generate(
            model_name, [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=200)
        raw = r.get("text", "") or ""
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {"verdict": "ok", "reason": "审计模型未返回有效JSON"}
        result = json.loads(m.group())
        return {
            "verdict": result.get("verdict", "ok"),
            "reason": str(result.get("reason", ""))[:100],
        }
    except Exception as e:
        logger.debug("llm audit failed: %s", e)
        return {"verdict": "ok", "reason": ""}


def format_report(findings) -> str:
    """审计发现 → 中文警告文本(追加到响应尾部)。"""
    if not findings:
        return ""
    lines = ["", "⚠ [审计] 推理链检查发现:"]
    for f in findings:
        lines.append(f"  - {f['detail']}")
    return "\n".join(lines)
