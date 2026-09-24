"""第四节收尾修复的测试 (2026-09-19 深测驱动)。

守五组:
  A. 4.4 感知引擎的实体名清洗 —— 长句/路径/通用名词/代词都不得变成实体名
  B. 4.6 office_cli / win32_input 有统一入口 (原来没有 run/execute → Agent 调不到)
  C. 4.6b core/plugin_manager.py 不再谎报成功 + 出口归一 (与 pipeline 那份同口径)
  D. 4.8 tiger_office.write_excel 新建表格 (原来只有 scan/format/extract)
  E. 4.7 ocr 的 tesseract 探测 + 诚实失败 (原来硬编码个人机路径)
  F. 4.5 工具目录无重名 TOOL (output.py 是重复的 openmeteo 旧草稿)
全离线 (OCR 真跑需要本地 tesseract; 没有就 skip)。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ───────────────────────── A. 4.4 实体名清洗 ─────────────────────────

class TestEntityNameCleaning:
    @pytest.fixture()
    def pe(self, tmp_path):
        import core.perception as _p
        _p.PERCEPTION_FILE = tmp_path / "perception.json"
        from core.perception import PerceptionEngine
        return PerceptionEngine()

    # 该建实体的
    @pytest.mark.parametrize("msg,name,field", [
        ("记住：张三 邮箱 zhangsan@test.com", "张三", "email"),
        ("记住：李四 电话 13900000000", "李四", "phone"),
        ("涛哥位于D:\\work\\项目", "涛哥", None),
        ("虎哥路径是D:\\projects\\demo", "虎哥", None),
    ])
    def test_legit_entity_extracted(self, pe, msg, name, field):
        ents = pe._extract_entities(msg)
        names = [n for n, _ in ents]
        assert name in names, f"{msg!r} → {ents}"

    # 绝不该建实体的 (深测在真实用户库里见过前两类)
    @pytest.mark.parametrize("msg", [
        r"看看C:\a\b.txt这份清单我在D:\work",          # 长句+路径 (真实污染过)
        r"内容我在D:\x",
        r"文件在D:\data",                              # 通用名词
        r"分析D:\a\b.txt这份清单的内容我在D:\x",
        "用一句话说明什么是二分查找。另外帮我看看这个文件",
        "记住：这是一个非常非常长的名字不应该被当成实体名 邮箱 a@b.com",
        "记住：12345 邮箱 a@b.com",                    # 纯数字名
    ])
    def test_pollution_rejected(self, pe, msg):
        assert pe._extract_entities(msg) == [], f"{msg!r} 不该建实体"

    def test_name_rules(self, pe):
        C = pe._clean_entity_name
        assert C("张三") == "张三"
        assert C("  涛哥 ") == "涛哥"
        assert C("") == "" and C(None) == ""
        assert C("这是一个很长的名字超过十二个字符了") == ""      # 超长
        assert C(r"C:\Users\x") == ""                             # 含路径分隔符
        assert C("文件") == "" and C("内容我") == ""              # 通用词/子串
        assert C("我") == ""                                      # 代词


# ─────────────────── B. 4.6 两个工具补了统一入口 ───────────────────

class TestToolEntryPoints:
    @pytest.mark.parametrize("tool", ["office_cli", "win32_input"])
    def test_has_run_entry(self, tool):
        t = (ROOT / "tools" / f"{tool}.py").read_text(encoding="utf-8")
        assert re.search(r"^async def run\(", t, re.M), f"{tool} 缺 run() 入口"
        assert "ACTIONS" in t, f"{tool} 缺可枚举动作表"

    @pytest.mark.parametrize("tool", ["office_cli", "win32_input"])
    def test_unknown_action_honest(self, tool):
        import importlib
        mod = importlib.import_module(f"tools.{tool}")
        r = asyncio.run(mod.run(action="__nope__"))
        assert r.get("success") is False, f"{tool} 未知动作必须诚实报错"
        assert "__nope__" in str(r.get("error"))

    def test_win32_actions_are_wrapped(self):
        """平函数的返回值要包成网关契约 (success/output)。"""
        import importlib
        mod = importlib.import_module("tools.win32_input")
        # 用一个不会真动手的 action 验证包装: 未知 action 已经测过, 这里验结构
        assert set(mod.ACTIONS) == {"click", "click_center", "type_text", "hotkey", "scroll"}
        assert callable(mod._ok) and mod._ok(None)["success"] is True

    def test_office_cli_actions_cover_api(self):
        import importlib
        mod = importlib.import_module("tools.office_cli")
        # 每个 office_xxx 平函数都要能通过 action 调到
        for fn in ("create", "get", "set", "add", "query", "view", "info", "read", "write_text"):
            assert fn in mod.ACTIONS, f"action {fn} 没接线"


# ─────────── C. 4.6b plugin_manager 不谎报 + 出口归一 ───────────

class TestPluginManagerHonest:
    @pytest.fixture()
    def pm(self, tmp_path):
        from core.plugin_manager import PluginManager
        return PluginManager(tmp_path)

    def test_unknown_tool_honest(self, pm):
        r = asyncio.run(pm.call("no_such_tool_xyz"))
        assert r.get("success") is False and "未知工具" in str(r.get("error"))

    def test_no_entry_module_honest(self, pm, tmp_path):
        (ROOT / "tools" / "_t_noentry_x.py").write_text("X=1\n", encoding="utf-8")
        try:
            pm._plugins["_t_noentry_x"] = {"name": "_t_noentry_x", "tool": "_t_noentry_x",
                                           "path": str(ROOT / "tools" / "_t_noentry_x.py")}
            r = asyncio.run(pm.call("_t_noentry_x"))
            assert r.get("success") is False and "没有可调用入口" in str(r.get("error"))
        finally:
            (ROOT / "tools" / "_t_noentry_x.py").unlink(missing_ok=True)

    def test_result_key_normalized(self, pm):
        (ROOT / "tools" / "_t_norm_x.py").write_text(
            "async def run(**kw):\n    return {'success': True, 'result': 'R-VALUE'}\n", encoding="utf-8")
        try:
            pm._plugins["_t_norm_x"] = {"name": "_t_norm_x", "tool": "_t_norm_x",
                                        "path": str(ROOT / "tools" / "_t_norm_x.py")}
            r = asyncio.run(pm.call("_t_norm_x"))
            assert str(r.get("output")) == "R-VALUE" and r.get("output_from") == "result"
        finally:
            (ROOT / "tools" / "_t_norm_x.py").unlink(missing_ok=True)

    def test_no_lie_string_left(self):
        """两份 PluginManager 都不得再有 '[{tool}] called' 这种谎报。

        ★ 必须**剥掉注释**再断言 —— 注释里正当地记录着这个旧 bug 的原文,
        直接搜全文会假 FAIL (本人踩过的"断言注释文本"坑)。
        """
        for f in ("core/plugin_manager.py", "core/pipeline.py"):
            code = []
            for line in (ROOT / f).read_text(encoding="utf-8").splitlines():
                s = line.lstrip()
                if s.startswith("#"):
                    continue                      # 整行注释
                code.append(re.sub(r"\s+#.*$", "", line))   # 行尾注释
            body = "\n".join(code)
            assert "] called" not in body, f"{f} 的代码里仍有谎报成功"
            assert 'f"[{tool_name}] called"' not in body, f"{f} 仍有谎报模板"


# ─────────────────── D. 4.8 write_excel ───────────────────

class TestWriteExcel:
    @pytest.fixture()
    def TO(self):
        import importlib
        return importlib.import_module("tools.tiger_office")

    def _read(self, path):
        import openpyxl
        wb = openpyxl.load_workbook(path)
        ws = wb.active
        rows = [[c.value for c in r] for r in ws.iter_rows(max_row=5)]
        wb.close()
        return rows

    def test_headers_rows_into_dir(self, TO, tmp_path):
        r = asyncio.run(TO.run(action="write_excel", path=str(tmp_path) + os.sep, title="销量",
                               headers=["门店", "数量"], rows=[["A", 1], ["B", 2]]))
        assert r.get("success"), r.get("error")
        assert r["path"].startswith(str(tmp_path)) and r["path"].endswith(".xlsx")
        rows = self._read(r["path"])
        assert ["门店", "数量"] in rows and ["A", 1] in rows

    def test_markdown_content(self, TO, tmp_path):
        md = "| 姓名 | 电话 |\n|---|---|\n| 张三 | 139 |"
        r = asyncio.run(TO.run(action="write_excel", path=str(tmp_path) + os.sep,
                               title="通讯录", content=md))
        assert r.get("success"), r.get("error")
        rows = self._read(r["path"])
        assert ["姓名", "电话"] in rows and ["张三", "139"] in rows

    def test_dict_rows(self, TO, tmp_path):
        r = asyncio.run(TO.run(action="write_excel", path=str(tmp_path / "k.xlsx"),
                               headers=["月份", "额"], rows=[{"月份": "1月", "额": 10}]))
        assert r.get("success")
        assert ["月份", "额"] in self._read(r["path"])

    def test_empty_honest_error(self, TO, tmp_path):
        r = asyncio.run(TO.run(action="write_excel", path=str(tmp_path) + os.sep))
        assert r.get("success") is False and "没有内容" in str(r.get("error"))

    def test_registered_in_dispatch(self, TO, tmp_path):
        """派发表里真的有 write_excel (不是光定义了函数)。"""
        t = (ROOT / "tools" / "tiger_office.py").read_text(encoding="utf-8")
        assert '"write_excel": _write_excel' in t


# ─────────────────── E. 4.7 ocr ───────────────────

class TestOcr:
    def test_tesseract_detection_not_hardcoded_only(self):
        t = (ROOT / "tools" / "ocr.py").read_text(encoding="utf-8")
        assert "def _find_tesseract" in t, "必须有探测函数 (原来硬编码单个路径)"
        assert 'os.environ.get("TESSERACT_CMD"' in t, "环境变量应可覆盖"
        assert 'shutil' in t or "_sh.which" in t, "应在 PATH 里找"

    def test_missing_input_honest(self):
        import importlib
        mod = importlib.import_module("tools.ocr")
        r = asyncio.run(mod.execute())
        assert r.get("success") is False and "缺少图片" in str(r.get("error"))

    def test_nonexistent_file_honest(self):
        import importlib
        mod = importlib.import_module("tools.ocr")
        r = asyncio.run(mod.execute(path=str(ROOT / "tmp" / "__nope__.png")))
        assert r.get("success") is False

    def test_returns_output_key(self):
        """返回必须有 output (契约), 不能只给 result。"""
        import importlib
        mod = importlib.import_module("tools.ocr")
        r = asyncio.run(mod.execute())
        assert "output" in r

    def test_real_ocr(self, tmp_path):
        mod = pytest.importorskip("tools.ocr")
        if not mod._TESSERACT_CMD:
            pytest.skip("本机没有 tesseract 二进制")
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            pytest.skip("Pillow 不在")
        img = Image.new("RGB", (600, 120), "white")
        ImageDraw.Draw(img).text((20, 40), "Hello World 12345", fill="black")
        p = tmp_path / "t.png"
        img.save(p)
        r = asyncio.run(mod.execute(path=str(p)))
        assert r.get("success"), r.get("error")
        assert "12345" in str(r.get("output")), r.get("output")


# ─────────────────── F. 4.5 无重名 TOOL ───────────────────

class TestNoDuplicateTools:
    def test_output_dup_removed(self):
        assert not (ROOT / "tools" / "output.py").exists(), "重复的 openmeteo 旧草稿应已移除"

    def test_no_output_in_known_tools_fallback(self):
        t = (ROOT / "core" / "skill_loader.py").read_text(encoding="utf-8")
        block = t.split("names |= {")[1].split("}")[0]
        assert '"output"' not in block, "手工兜底清单里不该再留 output"

    def test_tool_names_unique(self, tmp_path):
        from core.plugin_manager import PluginManager
        pm = PluginManager(tmp_path)
        seen = {}
        for k, v in pm._plugins.items():
            t = Path(v["path"]).read_text(encoding="utf-8", errors="replace")
            m = re.search(r'"name"\s*:\s*"([^"]+)"', t)
            nm = m.group(1) if m else k
            seen.setdefault(nm, []).append(k)
        dup = {k: v for k, v in seen.items() if len(v) > 1}
        assert not dup, f"TOOL 名重复: {dup}"
