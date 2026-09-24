"""Auth system setup — create initial admin user."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from config.settings import DATA_DIR
from core.auth import init_auth, get_auth

auth = init_auth(DATA_DIR / "auth.db")

existing = auth.db.get_user("admin")
if existing:
    print(f"Admin exists (token: {existing.token[:15]}...)")
    sys.exit(0)

import secrets
password = secrets.token_hex(8)
u = auth.db.create_user("admin", password, "admin")
print(f"Admin created:")
print(f"  Username: admin")
print(f"  Password: {password}")
print(f"  Token:    {u.token}")
print("Save these credentials.")
