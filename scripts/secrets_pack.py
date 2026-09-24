#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭据打包 —— 把所有敏感文件加密成**一个** secrets.enc, 随仓库走。

为什么这么做 (用户 2026-09-20 拍板 "凭据加密入库")
═══════════════════════════════════════════════════════════════════
私有仓库里也**不该放明文凭据**:
  · GitHub 对私有仓库**同样跑 secret scanning**, 云凭据会被告警/可能被服务商吊销
  · 凭据一旦推上去, 就永远留在 git 历史里 (删了也在) —— 将来转公开/加协作者就是裸奔
  · 但"恢复时找不到凭据"是真痛点 (换机器/重装)

所以: **明文不进仓库, 加密后进仓库**。恢复时只要记得口令, 一键还原全部凭据。

设计 (与项目既有机制对齐)
───────────────────────────────────────────────────────────────
· 项目本来就有 `config/crypto.py` (Fernet) —— 本脚本**复用同一个算法**, 不另造一套
· 密钥来源: **口令派生** (scrypt), 而不是随机 key 文件
  理由: 随机 key 文件一旦丢了, 仓库里的密文就永远解不开 (等于没备份)。
        口令派生的好处 = 只要记得口令, 任何机器都能恢复。这是"能救回来"的关键。
· 产物 secrets.enc 结构:  b"TMMSEC1\\n" + <16B salt> + <Fernet token>
· 打包哪些文件: 见 TARGETS + TARGET_GLOBS (密钥/凭据类, 不是全部数据)

用法
───────────────────────────────────────────────────────────────
  python scripts/secrets_pack.py                 # 交互输入口令 (推荐)
  TMM_SECRETS_PASSPHRASE=xxx python scripts/secrets_pack.py
  python scripts/secrets_pack.py --passphrase-file D:\\pw.txt --out secrets.enc

★ 口令**绝不写进任何文件、绝不上传**。丢了口令 = 密文作废 (只能重新填一遍 Key)。
"""
import argparse
import base64
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAGIC = b"TMMSEC1\n"
SALT_LEN = 16

# 要打包的敏感文件 (相对项目根)。存在才打包。
TARGETS = [
    ".crypto.key",                      # Fernet 主密钥 (users.enc / prefs.enc 等靠它)
    "keys.json",                        # 各模型 API Key
    "keys_github.json",                 # GitHub token
    "relay_tokens.json",                # relay 凭据
    "data/users.enc",                   # 加密用户表
]
# 机器名变体 (relay_tokens(LAPTOP-xxx).json / keys(LAPTOP-xxx).json) —— 用**通配**匹配。
# ★ 2026-09-21: 原来把作者的主机名写死成了两个文件名 —— 公开仓库不该带这个。
TARGET_GLOBS = ["relay_tokens(*).json", "keys(*).json"]


def _expand_targets(root: Path) -> list:
    """静态名单 + 通配变体 (去重, 只保留真实存在的文件)。"""
    out = [t for t in TARGETS if (root / t).is_file()]
    for pat in TARGET_GLOBS:
        out += [q.name for q in sorted(root.glob(pat)) if q.is_file() and q.name not in out]
    return out


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """口令 → Fernet key (scrypt)。与 config/crypto.py 同为 Fernet, 只是密钥来源不同。"""
    import hashlib
    raw = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt,
                         n=2 ** 14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(raw)


def get_passphrase(args) -> str:
    if getattr(args, "passphrase_file", None):
        return Path(args.passphrase_file).read_text(encoding="utf-8").strip()
    env = os.environ.get("TMM_SECRETS_PASSPHRASE")
    if env:
        return env.strip()
    try:
        p1 = getpass.getpass("设置凭据口令 (不会显示, 不会写盘): ")
        p2 = getpass.getpass("再输一次确认: ")
    except Exception:
        p1 = input("设置凭据口令 (明文显示): ").strip()
        p2 = p1
    if not p1:
        print("★ 口令为空 —— 中止 (不支持空口令)")
        sys.exit(2)
    if p1 != p2:
        print("★ 两次不一致 —— 中止")
        sys.exit(2)
    return p1


def main() -> int:
    ap = argparse.ArgumentParser(description="把敏感文件加密打包成 secrets.enc")
    ap.add_argument("--out", default=str(ROOT / "secrets.enc"), help="输出文件 (默认 <项目>/secrets.enc)")
    ap.add_argument("--passphrase-file", default=None, help="从文件读口令 (用引号包住路径)")
    ap.add_argument("--root", default=str(ROOT), help="项目根 (默认脚本上一级)")
    args = ap.parse_args()
    root = Path(args.root)

    from cryptography.fernet import Fernet
    import secrets as _s

    payload = {}
    missing = []
    for rel in _expand_targets(root):
        p = root / rel
        if p.is_file():
            payload[rel] = base64.b64encode(p.read_bytes()).decode("ascii")
        else:
            missing.append(rel)
    if not payload:
        print("★ 没找到任何要打包的凭据文件 —— 检查 --root")
        return 2

    salt = _s.token_bytes(SALT_LEN)
    cipher = Fernet(derive_key(get_passphrase(args), salt))
    blob = cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(MAGIC + salt + blob)

    print(f"✓ 已打包 {len(payload)} 个文件 → {out}  ({out.stat().st_size} 字节)")
    for k in payload:
        print(f"    + {k}")
    if missing:
        print("  (不存在, 跳过):")
        for k in missing:
            print(f"    - {k}")
    print("\n★ 口令请存到密码管理器 —— 丢了它, 这个密文就解不开了 (无法找回)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
