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
    """占位/示例 key 判据 (小写比较, 且**不把空串放进 startswith 元组** —— 那样恒真)。

    踩过: 曾写成 `startswith(("sk-把你的", "sk-your", ""))` → 空串让 startswith 永远为真
    → 把已配置的 4 个模型全报成"还没有"(向导骗自己)。
    """
    s = str(val or "").strip().lower()
    if not s:
        return True
    return any(s.startswith(p.lower()) or p.lower() in s
               for p in ("sk-把你的", "sk-your", "placeholder", "your-key", "填这里"))


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
    """收尾跑分发门禁 —— 让它说话, 不靠我宣布。"""
    v = PROJECT_ROOT / "scripts" / "verification" / "verify_distribution_ready.py"
    if not v.exists():
        print("  ⚠ 分发门禁不存在, 跳过")
        return 0
    r = subprocess.run([sys.executable, "-B", str(v)], cwd=str(PROJECT_ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.splitlines() if "结果:" in l]
    print("  分发门禁:", tail[-1].strip() if tail else "(无结果行)")
    if r.returncode != 0:
        print("  ⚠ 门禁有红 —— 先看上面明细, 别急着跑")
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
    step(4, "自检")
    rc = gate()
    print("\n" + "=" * 60)
    print("  下一步")
    print("=" * 60)
    print("  1. 填 API Key:      编辑 keys.json (至少一个模型)")
    print("  2. 起引擎:          桌面「虎哥 Tiger.M.M」或 pythonw -B tmm_app.py")
    print("  3. 命令行:          tmm.bat")
    print("  4. 全门禁验收:      verify.bat      (~10 分钟, 全绿才算装对)")
    print("  5. 打包/分发前:     读 docs/data_boundary.md (哪些不该出门)")
    print("=" * 60)
    return rc


if __name__ == "__main__":
    sys.exit(main())
