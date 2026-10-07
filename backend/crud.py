import os
import json
import secrets
import copy
from datetime import datetime
from typing import List, Optional, Any, Dict
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
import models, schemas
from settings import settings

# ─── Constants ───────────────────────────────

BASE_PORT = settings.instance_base_port
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "base_config_template.json")

def get_default_config() -> Dict[str, Any]:
    with open(TEMPLATE_PATH, "r") as f:
        return json.load(f)

# ─── Tenants ─────────────────────────────────

def get_tenant(db: Session, tenant_id: str) -> Optional[models.Tenant]:
    return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()

def create_tenant(db: Session, tenant: schemas.TenantCreate, tenant_id: Optional[str] = None) -> models.Tenant:
    db_tenant = models.Tenant(id=tenant_id or _gen_uuid(), name=tenant.name)
    db.add(db_tenant)
    db.commit()
    db.refresh(db_tenant)
    return db_tenant

# ─── Instances ───────────────────────────────

def get_instances(db: Session, tenant_id: str) -> List[models.Instance]:
    instances = db.query(models.Instance).filter(models.Instance.tenant_id == tenant_id).order_by(models.Instance.created_at.desc()).all()
    for inst in instances:
        _attach_ui_fields(inst)
    return instances

def get_instance(db: Session, instance_id: str) -> Optional[models.Instance]:
    inst = db.query(models.Instance).filter(models.Instance.id == instance_id).first()
    if inst:
        _attach_ui_fields(inst)
    return inst

def get_instance_by_name(db: Session, tenant_id: str, name: str) -> Optional[models.Instance]:
    target = name.strip().lower()
    return db.query(models.Instance).filter(
        models.Instance.tenant_id == tenant_id,
        models.Instance.name.ilike(target),
    ).first()

def instance_folder_exists_for_name(db: Session, tenant_id: str, name: str) -> bool:
    from services.config_builder import sanitize_instance_name

    target = sanitize_instance_name(name).lower()
    for inst in db.query(models.Instance).filter(models.Instance.tenant_id == tenant_id).all():
        if sanitize_instance_name(inst.name).lower() == target:
            return True
    return False

def create_instance(db: Session, instance: schemas.InstanceCreate) -> models.Instance:
    # 1. Create Instance Record
    port = get_next_available_port(db)
    db_instance = models.Instance(
        name=instance.name,
        tenant_id=instance.tenant_id,
        description=instance.description,
        port=port
    )
    db.add(db_instance)
    db.flush() # Get ID

    # 2. Clone and Prepare Config with clean defaults
    config_json = copy.deepcopy(get_default_config())
    
    # Inject unique values
    config_json["gateway"]["port"] = port
    config_json["gateway"]["auth"]["token"] = secrets.token_hex(32)
    # Set workspace to instance-specific directory (overrides empty template value)
    from services.config_builder import get_instance_dir
    config_json["agents"]["defaults"]["workspace"] = get_instance_dir(instance.name)
    _apply_instance_task_defaults(config_json, instance.name, instance.description or "")
    _sanitize_runtime_secrets(config_json)
    _normalize_cost_controls(config_json)
    
    # Store in DB
    db_config = models.InstanceConfig(
        instance_id=db_instance.id,
        config_json=config_json
    )
    db.add(db_config)
    
    db.commit()
    db.refresh(db_instance)
    _attach_ui_fields(db_instance)
    return db_instance

def delete_instance(db: Session, instance_id: str) -> bool:
    """Delete instance and all related records (cascade delete for InstanceConfig)."""
    db_instance = get_instance(db, instance_id)
    if not db_instance:
        return False
    # Delete instance - cascade will handle InstanceConfig deletion
    db.delete(db_instance)
    db.commit()
    return True

def update_instance(db: Session, instance_id: str, updates: schemas.InstanceUpdate) -> Optional[models.Instance]:
    db_instance = get_instance(db, instance_id)
    if not db_instance:
        return None
    for key, value in updates.model_dump(exclude_unset=True).items():
        setattr(db_instance, key, value)
    db.commit()
    db.refresh(db_instance)
    _attach_ui_fields(db_instance)
    return db_instance

# ─── Config Management ──────────────────────

def get_instance_config(db: Session, instance_id: str, mask: bool = True) -> Optional[Dict[str, Any]]:
    db_config = db.query(models.InstanceConfig).filter(models.InstanceConfig.instance_id == instance_id).first()
    if not db_config:
        return None
    
    config = copy.deepcopy(db_config.config_json)
    if mask:
        _mask_config_secrets(config)
    return config

def update_instance_config(db: Session, instance_id: str, new_config: Dict[str, Any]) -> bool:
    db_config = db.query(models.InstanceConfig).filter(models.InstanceConfig.instance_id == instance_id).first()
    if not db_config:
        return False

    # Preserve existing secrets when masked placeholders are submitted by the UI.
    # The UI receives masked values (e.g. "sk-****abcd") and may send them back unchanged.
    old_config = copy.deepcopy(db_config.config_json)
    _restore_masked_secrets(old_config, new_config)
    _sanitize_runtime_secrets(new_config)
    _migrate_deprecated_models(new_config)
    sync_related_config_sections(new_config)
    _normalize_cost_controls(new_config)

    # Overwrite completely as per strict requirements
    db_config.config_json = new_config
    db_config.updated_at = datetime.utcnow()
    flag_modified(db_config, "config_json")
    db.commit()
    return True

def merge_runtime_config_with_db(runtime_config: Dict[str, Any], db_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Preserve UI-only DB settings when loading a runtime promptclaw.json.

    The runtime config intentionally omits fields that the gateway schema does
    not accept, such as agents.defaults.systemPrompt and tools.exec.enabled.
    When a started instance has a promptclaw.json on disk, we still allow manual
    disk edits to win for runtime-safe fields, but keep the DB-only controls.
    """
    merged = copy.deepcopy(runtime_config or {})
    existing = db_config or {}

    existing_defaults = existing.get("agents", {}).get("defaults", {})
    merged_defaults = merged.setdefault("agents", {}).setdefault("defaults", {})
    for key in ("systemPrompt", "mcp"):
        if key not in merged_defaults and key in existing_defaults:
            merged_defaults[key] = copy.deepcopy(existing_defaults[key])

    existing_exec = existing.get("tools", {}).get("exec", {})
    merged_exec = merged.setdefault("tools", {}).setdefault("exec", {})
    if "enabled" not in merged_exec and "enabled" in existing_exec:
        merged_exec["enabled"] = existing_exec["enabled"]

    return merged

def reset_instance_config(db: Session, instance_id: str) -> Optional[Dict[str, Any]]:
    db_instance = get_instance(db, instance_id)
    db_config = db.query(models.InstanceConfig).filter(models.InstanceConfig.instance_id == instance_id).first()
    if not db_instance or not db_config:
        return None
    
    # Fresh clone of template
    new_config = copy.deepcopy(get_default_config())
    # Preserve instance-specific values
    new_config["gateway"]["port"] = db_instance.port
    old_token = ((db_config.config_json or {}).get("gateway", {}).get("auth", {}).get("token", ""))
    if isinstance(old_token, str) and old_token and "*" not in old_token:
        new_config["gateway"]["auth"]["token"] = old_token
    else:
        new_config["gateway"]["auth"]["token"] = secrets.token_hex(32)

    from services.config_builder import get_instance_dir
    new_config["agents"]["defaults"]["workspace"] = get_instance_dir(db_instance.name)
    _apply_instance_task_defaults(new_config, db_instance.name, db_instance.description or "")
    _sanitize_runtime_secrets(new_config)
    _migrate_deprecated_models(new_config)
    sync_related_config_sections(new_config)
    _normalize_cost_controls(new_config)
    
    db_config.config_json = new_config
    db_config.updated_at = datetime.utcnow()
    flag_modified(db_config, "config_json")
    db.commit()
    return new_config

# ─── Helpers ─────────────────────────────────

def get_next_available_port(db: Session) -> int:
    used_ports = {r.port for r in db.query(models.Instance.port).all() if r.port}
    port = BASE_PORT
    while port in used_ports:
        port += 10 # Spread them out a bit
    return port

def _attach_ui_fields(inst: models.Instance):
    """Calculate UI helper fields from the JSON config."""
    if not inst.config:
        return
    cfg = inst.config.config_json
    try:
        primary_model = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
        provider_prefix = primary_model.split("/")[0] if "/" in primary_model else ""
        providers = cfg.get("models", {}).get("providers", {})
        provider_cfg = providers.get(provider_prefix) or providers.get("custom-api-openrouter-ai", {})
        inst.llm_provider = provider_cfg.get("provider") or provider_prefix or "unknown"
        inst.model_name = primary_model.split("/")[-1]
        inst.slack_enabled = cfg.get("channels", {}).get("slack", {}).get("enabled", False)
        raw_token = cfg.get("gateway", {}).get("auth", {}).get("token", "") or ""
        if len(raw_token) > 8:
            inst.gateway_auth_token = raw_token[:4] + "****" + raw_token[-4:]
        else:
            inst.gateway_auth_token = None
    except Exception as exc:
        logger.warning(f"Failed to attach UI fields for instance {inst.id}: {exc}")

def migrate_telegram_to_slack(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    If the config has channels.telegram but not channels.slack,
    migrate the telegram settings to slack structure.
    """
    channels = config.get("channels", {})
    has_telegram = "telegram" in channels
    has_slack = "slack" in channels

    if has_telegram and not has_slack:
        telegram_cfg = channels["telegram"]
        slack_cfg = {
            "enabled": telegram_cfg.get("enabled", False),
            "historyLimit": telegram_cfg.get("historyLimit", 0),
            "dmHistoryLimit": telegram_cfg.get("dmHistoryLimit", 0),
            "dmPolicy": telegram_cfg.get("dmPolicy", "disabled"),
            "allowFrom": telegram_cfg.get("allowFrom", []),
            "botToken": telegram_cfg.get("botToken", ""),
            "appToken": "",
            "signingSecret": "",
        }
        channels["slack"] = slack_cfg

        # Also migrate plugins and elevated allowFrom
        plugins = config.get("plugins", {})
        entries = plugins.get("entries", {})
        if "telegram" in entries and "slack" not in entries:
            entries["slack"] = entries.pop("telegram")

        tools = config.get("tools", {})
        elevated = tools.get("elevated", {})
        allow_from = elevated.get("allowFrom", {})
        if "telegram" in allow_from and "slack" not in allow_from:
            allow_from["slack"] = allow_from.pop("telegram")

    # Clean up telegram entries if slack is now present
    if has_slack:
        channels.pop("telegram", None)
        # Remove unrecognized 'groups' key from slack config
        slack_cfg = channels.get("slack", {})
        slack_cfg.pop("groups", None)
        plugins = config.get("plugins", {})
        entries = plugins.get("entries", {})
        entries.pop("telegram", None)
        load_paths = plugins.get("load", {}).get("paths", [])
        plugins.get("load", {})["paths"] = [p for p in load_paths if "telegram" not in p.lower()]
        installs = plugins.get("installs", {})
        installs.pop("telegram", None)
        tools = config.get("tools", {})
        elevated = tools.get("elevated", {})
        elevated.get("allowFrom", {}).pop("telegram", None)

    return config


def _mask_config_secrets(cfg: Any):
    """Recursively mask API keys and tokens in the JSON."""
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if k in ["apiKey", "botToken", "token"] and isinstance(v, str) and len(v) > 8:
                cfg[k] = v[:4] + "*" * (len(v) - 8) + v[-4:]
            else:
                _mask_config_secrets(v)
    elif isinstance(cfg, list):
        for item in cfg:
            _mask_config_secrets(item)

def _restore_masked_secrets(old_cfg: Any, new_cfg: Any):
    """Recursively restore old secret values when incoming values are masked placeholders."""
    secret_keys = {"apiKey", "botToken", "token"}

    if isinstance(old_cfg, dict) and isinstance(new_cfg, dict):
        for key, old_value in old_cfg.items():
            if key not in new_cfg:
                continue
            new_value = new_cfg[key]
            if (
                key in secret_keys
                and isinstance(old_value, str)
                and isinstance(new_value, str)
                and "*" in new_value
            ):
                new_cfg[key] = old_value
            else:
                _restore_masked_secrets(old_value, new_value)
    elif isinstance(old_cfg, list) and isinstance(new_cfg, list):
        for old_item, new_item in zip(old_cfg, new_cfg):
            _restore_masked_secrets(old_item, new_item)

def _apply_instance_task_defaults(cfg: Dict[str, Any], name: str, description: str):
    """Create an instance-scoped prompt and enable local data access."""
    defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
    task = (description or "").strip()
    task_line = task if task else f"Assist with tasks for the {name} instance."

    defaults["systemPrompt"] = (
        f"You are {name}, an OpenClaw instance created for one specific purpose.\n\n"
        f"Primary task: {task_line}\n\n"
        "Stay focused on this instance's purpose. Do not behave like a general chatbot "
        "when the user asks about unrelated work; briefly redirect them to this instance's task.\n\n"
        "Workspace and data rules:\n"
        "- Your workspace root is this instance folder.\n"
        "- Uploaded datasets and files are in the `data/` directory under the workspace root.\n"
        "- When the user asks about uploaded data, inspect `data/` yourself first. Do not ask the user for a file path.\n"
        "- Use relative paths such as `data/employees.csv`.\n"
        "- Base answers only on files you can actually read, and mention the file used when relevant.\n\n"
        "Execution rules:\n"
        "- Complete the requested task before replying when tools are available.\n"
        "- Ask a clarifying question only when the task cannot be completed from the workspace, uploaded data, or conversation."
    )

    tools = cfg.setdefault("tools", {})
    tools.setdefault("profile", "minimal")
    exec_cfg = tools.setdefault("exec", {})
    exec_cfg["enabled"] = True
    exec_cfg.setdefault("security", "full")
    allow = tools.get("alsoAllow")
    if not isinstance(allow, list):
        allow = []
    for item in ("exec", "process", "fs"):
        if item not in allow:
            allow.append(item)
    tools["alsoAllow"] = allow

def _sanitize_runtime_secrets(cfg: Dict[str, Any]):
    """
    Ensure runtime secrets are real values, not masked placeholders (e.g. '****').
    This keeps started instances from using invalid tokens/keys after UI edits.
    """
    if not isinstance(cfg, dict):
        return

    default_cfg = get_default_config()

    # gateway.auth.token
    gw = cfg.setdefault("gateway", {})
    auth = gw.setdefault("auth", {})
    token = auth.get("token")
    if not isinstance(token, str) or not token.strip() or "*" in token:
        auth["token"] = secrets.token_hex(32)

    # models.providers.custom-api-openrouter-ai.apiKey
    providers = cfg.setdefault("models", {}).setdefault("providers", {})
    if not providers:
        providers["custom-api-openrouter-ai"] = copy.deepcopy(
            default_cfg.get("models", {}).get("providers", {}).get("custom-api-openrouter-ai", {})
        )
    if "custom-api-openrouter-ai" in providers:
        provider = providers["custom-api-openrouter-ai"]
        api_key = provider.get("apiKey")
        if not isinstance(api_key, str) or not api_key.strip() or "*" in api_key:
            default_api_key = (
                default_cfg.get("models", {})
                .get("providers", {})
                .get("custom-api-openrouter-ai", {})
                .get("apiKey", "")
            )
            if isinstance(default_api_key, str) and default_api_key and "*" not in default_api_key:
                provider["apiKey"] = default_api_key
            elif isinstance(api_key, str) and "*" not in api_key:
                provider["apiKey"] = api_key
            else:
                provider["apiKey"] = ""

    # channels.slack.botToken, appToken, signingSecret (masked placeholder -> clear)
    slack = cfg.setdefault("channels", {}).setdefault("slack", {})
    bot_token = slack.get("botToken")
    if isinstance(bot_token, str) and "*" in bot_token:
        default_bot_token = default_cfg.get("channels", {}).get("slack", {}).get("botToken", "")
        slack["botToken"] = default_bot_token if isinstance(default_bot_token, str) else ""

    app_token = slack.get("appToken")
    if isinstance(app_token, str) and "*" in app_token:
        default_app_token = default_cfg.get("channels", {}).get("slack", {}).get("appToken", "")
        slack["appToken"] = default_app_token if isinstance(default_app_token, str) else ""

    signing_secret = slack.get("signingSecret")
    if isinstance(signing_secret, str) and "*" in signing_secret:
        default_signing_secret = default_cfg.get("channels", {}).get("slack", {}).get("signingSecret", "")
        slack["signingSecret"] = default_signing_secret if isinstance(default_signing_secret, str) else ""

    # channels.slack.dmPolicy must be compatible with allowFrom.
    allow_from = slack.get("allowFrom")
    dm_policy = slack.get("dmPolicy")
    valid_dm_policies = {"pairing", "allowlist", "open", "disabled"}

    # If Slack is disabled, use a fully disabled DM policy to avoid
    # policy-specific allowFrom validation requirements.
    if slack.get("enabled") is False:
        slack["dmPolicy"] = "disabled"
        dm_policy = "disabled"

    if dm_policy not in valid_dm_policies:
        slack["dmPolicy"] = "disabled" if slack.get("enabled") is False else "open"
        dm_policy = slack["dmPolicy"]
    if dm_policy == "allowlist":
        has_allow_from = isinstance(allow_from, list) and len([x for x in allow_from if isinstance(x, str) and x.strip()]) > 0
        if not has_allow_from:
            slack["dmPolicy"] = "open"
            dm_policy = "open"
    if dm_policy == "open":
        values = allow_from if isinstance(allow_from, list) else []
        if "*" not in values:
            slack["allowFrom"] = ["*"]

    # Ensure tools.exec.enabled defaults to false if missing. Simple chat should
    # not expose command tools unless the user explicitly enables them.
    tools = cfg.setdefault("tools", {})
    exec_cfg = tools.setdefault("exec", {})
    if "enabled" not in exec_cfg:
        exec_cfg["enabled"] = False

    # Session memory can do extra work on session resets, so default it off.
    hooks = cfg.setdefault("hooks", {})
    internal = hooks.setdefault("internal", {})
    if "enabled" not in internal:
        internal["enabled"] = True
    entries = internal.setdefault("entries", {})
    session_memory = entries.setdefault("session-memory", {})
    if "enabled" not in session_memory:
        session_memory["enabled"] = False

    # Ensure agents.defaults.mcp.servers exists
    agents = cfg.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})
    mcp = defaults.setdefault("mcp", {})
    if "servers" not in mcp:
        mcp["servers"] = []

    # Ensure agents.defaults.systemPrompt exists
    if "systemPrompt" not in defaults:
        defaults["systemPrompt"] = default_cfg.get("agents", {}).get("defaults", {}).get("systemPrompt", "")


def sync_related_config_sections(cfg: Dict[str, Any]):
    """Keep feature toggles, plugin entries, and allow lists in one canonical shape."""
    if not isinstance(cfg, dict):
        return

    _ensure_primary_provider_key(cfg)

    channels = cfg.setdefault("channels", {})
    slack = channels.setdefault("slack", {})

    tools = cfg.setdefault("tools", {})
    exec_cfg = tools.setdefault("exec", {})
    web_cfg = tools.setdefault("web", {})
    search_cfg = web_cfg.setdefault("search", {})

    plugins = cfg.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})

    entries.setdefault("slack", {})["enabled"] = slack.get("enabled") is True
    entries.setdefault("duckduckgo", {})["enabled"] = search_cfg.get("enabled") is True
    entries.pop("image", None)

    also_allow = tools.get("alsoAllow")
    if not isinstance(also_allow, list):
        also_allow = []
    required_exec = ["exec", "process", "fs"]
    if exec_cfg.get("enabled") is True:
        for item in required_exec:
            if item not in also_allow:
                also_allow.append(item)
    else:
        also_allow = [item for item in also_allow if item not in required_exec]
    tools["alsoAllow"] = also_allow


def _ensure_primary_provider_key(cfg: Dict[str, Any]):
    """Ensure models.providers has a key matching agents.defaults.model.primary."""
    primary = (
        cfg.setdefault("agents", {})
        .setdefault("defaults", {})
        .setdefault("model", {})
        .get("primary", "")
    )
    if not isinstance(primary, str) or "/" not in primary:
        return

    provider_key = primary.split("/")[0]
    if not provider_key:
        return

    providers = cfg.setdefault("models", {}).setdefault("providers", {})
    if provider_key in providers:
        if isinstance(providers[provider_key], dict):
            providers[provider_key].setdefault("provider", provider_key)
        return

    source_key = "custom-api-openrouter-ai" if "custom-api-openrouter-ai" in providers else None
    if not source_key:
        for key, provider in providers.items():
            if isinstance(provider, dict) and provider.get("baseUrl") and provider.get("models"):
                source_key = key
                break

    if source_key and isinstance(providers.get(source_key), dict):
        providers[provider_key] = copy.deepcopy(providers[source_key])
        providers[provider_key]["provider"] = provider_key


def _migrate_deprecated_models(cfg: Dict[str, Any]):
    """Replace model IDs known to be rejected by providers."""
    if not isinstance(cfg, dict):
        return

    replacements = {
        "groq/mixtral-8x7b-32768": {
            "provider_key": "groq",
            "provider": {
                "baseUrl": "https://api.groq.com/openai/v1",
                "api": "openai-completions",
            },
            "model": {
                "id": "llama-3.3-70b-versatile",
                "name": "Llama 3.3 70B Versatile",
                "contextWindow": 131072,
                "input": ["text"],
                "reasoning": False,
            },
        },
    }

    agents = cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})
    primary = agents.get("primary")
    replacement = replacements.get(primary)
    if not replacement:
        return

    provider_key = replacement["provider_key"]
    model = copy.deepcopy(replacement["model"])
    agents["primary"] = f"{provider_key}/{model['id']}"

    provider = cfg.setdefault("models", {}).setdefault("providers", {}).setdefault(provider_key, {})
    for key, value in replacement["provider"].items():
        provider[key] = value
    provider.setdefault("apiKey", "")

    models = provider.get("models")
    if not isinstance(models, list):
        models = []
    models = [item for item in models if item.get("id") != "mixtral-8x7b-32768"] if all(isinstance(item, dict) for item in models) else []
    if not any(isinstance(item, dict) and item.get("id") == model["id"] for item in models):
        models.insert(0, model)
    provider["models"] = models


def _normalize_cost_controls(cfg: Dict[str, Any]):
    """Keep runtime config chat-first and prevent disabled capabilities loading."""
    if not isinstance(cfg, dict):
        return

    models_cfg = cfg.setdefault("models", {})
    if models_cfg.get("mode") in (None, "merge"):
        models_cfg["mode"] = "replace"

    tools = cfg.setdefault("tools", {})
    tools.setdefault("profile", "minimal")
    exec_cfg = tools.setdefault("exec", {})
    elevated_cfg = tools.setdefault("elevated", {})
    web_cfg = tools.setdefault("web", {})
    search_cfg = web_cfg.setdefault("search", {})
    fetch_cfg = web_cfg.setdefault("fetch", {})

    if tools.get("profile") == "minimal":
        allowed = tools.get("alsoAllow")
        if not isinstance(allowed, list):
            allowed = []
        tools["alsoAllow"] = [
            item for item in allowed
            if item not in {"exec", "process", "fs"} or exec_cfg.get("enabled") is True
        ]

    if exec_cfg.get("enabled") is not True:
        exec_cfg["enabled"] = False
    if elevated_cfg.get("enabled") is not True:
        elevated_cfg["enabled"] = False
    if search_cfg.get("enabled") is not True:
        search_cfg["enabled"] = False
    if fetch_cfg.get("enabled") is not True:
        fetch_cfg["enabled"] = False

    slack = cfg.setdefault("channels", {}).setdefault("slack", {})
    plugins = cfg.setdefault("plugins", {}).setdefault("entries", {})
    if slack.get("enabled") is not True:
        plugins.setdefault("slack", {})["enabled"] = False
    if search_cfg.get("enabled") is not True:
        plugins.setdefault("duckduckgo", {})["enabled"] = False
    for plugin_id in ("browser", "file-transfer", "memory-core"):
        plugins.setdefault(plugin_id, {})["enabled"] = False
    plugins.pop("image", None)
