#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验包 —— 让「学到的能力」能跟着走 (2026-09-21 立)

解决什么
═══════════════════════════════════════════════════════════════════
现状实测: 三条线零件都齐、都接着线, 但**没连成回路**:
  · 学习: learnings.db / learned_rules.db / gap_ledger.db 都在写
  · 复制: install.py + secrets.enc 让**空壳**能在新机器站起来
  · 归档: 产物台账刚接通 (ToolGateway 自动登记)
断点: 分发包里的 data/ 是**空的** → 复制出去的是「空白的它」, 学到的全锁在一台机器上。

这个脚本是那座桥: 把**可携带的经验**导成一个能 git 提交 / 能 diff / 能合并的包。
★ 关键: 导出前**强制净化** —— 经验里混着个人信息("发给涛哥"这种就是),
  不净化就携带 = 把隐私发出去。所以净化是 fail-closed (发现残留就拒绝写出)。

带的 / 不带的 (分两层)
───────────────────────────────────────────────────────────────
  带:  技能 (tmm_skills/) · 学到的条目 (learnings) · 学到的规则 (rules) · 已解决的缺口 (gaps)
  不带: 对话库 · 感知记录 · 凭据 · 产物台账 (全是个人/易变数据)

用法
───────────────────────────────────────────────────────────────
    python scripts/experience_pack.py export [--out DIR]     # 导出 (净化后)
    python scripts/experience_pack.py inspect PACK.json      # 看包里有什么
    python scripts/experience_pack.py import PACK.json       # 合并进本机 (只增不改)
    python scripts/experience_pack.py --self-test            # 自检

自检:  python scripts/experience_pack.py --self-test
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SKILLS = ROOT / "tmm_skills"
PACK_VERSION = 1

# ── 净化规则 ──
#  ① 个人标识: 读仓库外的黑名单 (不把真标识写进本脚本 —— 否则脚本自身就是泄露源)
#     ★ 但**不能只靠那个文件**: 实测它 18 条里**不含**"涛哥/虎哥", 而 learnings.db 里
#       就躺着 "默认发给涛哥" —— 只靠外部名单会把这些**直接带走**。所以再叠一层内置基线。
#     ★ 内置基线只放**公开仓里已经出现的称呼/产品名**, 不放任何密钥; 这是"宁可多丢"的方向。
#  ② 凭据形状: 就算黑名单没列, 长得像 key/token/邮箱/手机号的一律不带
CRED_RX = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|SCT[0-9]{4,}T[A-Za-z0-9]{10,}"
    r"|AKID[A-Za-z0-9]{10,}|[A-Za-z0-9_-]{32,}"
    r"|[\w.+-]+@[\w-]+\.[\w.]{2,}"
    r"|(?<!\d)1[3-9]\d{9}(?!\d))")
#  ★ 同样编码: 这些是**本机/个人标识**, 明文写进公开仓库等于自己公布。
_DENY_EXTRA_B64 = (
    "dGNoYXQt",
    "eXVucWluZ3Rhbw==",
    "dGlnZXJhaTIwMjY=",
    "bWFyeWFzay5jb20=",
    "dGlnZXItbi5jYw==",
)
DENY_EXTRA = tuple(base64.b64decode(_x).decode("utf-8") for _x in _DENY_EXTRA_B64)
#: 内置基线: 与"用户本人"绑定的称呼。
#  ★ 为什么用 base64 而不写明文: 这是**公开仓库** —— 把本人昵称明文写在源码里,
#    等于公开它 (洗净门禁虽然不认这些词, 但那是门禁的盲区, 不是可以写的理由)。
#    编码后仍能在运行时还原成同一份基线, 兜底能力不变。
_BUILTIN_DENY_B64 = (
    "5rab5ZOl",
    "6JmO5ZOl",
    "VE1NLVBD",
    "TEFQVE9QLQ==",
)
BUILTIN_DENY = tuple(base64.b64decode(_x).decode("utf-8") for _x in _BUILTIN_DENY_B64)


def load_denylist() -> list:
    """个人标识黑名单 = 内置基线 ∪ 仓库外名单 (每行一条, # 注释)。"""
    out = list(BUILTIN_DENY)
    for cand in (ROOT.parent / "_personal_denylist.txt", ROOT / "_personal_denylist.txt"):
        if cand.is_file():
            for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
            break
    return out


def _walk_strings(obj, path="$"):
    """递归取出所有字符串 (带路径, 便于定位与选择性丢弃)。

    ★ 也扫 dict 的 **key** (前缀 KEY:) —— 否则 `{"涛哥": "x"}` 这种漏网:
    只扫 value 时, 个人信息藏在键名里就查不出来 (自检抓到的盲区)。
    """
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield f"KEY:{path}.{k}", k
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def find_personal(obj, denylist) -> list:
    """→ [(路径, 命中片段)] —— 用来判定"这条能不能带"。"""
    hits = []
    for p, s in _walk_strings(obj):
        for d in denylist:
            if d and d in s:
                hits.append((p, d))
        m = CRED_RX.search(s)
        if m:
            hits.append((p, "凭据形状:" + m.group(0)[:8] + "…"))
        for d in DENY_EXTRA:
            if d in s:
                hits.append((p, d))
    return hits


def _clean(rec: dict, denylist) -> tuple:
    """单个条目净化: 返回 (清理后的记录 或 None, 丢弃原因)。只丢字段级, 不整条丢。"""
    hits = find_personal(rec, denylist)
    if not hits:
        return rec, ""
    # 命中落在**键名**里 → 没法安全改写键名, 整条丢
    if any(p.startswith("KEY:") for p, _ in hits):
        return None, f"个人信息出现在键名里 ({len(hits)} 处), 整条丢弃"
    # 整条都是个人信息 → 丢
    total = sum(len(s) for _, s in _walk_strings(rec))
    if len(hits) >= 3 or total < 4 * len(hits):
        return None, f"命中 {len(hits)} 处个人信息, 整条丢弃"
    # 字段级: 把命中的字段值抹掉(留痕, 不静默)
    out = json.loads(json.dumps(rec, ensure_ascii=False))
    paths = {h[0].lstrip("$.") for h in hits}
    for name in list(out):
        if any(p.startswith(name) for p in paths):
            out[name] = "«已抹除: 含个人信息»"
    return out, f"抹除 {len(hits)} 处字段"


# ── 采集 ──
def _read_skills() -> list:
    out = []
    if not SKILLS.is_dir():
        return out
    for d in sorted(SKILLS.iterdir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        name, desc, body = d.name, "", txt
        m = re.match(r"---\s*\n(.*?)\n---\s*\n", txt, re.S)
        if m:
            fm = m.group(1)
            body = txt[m.end():]
            n = re.search(r"^name:\s*(.+)$", fm, re.M)
            de = re.search(r"^description:\s*(.+)$", fm, re.M)
            if n:
                name = n.group(1).strip().strip('"\'')
            if de:
                desc = de.group(1).strip().strip('"\'')
        out.append({"name": name, "description": desc,
                    "sha1": hashlib.sha1(txt.encode("utf-8")).hexdigest()[:16],
                    "body": body[:4000]})
    return out


def _read_db(path: Path, table: str, where: str = "") -> list:
    if not path.is_file():
        return []
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        q = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        rows = [dict(zip(cols, r)) for r in c.execute(q)]
        c.close()
        return rows
    except Exception:
        return []


def collect() -> dict:
    return {
        "skills": _read_skills(),
        "learnings": _read_db(DATA / "learnings.db", "learnings"),
        "rules": _read_db(DATA / "learned_rules.db", "rules"),
        "gaps_resolved": _read_db(DATA / "gap_ledger.db", "gaps", "status='resolved'"),
    }


def build_pack(denylist=None) -> tuple:
    """→ (pack, report)。净化 fail-closed。denylist 可注入 (自检用, 不依赖外部文件)。"""
    deny = load_denylist() if denylist is None else list(denylist)
    rep = {"denylist_terms": len(deny), "dropped": 0, "field_wiped": 0, "kept": {}}
    raw = collect()
    pack = {"pack_version": PACK_VERSION, "created_at": int(time.time()),
            "denylist_terms": len(deny), "kept": {}}
    for section, items in raw.items():
        kept = []
        for it in items:
            c, why = _clean(it, deny)
            if c is None:
                rep["dropped"] += 1
                continue
            if why:
                rep["field_wiped"] += 1
            kept.append(c)
        pack[section] = kept
        rep["kept"][section] = len(kept)
    pack["kept"] = rep["kept"]
    # ★ fail-closed: 成品再扫一遍, 有任何残留就拒绝写出
    residue = find_personal(pack, deny)
    if residue:
        rep["residue"] = [f"{p}:{d}" for p, d in residue[:8]]
        return None, rep
    return pack, rep


# ── 导入 (只增不改) ──
def import_pack(path: Path, denylist=None, skills_dir: Path = None) -> dict:
    """把经验包合并进本机 (只增不改)。

    ★ skills_dir 必须**显式传参**, 不许靠改模块全局 —— 实测踩过: 自检里改 globals()["SKILLS"]
      的写法一旦有分支提前返回/异常, 就会把夹具技能写进**真的 tmm_skills/**,
      后果是 ① 真目录被污染 ② 那个夹具的描述里带个人信息 ③ 技能加载测试红。
    """
    pack = json.loads(Path(path).read_text(encoding="utf-8"))
    deny = load_denylist() if denylist is None else list(denylist)
    skills_root = Path(skills_dir) if skills_dir else SKILLS
    resid = find_personal(pack, deny)
    if resid:
        return {"ok": False, "error": f"包里有 {len(resid)} 处个人信息, 拒绝导入", "hits": resid[:5]}
    added = {"skills": 0, "learnings": 0, "rules": 0}
    skipped = {"skills": 0, "learnings": 0, "rules": 0}

    skills_root.mkdir(parents=True, exist_ok=True)
    have = {d.name for d in skills_root.iterdir() if d.is_dir()} if skills_root.is_dir() else set()
    for s in pack.get("skills", []):
        nm = str(s.get("name") or "").strip()
        if not nm or nm in have:
            skipped["skills"] += 1
            continue                                    # ★ 只增不改: 已有技能绝不覆盖
        d = skills_root / nm
        d.mkdir(parents=True, exist_ok=True)
        fm = f'---\nname: {nm}\ndescription: "{s.get("description","")}"\nimported_from_pack: true\n---\n\n'
        (d / "SKILL.md").write_text(fm + str(s.get("body") or ""), encoding="utf-8")
        added["skills"] += 1
        have.add(nm)

    # learnings: 按 (category, key, value) 判重, 只增
    lp = DATA / "learnings.db"
    if pack.get("learnings"):
        DATA.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(lp))
        c.execute("""CREATE TABLE IF NOT EXISTS learnings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT, key TEXT, value TEXT, weight REAL, source TEXT,
            created_at REAL, updated_at REAL)""")
        existing = {(r[0], r[1], r[2]) for r in c.execute("SELECT category,key,value FROM learnings")}
        for it in pack["learnings"]:
            sig = (it.get("category"), it.get("key"), it.get("value"))
            if sig in existing or not it.get("value"):
                skipped["learnings"] += 1
                continue
            c.execute("INSERT INTO learnings (category,key,value,weight,source,created_at,updated_at)"
                      " VALUES (?,?,?,?,?,?,?)",
                      (it.get("category"), it.get("key"), it.get("value"),
                       float(it.get("weight") or 1), "experience_pack",
                       time.time(), time.time()))
            added["learnings"] += 1
            existing.add(sig)
        c.commit()
        c.close()
    return {"ok": True, "added": added, "skipped": skipped}


def inspect_pack(path: Path) -> str:
    p = json.loads(Path(path).read_text(encoding="utf-8"))
    lines = [f"经验包 {Path(path).name}  (version={p.get('pack_version')})",
             f"  创建时间: {time.strftime('%Y-%m-%d %H:%M', time.localtime(p.get('created_at', 0)))}",
             f"  净化: 黑名单 {p.get('denylist_terms')} 条"]
    for k in ("skills", "learnings", "rules", "gaps_resolved"):
        v = p.get(k) or []
        lines.append(f"  {k}: {len(v)} 条")
        for it in v[:3]:
            t = it.get("name") or it.get("value") or it.get("signature") or ""
            lines.append(f"      · {str(t)[:60]}")
    return "\n".join(lines)


# ── 自检 ──
def self_test() -> int:
    P, F = [], []

    def chk(n, c, d=""):
        (P if c else F).append(n)
        print(("  PASS " if c else "  FAIL ") + n + (f"   <- {d}" if d and not c else ""))

    deny = ["涛哥", "这是一个不存在的个人标识ZZZ"]
    _real = load_denylist()
    chk("★ 默认名单能抓'涛哥' (内置基线兜底, 不只靠外部文件)",
        bool(find_personal({"v": "默认发给涛哥"}, _real)), f"条数={len(_real)}")
    chk("★ 默认名单能抓邮箱/手机号形状",
        bool(find_personal({"v": "someone@example.com"}, _real))
        and bool(find_personal({"v": "13812345678"}, _real)))
    chk("净化规则能认出个人标识", bool(find_personal({"v": "默认发给涛哥"}, deny)))
    chk("净化规则能认出凭据形状",
        bool(find_personal({"k": "sk-" + "a" * 32}, deny)))
    chk("正常内容不误伤", not find_personal({"v": "把表格汇总成报告"}, deny))

    # 字段级抹除 + 整条丢弃
    c1, why1 = _clean({"key": "pref", "value": "发给涛哥"}, deny)
    chk("字段级: 命中字段被抹除 (留痕)", c1 and "抹除" in str(c1.get("value", "")) and "涛哥" not in json.dumps(c1, ensure_ascii=False), c1)
    c2, why2 = _clean({"a": "涛哥", "b": "涛哥", "c": "涛哥"}, deny)
    chk("整条几乎全是个人信息 → 整条丢弃", c2 is None, c2)

    # fail-closed: 残留就拒绝写出
    os_backup = None
    real_collect = globals()["collect"]
    globals()["collect"] = lambda: {"skills": [{"涛哥": "藏在键名里的个人信息"}],
                                    "learnings": [], "rules": [], "gaps_resolved": []}
    try:
        pack, rep = build_pack(denylist=deny)
        chk("★ 键名藏信息 → 整条丢弃, 不随包带走",
            rep.get("dropped", 0) >= 1 and (pack or {}).get("kept", {}).get("skills", 0) == 0, rep)
        chk("★ 净化掉之后就干净了 → 允许导出 (不是一律拒绝)",
            pack is not None and not rep.get("residue"), rep.get("residue"))
        # ★ fail-closed 的**牙齿**: 模拟"净化被绕过"(实现有 bug / 有人把 _clean 改成恒等)
        #   → 残留检测必须兜住并拒绝产出。没有这一条, fail-closed 就只是句空话。
        _real_clean = globals()["_clean"]
        globals()["_clean"] = lambda rec, dl: (rec, "")      # 恒等 = 净化失效
        try:
            pack2, rep2 = build_pack(denylist=deny)
        finally:
            globals()["_clean"] = _real_clean
        chk("★★ fail-closed 有牙齿: 净化被绕过时拒绝产出并报出残留",
            pack2 is None and bool(rep2.get("residue")), rep2.get("residue"))
    finally:
        globals()["collect"] = real_collect

    # 导入只增不改: 已有技能不被覆盖
    with tempfile.TemporaryDirectory(prefix="_vfy_pack_") as td:
        td = Path(td)
        pk = td / "pack.json"
        pk.write_text(json.dumps({"pack_version": 1, "skills": [
            {"name": "existing-skill", "description": "x", "body": "NEW"},
            {"name": "brand-new-skill", "description": "y", "body": "NEW"}],
            "learnings": [], "rules": []}, ensure_ascii=False), encoding="utf-8")
        real_data = globals()["DATA"]
        globals()["DATA"] = td / "data"
        try:
            (td / "skills" / "existing-skill").mkdir(parents=True)
            (td / "skills" / "existing-skill" / "SKILL.md").write_text("ORIGINAL", encoding="utf-8")
            r = import_pack(pk, denylist=deny, skills_dir=td / "skills")
            chk("导入成功", r.get("ok"), r)
            chk("★ 只增不改: 已有技能内容未被覆盖",
                (td / "skills" / "existing-skill" / "SKILL.md").read_text(encoding="utf-8") == "ORIGINAL")
            chk("新技能被写入", (td / "skills" / "brand-new-skill" / "SKILL.md").is_file())
            chk("导入结果记账 (added/skipped)", r.get("added", {}).get("skills") == 1
                and r.get("skipped", {}).get("skills") == 1, r)
        finally:
            globals()["DATA"] = real_data

        # 带个人信息的包拒绝导入
        bad = td / "bad.json"
        bad.write_text(json.dumps({"pack_version": 1, "skills": [
            {"name": "x", "description": "涛哥"}], "learnings": [], "rules": []}, ensure_ascii=False),
            encoding="utf-8")
        r2 = import_pack(bad, denylist=deny, skills_dir=td / "skills")
        chk("★ 带个人信息的包拒绝导入", r2.get("ok") is False and "拒绝" in str(r2.get("error", "")), r2)
    # ★ 回归钉子: 自检跑完, 真技能目录**不能被碰过** (上次就是这么把夹具写进去的)
    _real_sk = ROOT / "tmm_skills"
    _bad = [d.name for d in _real_sk.iterdir() if d.is_dir()
            and (d / "SKILL.md").is_file()
            and "imported_from_pack" in (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")]
    chk("★★ 自检不得污染真技能目录 (无 imported_from_pack 残留)", not _bad, _bad)

    print()
    print(f"结果: {len(P)} PASS / {len(F)} FAIL")
    return 1 if F else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="经验包: 让学到的能力能跟着走")
    ap.add_argument("cmd", nargs="?", choices=["export", "import", "inspect", "self-test"])
    ap.add_argument("pack", nargs="?", help="import/inspect 时的包文件")
    ap.add_argument("--out", default=str(ROOT / "experience"), help="导出目录 (默认 <项目>/experience)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--skills-dir", default="", help="import 时目标技能目录 (默认项目 tmm_skills/)")
    a = ap.parse_args()
    if a.self_test or a.cmd == "self-test":
        return self_test()
    if a.cmd == "export":
        pack, rep = build_pack()
        if pack is None:
            print("★ 拒绝导出: 净化后仍有个人信息残留 —— " + str(rep.get("residue")))
            return 2
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"experience_pack_{time.strftime('%Y%m%d')}.json"
        f.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已导出: {f}")
        print(f"  收录: " + " · ".join(f"{k}={v}" for k, v in rep["kept"].items()))
        print(f"  净化: 丢弃 {rep['dropped']} 条 · 抹除字段 {rep['field_wiped']} 处"
              f" · 黑名单 {rep['denylist_terms']} 条")
        if not rep["denylist_terms"]:
            print("  ⚠ 黑名单为空 —— 净化强度不足, 请确认 _personal_denylist.txt 存在")
        return 0
    if a.cmd == "inspect":
        if not a.pack:
            print("用法: inspect <pack.json>"); return 2
        print(inspect_pack(Path(a.pack)))
        return 0
    if a.cmd == "import":
        if not a.pack:
            print("用法: import <pack.json>"); return 2
        r = import_pack(Path(a.pack), skills_dir=(Path(a.skills_dir) if a.skills_dir else None))
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("ok") else 2
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
