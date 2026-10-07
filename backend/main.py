"""PromptIQ - PromptClaw Manager API entry point."""
import copy
import json
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from limiter import limiter

import models
from database import Base, SessionLocal, engine
from routers import auth as auth_router, instances, agents as agents_router
from settings import settings

logger = logging.getLogger(__name__)
INSTANCES_DIR = settings.instances_root

Base.metadata.create_all(bind=engine)

ADMIN_EMAIL = settings.admin_email
ADMIN_PASSWORD = settings.admin_password
ADMIN_NAME = settings.admin_name
ADMIN_TENANT = settings.admin_tenant_id
ADMIN_TENANT_NAME = settings.admin_tenant_name
LEGACY_ADMIN_EMAIL = settings.legacy_admin_email
LEGACY_ADMIN_TENANT = settings.legacy_admin_tenant_id
ADMIN_INSTANCE_IDS = [
    "1b64c9df-16f7-4e64-9505-44eeb7e8aefa",
    "3969977c-bc99-400d-b30f-7c3f371e9f36",
    "62301919-112e-4228-87b9-be083acc2127",
]
INSTANCE_NAMES_MAP = {
    "1b64c9df-16f7-4e64-9505-44eeb7e8aefa": "Legal",
    "3969977c-bc99-400d-b30f-7c3f371e9f36": "Chatbot",
    "62301919-112e-4228-87b9-be083acc2127": "Research",
}


def _next_available_port(db: Session, used_ports: set[int]) -> int:
    port = settings.instance_base_port
    while port in used_ports:
        port += 10
    used_ports.add(port)
    return port


def import_existing_instance_dirs(db: Session):
    """Import any on-disk instance folders that are missing DB records."""
    if not INSTANCES_DIR.exists():
        return

    used_ports = {row[0] for row in db.query(models.Instance.port).all() if row[0]}
    imported_count = 0

    for instance_dir in sorted(INSTANCES_DIR.iterdir()):
        if not instance_dir.is_dir():
            continue

        instance_id = instance_dir.name
        
        # Check if instance already exists by ID (for hash-based folders)
        if db.query(models.Instance).filter(models.Instance.id == instance_id).first():
            continue

        # Check if any existing instance's sanitized name already maps to this folder
        existing_sanitized = set()
        for inst in db.query(models.Instance).all():
            sn = inst.name.replace(' ', '-').replace('_', '-')
            sn = ''.join(c for c in sn if c.isalnum() or c == '-').strip('-')
            existing_sanitized.add(sn if sn else 'instance')
        if instance_id in existing_sanitized:
            logger.info(f"[seed] Skipping folder '{instance_id}' — name already claimed by another instance")
            continue

        # Try both promptclaw.json and openclaw.json for backward compatibility
        config_path = instance_dir / "promptclaw.json"
        if not config_path.exists():
            config_path = instance_dir / "openclaw.json"
        if not config_path.exists():
            logger.warning(f"[seed] Skipping instance without config: {instance_id}")
            continue

        try:
            config_json = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[seed] Failed to read config for {instance_id}: {exc}")
            continue

        config_json = copy.deepcopy(config_json)
        configured_port = config_json.get("gateway", {}).get("port")
        if not isinstance(configured_port, int) or configured_port in used_ports:
            configured_port = _next_available_port(db, used_ports)
        else:
            used_ports.add(configured_port)

        config_json.setdefault("gateway", {})
        config_json["gateway"]["port"] = configured_port

        model_name = (
            config_json.get("agents", {})
            .get("defaults", {})
            .get("model", {})
            .get("primary", "unknown-model")
        )

        # Use friendly name if available, otherwise generate from model
        friendly_name = INSTANCE_NAMES_MAP.get(instance_id)
        if not friendly_name:
            friendly_name = f"PromptClaw {instance_id[:8]}"

        db_instance = models.Instance(
            id=instance_id,
            tenant_id=ADMIN_TENANT,
            name=friendly_name,
            description=f"Imported existing instance ({model_name})",
            status="stopped",
            port=configured_port,
            pid=None,
        )
        db.add(db_instance)
        db.add(models.InstanceConfig(instance_id=instance_id, config_json=config_json))
        imported_count += 1
        logger.info(f"[seed] Imported instance {instance_id}: {friendly_name}")

    if imported_count:
        logger.info(f"[seed] Imported {imported_count} existing instance folder(s)")


def normalize_chat_first_configs(db: Session):
    """Migrate older full-tools defaults to the cheaper chat-first profile."""
    from services import config_builder
    import crud

    changed_count = 0
    rows = db.query(models.InstanceConfig).all()
    for row in rows:
        cfg = copy.deepcopy(row.config_json or {})
        tools = cfg.setdefault("tools", {})
        web = tools.setdefault("web", {})
        search = web.setdefault("search", {})
        fetch = web.setdefault("fetch", {})
        elevated = tools.setdefault("elevated", {})
        exec_cfg = tools.setdefault("exec", {})
        plugins = cfg.setdefault("plugins", {}).setdefault("entries", {})
        hooks = cfg.setdefault("hooks", {}).setdefault("internal", {}).setdefault("entries", {})
        memory = hooks.setdefault("session-memory", {})

        looked_like_old_default = (
            tools.get("profile") == "full"
            and elevated.get("enabled") is True
            and search.get("enabled") is True
        )

        if looked_like_old_default:
            tools["profile"] = "minimal"
            tools["alsoAllow"] = []
            exec_cfg["enabled"] = False
            elevated["enabled"] = False
            search["enabled"] = False
            fetch["enabled"] = False
            memory["enabled"] = False
            plugins.setdefault("duckduckgo", {})["enabled"] = False
            plugins.setdefault("slack", {})["enabled"] = False
            for plugin_id in ("browser", "file-transfer", "memory-core", "image"):
                plugins.setdefault(plugin_id, {})["enabled"] = False

        models_cfg = cfg.setdefault("models", {})
        if models_cfg.get("mode") in (None, "merge"):
            models_cfg["mode"] = "replace"
        crud._migrate_deprecated_models(cfg)
        crud.sync_related_config_sections(cfg)

        if cfg != row.config_json:
            row.config_json = cfg
            flag_modified(row, "config_json")
            changed_count += 1
            inst = db.query(models.Instance).filter(models.Instance.id == row.instance_id).first()
            if inst:
                try:
                    config_builder.activate_instance(inst.name, inst.id, cfg)
                except Exception as exc:
                    logger.warning(f"[seed] Failed to activate normalized config for {inst.id}: {exc}")

    if changed_count:
        db.flush()
        logger.info(f"[seed] Normalized {changed_count} instance config(s) to chat-first defaults")


def seed_admin_user():
    """
    Ensure the PromptClaw Manager admin account exists and owns the shared tenant.

    On startup this will:
      1. Create the shared admin tenant if it does not exist.
      2. Re-parent orphaned or legacy instances to that tenant.
      3. Create or normalize the admin user to the expected credentials.
    """
    if not settings.admin_seed_enabled:
        logger.info("[seed] Admin seed disabled by ADMIN_SEED_ENABLED")
        return

    from auth import hash_password

    db: Session = SessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.id == ADMIN_TENANT).first()
        if not tenant:
            tenant = models.Tenant(id=ADMIN_TENANT, name=ADMIN_TENANT_NAME)
            db.add(tenant)
            db.flush()
            logger.info(f"[seed] Created tenant: {ADMIN_TENANT}")
        elif tenant.name != ADMIN_TENANT_NAME:
            tenant.name = ADMIN_TENANT_NAME

        orphaned_instances = (
            db.query(models.Instance)
            .filter(~models.Instance.tenant_id.in_(db.query(models.Tenant.id)))
            .all()
        )
        for inst in orphaned_instances:
            logger.info(f"[seed] Re-parenting orphaned instance {inst.id} -> {ADMIN_TENANT}")
            inst.tenant_id = ADMIN_TENANT
        if orphaned_instances:
            db.flush()

        legacy_tenant = db.query(models.Tenant).filter(models.Tenant.id == LEGACY_ADMIN_TENANT).first()
        if legacy_tenant:
            legacy_instances = db.query(models.Instance).filter(models.Instance.tenant_id == LEGACY_ADMIN_TENANT).all()
            for inst in legacy_instances:
                inst.tenant_id = ADMIN_TENANT

            legacy_users = db.query(models.User).filter(models.User.tenant_id == LEGACY_ADMIN_TENANT).all()
            for user in legacy_users:
                user.tenant_id = ADMIN_TENANT

            db.delete(legacy_tenant)
            logger.info("[seed] Migrated legacy admin tenant to shared admin tenant")

        # Ensure the three pre-existing instances always belong to the admin tenant
        # and have proper friendly names + matching folder names
        admin_owned_count = 0
        renamed_count = 0
        for inst_id in ADMIN_INSTANCE_IDS:
            inst = db.query(models.Instance).filter(models.Instance.id == inst_id).first()
            if not inst:
                continue

            # Re-assign to admin tenant if needed
            if inst.tenant_id != ADMIN_TENANT:
                logger.info(f"[seed] Re-assigning instance {inst_id} ({inst.name}) to admin tenant")
                inst.tenant_id = ADMIN_TENANT
                admin_owned_count += 1

            # Rename to friendly name
            friendly_name = INSTANCE_NAMES_MAP.get(inst_id)
            if friendly_name and inst.name != friendly_name:
                old_name = inst.name
                # Rename folder on disk
                old_dir = INSTANCES_DIR / old_name
                new_dir = INSTANCES_DIR / friendly_name
                if old_dir.exists() and not new_dir.exists():
                    try:
                        old_dir.rename(new_dir)
                        logger.info(f"[seed] Renamed folder '{old_name}' -> '{friendly_name}'")
                    except Exception as e:
                        logger.warning(f"[seed] Failed to rename folder '{old_name}': {e}")

                inst.name = friendly_name
                renamed_count += 1
                logger.info(f"[seed] Renamed instance {inst_id} '{old_name}' -> '{friendly_name}'")

        if admin_owned_count:
            db.flush()
            logger.info(f"[seed] Moved {admin_owned_count} existing instance(s) to admin tenant")
        if renamed_count:
            db.flush()
            logger.info(f"[seed] Renamed {renamed_count} existing instance(s) to friendly names")

        admin_instances = db.query(models.Instance).filter(models.Instance.tenant_id == ADMIN_TENANT).all()
        logger.info(f"[seed] Admin tenant has {len(admin_instances)} instance(s)")

        admin_user = db.query(models.User).filter(models.User.email == ADMIN_EMAIL).first()
        legacy_user = db.query(models.User).filter(models.User.email == LEGACY_ADMIN_EMAIL).first()
        hashed_password = hash_password(ADMIN_PASSWORD)

        if admin_user and legacy_user and admin_user.id != legacy_user.id:
            legacy_user.tenant_id = ADMIN_TENANT
            legacy_user.is_active = False
            logger.info("[seed] Disabled duplicate legacy admin user")
            legacy_user = None

        if admin_user:
            admin_user.name = ADMIN_NAME
            admin_user.email = ADMIN_EMAIL
            admin_user.hashed_password = hashed_password
            admin_user.tenant_id = ADMIN_TENANT
            admin_user.is_active = True
            logger.info(f"[seed] Normalized admin user: {ADMIN_EMAIL}")
        elif legacy_user:
            legacy_user.name = ADMIN_NAME
            legacy_user.email = ADMIN_EMAIL
            legacy_user.hashed_password = hashed_password
            legacy_user.tenant_id = ADMIN_TENANT
            legacy_user.is_active = True
            logger.info(f"[seed] Migrated legacy admin user to: {ADMIN_EMAIL}")
        else:
            user = models.User(
                name=ADMIN_NAME,
                email=ADMIN_EMAIL,
                hashed_password=hashed_password,
                tenant_id=ADMIN_TENANT,
            )
            db.add(user)
            logger.info(f"[seed] Created admin user: {ADMIN_EMAIL}")

        import_existing_instance_dirs(db)
        normalize_chat_first_configs(db)

        db.commit()
        logger.info("[seed] Admin seed complete.")
    except Exception as exc:
        db.rollback()
        logger.error(f"[seed] Seed failed: {exc}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime()
    seed_admin_user()
    yield


app = FastAPI(
    title="PromptIQ PromptClaw Manager",
    description="Multi-instance manager for PromptClaw agents.",
    version="2.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router, prefix="/api")
app.include_router(instances.router, prefix="/api")
app.include_router(agents_router.router, prefix="/api")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/health/runtime")
def runtime_health_check():
    status = settings.runtime_status()
    ok = status["instances_root_writable"]
    if settings.is_production:
        ok = ok and status["openclaw_available"] and status["node_available"]
    return {"status": "ok" if ok else "degraded", **status}
