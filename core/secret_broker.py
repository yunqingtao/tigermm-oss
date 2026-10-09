# -*- coding: utf-8 -*-
"""凭据代管 (credential broker) —— 抄 Meta Muse Sentinel 里的"凭据代管"这一层。

为什么要有这个东西 (要解决的真问题)
═══════════════════════════════════════════════════════════════════
现状: tools/*.py 各自直接读 keys.json 拿真凭据 —— send_email/check_mail 读
`mail.password`, model_client 读 `deepseek.key`, sms_send 读 `sms.secret_key`。
后果两条, 都是真缺陷:
  ① 真凭据会顺着**工具返回值 / 异常回显 / 日志**流回上下文。工具里任何一句
     `return {"output": f"...{password}..."}` 或一次 requests 异常的 URL 回显,
     就把明文送进模型上下文。模型再写进回复/写进文件 = 明文外泄。
     (Muse 内测那次"识别生日玩具顺手拖 iCloud 私照"是同一类事故。)
  ② 没有"谁在什么时候用了哪把钥匙"的账 —— 轮换时不知道该动谁, 泄了也无从定位。

Muse 的解法: 真凭据只在**网络边界**才被换进去, 运行时代码只看到一次性替代令牌。
本模块做同一件事的本机版, 三件事:
  · 入向 inject: 参数里的 `{{secret:NAME}}` 占位符 → 在工具真正执行**前一刻**
    由网关替换成真值。模型/技能/工作流只写占位符, 从不写明文。
  · 出向 redact: 工具返回的任何文本/结构里若含真值 → 换回占位符再交给
    模型 / 日志 / UI。这一步是真正堵住"凭据顺流回上下文"的那一刀。
  · 记账 audit: 只记**名字**与方向 (inject/redact/read), 永不留值。

铁律 (改这个文件前先读)
───────────────────────────────────────────────────────────────
1. **真值绝不进日志、进模型上下文、进用户可见输出。** 记名字可以, 记值不行。
2. 未知占位符 **fail closed**: 报错, 不许把 `{{secret:x}}` 原样丢给工具
   (否则工具拿到字面量字符串, 要么静默失败要么把占位符发出去)。
3. 出向替换有**最小长度门槛** (MIN_REDACT_LEN)。短值 (如模型名/端口) 不参与,
   否则会把正常文本打碎成乱码。密钥一律 ≥ 20 字符, 门槛挡得住。
4. 分类靠**键名** (`_is_secret_name`), 不靠长度猜 —— keys.json 里
   `deepseek.model="deepseek-chat"`、`deepseek.url` 是配置不是凭据, 不许被替换掉。
5. 加密仓 (data/secrets.enc) 是**可选的**: 没初始化 crypto 时跳过, 不影响
   keys.json 这条路 (加性, 旧端兼容)。

对外接口
───────────────────────────────────────────────────────────────
    get_broker()                  -> SecretBroker (单例, 自动加载)
    broker.names()                -> ["deepseek.key", "mail.password", ...]
    broker.has(name) / resolve(name)
    broker.inject_args(kwargs)    -> kwargs (占位符已换真值)
    broker.redact_obj(result)     -> 同结构, 真值已换占位符
    broker.audit_tail(n)
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("core.secret_broker")

ROOT = Path(__file__).resolve().parent.parent

#: 出向替换的最小长度门槛 (见铁律 3)
MIN_REDACT_LEN = 12

#: 占位符语法: {{secret:mail.password}} / {{secret:deepseek.key}}
PLACEHOLDER_RE = re.compile(r"\{\{\s*secret\s*:\s*([A-Za-z0-9_.\-]+)\s*\}\}")

#: 键名分类 (见铁律 4)
_SECRET_SUFFIXES = (".key", ".password", ".passwd", ".secret", ".secret_key",
                    ".secret_id", ".token", ".apikey", ".api_key")
_SECRET_WORDS = ("password", "passwd", "secret", "token", "apikey", "api_key")

#: 兜底形态识别 (真值不在册时的最后一道): sk-xxx / 32+ 的十六进制串
_SHAPE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]


def _is_secret_name(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    if n.endswith(_SECRET_SUFFIXES):
        return True
    return any(w in n for w in _SECRET_WORDS)


def _flatten(d, prefix=""):
    """{"mail": {"password": "x"}} -> {"mail.password": "x"} (只收标量, 跳过 _说明)"""
    out = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if str(k).startswith("_"):
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        elif isinstance(v, (str, int, float)) and str(v).strip():
            out[key] = str(v)
    return out


class SecretBroker:
    """凭据代管。线程内共享单例即可, 无状态写入, 只在内存里留真值。"""

    def __init__(self, keys_path: Path = None, enc_path: Path = None):
        self.keys_path = Path(keys_path) if keys_path else ROOT / "keys.json"
        self.enc_path = Path(enc_path) if enc_path else ROOT / "data" / "secrets.enc"
        self._secrets = {}          # name -> 真值 (只在内存)
        self._names_loaded = []
        self._audit = []            # 内存尾巴, 落盘另有一份
        self._roots = [ROOT]        # 相对路径解析基
        self.load()

    # ── 加载 ──────────────────────────────────────────────
    def load(self):
        self._secrets = {}
        # ① keys.json (明文, 沿用现状, 不迁移、不破坏老路径)
        try:
            if self.keys_path.exists():
                raw = json.loads(self.keys_path.read_text(encoding="utf-8"))
                for name, val in _flatten(raw).items():
                    if _is_secret_name(name):
                        self._secrets[name] = val
        except Exception as e:
            logger.warning("secret_broker: keys.json 读取失败 (%s) —— 占位符将 fail closed", e)
        # ② data/secrets.enc (可选加密仓; 没有 crypto 就跳过)
        try:
            if self.enc_path.exists():
                from config.crypto import secure_load
                for name, val in _flatten(secure_load(self.enc_path)).items():
                    self._secrets[name] = str(val)
        except Exception as e:
            logger.warning("secret_broker: secrets.enc 跳过 (%s)", e)
        # ③ 环境变量形态 (DEEPSEEK_API_KEY 之类) —— 只登记, 供占位符引用
        #   ★ 2026-09-26 收窄: 只按名字匹配会把 `TMM_SECRET_AUDIT`(一个**路径**)、
        #   `..._DIR/_FILE/_LOG` 这类也当凭据登记 —— 名字的 secret 字样对上了, 值不是凭据。
        #   后果虽轻 (拿路径去替换文本 → 打碎日志), 但"在册项"必须是真凭据, 否则这张表
        #   本身不可信。故加两道: 值不含路径分隔符、且长度够 (真 key 都够长)。
        try:
            import os
            for env_name, val in os.environ.items():
                if not _is_secret_name(env_name) or not val:
                    continue
                if env_name.upper().endswith(("_AUDIT", "_DIR", "_PATH", "_FILE", "_LOG", "_MODE")):
                    continue
                if any(sep in str(val) for sep in ("/", "\\", ":")):
                    continue
                if len(str(val)) < MIN_REDACT_LEN:
                    continue
                self._secrets.setdefault(env_name, str(val))
        except Exception:
            pass
        self._names_loaded = sorted(self._secrets)
        return self._names_loaded

    def names(self):
        return list(self._names_loaded)

    def has(self, name: str) -> bool:
        return name in self._secrets

    def resolve(self, name: str):
        """取真值。**只允许代管层和工具执行前的注入调用**, 不许把返回值写进日志。"""
        return self._secrets.get(name)

    # ── 入向: 占位符 → 真值 ────────────────────────────────
    def inject_text(self, text: str):
        """(新文本, 用到的名字, 缺失的名字)。缺失即 fail closed, 不原样透传。"""
        if not isinstance(text, str) or "{{" not in text:
            return text, [], []

        used, missing = [], []

        def _sub(m):
            name = m.group(1)
            val = self._secrets.get(name)
            if val is None:
                missing.append(name)
                return m.group(0)
            used.append(name)
            return val

        return PLACEHOLDER_RE.sub(_sub, text), used, missing

    def inject_args(self, kwargs: dict):
        """把 kwargs 里所有字符串 (含 list/dict 内的) 的占位符换成真值。

        返回 __ (kwargs, used, missing)__ 见 `.inject_args_ex`; 本方法是兼容壳。
        """
        new, used, missing = self.inject_args_ex(kwargs)
        return new

    def inject_args_ex(self, kwargs: dict):
        used, missing = [], []

        def walk(v):
            if isinstance(v, str):
                t, u, m = self.inject_text(v)
                used.extend(u)
                missing.extend(m)
                return t
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            return v

        out = {k: walk(v) for k, v in (kwargs or {}).items()}
        return out, sorted(set(used)), sorted(set(missing))

    def used_in(self, text: str):
        """scan 用: 文本里出现了哪些占位符名。"""
        if not isinstance(text, str):
            return []
        return sorted(set(PLACEHOLDER_RE.findall(text)))

    # ── 出向: 真值 → 占位符 ────────────────────────────────
    def redact_text(self, text: str):
        """(新文本, 抹掉的秘密名)。已知真值 + 兜底形态两遍扫。"""
        if not isinstance(text, str) or not text:
            return text, []
        hit = []
        # ① 已知真值 (长的先替换, 防短值切碎长值)
        for name in sorted(self._secrets, key=lambda n: -len(self._secrets[n])):
            val = self._secrets[name]
            if len(val) < MIN_REDACT_LEN:
                continue
            if val in text:
                text = text.replace(val, "{{secret:%s}}" % name)
                hit.append(name)
        # ② 形态兜底 (真值不在册: 新写的 key 还没登记)
        for pat in _SHAPE_PATTERNS:
            for m0 in pat.findall(text):
                if len(m0) < MIN_REDACT_LEN:
                    continue
                text = text.replace(m0, "{{secret:unregistered}}")
                hit.append("unregistered")
        return text, sorted(set(hit))

    def redact_obj(self, obj, _depth=0):
        """递归抹真值。列表/dict/字符串都过; 深度护栏防环。"""
        if _depth > 8:
            return obj
        if isinstance(obj, str):
            return self.redact_text(obj)[0]
        if isinstance(obj, list):
            return [self.redact_obj(x, _depth + 1) for x in obj]
        if isinstance(obj, dict):
            return {k: self.redact_obj(v, _depth + 1) for k, v in obj.items()}
        return obj

    # ── 记账 ──────────────────────────────────────────────
    def _audit_path(self):
        try:
            import os
            p = os.environ.get("TMM_SECRET_AUDIT")
            if p:
                return Path(p)
        except Exception:
            pass
        return ROOT / "data" / "secret_audit.jsonl"

    def audit(self, direction: str, tool: str, names, note: str = ""):
        """direction: inject(入向换真值) / redact(出向抹真值) / missing(未知占位符)"""
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "direction": direction,
            "tool": tool,
            "names": list(names or []),
            "note": note[:120],
        }
        self._audit.append(entry)
        if len(self._audit) > 200:
            self._audit = self._audit[-200:]
        try:
            p = self._audit_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("secret_broker: 审计落盘失败 %s", e)
        return entry

    def audit_tail(self, n: int = 20):
        return self._audit[-n:]

    def stats(self):
        return {
            "registered": len(self._names_loaded),
            "names": self._names_loaded,
            "audit_entries": len(self._audit),
        }


_BROKER = None


def get_broker() -> SecretBroker:
    """单例。加载失败也不抛 —— 返回空册 (占位符一律 fail closed)。"""
    global _BROKER
    if _BROKER is None:
        try:
            _BROKER = SecretBroker()
        except Exception as e:
            logger.warning("secret_broker: 初始化失败 %s —— 用空册兜底", e)
            b = SecretBroker.__new__(SecretBroker)
            b.keys_path = ROOT / "keys.json"
            b.enc_path = ROOT / "data" / "secrets.enc"
            b._secrets, b._names_loaded, b._audit, b._roots = {}, [], [], [ROOT]
            _BROKER = b
    return _BROKER


def reload_broker():
    """轮换凭据后热重载 (工具/命令用)。"""
    global _BROKER
    _BROKER = None
    return get_broker()
