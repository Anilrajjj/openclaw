"""
Fernet symmetric encryption for API keys and bot tokens.
FERNET_KEY is auto-generated on first run and persisted to .env.
"""
from __future__ import annotations
import os
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv, set_key

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _bootstrap_key() -> str:
    load_dotenv(dotenv_path=_ENV_PATH)
    key = os.getenv("FERNET_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        _ENV_PATH.touch(exist_ok=True)
        set_key(str(_ENV_PATH), "FERNET_KEY", key)
        print(f"[encryption] Generated new FERNET_KEY -> {_ENV_PATH}")
    return key


_fernet = Fernet(_bootstrap_key().encode())


def encrypt(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()


def mask(plain: str) -> str:
    if not plain:
        return "••••••••"
    suffix = plain[-4:] if len(plain) >= 4 else plain
    return f"••••••••{suffix}"
