"""
PerceptionEngine -- Tiger.M.M 感知层
===================================
不是存，是炼。每次对话后从记忆里提取模式、更新认知、修正行为。

三层感知:
  1. 模式提取 -- 从对话历史里发现规律
  2. 环境感知 -- 记住路径、实体、上下文
  3. 行为更新 -- 被纠正过的事不再犯
"""

import os, re, json, time, logging
from pathlib import Path
from collections import Counter
from typing import Optional

logger = logging.getLogger("core.perception")

DATA_DIR = Path(__file__).parent.parent / "data"
# ★ 隔离变量 (2026-09-20 加, 同 TMM_SESSION_DB 模式):
#   验证/门禁/测试子进程可指到沙箱文件, 保证**永不碰用户真实感知库**。
#   实测来由: 探针句子曾被写进真实 data/perception.json, 且引擎内存态还会回写。
PERCEPTION_FILE = Path(os.environ.get("TMM_PERCEPTION_FILE") or (DATA_DIR / "perception.json"))


class PerceptionEngine:
    """每次对话后分析，更新感知模型。"""

    def __init__(self):
        self.data = self._load()

    def _load(self) -> dict:
        if PERCEPTION_FILE.exists():
            try:
                data = json.loads(PERCEPTION_FILE.read_text(encoding="utf-8"))
                # 惰性迁移: 旧数据无 relations 时从 entities 生成初始图谱
                if "relations" not in data:
                    rels = []
                    for name, info in (data.get("entities") or {}).items():
                        for field in ("email", "phone", "path", "type"):
                            if info.get(field):
                                rels.append({
                                    "subject": name, "predicate": field,
                                    "object": str(info[field]), "weight": 1.0,
                                    "last_seen": info.get("last_seen", time.time()),
                                    "examples": [f"迁移自entities.{name}.{field}"],
                                })
                    data["relations"] = rels
                return data
            except Exception:
                pass
        return {
            "preferences": {},
            "environment": {},
            "entities": {},
            "relations": [],  # 语义图谱三元组: {subject, predicate, object, weight, last_seen, examples}
            "corrections": [],
            "patterns": {},
            "project_context": {
                "projects": [],
                "needs": [],
                "decisions": [],
                "style": "",
                "updated_at": 0,
            },
            "stats": {"analyzed": 0, "last_run": 0},
        }

    def _save(self):
        self.data["stats"]["last_run"] = time.time()
        PERCEPTION_FILE.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ---------------------------------------------------------------
    # 核心入口: 分析一轮对话
    # ---------------------------------------------------------------

    def analyze(self, user_message: str, assistant_response: str,
                memory=None) -> dict:
        """分析一轮对话，更新感知模型。返回本次发现。"""
        findings = {
            "new_paths": self._extract_paths(user_message, assistant_response),
            "new_entities": self._extract_entities(user_message),
            "new_relations": self._extract_relations(user_message),
            "model_used": self._extract_model(user_message),
            "corrections": self._detect_corrections(user_message),
            "task_pattern": self._extract_task(user_message),
        }

        for path, context in findings["new_paths"]:
            old = self.data["environment"].get(path, {})
            self.data["environment"][path] = {
                "context": context,
                "seen_at": time.time(),
                "count": old.get("count", 0) + 1,
            }

        for name, info in findings["new_entities"]:
            if name not in self.data["entities"]:
                self.data["entities"][name] = info
            else:
                self.data["entities"][name].update(info)
            self.data["entities"][name]["last_seen"] = time.time()

        # ── 语义图谱: 合并本轮提取的三元组 (同实体同关系加权) ──
        for rel in findings.get("new_relations", []):
            self._merge_relation(rel)

        if findings["corrections"]:
            self.data["corrections"].extend(findings["corrections"])
            self.data["corrections"] = self.data["corrections"][-50:]

        if findings["model_used"]:
            self.data["preferences"]["last_model"] = findings["model_used"]

        if findings["task_pattern"]:
            key = findings["task_pattern"]["key"]
            if key not in self.data["patterns"]:
                self.data["patterns"][key] = {"count": 0, "examples": []}
            self.data["patterns"][key]["count"] += 1
            self.data["patterns"][key]["examples"].append({
                "msg": user_message[:200], "time": time.time()})
            self.data["patterns"][key]["examples"] = (
                self.data["patterns"][key]["examples"][-5:])

        self.data["stats"]["analyzed"] += 1
        self._save()
        return findings

    # ---------------------------------------------------------------
    # 提取器
    # ---------------------------------------------------------------

    def _extract_paths(self, user_msg: str, assistant_msg: str) -> list:
        paths = []
        combined = user_msg + " " + assistant_msg
        for m in re.finditer(r'([A-Za-z]:\\[^\s,，。；;]+)', combined):
            p = m.group(1).rstrip('.,;，。；')
            ctx_start = max(0, m.start() - 60)
            ctx_end = min(len(combined), m.end() + 60)
            ctx = combined[ctx_start:ctx_end].strip()
            paths.append((p, ctx[:150]))
        for kw, resolved in [
            ("桌面", os.path.expanduser(r"~\Desktop")),
            ("文档", os.path.expanduser(r"~\Documents")),
        ]:
            if kw in user_msg and resolved not in [p[0] for p in paths]:
                paths.append((resolved, f"用户提到了{kw}"))
        return paths

    # 通用名词不是实体名 ("文件在D:\data" 曾把"文件"当实体建进去)
    _NOT_A_NAME = frozenset((
        "文件", "文档", "表格", "图片", "目录", "文件夹", "路径", "项目", "内容", "东西",
        "这个", "那个", "这里", "那里", "我的", "你的", "他的", "它的", "上面", "下面",
        # 代词: "…清单，我在D:\work" 的贪心回溯会把"我"捞成实体名 (实测)
        "我", "你", "他", "她", "它", "咱", "俺", "我们", "你们", "他们", "咱们",
        "一个", "什么", "哪个", "怎么", "为什么", "可以", "需要", "然后", "现在",
    ))

    @classmethod
    def _clean_entity_name(cls, raw_name: str) -> str:
        r"""把候选名字收敛成**像实体名**的样子, 否则返回空串 (不建实体)。

        ★ 2026-09-19 深测修 (真缺陷): 原实现用 `(?:\\S+?)` 取名字 —— **没有长度上限、
        也没排除标点/路径分隔符**, 于是一段无空格的长串会被整段当成实体名。实测用户库里
        真的出现过 `C:...七瞬_第一章_素材清单.txt这是一份素材清单，我` 与
        `用一句话说明什么是二分查找。另外帮我` 这种"实体", 既污染库也污染提示词。

        判据 (与关系抽取同一套克制口径):
          · 去首尾空白/引号后长度 1..12
          · 不含标点/空白/路径分隔符/盘符   (，。；、！？,.;:!? \ / : @ # <>|*"')
          · 不含数字串 (电话号码/时间戳不是名字)
          · 不是通用名词 / 代词
        """
        if not isinstance(raw_name, str):
            return ""
        name = raw_name.strip().strip('"\'“”《》[]()（）')
        if not name or not (1 <= len(name) <= 12):
            return ""
        if re.search(r'[\s,，。；、！？!?;:：.;/\\@#<>|*"\'（）()\[\]{}　]', name):
            return ""
        if re.search(r'\d', name):
            return ""
        # 子串判定: 名字里**含有**通用词/代词就不是实体名
        # (实测 "内容我在D:\x" 的贪心回溯会捞成 "内容我" —— "含内容" 直接否掉)
        if any(tok in name for tok in cls._NOT_A_NAME):
            return ""
        return name

    def _extract_entities(self, msg: str) -> list:
        entities = []
        # ① "记住：<名> 邮箱/电话 <值>" —— 名字限定 1~12 个非标点字符 (原来是无上限的 \S+?)
        m = re.search(
            r'记住[：:]\s*([^\s,，。；、]{1,12}?)\s*(?:邮箱|电话|手机|地址)\s*'
            r'([\w.+-]+@[\w.-]+|\d{7,})', msg)
        if m:
            name = self._clean_entity_name(m.group(1))
            if name:
                val = m.group(2)
                field = "email" if "@" in val else "phone"
                entities.append((name, {field: val}))
        # ② "<名>在/位于 <盘符路径>" —— 名字同样限定, 且**不能用裸"在"放宽**
        #    (中文里"在"极常见: "文件在D盘某目录" 会把"文件"当实体)。
        #    只在 "名字+在+盘符路径" 这种紧邻形态下取, 并要求名字像人名/项目名。
        m = re.search(
            r'([^\s,，。；、]{1,12}?)(?:位于|路径是|路径)\s*([A-Za-z]:\\[^\s,，。；]+)', msg)
        if not m:
            m = re.search(
                r'([^\s,，。；、]{1,12}?)在\s*([A-Za-z]:\\[^\s,，。；]+)', msg)
        if m:
            name = self._clean_entity_name(m.group(1))
            if name:
                entities.append((name, {"path": m.group(2)}))
        return entities

    def _extract_model(self, msg: str) -> Optional[str]:
        m = re.search(r'@(\w+)', msg)
        return m.group(1) if m else None

    # ── 语义图谱: 从自然语言提取关系三元组 ──
    # 支持句式: X属于Y / X在Y工作 / X负责Y / X是Y的Z / X的邮箱是Y / X的路径是Y / X和Y是Z关系
    RELATION_PATTERNS = [
        # (正则, 主语组, 谓语, 宾语组) — 主语/宾语排除中文标点, 防止跨句吞词
        (r'([^，。；、\s]{1,12})(?:属于|隶属|归于)\s*([^，。；、\s]{1,20})', 1, "属于", 2),
        (r'([^，。；、\s]{1,12})(?:在|就职于|工作于)\s*([^，。；、\s]{1,20})(?:工作|任职|上班)?', 1, "属于", 2),
        (r'([^，。；、\s]{1,12})(?:负责|主管|管理|维护)\s*([^，。；、\s]{1,20})', 1, "负责", 2),
        (r'([^，。；、\s]{1,12})(?:是|为)\s*([^，。；、\s]{1,20})(?:的)?(?:同事|朋友|老板|上级|下属|搭档)', 1, "同事", 2),
        (r'([^，。；、\s]{1,12})(?:参与了|参与|在做|在做项目)\s*([^，。；、\s]{1,20})', 1, "参与", 2),
        (r'([^，。；、\s]{1,12})(?:的)?(邮箱|电话|手机|路径|地址|位置)是\s*([^，。；、\s]{1,40})', 1, None, 3),  # predicate=field
        (r'([^，。；、\s]{1,12})和([^，。；、\s]{1,12})(?:是|属于)?(?:同事|一起|同组)', 1, "同事", 2),
        (r'记住[：:]?\s*([^，。；、\s]{1,12})(?:是|属于|负责|在)\s*([^，。；、\s]{1,20})', 1, None, 2),
    ]

    def _extract_relations(self, msg: str) -> list:
        """从用户消息提取关系三元组 [{subject, predicate, object}]。"""
        rels = []
        for pat, sub_g, pred, obj_g in self.RELATION_PATTERNS:
            for m in re.finditer(pat, msg):
                subject = m.group(sub_g).strip()
                obj = m.group(obj_g).strip()
                if not subject or not obj:
                    continue
                predicate = pred
                # "邮箱是" 类 → predicate 用字段名
                if predicate is None and m.lastindex >= 2:
                    predicate = m.group(2)
                if not predicate:
                    continue
                # 过滤纯标点/空实体
                if re.fullmatch(r'[\s,，。；、]+', subject) or re.fullmatch(r'[\s,，。；、]+', obj):
                    continue
                # 过滤明显非实体词(代词/指示词)
                if subject in ("我的", "他的", "她的", "你的", "这个", "那个",
                               "我", "你", "他", "她", "它", "我们", "你们", "他们"):
                    continue
                # 过滤疑问句: "老王属于哪个项目?" 不应提取为关系
                if re.search(r'(哪个|什么|谁|怎么|多少|几|哪|吗|呢|？|\?)', subject + obj):
                    continue
                rels.append({"subject": subject, "predicate": predicate, "object": obj})
        # 去重(同三元组)
        seen = set()
        uniq = []
        for r in rels:
            key = (r["subject"], r["predicate"], r["object"])
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        return uniq

    def _merge_relation(self, rel: dict):
        """合并一条关系: 同 subject+predicate+object 加权刷新, 否则追加。"""
        rels = self.data.setdefault("relations", [])
        now = time.time()
        for existing in rels:
            if (existing.get("subject") == rel["subject"]
                    and existing.get("predicate") == rel["predicate"]
                    and existing.get("object") == rel["object"]):
                existing["weight"] = existing.get("weight", 1.0) + 1.0
                existing["last_seen"] = now
                if len(existing.get("examples", [])) < 3:
                    existing.setdefault("examples", []).append(rel.get("example", ""))
                return
        rels.append({
            "subject": rel["subject"], "predicate": rel["predicate"],
            "object": rel["object"], "weight": 1.0,
            "last_seen": now,
            "examples": [rel.get("example", "")] if rel.get("example") else [],
        })
        # 图谱上限 200 条, 超出按权重+新鲜度淘汰
        if len(rels) > 200:
            rels.sort(key=lambda r: (r.get("weight", 1.0), r.get("last_seen", 0)), reverse=True)
            self.data["relations"] = rels[:200]
        self._save()

    def add_relation(self, subject: str, predicate: str, obj: str,
                     example: str = "") -> None:
        """程序化添加关系(供其他模块/工具调用)。"""
        self._merge_relation({"subject": subject, "predicate": predicate,
                              "object": obj, "example": example})

    def query_relations(self, entity: str, limit: int = 10) -> list:
        """查询某实体的所有关系(正向+反向), 按权重降序。"""
        rels = self.data.get("relations", [])
        hits = [r for r in rels
                if r.get("subject") == entity or r.get("object") == entity]
        hits.sort(key=lambda r: r.get("weight", 1.0), reverse=True)
        return hits[:limit]

    def get_graph_context(self, limit: int = 15) -> str:
        """渲染语义图谱为模型可读文本: 实体 + 关系 + 反向推理提示。"""
        rels = self.data.get("relations", [])
        if not rels:
            return ""
        # 只输出近期活跃(30天内见过)且权重 >= 2 的强关系, 或最近见过的高权重
        now = time.time()
        active = [r for r in rels
                  if r.get("weight", 1.0) >= 2.0
                  and now - r.get("last_seen", 0) < 86400 * 30]
        if not active:
            active = sorted(rels, key=lambda r: r.get("weight", 1.0), reverse=True)[:limit]
        active = active[:limit]
        lines = ["[Knowledge Graph]"]
        for r in active:
            sub, pred, obj = r.get("subject", "?"), r.get("predicate", "?"), r.get("object", "?")
            lines.append(f"  {sub} --[{pred}]--> {obj}")
        lines.append("  使用关系推理: 如问'老王属于哪个项目', 从图中找 老王 --[属于]--> X")
        return "\n".join(lines)

    # ★ 纠正标记词表 (2026-09-20 修: 裸子串误报率极高)
    #   实测踩到: 裸 `别` 会命中"识**别**/区**别**/特**别**/级**别**/分**别**/告**别**",
    #   裸 `不是` 会命中"**是不是**该…"(那是提问), 裸 `换` 会命中"转**换**/交**换**"。
    #   结果: 10 句正常话里有 8 句被记成"用户在纠正我", 而 perception 会把纠正注入
    #   系统提示词 ([Learned corrections — do NOT repeat these mistakes]) → 直接污染行为。
    #   修法: 用否定环视排除复合词; 过松的裸 `换` 直接弃用 (漏判比误判安全)。
    _CORRECTION_MARKERS = [
        (r'不对',            r'不对'),
        (r'不是',            r'(?<!是)不是'),          # 排除 "是不是…" (提问)
        (r'错了',            r'错了'),
        (r'别',              r'(?<![识区特级分告个差性派鉴辨类离])别(?![人的])'),
        (r'不要',            r'不要'),
        (r'重新',            r'重新'),
        (r'应该是',          r'应该是'),
        (r'改成',            r'改成'),
        (r'换成',            r'(?<![转替变改])换成'),   # 排除 "转换成/替换成/变成"
    ]

    def _detect_corrections(self, user_msg: str) -> list:
        corrections = []
        for marker, pat in self._CORRECTION_MARKERS:
            if re.search(pat, user_msg):
                corrections.append({
                    "marker": marker,
                    "user_said": user_msg[:200],
                    "time": time.time()})
                break
        return corrections

    def _extract_task(self, msg: str) -> Optional[dict]:
        patterns = [
            (r'(发|发送|传).*?(?:文件|图片).*?给\s*(\S+)', "send_file"),
            (r'(检查|查看|看).*?(?:状态|情况)', "check_status"),
            (r'(搜|搜索|查|找).*?(?:一下|信息)', "search"),
            (r'(写|创建|生成).*?(?:文件|代码|脚本)', "create_file"),
            (r'(列|列出|看).*?(?:目录|文件|桌面)', "list_dir"),
        ]
        for pat, key in patterns:
            if re.search(pat, msg):
                return {"key": key, "msg": msg[:200]}
        return None

    # ---------------------------------------------------------------
    # 查询接口: 让模型利用感知结果
    # ---------------------------------------------------------------

    def get_environment_context(self) -> str:
        env = self.data.get("environment", {})
        if not env:
            return ""
        recent = sorted(env.items(),
                       key=lambda x: x[1].get("seen_at", 0), reverse=True)[:10]
        lines = ["Known locations (from past conversations):"]
        for path, info in recent:
            ctx = info.get("context", "")[:60]
            count = info.get("count", 1)
            star = " *" if count >= 3 else ""
            lines.append(f"  {path}{star} -- {ctx}")
        return "\n".join(lines)

    def get_entity_context(self) -> str:
        ents = self.data.get("entities", {})
        if not ents:
            return ""
        lines = ["Known contacts/entities:"]
        for name, info in ents.items():
            details = ", ".join(
                f"{k}={v}" for k, v in info.items() if k not in ("last_seen",))
            if details:
                lines.append(f"  {name}: {details}")
        return "\n".join(lines)

    def get_correction_context(self) -> str:
        corrections = self.data.get("corrections", [])
        if not corrections:
            return ""
        recent = corrections[-5:]
        lines = ["Past corrections (DO NOT repeat these mistakes):"]
        for c in recent:
            ago = self._time_ago(c.get("time", 0))
            lines.append(f"  [{ago}] User said: {c.get('user_said', '')[:100]}")
        return "\n".join(lines)

    def get_pattern_context(self) -> str:
        patterns = self.data.get("patterns", {})
        if not patterns:
            return ""
        frequent = [(k, v) for k, v in patterns.items()
                    if v.get("count", 0) >= 2]
        if not frequent:
            return ""
        lines = ["Frequent task patterns:"]
        for key, info in sorted(frequent,
                                key=lambda x: x[1]["count"], reverse=True)[:5]:
            lines.append(f"  {key}: done {info['count']} times")
        return "\n".join(lines)

    # ---------------------------------------------------------------
    # Project context: let model know what we're building/missing/decided
    # ---------------------------------------------------------------

    def set_brief(self, projects=None, needs=None, decisions=None, style=None):
        """Set project context manually."""
        ctx = self.data.setdefault("project_context", {
            "projects": [], "needs": [], "decisions": [],
            "style": "", "updated_at": 0,
        })
        if projects is not None:
            ctx["projects"] = projects if isinstance(projects, list) else [projects]
        if needs is not None:
            ctx["needs"] = needs if isinstance(needs, list) else [needs]
        if decisions is not None:
            ctx["decisions"] = decisions if isinstance(decisions, list) else [decisions]
        if style is not None:
            ctx["style"] = style
        ctx["updated_at"] = time.time()
        self._save()

    def get_project_context(self) -> str:
        """Render project context for model system prompt."""
        ctx = self.data.get("project_context", {})
        if not ctx:
            return ""
        lines = []
        projects = ctx.get("projects", [])
        if projects:
            lines.append("## Active Projects")
            for p in projects:
                lines.append(f"  - {p}")
        needs = ctx.get("needs", [])
        if needs:
            lines.append("\n## Current Needs / Gaps")
            for n in needs:
                lines.append(f"  - {n}")
        decisions = ctx.get("decisions", [])
        if decisions:
            lines.append("\n## Recent Decisions & Progress")
            for d in decisions[-10:]:
                lines.append(f"  - {d}")
        style = ctx.get("style", "")
        if style:
            lines.append(f"\n## Communication Style\n  {style}")
        if not lines:
            return ""
        return "\n".join(lines)

    def add_decision(self, text: str):
        """Auto-append a decision."""
        ts = time.strftime("%m/%d %H:%M")
        ctx = self.data.setdefault("project_context", {
            "projects": [], "needs": [], "decisions": [],
            "style": "", "updated_at": 0,
        })
        ctx["decisions"].append(f"[{ts}] {text}")
        ctx["decisions"] = ctx["decisions"][-50:]
        ctx["updated_at"] = time.time()
        self._save()

    def get_prompt_context(self) -> str:
        """Compact context for LLM system prompt injection."""
        lines = []
        data = self.data
        
        # Recent corrections (last 3) — stored as list of {marker, user_said, time}
        corrections = data.get("corrections", [])
        if isinstance(corrections, list) and corrections:
            cors = sorted(corrections, key=lambda x: x.get("time", 0), reverse=True)[:3]
            lines.append("[Learned corrections — do NOT repeat these mistakes]")
            for c in cors:
                user_said = c.get("user_said", "")
                if user_said:
                    lines.append(f"  NEVER: {user_said}")
        
        # Tool success patterns
        tool_ok = data.get("tool_ok", {})
        tool_fail = data.get("tool_fail", {})
        patterns = []
        for name, ok_count in sorted(tool_ok.items(), key=lambda x: x[1], reverse=True)[:5]:
            fail_count = tool_fail.get(name, 0)
            total = ok_count + fail_count
            rate = int(ok_count / total * 100) if total > 0 else 100
            patterns.append(f"{name} ({rate}% success, {total} calls)")
        if patterns:
            lines.append("[Tool reliability]")
            for p in patterns:
                lines.append(f"  PREFER: {p}")
        
        # Project context (from project_context, not project.brief)
        pc = data.get("project_context", {})
        if pc.get("projects"):
            lines.append(f"[Projects] {', '.join(pc['projects'])}")
        if pc.get("decisions"):
            lines.append(f"[Decisions] {'; '.join(pc['decisions'][-3:])}")
        
        # Recent entities (dict keyed by name, sorted by last_seen)
        entities = data.get("entities", {})
        if isinstance(entities, dict) and entities:
            recent = sorted(entities.items(),
                            key=lambda kv: kv[1].get("last_seen", 0),
                            reverse=True)[:5]
            ents = [name for name, _ in recent]
            if ents:
                lines.append(f"[Known] {', '.join(ents)}")
        
        return "\n".join(lines) if lines else ""
    
    def record_tool(self, tool_name: str, success: bool):
        """Record tool execution result for reliability tracking."""
        key = "tool_ok" if success else "tool_fail"
        self.data.setdefault(key, {})
        self.data[key][tool_name] = self.data[key].get(tool_name, 0) + 1
        # Save every 10 records
        total = sum(self.data.get("tool_ok", {}).values()) + sum(self.data.get("tool_fail", {}).values())
        if total % 10 == 0:
            self._save()
    
    def get_full_context(self) -> str:
        sections = [
            self.get_project_context(),
            self.get_environment_context(),
            self.get_entity_context(),
            self.get_correction_context(),
            self.get_pattern_context(),
        ]
        return "\n\n".join(s for s in sections if s)

    # ---------------------------------------------------------------
    # 主动合成：从记忆里提炼建议
    # ---------------------------------------------------------------

    def suggest(self) -> list[dict]:
        """Synthesize patterns/corrections/tool-stats into actionable suggestions.

        Returns list of {type, priority, title, detail} dicts.
        Types: 'automate' (frequent task → cron/skill), 'fix' (tool failing → switch),
               'learn' (correction → behavior rule), 'cleanup' (stale data → prune)
        """
        suggestions = []

        # 1. Frequent task patterns → suggest automation
        patterns = self.data.get("patterns", {})
        for key, info in patterns.items():
            count = info.get("count", 0)
            if count >= 3:
                examples = info.get("examples", [])
                recent = [e for e in examples if time.time() - e.get("time", 0) < 86400 * 7]
                if len(recent) >= 2:
                    actions = {
                        "send_file": "发文件",
                        "check_status": "检查状态",
                        "search": "搜索",
                        "create_file": "创建文件",
                        "list_dir": "列目录",
                    }
                    action_cn = actions.get(key, key)
                    suggestions.append({
                        "type": "automate",
                        "priority": "medium",
                        "title": f"重复任务: {action_cn}",
                        "detail": f"过去7天做了{len(recent)}次「{action_cn}」。可以考虑做成 Skill 或定时任务。",
                        "data": {"pattern_key": key, "count": count, "recent": len(recent)},
                    })

        # 2. Frequent corrections → suggest behavior rule
        corrections = self.data.get("corrections", [])
        if len(corrections) >= 3:
            recent_cors = [c for c in corrections if time.time() - c.get("time", 0) < 86400 * 7]
            if len(recent_cors) >= 2:
                suggestions.append({
                    "type": "learn",
                    "priority": "high",
                    "title": "用户频繁纠正",
                    "detail": f"过去7天被纠正了{len(recent_cors)}次。常见触发词: {', '.join(set(c.get('marker','') for c in recent_cors))}",
                    "data": {"correction_count": len(recent_cors)},
                })

        # 3. Failing tools → suggest alternatives
        tool_ok = self.data.get("tool_ok", {})
        tool_fail = self.data.get("tool_fail", {})
        for name, fail_count in tool_fail.items():
            ok_count = tool_ok.get(name, 0)
            total = ok_count + fail_count
            if total >= 3 and ok_count / total < 0.5:
                suggestions.append({
                    "type": "fix",
                    "priority": "high",
                    "title": f"工具不稳定: {name}",
                    "detail": f"{name} 成功率 {int(ok_count/total*100)}% ({ok_count}/{total})。建议检查或切换备选工具。",
                    "data": {"tool": name, "success_rate": round(ok_count/total, 2)},
                })

        # 4. Tool reliability ranking for prompt injection
        if tool_ok or tool_fail:
            ranking = []
            all_tools = set(list(tool_ok.keys()) + list(tool_fail.keys()))
            for t in all_tools:
                ok = tool_ok.get(t, 0)
                fail = tool_fail.get(t, 0)
                total = ok + fail
                if total >= 2:
                    rate = ok / total
                    ranking.append((t, rate, total))
            if ranking:
                ranking.sort(key=lambda x: -x[1])
                worst = [f"{t}({int(r*100)}%)" for t, r, n in ranking if r < 0.7][:3]
                best = [f"{t}({int(r*100)}%)" for t, r, n in ranking if r >= 0.9][:3]
                if worst:
                    suggestions.append({
                        "type": "fix",
                        "priority": "low",
                        "title": "工具可靠性排名",
                        "detail": f"不稳定: {', '.join(worst)}" +
                                  (f" | 可靠: {', '.join(best)}" if best else ""),
                        "data": {"ranking": [(t, round(r, 2)) for t, r, _ in ranking]},
                    })

        return suggestions

    def get_suggestions_context(self) -> str:
        """Render suggestions as a compact string for LLM prompt injection."""
        sug = self.suggest()
        if not sug:
            return ""
        lines = ["[Perception suggestions]"]
        for s in sug[:5]:
            lines.append(f"  [{s['priority']}] [{s['type']}] {s['title']}: {s['detail'][:120]}")
        return "\n".join(lines)

    @staticmethod
    def _time_ago(ts: float) -> str:
        diff = time.time() - ts
        if diff < 60: return "just now"
        elif diff < 3600: return f"{int(diff/60)}m ago"
        elif diff < 86400: return f"{int(diff/3600)}h ago"
        return f"{int(diff/86400)}d ago"


# ═══ Singleton ═══
# 🔴 必须单例: pipeline 持有实例A, 若其他模块(如 command_handler)另 new 实例B,
# 内存不同步 → B 写入 relations 后被 A 的 analyze _save() 用旧快照覆盖(竞态,
# 2026-08-18 实测: 用户"记住老王"后下一条对话 analyze 即抹掉图谱)。
_perception_singleton = None


def get_perception():
    """进程级单例: 所有模块拿同一实例, 杜绝写覆盖竞态。"""
    global _perception_singleton
    if _perception_singleton is None:
        _perception_singleton = PerceptionEngine()
    return _perception_singleton


def reset_perception_singleton():
    """测试用: 重置单例(隔离状态)。"""
    global _perception_singleton
    _perception_singleton = None
