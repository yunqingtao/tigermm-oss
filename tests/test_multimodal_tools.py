"""tests for multimodal tools: vision (看图) + image_gen (生图).

覆盖点 (全部**离线**, 不打网络/不花钱):
  · 入参清洗 —— 技能层/关键词路由会把**整句**透传, 工具必须自己捞/剥
  · ★ 剥引导词不能过头 (单字候选"画"曾咬掉描述正文) ← 变异测试守这个
  · ★ 诚实报错 —— 缺图/图不存在/缺描述/缺凭证 → success=False + 明确 error, 不猜不编
  · 零副作用 —— 失败路径不发请求
  · 凭证解析顺序 —— 环境变量 > keys.json
  · 能力判定 —— 纯文本模型必须判为"看不见图"(否则 base64 会给它 → 编造)
"""
import sys, os, json, asyncio, base64
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import vision as V
from tools import image_gen as G


@pytest.fixture(autouse=True)
def _clear_key_cache():
    """凭证缓存是模块级的 —— 每个用例清一次, 免得互相污染。"""
    V._KEY_CACHE.clear()
    yield
    V._KEY_CACHE.clear()


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return p


# ─────────────────────────── 入参清洗 ───────────────────────────

class TestVisionImageExtraction:
    """整句里捞图片路径 (技能层透传的是原话, 不是纯路径)。"""

    @pytest.mark.parametrize("msg,want", [
        ("看看这张图 D:/x/y.png 里有什么", "D:/x/y.png"),
        ("这张图 https://e.com/a.jpg 画了什么", "https://e.com/a.jpg"),
        ("图里有什么 shot.png", "shot.png"),
        ("你好啊", ""),
        ("", ""),
    ])
    def test_extract(self, msg, want):
        got = V._extract_image(msg)
        assert got.replace("\\", "/").lower() == want.replace("\\", "/").lower()

    def test_backslash_path(self):
        assert V._extract_image(r"看图 C:\Users\a\b\c.JPEG").replace("\\", "/") == "C:/Users/a/b/c.JPEG"


class TestPromptStripping:
    """剥掉"画一张"这类引导词 —— 但不能剥过头。"""

    @pytest.mark.parametrize("src,want", [
        ("画一张赛博朋克中国龙", "赛博朋克中国龙"),
        ("帮我生成一张水墨山水画", "水墨山水画"),
        ("请帮我画一只猫", "一只猫"),
        ("生成图片：霓虹紫的机械龙", "霓虹紫的机械龙"),
        ("画个戴墨镜的柴犬", "戴墨镜的柴犬"),
        ("赛博朋克城市", "赛博朋克城市"),          # 无引导词 → 原样
    ])
    def test_strip_lead(self, src, want):
        assert want in G._extract_prompt(src)

    def test_no_overstrip(self):
        """★ 回归守卫: 单字候选"画"曾被循环反复作用, 咬掉描述正文。
        (原 bug: "画一张画中有画的画" → "中有画的画")
        """
        got = G._extract_prompt("画一张画中有画的画")
        assert got == "画中有画的画"


# ─────────────────────────── 诚实报错 ───────────────────────────

class TestHonestFailure:
    """做不到就说做不到 —— 不编造、不谎报成功。"""

    def test_vision_no_image(self):
        r = asyncio.run(V.run())
        assert r["success"] is False and r["error"]

    def test_vision_missing_file(self, tmp_path):
        """文件检查发生在联网之前 → 给显式模型名即可确定性验证。"""
        r = asyncio.run(V._see_local(str(tmp_path / "nope.png"), "看图", model="qwen2.5vl:7b"))
        assert r["success"] is False and "不存在" in r["error"]

    def test_vision_no_image_in_text(self):
        r = asyncio.run(V.run(query="你好啊聊聊天"))
        assert r["success"] is False and r["error"]

    def test_vision_no_credentials(self, img, monkeypatch):
        """★ 钉住 dashscope 分支 —— 不钉的话本机 ollama 有视觉模型就会成功, 用例失真。"""
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("QWEN_API_KEY", raising=False)
        monkeypatch.setattr(V, "_resolve_key", lambda: "")
        r = asyncio.run(V.run(image=str(img), provider="dashscope"))
        assert r["success"] is False and "凭证" in r["error"]

    def test_missing_file_checked_before_network(self, tmp_path, monkeypatch):
        """★ 输入先验: 图不存在时**一个网络请求都不该发**
        (成本顺序: 本地就能判定的失败, 别去探 ollama/百炼 —— 本轮 ad-hoc 抓到过)。
        """
        calls = []

        def boom(req, *a, **k):
            calls.append(str(getattr(req, "full_url", req))[:60])
            raise AssertionError("本地就能判定的失败不该进网络")

        monkeypatch.setattr(V.urllib.request, "urlopen", boom)
        r = asyncio.run(V.run(image=str(tmp_path / "nope.png"), provider="auto"))
        assert r["success"] is False and "不存在" in r["error"]
        assert calls == []

    def test_vision_no_local_model(self, img, monkeypatch):
        monkeypatch.setattr(V, "_local_vision_model", lambda: None)
        r = asyncio.run(V.run(image=str(img), provider="local"))
        assert r["success"] is False and "ollama" in r["error"]

    def test_imagegen_no_prompt(self):
        r = asyncio.run(G.run())
        assert r["success"] is False and r["error"]

    def test_imagegen_no_credentials(self, monkeypatch):
        monkeypatch.setattr(G, "_resolve_key", lambda: "")
        r = asyncio.run(G.run(prompt="一只猫"))
        assert r["success"] is False and "凭证" in r["error"]


# ─────────────────────────── 零副作用 ───────────────────────────

class TestNoSideEffects:
    """失败路径不得发网络请求 (否则白烧配额)。"""

    def test_failures_never_hit_network(self, tmp_path, monkeypatch):
        calls = []

        def boom(req, *a, **k):
            calls.append(str(getattr(req, "full_url", req))[:60])
            raise AssertionError("失败路径不该发请求")

        monkeypatch.setattr(V.urllib.request, "urlopen", boom)
        monkeypatch.setattr(G.urllib.request, "urlopen", boom)
        monkeypatch.setattr(V, "_resolve_key", lambda: "")
        monkeypatch.setattr(G, "_resolve_key", lambda: "")

        monkeypatch.setattr(V, "_local_vision_model", lambda: None)

        async def _all():
            await V.run()                                                          # 缺图
            await V.run(image=str(tmp_path / "nope.png"), provider="local")         # 图不存在
            await V.run(query="没有图的句子")                                       # 句中无图
            await G.run()                                                          # 缺描述

        asyncio.run(_all())
        assert calls == []


# ─────────────────────────── 凭证 + 能力判定 ───────────────────────────

class TestCredentials:
    def test_env_precedence(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", "ENV_KEY")
        assert V._resolve_key() == "ENV_KEY"
        assert G._resolve_key() == "ENV_KEY"

    def test_qwen_env_alias(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.setenv("QWEN_API_KEY", "ALIAS_KEY")
        assert V._resolve_key() == "ALIAS_KEY"

    def test_fallback_to_keys_json(self, monkeypatch):
        # ★ 2026-09-23: keys.json 是**凭据文件**, 按设计不进分发包 ⇒ 包里跑必假红。
        #   这是"依赖不在"不是"代码坏" → 缺它就 SKIP 并说明 (项目口径, 与门禁同一条)。
        kf = Path(__file__).resolve().parent.parent / "keys.json"
        if not kf.is_file():
            pytest.skip("keys.json 不在 (凭据文件不进包) —— 无法验证回落")
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("QWEN_API_KEY", raising=False)
        k = V._resolve_key()
        assert len(k) > 20, "应回落到 keys.json 的 qwen.key"

    def test_tool_declarations(self):
        for mod, nm in ((V, "vision"), (G, "image_gen")):
            t = mod.TOOL
            assert t["name"] == nm and t["params"] and t["description"]
            assert {p["name"] for p in t["params"]}


class TestCanSeeImages:
    """★ 能力判定: 纯文本模型绝不能拿到图片 base64 (否则会编造图片内容)。"""

    @pytest.mark.parametrize("model,can_see", [
        ("deepseek", False), ("mimo", False), ("ollama", False),
        ("qwen3.7-flash", False), ("", False),
        ("qwen-vl-plus", True), ("llava:7b", True), ("minicpm-v", True), ("gemma3:4b", True),
    ])
    def test_gate(self, model, can_see):
        from core.pipeline import Level4Pipeline
        assert Level4Pipeline._can_see_images(model) is can_see


# ─────────────────────────── provider 路由 (本地优先) ───────────────────────────

class TestProviderRouting:
    """auto = 本地优先; 两档都失败必须**诚实汇总**, 不许编造。"""

    def test_local_success(self, img, monkeypatch):
        async def fake(src, ask, model=""):
            return {"success": True, "output": "本地看见的", "provider": "local", "model": "qwen2.5vl:7b"}
        monkeypatch.setattr(V, "_see_local", fake)
        r = asyncio.run(V.run(image=str(img), provider="local"))
        assert r["success"] and r["provider"] == "local"

    def test_auto_prefers_local(self, img, monkeypatch):
        async def fake(src, ask, model=""):
            return {"success": True, "output": "本地", "provider": "local"}
        async def nope(src, ask, model=""):
            raise AssertionError("auto 不该先去 dashscope")
        monkeypatch.setattr(V, "_see_local", fake)
        monkeypatch.setattr(V, "_see_dashscope", nope)
        r = asyncio.run(V.run(image=str(img), provider="auto"))
        assert r["success"] and r["provider"] == "local"

    def test_auto_falls_back_to_dashscope(self, img, monkeypatch):
        async def fail(src, ask, model=""):
            return {"success": False, "error": "本地没模型", "output": ""}
        async def ok(src, ask, model=""):
            return {"success": True, "output": "云端看见的", "provider": "dashscope"}
        monkeypatch.setattr(V, "_see_local", fail)
        monkeypatch.setattr(V, "_see_dashscope", ok)
        r = asyncio.run(V.run(image=str(img), provider="auto"))
        assert r["success"] and r["provider"] == "dashscope"

    def test_auto_both_fail_is_honest(self, img, monkeypatch):
        async def fail(src, ask, model=""):
            return {"success": False, "error": "X失败", "output": ""}
        monkeypatch.setattr(V, "_see_local", fail)
        monkeypatch.setattr(V, "_see_dashscope", fail)
        r = asyncio.run(V.run(image=str(img), provider="auto"))
        assert r["success"] is False
        assert "X失败" in r["error"], "两个 provider 的错误都要如实报出来"
        assert not str(r.get("output") or "").strip(), "失败时绝不能有内容输出"

    def test_local_thinking_only_is_used_and_flagged(self, img, monkeypatch):
        """thinking 模型可能把答案全放思考里 —— 用它(模型自己的输出)但要如实标注。"""
        class _R:
            def read(self):
                return json.dumps({"message": {"content": "", "thinking": "图中写着 TMM739"}}).encode()
        monkeypatch.setattr(V.urllib.request, "urlopen", lambda *a, **k: _R())
        r = asyncio.run(V._see_local(str(img), "图里有什么", model="gemma4-12b-fullgpu:latest"))
        assert r["success"], f"期望成功, 实际: {r}"
        assert r.get("from_thinking") is True and "TMM739" in r["output"]


# ─────────────────────────── ollama 能力判定 (权威来源) ───────────────────────────

class TestOllamaCapabilities:
    """别按名字猜 —— 问 ollama 自报的 capabilities。"""

    def test_caps_win_over_name(self, monkeypatch):
        from core.pipeline import Level4Pipeline as L
        L._ollama_caps_cache.clear()
        monkeypatch.setattr(L, "_ollama_capabilities", classmethod(lambda cls, m: {"completion", "vision"}))
        # 名字里没有任何视觉提示词, 但自报能看图 → True
        assert L._can_see_images("gemma4-12b-fullgpu:latest") is True

    def test_caps_say_no_vision(self, monkeypatch):
        from core.pipeline import Level4Pipeline as L
        monkeypatch.setattr(L, "_ollama_capabilities", classmethod(lambda cls, m: {"completion", "tools"}))
        assert L._can_see_images("llama3:8b") is False

    def test_unknown_falls_back_to_name_hint(self, monkeypatch):
        from core.pipeline import Level4Pipeline as L
        monkeypatch.setattr(L, "_ollama_capabilities", classmethod(lambda cls, m: None))
        assert L._can_see_images("qwen-vl-plus") is True     # 云端模型 → 名字提示
        assert L._can_see_images("deepseek-chat") is False

    def test_resolved_model_id_uses_config(self):
        from core.pipeline import Level4Pipeline as L
        class _MC:
            def _get_model_config(self, n):
                return {"model": "gemma4-12b-fullgpu:latest"} if n == "ollama" else {}
        assert L._resolved_model_id(type("S", (), {"model_client": _MC()})(), "ollama") == "gemma4-12b-fullgpu:latest"
