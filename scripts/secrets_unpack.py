#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭据恢复 —— 用口令解开 secrets.enc, 把凭据还原到项目里。

这是"新机器三步跑起来"的第三步 (见 docs/恢复.md):
    ① git clone           ② python install.py           ③ python scripts/secrets_unpack.py

用法
───────────────────────────────────────────────────────────────
  python scripts/secrets_unpack.py                       # 交互输入口令
  TMM_SECRETS_PASSPHRASE=xxx python scripts/secrets_unpack.py
  python scripts/secrets_unpack.py --dry-run             # 只看包里有什么, 不写盘
  python scripts/secrets_unpack.py --force               # 已存在也覆盖 (默认会问)

安全约定
───────────────────────────────────────────────────────────────
· 默认**不覆盖**已存在的文件 —— 防止把新配的 Key 用旧密文盖掉
· --dry-run 只列出内容清单, 不落盘
· 口令不写盘、不上网; 解出来的明文文件会被加进 .gitignore (本来就已忽略)
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


def derive_key(passphrase: str, salt: bytes) -> bytes:
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
        return getpass.getpass("凭据口令: ")
    except Exception:
        return input("凭据口令 (明文显示): ").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="从 secrets.enc 还原凭据")
    ap.add_argument("--in", dest="src", default=str(ROOT / "secrets.enc"),
                    help="密文文件 (默认 <项目>/secrets.enc)")
    ap.add_argument("--root", default=str(ROOT), help="还原到哪个项目根")
    ap.add_argument("--dry-run", action="store_true", help="只列内容, 不写盘")
    ap.add_argument("--force", action="store_true", help="已存在也覆盖 (默认跳过)")
    ap.add_argument("--passphrase-file", default=None, help="从文件读口令")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_file():
        print(f"★ 找不到密文: {src}")
        print("  (分发包里应带 secrets.enc; 若没有, 说明打包时被排除表挡了)")
        return 2
    raw = src.read_bytes()
    if not raw.startswith(MAGIC):
        print("★ 文件头不对 —— 这不是本工具的密文 (或已损坏)")
        return 2
    salt = raw[len(MAGIC):len(MAGIC) + SALT_LEN]
    blob = raw[len(MAGIC) + SALT_LEN:]

    from cryptography.fernet import Fernet
    try:
        payload = json.loads(Fernet(derive_key(get_passphrase(args), salt)).decrypt(blob).decode("utf-8"))
    except Exception as e:
        print(f"★ 解密失败: {type(e).__name__}")
        print("  最常见原因: 口令不对 (Fernet 无法区分'口令错'和'文件损坏', 都报这个)")
        return 3

    root = Path(args.root)
    print(f"包里 {len(payload)} 个文件:")
    wrote = skipped = 0
    for rel, b64 in sorted(payload.items()):
        dest = root / rel
        exists = dest.exists()
        state = "已存在" if exists else "新建"
        if args.dry_run:
            print(f"    {rel:<40} {len(base64.b64decode(b64)):>7}B  [{state}] (dry-run)")
            continue
        if exists and not args.force:
            print(f"    {rel:<40} 跳过 (已存在; 要覆盖加 --force)")
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(b64))
        try:
            os.chmod(dest, 0o600)      # 与 config/crypto.py 的权限约定一致
        except Exception:
            pass
        print(f"    {rel:<40} 已还原 ({len(base64.b64decode(b64))}B)")
        wrote += 1

    if not args.dry_run:
        print(f"\n✓ 还原 {wrote} 个, 跳过 {skipped} 个")
        print("  下一步: python main.py   (自检会告诉你哪家模型已配好)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
