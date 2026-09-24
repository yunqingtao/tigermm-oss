
"""
Tiger.M.M 脑子 — 决策树路由引擎
================================
不是流水线。是决策树。一句话进来，判断属于哪条路，走通就停。

        快速匹配 → 463规则 + 已学工作流 + 记忆召回 (0.0s)
        分类路由 → 工作流? 实体? 技能? 意图? (0.0s)
        拜师     → deepseek教一次 → 学会 → 升级到快速匹配
"""

import re
import time
import logging

logger = logging.getLogger("mary3.brain")

class TigerBrain:
    """虎哥的脑子。一句话进来，决策→路由→执行。"""

    def __init__(self, auto_guide, knowledge_engine, workflow_engine,
                 intent_reasoner, plugin_mgr, model_client):
        self.ag = auto_guide
        self.ke = knowledge_engine
        self.we = workflow_engine
        self.ir = intent_reasoner
        self.pm = plugin_mgr
        self.mc = model_client
        self._quick_workflows = {}
        from core.memory import TigerMemory
        self.mem = TigerMemory()

    # ═══════════════════════════════════
    # 决策树入口
    # ═══════════════════════════════════

    async def think(self, message, ext_model=None):
        """脑子主循环。不是串行流水线，是决策树路由。"""
        t0 = time.time()
        self.mem.add_turn("user", message)

        # 分支1: 快速匹配
        result = self._quick_match(message, ext_model)
        if result:
            result["elapsed"] = round(time.time() - t0, 2)
            result["model"] = "local"
            self.mem.add_turn("assistant", result.get("response", ""))
            return result

        # 分支2: 分类路由
        route = self._classify(message, ext_model)
        result = await self._route(route, message, t0)
        if result:
            self.mem.add_turn("assistant", result.get("response", ""))
            return result

        # 分支3: 拜师
        return await self._ask_model(message, ext_model, t0)

    # ═══════════════════════════════════
    # 分支1: 快速匹配
    # 优先级: 元问题 > 记忆召回 > 身份 > tech > 工作流
    # ═══════════════════════════════════

    # 动作指令特征 —— 命中任一 → 罐头答案让位 (让路由表处理真实请求)
    _ACTION_RE = re.compile(
        r'(?:[\w.+-]+@[\w-]+\.[\w.]+)'                       # 邮箱
        r'|(?:https?://)'                                       # URL
        r'|(?:主题|内容|正文)\s*[:：]'                          # 邮件字段
        r'|(?:[A-Za-z]:[\\/])'                                 # 绝对路径
        r'|(?:\.(?:txt|docx?|pptx?|pdf|xlsx?|csv|json|md|png|jpe?g|zip)\b)'  # 文件扩展名
    )
    _ACTION_VERBS = ["发给", "发送给", "发邮件", "写邮件", "发封邮件", "写入", "保存到", "存到",
                     "创建", "新建", "删除", "复制", "移动到", "重命名", "截图", "截屏",
                     "下载", "打开", "搜索", "搜一下", "查一下", "翻译", "整理"]

    @classmethod
    def _looks_like_action_request(cls, message: str) -> bool:
        """这条消息是"要我干活"还是"问我/跟我打招呼"?
        干活的交给路由表; 罐头答案只答后者。"""
        if cls._ACTION_RE.search(message):
            return True
        return any(v in message for v in cls._ACTION_VERBS)

    def _quick_match(self, message, ext_model=None):
        """463条规则 + 已学工作流 — 精确命中直接答"""
        if ext_model and ext_model != "auto":
            return None

        has_connector = any(w in message for w in
                           ["然后", "接着", "先", "再", "之后", "并", "同时"])

        # 1. 元问题: "刚才说了什么/学了什么"
        if any(kw in message for kw in ["刚才", "刚刚", "上一句"]):
            if "学" in message or "记住" in message:
                last = self.mem.last_user_message()
                return {"response": "刚才: " + last, "intent": "memory_recall"}
            if "说" in message or "问" in message:
                recent = self.mem.recent(5)
                lines = []
                for t in recent:
                    role = "你" if t["role"] == "user" else "我"
                    lines.append(role + ": " + t["text"][:80])
                return {"response": chr(10).join(lines), "intent": "memory_recall"}

        # 2. 记忆召回: "以前聊过xxx吗"
        if message.startswith("以前") or message.startswith("之前") or "还记得" in message:
            results = self.mem.recall(message, limit=3)
            if results:
                items = ["[" + r["ago"] + "] " + r["content"][:100] for r in results]
                return {"response": "想起来这些:" + chr(10) + chr(10).join(items),
                        "intent": "memory_recall"}
            else:
                return {"response": "没想起来。记忆库还是空的，多聊几句就有了。",
                        "intent": "memory_recall"}

        # 3. 身份规则 (463条)
        # ★★ 2026-09-19 修 (真缺陷): 罐头答案是**子串匹配** → 只要消息里出现"你好"就触发。
        #   实测: "发邮件给 x@y.com 主题:嗨 内容:你好" → 回"你好！虎哥在此" , 发信请求被吞。
        #   同类: "搜索一下 Python 装饰器" → 被 tech 罐头(conda 答案)截走。
        #   原则: AutoGuide 是**知识/身份缓存**, 不是动作路由 —— 只答问句/陈述,
        #   遇到"动作指令"(含邮箱/URL/主题:/内容:/路径/工具动词)就该让位给路由表。
        if self._looks_like_action_request(message):
            result = None
        else:
            result = self.ag.respond(message)
        if result:
            intent = result.get("intent", "general")
            if intent in ("identity", "greeting"):
                return {"response": result.get("response", ""), "intent": intent}
            is_entity_op = any(kw in message for kw in
                              ["记住", "的电话", "的邮箱", "的手机", "的地址", "的爱好", "的公司", "的职位", "的生日", "的微信", "的备注", "卡片", "是谁"])
            if not has_connector and not is_entity_op and intent in (
                    "tech", "knowledge", "learned_action", "learned"):
                return {"response": result.get("response", ""), "intent": intent}

        # 4. 已学工作流
        wf = self.we.match(message)
        if wf and wf.get("desc"):
            return {"response": wf["desc"], "intent": "workflow_cached"}

        return None

    # ═══════════════════════════════════
    # 分支2: 分类 → 路由
    # ═══════════════════════════════════

    def _classify(self, message, ext_model=None):
        """同时判断消息类型。返回路由标签。"""
        if ext_model and ext_model != "auto":
            return "model"

        # 1. 工作流? (连接词 + 能拆>=2步)
        has_connector = any(w in message for w in
                           ["然后", "接着", "先", "再", "之后", "并", "同时"])
        if has_connector:
            tasks = self.we.parse(message)
            if len(tasks) >= 2:
                return "workflow"

        # 2. 实体学习?
        if "记住" in message and (":" in message or "：" in message or
                                   any(kw in message for kw in ["电话", "邮箱", "手机"])):
            return "entity_learn"

        # 3. 实体查询?
        if any(kw in message for kw in
               ["的邮箱", "的电话", "的手机", "的地址", "是谁", "是哪个"]):
            return "entity_query"

        # 4. 意图推理 (优先于技能)
        try:
            ir = self.ir.reason(message)
            if ir.get("confidence", 0) >= 0.5 and ir.get("intent") != "unknown":
                return "intent"
        except Exception:
            pass

        # 5. 技能匹配
        parsed = self.ke.parse(message)
        sk = parsed.get("skill", (None, None, 0))
        if sk[0] and sk[2] > 0.02:
            return "skill"

        return "model"

    async def _route(self, route, message, t0):
        """路由到对应处理器"""
        try:
            if route == "workflow":
                return await self._handle_workflow(message, t0)
            elif route == "entity_learn":
                r = self.ke.learn_entity(message, None)
                if r.get("success"):
                    return {**r, "intent": "entity_learn", "model": "local",
                            "elapsed": round(time.time() - t0, 2)}
            elif route == "entity_query":
                r = self.ke.query_entity(message, None)
                if r.get("found"):
                    return {**r, "intent": "entity_query", "model": "local",
                            "elapsed": round(time.time() - t0, 2)}
            elif route == "skill":
                parsed = self.ke.parse(message)
                result = await self.ke.execute(parsed, self.pm)
                if result.get("success"):
                    return {"response": "OK " + parsed["skill"][0] + ": " +
                            str(result.get("output", "done")),
                            "intent": "knowledge_exec", "model": "local",
                            "elapsed": round(time.time() - t0, 2)}
                # 失败诚实报出 (不要静默 return None 让模型猜出个"已完成")
                _emsg = str(result.get("error") or "").strip()
                if _emsg:
                    return {"response": f"✗ {parsed['skill'][0]} 没执行成功: {_emsg}",
                            "intent": "knowledge_exec", "model": "local",
                            "elapsed": round(time.time() - t0, 2)}
            elif route == "intent":
                ir = self.ir.reason(message)
                return await self._handle_intent(ir, t0)
        except Exception as e:
            logger.debug("Route %s failed: %s", route, e)
        return None

    # ═══════════════════════════════════
    # 路由处理器
    # ═══════════════════════════════════

    async def _handle_workflow(self, message, t0):
        tasks = self.we.parse(message)
        if len(tasks) < 2:
            return None
        ctx = await self.we.execute(tasks, self.pm)
        if ctx.get("_success"):
            self.we.learn(message, ctx["_steps"])
            steps_desc = " -> ".join(s["skill"] for s in ctx["_steps"])
            return {"response": "OK " + steps_desc,
                    "intent": "workflow_exec", "model": "local",
                    "elapsed": round(time.time() - t0, 2)}
        return None

    async def _handle_intent(self, ir, t0):
        slots = ir.get("slots", {})
        intent = ir["intent"]
        if intent == "send" and slots.get("to"):
            raw_what = ir["entities"].get("what", "")
            has_file = raw_what and "." in raw_what and len(raw_what) <= 50
            if slots.get("path") and not has_file:
                try:
                    from storage.file_secure import safe_list_dir
                    items = safe_list_dir(slots["path"])
                    fl = ["  " + i.name + ("/" if i.is_dir() else "") +
                          " (" + str(i.stat().st_size) + "B)" for i in items[:30]]
                    listing = chr(10).join(fl) or "(empty)"
                except Exception as e:
                    listing = "(error: " + str(e) + ")"
                return {"response": "[" + ir["entities"].get("who", "?") +
                        "] 目录:" + chr(10) + listing + chr(10) + chr(10) + "要发哪个文件？",
                        "intent": "file_send_list", "model": "local",
                        "elapsed": round(time.time() - t0, 2)}
        elif intent == "list" and slots.get("path"):
            try:
                from storage.file_secure import safe_list_dir
                items = safe_list_dir(slots["path"])
                fl = ["  " + i.name + ("/" if i.is_dir() else "") for i in items[:50]]
                return {"response": chr(10).join(fl) or "(empty)",
                        "intent": "list_dir", "model": "local",
                        "elapsed": round(time.time() - t0, 2)}
            except Exception:
                pass
        elif intent == "screenshot":
            result = await self.pm.call("windows_desktop", action="screenshot")
            return {"response": "OK 截图已保存", "intent": "screenshot",
                    "model": "local", "elapsed": round(time.time() - t0, 2)}
        elif intent == "system":
            result = await self.pm.call("system_info")
            return {"response": str(result), "intent": "system_info",
                    "model": "local", "elapsed": round(time.time() - t0, 2)}
        return None

    # ═══════════════════════════════════
    # 分支3: 拜师
    # ═══════════════════════════════════

    async def _ask_model(self, message, ext_model, t0):
        model = ext_model if (ext_model and ext_model != "auto") else "deepseek"
        try:
            ctx = self.mem.build_context_for_model(message)
            system_content = "虎哥(Tiger.M.M)，济南本地AI Agent。大哥贠庆涛(涛哥)。中文回复，自称虎哥。"
            if ctx:
                system_content += chr(10) + chr(10) + ctx
            response = await self.mc.chat(model, [
                {"role": "system", "content": system_content},
                {"role": "user", "content": message}
            ])
            if response:
                self.mem.remember(message, "user")
                self.mem.remember(response, "assistant")
                self.mem.add_turn("assistant", response)
                return {"response": response, "intent": "general",
                        "model": model, "elapsed": round(time.time() - t0, 2)}
        except Exception as e:
            logger.warning("Model %s failed: %s", model, e)
        return {"response": "虎哥暂时没法回答。试试换个说法？",
                "intent": "error", "model": "local",
                "elapsed": round(time.time() - t0, 2)}
