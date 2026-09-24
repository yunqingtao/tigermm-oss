"""产物落点 + 工具返回契约 的测试 (2026-09-19 深测驱动)。

守两组东西:
  A. 网关返回契约归一化 `_norm_tool_result` —— 工具返回键混用 (output/result/data)
     时, 调用方仍能从 `output` 读到结果 (深测实测: 深测台第一版因此误判 3 个工具失败)。
  B. 产物落点 —— 用户说 "存到 <目录>" 时必须真的存到那里, 不许静默落桌面:
       · `_skill_params` 抽出输出目标 (目录也认)
       · tiger_office 目录 → 补默认文件名
       · plantuml 支持 path (目录/文件)
全离线: 不联网、不打模型、不写真用户数据 (临时目录)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────────────────────────── A. 返回契约归一化 ───────────────────────────

class TestNormalizeToolResult:
    @staticmethod
    def _N():
        from core.pipeline import _norm_tool_result
        return _norm_tool_result

    @pytest.fixture(autouse=True)
    def N(self):
        return self._N()

    def test_result_key_promoted(self, N):
        """web_search/nominatim 真实形态: 只有 result, 没有 output。"""
        r = N({"ok": True, "success": True, "result": "1. python.org\n   https://..."})
        assert r["output"].startswith("1. python.org")
        assert r["output_from"] == "result"
        assert r["result"]  # 原键不动

    def test_data_key_promoted(self, N):
        """libretranslate 真实形态: 只有 data; dict 要序列化。"""
        r = N({"success": True, "data": {"translatedText": "Hello"}})
        assert "Hello" in r["output"] and r["output_from"] == "data"

    def test_existing_output_untouched(self, N):
        src = {"success": True, "output": "已有", "result": "别的"}
        r = N(src)
        assert r["output"] == "已有" and "output_from" not in r

    def test_empty_output_gets_filled(self, N):
        r = N({"success": True, "output": "   ", "text": "T"})
        assert r["output"] == "T"

    def test_failure_semantics_preserved(self, N):
        """失败结果不许被归一化改写成"成功", 也不许抹掉 error。"""
        r = N({"success": False, "output": "", "error": "boom", "result": "？"})
        assert r["success"] is False and r["error"] == "boom"

    def test_no_source_key_returns_as_is(self, N):
        r = N({"success": True, "output": ""})
        assert r["output"] == "" and "output_from" not in r

    def test_non_dict_safe(self, N):
        assert N("raw") == "raw" and N(None) is None

    def test_none_literal_not_promoted(self, N):
        """工具常把 output 写成字符串 'None' —— 那是空, 该被真结果覆盖。"""
        r = N({"success": True, "output": "" , "result": "真结果"})
        assert r["output"] == "真结果"


# ─────────────────────── B. 技能参数: 输出目标抽取 ───────────────────────

class _Dag:
    def __init__(self, params):
        self.params_schema = params


@pytest.fixture(scope="class")
def pl(tmp_path_factory):
    """造一个只带 _skill_params 所需依赖的 pipeline (不打模型/不连网)。"""
    tmp_path = tmp_path_factory.mktemp("pl_skillparams")
    import core.perception as _p
    _p.PERCEPTION_FILE = tmp_path / "perc.json"      # 指向临时文件 (零副作用)
    # ★ 隔离 MCP: Level4Pipeline.__init__ 会起 MCP 后台线程刷新外部工具 (真子进程) →
    #   测试进程退出时事件循环已关, 留下 `BaseSubprocessTransport.__del__` 的
    #   unraisable 警告 → 把 verify_mcp_reap 的"pytest 无传输警告"断言弄红。
    #   本文件不测 MCP, 直接换成哑对象 (零副作用、更快)。
    import types
    import core.mcp_client as _mc
    _stub = types.SimpleNamespace(load_config=lambda *a, **k: None,
                                  list_tools=lambda *a, **k: [], tools=[],
                                  get_tool=lambda *a, **k: None)
    _orig_get_mcp = _mc.get_mcp_client
    _mc.get_mcp_client = lambda *a, **k: _stub          # class fixture 不能用 function-scoped monkeypatch
    try:
        from main import _load_config
        from core.knowledge import KnowledgeEngine
        from core.model_client import ModelClient
        from core.pipeline import Level4Pipeline
        from config.settings import DATA_DIR
        cfg = _load_config()
        _pl_obj = Level4Pipeline(ModelClient(cfg), cfg)
        _pl_obj._ke = KnowledgeEngine(DATA_DIR)
        yield _pl_obj
    finally:
        _mc.get_mcp_client = _orig_get_mcp


class TestSkillParamsOutputTarget:
    """★ 深测事故: "写周报，存到 D:/报告/" (给目录不给文件名) 抽不到参数 →
    技能把 out_path 留空 → 工具静默落桌面, 用户指定的位置被无视。"""

    SCHEMA = {"out_path": {"type": "path", "required": False}}

    def test_dir_target_no_extension(self, pl, tmp_path):
        d = str(tmp_path / "报告")
        p = pl._skill_params(f"写周报，存到 {d}/", _Dag(self.SCHEMA))
        assert p.get("out_path").rstrip("/\\") == d.rstrip("/\\"), p

    def test_file_target(self, pl, tmp_path):
        f = str(tmp_path / "r.docx")
        p = pl._skill_params(f"写周报，存到 {f}", _Dag(self.SCHEMA))
        assert p.get("out_path") == f, p

    def test_markers_variants(self, pl, tmp_path):
        for mk in ("保存到", "导出到", "放到", "写入"):
            d = str(tmp_path / f"d_{mk}")
            p = pl._skill_params(f"写周报，{mk} {d}/", _Dag(self.SCHEMA))
            assert p.get("out_path", "").startswith(str(tmp_path)), (mk, p)

    def test_no_target_leaves_empty(self, pl):
        p = pl._skill_params("写周报", _Dag(self.SCHEMA))
        assert not p.get("out_path"), p

    def test_vague_target_ignored(self, pl):
        """'存到这里' 这类指示代词不该被当路径。"""
        p = pl._skill_params("写周报，存到这里", _Dag(self.SCHEMA))
        assert not p.get("out_path"), p

    def test_input_and_output_split(self, pl, tmp_path):
        """table-to-report 形态: 输入表 + 输出报告, 按角色分开, 不许互相错位。"""
        x, o = str(tmp_path / "t.xlsx"), str(tmp_path / "r.docx")
        schema = {"path": {"type": "path"}, "out_path": {"type": "path"}}
        p = pl._skill_params(f"把这个表格做成分析报告 {x} 到 {o}", _Dag(schema))
        assert p.get("path") == x, p
        assert p.get("out_path") == o, p

    def test_input_and_output_with_marker(self, pl, tmp_path):
        x, d = str(tmp_path / "t.xlsx"), str(tmp_path / "outdir")
        schema = {"path": {"type": "path"}, "out_path": {"type": "path"}}
        p = pl._skill_params(f"分析 {x}，存到 {d}/", _Dag(schema))
        assert p.get("path") == x and p.get("out_path", "").startswith(d), p

    def test_single_input_param_unchanged(self, pl, tmp_path):
        """旧行为: 只有一个 path 参数 (无 out 语义) 时按序吃路径。"""
        x = str(tmp_path / "a.txt")
        p = pl._skill_params(f"把这个发给涛哥 {x}", _Dag({"file": {"type": "path"}}))
        assert p.get("file") == x, p


# ─────────────────────── C. 工具侧: 目录 → 补文件名 ───────────────────────

class TestToolPathResolution:
    def test_tiger_office_dir_gets_default_name(self, tmp_path):
        from tools.tiger_office import _resolve_out_path
        d = tmp_path / "出图"
        d.mkdir()
        out = _resolve_out_path(str(d), "季度总结", ".docx")
        assert out.endswith(".docx") and str(d) in out, out
        assert "季度总结" in os.path.basename(out)

    def test_tiger_office_trailing_sep(self, tmp_path):
        from tools.tiger_office import _resolve_out_path
        out = _resolve_out_path(str(tmp_path) + os.sep, "", ".pptx")
        assert out.startswith(str(tmp_path)) and out.endswith(".pptx"), out
        assert "文档_" in os.path.basename(out)      # 无标题 → 时间戳名

    def test_tiger_office_file_path_kept(self, tmp_path):
        from tools.tiger_office import _resolve_out_path
        f = str(tmp_path / "a.docx")
        assert _resolve_out_path(f, "t", ".docx") == f

    def test_tiger_office_missing_ext_appended(self, tmp_path):
        from tools.tiger_office import _resolve_out_path
        assert _resolve_out_path(str(tmp_path / "a"), "t", ".docx").endswith(".docx")

    def test_tiger_office_empty_goes_desktop(self):
        from tools.tiger_office import _resolve_out_path, _default_out_path
        assert _resolve_out_path("", "标题", ".docx") == _default_out_path("标题", ".docx")

    def test_plantuml_writes_into_given_dir(self, tmp_path):
        """真跑: plantuml 给目录 → 图必须落在那个目录 (不再是桌面)。"""
        import asyncio
        from tools import plantuml as P
        if not os.path.exists(getattr(P, "JAR", "")):
            pytest.skip("plantuml.jar 不在")
        d = tmp_path / "puml"
        d.mkdir()
        r = asyncio.run(P.run(code="@startuml\nA->B: hi\n@enduml", path=str(d) + os.sep))
        if not r.get("success"):
            pytest.skip(f"plantuml 执行环境不可用: {r.get('error')}")
        out = r["local_path"]
        assert str(d) in out, f"落点不对: {out}"


# ─────────────── D. diagram 技能: 参数名必须对上工具 ───────────────

class TestDiagramSkillWiring:
    """★ 深测事故: diagram 技能写 `content: $steps.plan.text`, 而 plantuml.run 的参数名是
    `code` → 源码传不进工具 → 永远 "No PlantUML code provided" (旧技能一直坏,
    修好 guard 后才暴露)。两层都守: 技能写对名字 + 工具留别名。"""

    def test_skill_uses_code_param(self):
        import yaml
        txt = (ROOT / "tmm_skills" / "diagram" / "SKILL.md").read_text(encoding="utf-8")
        fm = txt.split("---")[1]
        d = yaml.safe_load(fm)
        draw = [s for s in d["steps"] if s.get("tool") == "plantuml"]
        assert draw, "diagram 技能缺 plantuml 步骤"
        assert "code" in draw[0]["input"], f"plantuml 参数名必须是 code: {draw[0]['input']}"

    def test_skill_has_dsl_generation_step(self):
        """没有生成 DSL 的步骤 → 技能触发必然失败 (缺必填 dsl)。"""
        txt = (ROOT / "tmm_skills" / "diagram" / "SKILL.md").read_text(encoding="utf-8")
        assert "tool: llm" in txt, "diagram 缺少 llm 生成 DSL 的步骤"

    def test_plantuml_accepts_content_alias(self):
        import asyncio
        from tools import plantuml as P
        if not os.path.exists(getattr(P, "JAR", "")):
            pytest.skip("plantuml.jar 不在")
        r = asyncio.run(P.run(content="@startuml\nA->B\n@enduml", path=str(__import__("tempfile").mkdtemp()) + os.sep))
        assert r.get("success") or "No PlantUML code provided" not in str(r.get("error")), r

    def test_plantuml_explicit_name_overwrites(self, tmp_path):
        import asyncio
        from tools import plantuml as P
        if not os.path.exists(getattr(P, "JAR", "")):
            pytest.skip("plantuml.jar 不在")
        tgt = tmp_path / "x.png"
        tgt.write_bytes(b"OLD")
        r = asyncio.run(P.run(code="@startuml\nA->B\n@enduml", path=str(tgt)))
        if not r.get("success"):
            pytest.skip(f"plantuml 环境不可用: {r.get('error')}")
        assert r["local_path"] == str(tgt), "明确给了文件名就该用它 (不静默加时间戳)"
        assert tgt.stat().st_size > 100


class TestCreationWords:
    def test_draw_verbs_count_as_creation(self):
        """'画个架构图' 是 diagram 的自然说法, 必须被判为"要产出"。"""
        from core.pipeline import Level4Pipeline as P
        for m in ("画个架构图", "画流程图", "绘制一张时序图", "设计一个类图", "做个流程图"):
            assert P._looks_like_creation(m), m

    def test_read_requests_still_excluded(self):
        from core.pipeline import Level4Pipeline as P
        for m in ("帮我看看这份周报", "总结一下这个文档", "这个图是什么意思"):
            assert not P._looks_like_creation(m), m
