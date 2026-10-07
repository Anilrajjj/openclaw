"""Centralized runtime settings for the PromptIQ backend.

The app should not know where Node.js, OpenClaw, or instance storage live.
Those are deployment details and belong in environment variables.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


load_dotenv()


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INSTANCES_ROOT = ROOT_DIR / "instances"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _resolve_command(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute() or any(sep in value for sep in ("/", "\\")):
        return str(path)
    return shutil.which(value) or value


def _is_available(value: str) -> bool:
    if not value:
        return False
    path = Path(value)
    if path.is_absolute() or any(sep in value for sep in ("/", "\\")):
        return path.exists()
    return shutil.which(value) is not None


@dataclass(frozen=True)
class Settings:
    app_env: str
    node_binary: str
    openclaw_entrypoint: str
    instance_base_port: int
    instances_root: Path
    promptclaw_home_root: Path
    cors_allowed_origins: list[str]
    jwt_secret: str
    jwt_algorithm: str
    jwt_expire_minutes: int
    admin_seed_enabled: bool
    admin_email: str
    admin_password: str
    admin_name: str
    admin_tenant_id: str
    admin_tenant_name: str
    legacy_admin_email: str
    legacy_admin_tenant_id: str

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    def gateway_command(self, port: int) -> list[str]:
        entrypoint = self.openclaw_entrypoint
        if not entrypoint:
            raise RuntimeError("OPENCLAW_ENTRYPOINT is not configured.")

        base_args = ["gateway", "--port", str(port), "--allow-unconfigured"]
        if entrypoint.endswith(".js"):
            if not self.node_binary:
                raise RuntimeError("NODE_BINARY is required when OPENCLAW_ENTRYPOINT points to a .js file.")
            return [self.node_binary, entrypoint, *base_args]
        return [entrypoint, *base_args]

    def runtime_status(self) -> dict[str, Any]:
        instances_root_writable = False
        try:
            self.instances_root.mkdir(parents=True, exist_ok=True)
            probe = self.instances_root / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            instances_root_writable = True
        except OSError:
            instances_root_writable = False

        node_available = True
        if self.openclaw_entrypoint.endswith(".js"):
            node_available = _is_available(self.node_binary)

        return {
            "app_env": self.app_env,
            "node_available": node_available,
            "openclaw_available": _is_available(self.openclaw_entrypoint),
            "instances_root": str(self.instances_root),
            "instances_root_writable": instances_root_writable,
            "instance_base_port": self.instance_base_port,
        }

    def validate_runtime(self, require_openclaw: bool | None = None) -> None:
        require_openclaw = self.is_production if require_openclaw is None else require_openclaw
        status = self.runtime_status()
        errors: list[str] = []

        if self.instance_base_port < 1024 or self.instance_base_port > 65535:
            errors.append("INSTANCE_BASE_PORT must be between 1024 and 65535.")
        if self.jwt_expire_minutes <= 0:
            errors.append("JWT_EXPIRE_MINUTES must be greater than zero.")
        if (self.is_production or require_openclaw) and not status["instances_root_writable"]:
            errors.append(f"INSTANCES_ROOT is not writable: {self.instances_root}")
        if require_openclaw and self.openclaw_entrypoint.endswith(".js") and not status["node_available"]:
            errors.append(f"NODE_BINARY is not available: {self.node_binary}")
        if require_openclaw and not status["openclaw_available"]:
            errors.append(f"OPENCLAW_ENTRYPOINT is not available: {self.openclaw_entrypoint}")
        if self.is_production:
            if self.jwt_secret == "fallback-dev-secret-key-change-me" or len(self.jwt_secret) < 32:
                errors.append("JWT_SECRET must be set to a strong secret in production.")
            if self.admin_seed_enabled and (
                not self.admin_password or self.admin_password == "sysadmin"
            ):
                errors.append("ADMIN_PASSWORD must be set to a non-default value in production.")

        if errors:
            raise RuntimeError("Runtime configuration invalid: " + " ".join(errors))


settings = Settings(
    app_env=_env("APP_ENV", "development"),
    node_binary=_resolve_command(_env("NODE_BINARY", "node")),
    openclaw_entrypoint=_resolve_command(_env("OPENCLAW_ENTRYPOINT", "openclaw")),
    instance_base_port=_env_int("INSTANCE_BASE_PORT", 18789),
    instances_root=Path(_env("INSTANCES_ROOT", str(DEFAULT_INSTANCES_ROOT))).resolve(),
    promptclaw_home_root=Path(
        _env("PROMPTCLAW_HOME_ROOT", str(Path.home() / ".promptclaw_instances"))
    ).resolve(),
    cors_allowed_origins=_csv_env("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"),
    jwt_secret=_env("JWT_SECRET", "fallback-dev-secret-key-change-me"),
    jwt_algorithm=_env("JWT_ALGORITHM", "HS256"),
    jwt_expire_minutes=_env_int("JWT_EXPIRE_MINUTES", 10080),
    admin_seed_enabled=_env_bool("ADMIN_SEED_ENABLED", True),
    admin_email=_env("ADMIN_EMAIL", "admin"),
    admin_password=_env("ADMIN_PASSWORD", "sysadmin"),
    admin_name=_env("ADMIN_NAME", "Admin"),
    admin_tenant_id=_env("ADMIN_TENANT_ID", "promptclaw-admin-tenant"),
    admin_tenant_name=_env("ADMIN_TENANT_NAME", "PromptClaw Manager"),
    legacy_admin_email=_env("LEGACY_ADMIN_EMAIL", ""),
    legacy_admin_tenant_id=_env("LEGACY_ADMIN_TENANT_ID", ""),
)
