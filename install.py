"""
Tiger.M.M 首次运行向导
用法:
    python install.py
    (用你自己装了依赖的那个 Python 跑即可; 3.10+ 都行)

做四件事:
  ① 检查 Python 版本
  ② 按 requirements.txt 装依赖 (单一来源, 不再各写一份清单)
  ③ 建目录 + 从 keys_template.json 生成 keys.json (单一模板来源)
  ④ 跑分发门禁自检 + 打印下一步

设计原则 (2026-09-20 分发整改):
  · 幂等: 已存在的 keys.json **绝不覆盖** (里面是用户的真凭据)
  · 依赖清单只认 requirements.txt —— 这里不再维护第二份 (曾经 install.py 写死
    14 个包, 与 requirements 漂移: 装了没在用的 playsound/whisper, 又漏了 fastapi/pywin32)
  · 收尾必须跑门禁 —— "装完了" 不等于 "装对了", 让门禁说话
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
REQUIRED_PYTHON = (3, 10)
REQ = PROJECT_ROOT / "requirements.txt"
TEMPLATE = PROJECT_ROOT / "keys_template.json"


def step(n, title):
    print(f"\n[{n}] {title}")
    print("-" * 60)


def check_python() -> bool:
    v = sys.version_info
    if v < REQUIRED_PYTHON:
        print(f"  ✗ 需要 Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}+, 当前 {v.major}.{v.minor}")
        return False
    print(f"  ✓ Python {v.major}.{v.minor}.{v.micro}")
    return True


def install_deps() -> None:
    """按 requirements.txt 装 —— 只用必需段 (可选增强段是注释, 不装也能跑)。"""
    if not REQ.exists():
        print("  ⚠ 没找到 requirements.txt, 跳过装依赖 (自己 pip install 吧)")
        return
    if os.environ.get("TMM_SKIP_DEPS") == "1":
        print("  (TMM_SKIP_DEPS=1 → 跳过装依赖)")
        return
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(REQ), "-q"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        print("  ✓ requirements.txt 依赖装好/已满足")
    else:
        tail = (r.stdout or "") + (r.stderr or "")
        print("  ⚠ pip 有报错 (继续, 多数功能靠兜底链仍可用)。末尾:")
        for l in tail.strip().splitlines()[-4:]:
            print("     ", l[:110])


def create_dirs() -> None:
    for d in ("data", "backups", "voice-memos", "logs", "tmp"):
        (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)
    print("  ✓ 目录就绪: data/ backups/ voice-memos/ logs/ tmp/")


def init_keys() -> None:
    """从 keys_template.json 生成 keys.json —— 已存在则**不碰**。"""
    keys = PROJECT_ROOT / "keys.json"
    if keys.exists():
        print("  ✓ keys.json 已存在 —— 保留原样 (里面是凭据, 绝不覆盖)")
        _report_keys(keys)
        return
    if TEMPLATE.exists():
        shutil.copy2(TEMPLATE, keys)
        print("  ✓ 已从 keys_template.json 生成 keys.json —— 请填入你的 API Key")
    else:
        keys.write_text(json.dumps({
            "_说明": "填入至少一个模型的 key",
            "deepseek": {"key": "", "url": "https://api.deepseek.com", "model": "deepseek-chat"},
            "ollama": {"key": "ollama", "url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  ✓ 已生成最小 keys.json (模板文件缺失, 用了内置兜底)")
    _report_keys(keys)


def _is_placeholder_key(val) -> bool:
    """占位/示例 key 判据 —— **共用 core.provider_catalog 里那一处**, 不在这里另写一套。

    踩过两回:
      ① 曾写成 `startswith(("sk-把你的", "sk-your", ""))` —— 空串让 startswith 永远为真
         → 把已配置的 4 个模型全报成"还没有"(向导骗自己)。别把空串塞进前缀元组。
      ② 判据分散两处(install 一套 / check_keys 一套) → 一边认成占位符、另一边拿去发请求
         → 报 `'latin-1' codec can't encode characters`。单一判据才好对齐。
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from core.provider_catalog import is_placeholder_key
        return is_placeholder_key(val)
    except Exception:
        s = str(val or "").strip().lower()
        if not s:
            return True
        return any(h in s for h in ("sk-把你的", "sk-your", "placeholder", "your-key", "填这里"))


def _report_keys(keys: Path) -> None:
    """只报**配了哪几家**, 绝不回显 key 内容。"""
    try:
        d = json.loads(keys.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"     ⚠ keys.json 读不了: {e}")
        return
    # ★ 踩过: 这里曾写成 startswith(("sk-把你的", "sk-your", "")) —— 空串让 startswith 恒真,
    #   于是**已配置的模型全被判成未配置**, 向导骗自己("还没有")。别把空串塞进前缀元组。
    ready = [k for k, v in d.items()
             if isinstance(v, dict) and str(v.get("key") or "").strip()
             and not _is_placeholder_key(v["key"])]
    print(f"     已配置的模型: {ready or '（还没有 —— 至少填一个才能起引擎）'}")
    if isinstance(d.get("mail"), dict):
        print(f"     邮箱: {'已配置' if d['mail'].get('user') else '未配置（可选，用了才需要）'}")


def gate() -> int:
    """收尾跑分发门禁 —— 让它说话, 不靠我宣布。

    ★ 2026-10-09: 这道门禁是**开发仓**资产 (scripts/ 按用户定稿不进包), 所以收件人
      这里**必然**不存在 —— 原来打 `⚠ 分发门禁不存在, 跳过` 会让他以为装漏了东西。
      分发包里不印这行 (开发仓里照跑)。
    """
    v = PROJECT_ROOT / "scripts" / "verification" / "verify_distribution_ready.py"
    if not v.exists():
        return 0
    r = subprocess.run([sys.executable, "-B", str(v)], cwd=str(PROJECT_ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if "结果:" in l]
    print("  分发门禁:", tail[-1].strip() if tail else "(无结果行)")
    if r.returncode != 0:
        print("  ⚠ 门禁有红 —— 先看上面明细, 别急着跑")
    return r.returncode


def key_check() -> int:
    """★ 2026-10-09 加: 填了 Key 就**当场验一次** —— 让它自己说"哪家能用"。

    为什么不是"按 sk- 前缀猜厂商": 前缀几乎所有 OpenAI 兼容厂商共用, 猜必错。
    唯一可靠的是**真发一次最小请求**(1 个 token), 让那家自己回话。
    """
    ck = PROJECT_ROOT / "check_keys.py"
    if not ck.exists():
        print("  ⚠ 没有 check_keys.py, 跳过")
        return 0
    # 先看有没有"真 Key"可验 (占位符/空的不算)
    keys = PROJECT_ROOT / "keys.json"
    has_real = False
    if keys.exists():
        try:
            d = json.loads(keys.read_text(encoding="utf-8"))
            has_real = any(isinstance(v, dict) and str(v.get("key") or "").strip()
                           and not _is_placeholder_key(v.get("key"))
                           for v in d.values())
        except Exception:
            pass
    if not has_real:
        print("  还没填 Key —— 填完再验（下面第 2 步告诉你跑哪个）")
        return 0
    r = subprocess.run([sys.executable, "-B", str(ck)], cwd=str(PROJECT_ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    for l in out.splitlines()[-28:]:
        print("  " + l)
    return r.returncode


def main() -> int:
    print("=" * 60)
    print("  Tiger.M.M 首次运行向导")
    print("=" * 60)
    step(1, "Python 版本")
    if not check_python():
        return 1
    step(2, "依赖 (requirements.txt)")
    install_deps()
    step(3, "目录与配置")
    create_dirs()
    init_keys()
    # ★ 2026-10-09: 第 4 步(分发门禁)是**开发仓**自检 —— 分发包里没有 scripts/, 所以
    #   收件人这里会是空标题 + 什么都不发生。只有**在开发仓**才印这一节。
    if (PROJECT_ROOT / "scripts" / "verification" / "verify_distribution_ready.py").exists():
        step(4, "自检")
        rc = gate()
    else:
        rc = 0
    step(5, "验 Key (真连一次, 看哪家能用)")
    key_check()
    print("\n" + "=" * 60)
    print("  下一步")
    print("=" * 60)
    print("  1. 配模型:          起界面后点左栏「模型 · 设置」—— 选一家、粘 Key、")
    print("                      点「测一下」当场真连一次, 保存即生效 (不用改文件、不用重启)")
    print("                      一分钱不想花 → 装 Ollama (https://ollama.com), 面板里点「刷新」即可")
    print("  2. 验 Key:          双击 check_keys.bat  (中文告诉你哪家可用、哪家该改哪儿)")
    print("  3. 起引擎:          桌面「虎哥 Tiger.M.M」或 pythonw -B tmm_app.py")
    print("  4. 命令行:          tmm.bat")
    # ★ 2026-10-09: 这里原来还印两行 **开发仓专用** 的提示 (verify.bat / docs/data_boundary.md)——
    #   分发包里两者都**不存在** (scripts/ 与 docs/ 按用户定稿不进包)。留着 = 收件人被指去
    #   跑一个不存在的文件、读一份不存在的文档。开发仓里那两件事照旧, 只是别印给收件人。
    print("=" * 60)
    return rc


if __name__ == "__main__":
    sys.exit(main())
