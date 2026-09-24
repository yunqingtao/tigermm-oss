r"""本地视觉 (vision/local) 契约 —— 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: 本机有视觉模型时 vision 工具必须真读得对; 没有时**报错必须能指导下一步**。

为什么单独立门 (2026-09-22 实测):
  · 用户问「ollama 下面没有吗」时, vision 报的是「本机 ollama 上没有可用的视觉模型」——
    而库里明明有 `qwen2.5vl:7b`。真因是 **ollama 服务没在跑**, 报错把「服务没开」
    说成了「没装模型」⇒ 用户照着去下载模型, 白折腾。这是**诚实失败**问题, 不是功能问题。
  · 修后实测本机对比 (同一张自造图, 目标编号 TMM-VISION-7391):
      qwen2.5vl:7b 读对 ✓ 1.8s(热) / gemma4:12b 48.4s / qwen3.5:9b 60.4s / minicpm-v4.6 读错
    ⇒ 名字含 vl/vision 优先的选择逻辑是对的, 不该改成"按新旧/体积挑"。

跑法: python -B scripts/verification/verify_vision_local.py
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
import logging          # noqa: E402
logging.disable(logging.CRITICAL)

_ISO = Path(tempfile.gettempdir()) / ("tmm_vision_iso_%d" % os.getpid())
_ISO.mkdir(parents=True, exist_ok=True)
SB = ROOT / "tmp" / "_vision_gate"
SB.mkdir(parents=True, exist_ok=True)
P, F, SK = [], [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    SK.append(name)
    print("  SKIP " + name + "   <- " + why)


def _ollama_alive():
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=3).read()
        return True
    except Exception:
        return False


def main() -> int:
    print("=" * 78)
    print("本地视觉 —— 真读得对 + 报错能指导下一步")
    print("=" * 78)
    from tools import vision as V

    KEY = "TMM-VISION-7391"

    # ── [1] 失败语义 (打桩, 不依赖真实服务状态) ──
    print("\n[1] 报错语义: 服务没起 ≠ 没装模型 (诚实失败)")
    _up, _url = V._ollama_up, V.urllib.request.urlopen

    V._ollama_up = lambda timeout=3: False
    m, why = V._local_vision_probe()
    chk("★ 服务没起 → 明说「服务未启动」而非「没有视觉模型」",
        m is None and "服务未启动" in why, why[:110])
    chk("★ 该报错给了下一步动作 (启动 ollama)", "启动" in why or "ollama" in why, why[:110])
    r = asyncio.run(V._see_local(str(SB / "x.png"), "看图"))
    chk("★ 工具层同样传递真实原因 (不再一律说没有可用的视觉模型)",
        "服务未启动" in str(r.get("error")), str(r.get("error"))[:110])

    V._ollama_up = lambda timeout=3: True

    class _Resp:
        def __init__(self, payload):
            self._p = json.dumps(payload).encode()

        def read(self):
            return self._p

    def _fake_urlopen(url, *a, **k):
        if str(url).endswith("/api/tags"):
            return _Resp({"models": [{"name": "llama3:8b"}, {"name": "bge-m3:latest"}]})
        return _Resp({})           # /api/show → 无 capabilities

    V.urllib.request.urlopen = _fake_urlopen
    m2, why2 = V._local_vision_probe()
    chk("★ 服务在但无视觉模型 → 报清「没有视觉模型」",
        m2 is None and "没有视觉模型" in why2, why2[:120])
    chk("★ 并给了可执行命令 (ollama pull)", "ollama pull" in why2, why2[:120])
    chk("★ 报清装了几个 (不是含糊其辞)", "已装 2 个" in why2, why2[:120])

    # 选择逻辑: 名字含 vl/vision 的优先 (用桩给两组候选)
    def _fake_urlopen2(url, *a, **k):
        u = str(url)
        if u.endswith("/api/tags"):
            return _Resp({"models": [{"name": "ui-tars:2b"}, {"name": "qwen2.5vl:7b"},
                                     {"name": "qwen3.5:9b"}]})
        body = json.loads(k.get("data") or b"{}")
        nm = (body or {}).get("model", "")
        caps = ["completion", "vision"]          # 三个都自报 vision
        return _Resp({"capabilities": caps})

    V.urllib.request.urlopen = _fake_urlopen2
    m3, _ = V._local_vision_probe()
    chk("★ 多个视觉模型时优先名字含 vl/vision 的 (qwen2.5vl)", m3 == "qwen2.5vl:7b", m3)

    # 环境变量可钉死 (用户手动指定优先于自动挑)
    os.environ["TMM_VISION_LOCAL_MODEL"] = "my-pinned-vl"
    chk("★ TMM_VISION_LOCAL_MODEL 钉死时以其为准",
        V._local_vision_probe()[0] == "my-pinned-vl", V._local_vision_probe())
    os.environ.pop("TMM_VISION_LOCAL_MODEL", None)

    V._ollama_up, V.urllib.request.urlopen = _up, _url   # 还原 (别污染后续真跑)

    # ── [2] 真读一张自造图 (ollama 活着才跑) ──
    print("\n[2] 真读图 (需要 ollama 服务在跑)")
    if not _ollama_alive():
        skip("真读图 (本机 ollama 未运行)", "非代码问题; 起 ollama 后重跑本门即为新鲜证据")
    else:
        try:
            from PIL import Image, ImageDraw, ImageFont
            img = Image.new("RGB", (760, 200), "white")
            d = ImageDraw.Draw(img)
            try:
                f = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 64)
            except Exception:
                f = ImageFont.load_default()
            d.text((40, 60), KEY, fill="black", font=f)
            p = _ISO / "vk.png"
            img.save(p)
            picked = V._local_vision_model()
            chk("★ 自动挑到本机视觉模型 (非 None)", bool(picked), picked)
            r = asyncio.run(V.run(image=str(p), question="图里的英文编号是什么？只回编号。",
                                  provider="local"))
            out = str(r.get("output") or r.get("error") or "")
            chk("★ 真读对图里的编号 " + KEY, KEY in out,
                f"model={r.get('model')} out={out[:80]!r}")
            chk("★ 真读图不报错且标了 provider/model",
                bool(r.get("success")) and bool(r.get("provider")) and bool(r.get("model")), str(r)[:120])
            # 耗时只作信息 (首次要加载模型, 冷启动可达 40s —— 别拿它当判据)
            print(f"       (信息) 本次 model={r.get('model')} 耗时={r.get('elapsed') or '-'}")
        except Exception as e:
            chk("真读图", False, repr(e)[:140])

    # ── [3] 既有契约防回退: 图不存在 → 零网络 ──
    print("\n[3] 防回退: 本地校验前置 (图不存在不许先花网络)")
    calls = []
    _u2 = V.urllib.request.urlopen

    def _spy(url, *a, **k):
        calls.append(str(url))
        return _u2(url, *a, **k)

    V.urllib.request.urlopen = _spy
    r = asyncio.run(V.run(image=str(SB / "no-such-file.png"), question="看图"))
    V.urllib.request.urlopen = _u2
    chk("★ 图片不存在 → 诚实报错且零网络请求", (not r.get("success")) and not calls,
        f"success={r.get('success')} calls={calls[:2]}")

    print("\n" + "=" * 78)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL" + (f" / {len(SK)} SKIP" if SK else ""))
    for f in F:
        print("  -", f)
    print("=" * 78)
    return 1 if F else 0


if __name__ == "__main__":
    import shutil
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(SB, ignore_errors=True)
        shutil.rmtree(_ISO, ignore_errors=True)
