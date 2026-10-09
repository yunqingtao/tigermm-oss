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

# ── 2026-09-26 加: 给用户看的正文里**不该出现**的内部标记 ──────────────────
# 真调用走 function calling 通道, 标记不会进正文 ⇒ 一旦出现就是"调用没执行、
# 被当文本吐出来了"(实测全史 10 轮)。零误报。
RAW_TOOL_MARKUP_RE = re.compile(r'<tool_call>|</?function=|<parameter=|\[\s*"?tool_calls"?\s*\]')

# ── 2026-09-26 加: 整条回复就是一行内部错误/日志的特征词 ────────────────────
# 只是"像日志"不够 (会误伤正常的报错并解释) —— 调用处**同时**要求短且无解释。
RAW_LOG_LINE_RE = re.compile(
    r'(Traceback \(most recent call last\)|EOF when reading|'
    r'^\s*Step\s*\d+\s*\(|^\s*(ERROR|Error|WARNING|CRITICAL)\s*[:：]|'
    r'^\s*File\s+"[^"]+",\s*line\s+\d+)', re.M)

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
    # ★ 2026-09-28 修正 (评测原生跑分实测出的假警报)
    #   资格集里漏了 **shell_exec** —— 而 shell 恰恰是实测中最常用的落盘手段。
    #   后果: TMM 用重定向/`python -c` 真把文件写出来了, 审计却报
    #   "响应给出具体路径…但本轮轨迹无文件类工具 — 路径可能来自记忆"(=暗指幻觉),
    #   用户看到的是"它在编", 实际上文件就在盘上。**假警报比不报警更伤信任** ——
    #   它会让用户学会忽略告警, 于是真幻觉也一起被忽略。
    #   code_engine 同理 (技能层落盘走它)。
    _PATH_TOOLS = {"file_ops", "windows_desktop", "browser", "firecrawl", "kb_import",
                   "shell_exec", "code_engine"}
    if path_m and tools_used and not (tools_used & _PATH_TOOLS):
        findings.append({
            "type": "unsupported_path",
            "severity": "medium",
            "detail": f"响应给出具体路径 {path_m[0][:60]}, 但本轮轨迹无文件类工具 — 路径可能来自记忆而非本次操作。",
        })
    if len(findings) >= limit:
        return findings[:limit]

    # ── 规则5: 工具调用被当文本吐给用户 (2026-09-26) ──
    # 起因 (实测全史): 252 个助手轮次里有 **10 轮**的回复正文就是一段
    #   `<tool_call><function=web_search><parameter=query>…`
    # —— 模型把工具调用**当普通文本写出来了**, 于是它**没有执行**,
    #   而用户看到的是内部标记而不是答案 (10 轮全在 general 路由)。
    # 为什么这条几乎零误报: 健康的回复**不可能**含这种标记 —— 真调用走的是
    #   function calling 通道, 标记不会出现在给用户看的正文里。
    # 为什么必须现在加: 现有规则1~4 全都漏它 (它们只查"声称"与"路径"),
    #   而 CLAIM_TOOLS 里一个声称词都对不上 ⇒ 这类回复一路无人过问。
    # ★ 已知局限 (诚实记下): 若某天有回复**故意举例**说明工具调用格式
    #   (如"格式是 <tool_call>…"), 这条会误报。全史 252 轮未出现该情形,
    #   且本审计**只报警不阻断**, 故先这样; 真出现再说 (别提前加"提到示例就跳过"
    #   的软化判据 —— 那会让真泄漏恰好因为带"示例"二字而漏网)。
    markup = RAW_TOOL_MARKUP_RE.search(text)
    if markup:
        findings.append({
            "type": "tool_markup_leak",
            "severity": "high",
            "detail": f"回复正文里出现了工具调用标记 {markup.group(0)!r} — "
                      f"工具**没有执行**, 用户看到的是内部标记而不是答案。",
        })
        if len(findings) >= limit:
            return findings[:limit]

    # ── 规则6: 整条回复就是一行内部错误/日志 (2026-09-26) ──
    # 起因 (实测全史): 2 轮 —— id=24 `Step 1 (output): Must provide city or both
    #   lat and lon`、id=638 `Step 10 (file_ops): EOF when reading a line`。
    #   执行器把**裸错误行**直接当成回复内容发了出去, 用户拿到一行天书。
    # ★ 判据必须咬住"**开头就是**日志"这一条, 别只 search:
    #   第一版用 search 判, 结果把"报错 + 成段解释"的正常回复也报了 ——
    #   那句里 `EOF when reading a line` 出现在**句子中间**
    #   ("刚才那一步失败了：EOF when reading a line —— 原因是我等不到输入…")。
    #   实测到这一点的是本门禁的反向控制 B4, 不是我看出来的。
    _first = next((ln.strip() for ln in text.strip().splitlines() if ln.strip()), "")
    if _first and RAW_LOG_LINE_RE.match(_first):
        _body = re.sub(r"[\s\W_]+", "", text)
        if len(text.strip()) <= 160 and text.count("\n") <= 2 and len(_body) <= 120:
            findings.append({
                "type": "raw_log_line",
                "severity": "medium",
                "detail": f"整条回复就是一行内部错误/日志 ({text.strip()[:60]!r}) — "
                          f"用户看到的是执行器的报错行, 不是人话。",
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
