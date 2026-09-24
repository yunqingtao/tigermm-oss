
"""
SkillRegistry — 技能注册中心
============================
统一扫描 skills/ + tmm_skills/ 目录，校验格式，去重，追踪成功率。
"""
import json, os, time, re, logging
import yaml
from pathlib import Path
from typing import Optional

logger = logging.getLogger("core.skill_registry")

REQUIRED_FIELDS = ["name", "version", "description"]

# Pre-compiled regex for YAML frontmatter extraction
_YAML_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---", re.DOTALL)


class SkillRegistry:
    """统一技能注册：扫描、校验、去重、统计。"""

    def __init__(self, search_paths=None):
        if search_paths is None:
            search_paths = [
                Path(__file__).parent.parent / "skills",
                Path(__file__).parent.parent / "tmm_skills",
            ]
        self.search_paths = [Path(p) for p in search_paths]
        self._skills = {}
        self._last_scan = 0
        self._scan_errors = []

    def scan(self, force=False):
        now = time.time()
        if not force and (now - self._last_scan) < 30 and self._skills:
            return self._skills

        self._skills = {}
        self._scan_errors = []

        for search_path in self.search_paths:
            if not search_path.exists():
                continue
            for skill_dir in search_path.iterdir():
                if not skill_dir.is_dir():
                    continue
                entry = self._load_skill(skill_dir)
                if entry:
                    name = entry["name"]
                    if name in self._skills:
                        existing = self._skills[name]
                        if self._version_gt(entry["version"], existing["version"]):
                            self._skills[name] = entry
                    else:
                        self._skills[name] = entry

        self._last_scan = now
        logger.info("SkillRegistry: %d skills, %d errors",
                   len(self._skills), len(self._scan_errors))
        return self._skills

    def _load_skill(self, skill_dir):
        json_path = skill_dir / "skill.json"
        md_path = skill_dir / "SKILL.md"

        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                data["_source"] = str(json_path)
                data["_format"] = "json"
                data.setdefault("schema_version", "1")
                data.setdefault("success_rate", 0.0)
                data.setdefault("call_count", 0)
                data.setdefault("validated_at", time.time())
                if self._validate(data):
                    return data
            except Exception as e:
                self._scan_errors.append(f"{skill_dir.name}: {e}")

        elif md_path.exists():
            try:
                data = self._parse_skill_md(md_path)
                if data:
                    data["_source"] = str(md_path)
                    data["_format"] = "md"
                    data.setdefault("schema_version", "1")
                    data.setdefault("success_rate", 0.0)
                    data.setdefault("call_count", 0)
                    data.setdefault("validated_at", time.time())
                    if self._validate(data):
                        return data
            except Exception as e:
                self._scan_errors.append(f"{skill_dir.name}: {e}")

        return None

    def _parse_skill_md(self, md_path):
        content = md_path.read_text(encoding="utf-8")
        m = _YAML_RE.match(content)
        if not m:
            return None

        yaml_str = m.group(1)
        # 必须用真 YAML 解析: 朴素逐行 partition(":") 会被嵌套键污染 ——
        # steps[].input.name 会把顶层 name 覆盖成 "$params.recipient" (实测)
        try:
            data = yaml.safe_load(yaml_str) or {}
        except yaml.YAMLError as e:
            logger.warning("SkillRegistry: YAML 解析失败 %s: %s", md_path.name, e)
            data = {}
        if not isinstance(data, dict):
            data = {}

        # triggers 兼容两种写法: triggers: [...] 或 trigger: {patterns: [...]}
        trig = data.get("triggers")
        if trig is None:
            _t = data.get("trigger")
            trig = _t.get("patterns", []) if isinstance(_t, dict) else []
        if isinstance(trig, str):
            trig = [trig]

        return {
            "name": data.get("name", md_path.parent.name),
            "version": str(data.get("version", "1.0")),
            "description": data.get("description", ""),
            "tags": data.get("tags", []) or [],
            "trigger": {"patterns": trig or [], "match": "any"},
            "requires": data.get("requires_tools", []) or [],
        }

    def _validate(self, data):
        for field in REQUIRED_FIELDS:
            if field not in data or not data[field]:
                self._scan_errors.append(f"{data.get('name', '?')}: missing '{field}'")
                return False
        return True

    def _version_gt(self, a, b):
        try:
            pa = [int(x) for x in a.replace("v", "").split(".")]
            pb = [int(x) for x in b.replace("v", "").split(".")]
            for i in range(max(len(pa), len(pb))):
                va = pa[i] if i < len(pa) else 0
                vb = pb[i] if i < len(pb) else 0
                if va != vb:
                    return va > vb
            return False
        except Exception:
            return a > b

    def list_all(self):
        self.scan()
        skills = list(self._skills.values())
        skills.sort(key=lambda s: s.get("call_count", 0), reverse=True)
        return skills

    def get(self, name):
        self.scan()
        return self._skills.get(name)

    def match_triggers(self, message):
        self.scan()
        matched = []
        msg_lower = message.lower()
        for name, skill in self._skills.items():
            trigger = skill.get("trigger", {})
            patterns = trigger.get("patterns", [])
            hits = [p for p in patterns if p.lower() in msg_lower]
            if hits:
                # Score by longest matched pattern (more specific = better)
                best_len = max(len(p) for p in hits)
                matched.append({
                    "name": name,
                    "skill": skill,
                    "matched_patterns": hits,
                    "_score": best_len,
                })
        # Sort by pattern specificity (longest match first), then call_count
        matched.sort(key=lambda m: (m["_score"], m["skill"].get("call_count", 0)), reverse=True)
        return matched

    def record_call(self, name, success):
        skill = self._skills.get(name)
        if not skill:
            return
        count = skill.get("call_count", 0) + 1
        old_rate = skill.get("success_rate", 0.0)
        old_ok = int((count - 1) * old_rate)
        new_ok = old_ok + (1 if success else 0)
        new_rate = round(new_ok / count, 2) if count > 0 else 0.0

        skill["call_count"] = count
        skill["success_rate"] = new_rate
        skill["validated_at"] = time.time()

        source = skill.get("_source")
        if source and os.path.exists(source) and skill.get("_format") == "json":
            try:
                data = json.loads(Path(source).read_text(encoding="utf-8"))
                data["success_rate"] = new_rate
                data["call_count"] = count
                data["validated_at"] = skill["validated_at"]
                Path(source).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to write skill stats: %s", e)

    def stats(self):
        self.scan()
        skills = list(self._skills.values())
        return {
            "total": len(skills),
            "by_source": {
                "json": sum(1 for s in skills if s.get("_format") == "json"),
                "md": sum(1 for s in skills if s.get("_format") == "md"),
            },
            "total_calls": sum(s.get("call_count", 0) for s in skills),
            "avg_success_rate": round(
                sum(s.get("success_rate", 0) for s in skills) / max(len(skills), 1), 2
            ),
            "scan_errors": len(self._scan_errors),
        }

    # ═══ 技能市场: 打包导出 / 导入 (2026-08-18) ═══

    def export_skill(self, name: str, out_dir=None) -> str:
        """导出技能目录为 zip: <name>-<version>.zip。返回 zip 路径, 失败返回空串。"""
        import zipfile
        self.scan()
        skill = self._skills.get(name)
        if not skill:
            logger.warning("export: skill %s not found", name)
            return ""
        src = Path(skill.get("_source", ""))
        skill_dir = src.parent
        if not skill_dir.exists():
            return ""
        out_dir = Path(out_dir) if out_dir else skill_dir.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        version = str(skill.get("version", "1.0")).replace(".", "_")
        zip_path = out_dir / f"{name}-{version}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in skill_dir.rglob("*"):
                if f.is_file() and "__pycache__" not in str(f):
                    zf.write(f, f.relative_to(skill_dir.parent))
        logger.info("export: %s -> %s", name, zip_path)
        return str(zip_path)

    def import_skill(self, zip_path, target_dir=None) -> dict:
        """从 zip 导入技能到 skills/。返回 {name, version, status}。"""
        import zipfile
        zpath = Path(zip_path)
        if not zpath.exists():
            return {"status": "error", "reason": f"文件不存在: {zpath}"}
        target = Path(target_dir) if target_dir else self.search_paths[0]
        target.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zpath) as zf:
                # 校验: zip 内必须含 skill.json 或 SKILL.md
                names = zf.namelist()
                has_meta = any(n.endswith("skill.json") or n.endswith("SKILL.md") for n in names)
                if not has_meta:
                    return {"status": "error", "reason": "zip 内缺少 skill.json 或 SKILL.md"}
                # 提取技能目录名(第一层目录)
                top_dirs = {n.split("/")[0] for n in names if "/" in n}
                if len(top_dirs) != 1:
                    return {"status": "error", "reason": f"zip 结构异常: 顶层目录 {top_dirs}"}
                skill_name = next(iter(top_dirs))
                dest = target / skill_name
                # 存在性检查(防覆盖)
                if dest.exists():
                    return {"status": "exists", "name": skill_name, "version": "?"}
                zf.extractall(target)
            # 校验导入后能扫描到
            self.scan(force=True)
            entry = self._skills.get(skill_name)
            return {
                "status": "ok",
                "name": skill_name,
                "version": str(entry.get("version", "?")) if entry else "?",
                "path": str(dest),
            }
        except zipfile.BadZipFile:
            return {"status": "error", "reason": "不是有效的 zip 文件"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}


_registry = None

def get_skill_registry():
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry
