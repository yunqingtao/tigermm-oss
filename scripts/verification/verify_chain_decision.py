r"""「要不要成链」判定契约 —— 常驻验证器 (ad-hoc 留档, 非 pytest 套件)

命题: 一句话该走**显式规划链** / 走**技能** / 就地单步, 必须判得准, 且**两个方向**都要守住:
  · 该成链的别漏 (漏了 = 用户说了"存到"却什么都没落)
  · 不该成链的别抢 (抢了 = 多花一次规划调用; 更糟的是技能只做了请求的一半)

为什么单独立门 (2026-09-22 语料实测驱动):
  旧判据靠**数字面连接词**("先/再/然后" ≥2)。自家 62 条标注语料实测:
    plan 召回 **50%** (7 条真多步被漏 —— 用户多数时候不写连接词)
    另 2 条废话/犹豫被误判成多步 ("然后再把这句发给涛哥" / "先不急着写，先看看再说")
  且抓到 3 条**真缺陷**: 技能抢走多步请求, 每条只做了请求的一半 ——
    · "先看看模型用量，再统计一下今天花了多少钱" → model-usage 只统计了用量
    · "查一下 ollama 有哪些模型，看看哪些能看图，整理成表格" → look-at-image 命中"**看图**"(藏在"能看图"里)
    · "把这三个文件都读一遍，比较一下不同，写成一份对比" → speak-aloud 命中"读一遍"(语义歧义: 读文件≠朗读)
  改法 (借 ADaPT 的思路: 按"执行者能不能一次干完"判, 不按句子形态): 判据换成**数动作段**
  (查询型动作 / 总动作 / 明确落盘目标), 并让"多步工具调查型"请求**优先于技能抢占**。

★ 两类语料分开报:
  chain_decision_corpus.json   调参集 (词表在这上面调出来的)
  chain_decision_holdout.json  留出集 (**不参与调参**, 用来检测过拟合 —— 100% 才算真稳)
  留出集有 1 条经**标签修订**并留痕 ("直接做，先写背景再写方案": '直接做'=跳过规划链,
  不是禁止技能, 所以期望是走技能而非单步)。

跑法: python -B scripts/verification/verify_chain_decision.py
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "verification"))
os.chdir(ROOT)
from _probe_env import activate          # noqa: E402
activate()
import logging                           # noqa: E402
logging.disable(logging.CRITICAL)

HERE = Path(__file__).resolve().parent
P, F = [], []


def chk(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("   <- " + str(detail)) if detail and not cond else ""))


def classify(PL, msg: str) -> str:
    """★ 忠实复刻真实决策链 (含**运行期守卫**, 不是只看"匹配到没")。

    实测教训: 只用 `_match_skill_trigger` 太粗 —— "帮我看看这份周报" 能匹配到技能,
    但真跑时会被**产出守卫**挡下 → 最终走的是通用兜底 (single)。所以必须把
    `_skill_dag_exec` 的三道门也复刻进来, 否则测的不是用户看到的结果:
      ① require_llm: 有 llm 步骤 或 纯产出型
      ② 产出守卫: 有 llm 步骤时, 需要 `像要产出` / `有显式素材` / `answer_mode` / `触发词主导整句`
    """
    if not PL._is_explicit_model({}):
        hit = None if PL._planner_preempts(msg) else PL._skill_match(msg, trigger_only=True)
        if hit:
            _name, dag, _score = hit
            steps = getattr(dag, "steps", None) or []
            if steps:
                has_llm = any((s or {}).get("tool") == "llm" for s in steps)
                if has_llm or PL._is_pure_producer(dag):
                    am = bool((getattr(dag, "raw_source", None) or {}).get("answer_mode"))
                    exempt = (PL._looks_like_creation(msg) or PL._has_explicit_source(msg)
                              or am or PL._trigger_dominant(msg, dag))
                    if (not has_llm) or exempt:
                        return "skill"
    if PL._match_planner(msg, {}):
        return "plan"
    return "single"


def run_corpus(PL, path: Path, label: str):
    d = json.loads(path.read_text(encoding="utf-8"))
    cases = d["cases"]
    bad = []
    for c in cases:
        got = classify(PL, c["msg"])
        if got != c["expect"]:
            bad.append((c["msg"], c["expect"], got, c.get("note", "")))
    n = len(cases)
    acc = 100.0 * (n - len(bad)) / max(n, 1)
    print(f"\n[{label}] {path.name}  {n} 条 → {n - len(bad)}/{n} = {acc:.1f}%")
    for m, e, g, note in bad:
        print(f"     FAIL {m[:44]:<46} 期望 {e:<7} 实际 {g:<7} | {note}")
    return n, len(bad), bad


def main() -> int:
    print("=" * 92)
    print("「要不要成链」判定契约 —— 调参集 + 留出集 双查 (纯判据, 零副作用)")
    print("=" * 92)
    from main import _load_config
    from core.model_client import ModelClient
    from core.pipeline import Level4Pipeline

    PL = Level4Pipeline(ModelClient(_load_config()), _load_config())

    n1, b1, _ = run_corpus(PL, HERE / "chain_decision_corpus.json", "调参集")
    chk(f"★ 调参集零错判 ({n1 - b1}/{n1})", b1 == 0, f"{b1} 条错")

    n2, b2, bad2 = run_corpus(PL, HERE / "chain_decision_holdout.json", "留出集")
    chk(f"★ 留出集零错判 ({n2 - b2}/{n2}) —— 过拟合检测", b2 == 0, f"{b2} 条错")

    # ── 三条最容易被改坏的行为, 单独钉死 (回归护栏) ──
    print("\n[回归护栏] 三条历史敏感行为")
    hard = [
        # 路由基线里的那条: 纯生成型多步必须仍走技能 (不能被我这条改动打坏)
        ("先写公司背景，再写推广方案，最后给出预算", "skill",
         "纯生成型多步 = 技能自己的 DAG; 曾因词表裸'算'命中'预算'被打成规划 (实测回归 48/49)"),
        # 废话多步词不能判成链
        ("然后再把这句发给涛哥", "single", "废话/语境依赖; 旧判据数连接词会误判"),
        # 单连接词无落盘不能判链
        ("先看看这个文件", "single", "1 个连接词 + 无落盘目标"),
    ]
    for msg, want, why in hard:
        got = classify(PL, msg)
        chk(f"★ {msg[:26]:<28} → {want}", got == want, f"实际 {got} ({why})")

    # ── 判据的边界: 抢占门只对"工具调查型"生效 ──
    print("\n[边界] 抢占门的作用面")
    chk("★ 两查询动作 → 抢占 (技能让位)", PL._planner_preempts("先看看端口，再查进程") is True)
    chk("★ 纯生成型多步 → 不抢占 (技能自己做)", PL._planner_preempts("先写背景，再写方案") is False)
    chk("★ 否定/犹豫 → 不抢占", PL._planner_preempts("先不急着写，先看看再说") is False)
    chk("单步查询 → 不抢占", PL._planner_preempts("看看这个文件") is False)

    print("\n" + "=" * 92)
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    for x in F:
        print("  -", x)
    print("=" * 92)
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
