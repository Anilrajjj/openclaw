import os
import json
import copy
import shutil
import logging
import re
from datetime import datetime
from settings import settings

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCES_ROOT = str(settings.instances_root)

def sanitize_instance_name(name: str) -> str:
    """
    Convert instance name to a safe filesystem directory name.
    Removes/replaces special characters, preserves readability.
    """
    # Replace spaces and underscores with hyphens
    name = name.replace(' ', '-').replace('_', '-')
    # Remove or replace special characters, keep only alphanumeric and hyphens
    name = re.sub(r'[^a-zA-Z0-9\-]', '', name)
    # Remove leading/trailing hyphens
    name = name.strip('-')
    # Ensure we have something left
    return name if name else 'instance'

def get_instance_dir(instance_name: str) -> str:
    """Get the directory path for an instance based on its name."""
    safe_name = sanitize_instance_name(instance_name)
    return os.path.join(INSTANCES_ROOT, safe_name)

def _fix_schema_values(config: dict):
    """Fix values that the OpenClaw gateway schema would reject.
    Only corrects known schema violations — preserves everything else.
    """
    if not isinstance(config, dict):
        return

    # Fix models.providers.*.api — must be a known API type
    valid_apis = {
        "openai-completions", "openai-responses", "openai-codex-responses",
        "anthropic-messages", "google-generative-ai", "github-copilot",
        "bedrock-converse-stream", "ollama", "azure-openai-responses",
    }
    providers = config.get("models", {}).get("providers", {})
    if isinstance(providers, dict):
        for pname, pcfg in providers.items():
            if isinstance(pcfg, dict) and pcfg.get("api") not in valid_apis:
                apt = pcfg.get("api", "")
                if apt and apt not in valid_apis:
                    logger.info(f"Fixed schema-invalid api '{apt}' for provider '{pname}' -> 'openai-completions'")
                    pcfg["api"] = "openai-completions"

    # Fix compaction.mode — must be "default" or "safeguard"
    agents = config.get("agents", {})
    defaults = agents.get("defaults", {})
    if isinstance(defaults, dict):
        compaction = defaults.get("compaction", {})
        if isinstance(compaction, dict):
            mode = compaction.get("mode")
            if mode not in ("default", "safeguard"):
                logger.info(f"Fixed schema-invalid compaction.mode '{mode}' -> 'safeguard'")
                compaction["mode"] = "safeguard"


def _sanitize_config(config_json: dict, instance_dir: str = None) -> dict:
    """Remove unrecognized keys from the config before writing to disk.

    The OpenClaw runtime validates promptclaw.json against a strict schema.
    Keys like systemPrompt, mcp, and tools.exec.enabled are managed by the
    PromptClaw Manager UI and stored in the DB but must be stripped before
    writing to disk so the runtime doesn't reject the config.

    When instance_dir is provided, systemPrompt is written to AGENTS.md
    in the instance's workspace directory so OpenClaw picks it up as a
    bootstrap file.
    """
    config = copy.deepcopy(config_json)
    _normalize_runtime_cost_controls(config)

    # Handle systemPrompt: write to AGENTS.md in workspace
    if instance_dir:
        agents = config.get("agents", {})
        defaults = agents.get("defaults", {})
        if isinstance(defaults, dict) and "systemPrompt" in defaults:
            system_prompt = defaults.pop("systemPrompt")
            # Get workspace path from config, or use instance_dir
            workspace = defaults.get("workspace")
            if not workspace:
                workspace = instance_dir
            # Write system prompt to AGENTS.md in workspace
            agents_md_path = os.path.join(workspace, "AGENTS.md")
            try:
                os.makedirs(workspace, exist_ok=True)
                with open(agents_md_path, "w", encoding="utf-8") as f:
                    f.write(system_prompt)
            except OSError as exc:
                logger.warning(f"Could not update AGENTS.md at {agents_md_path}: {exc}")

    # Remove mcp key (not recognized by OpenClaw schema)
    agents = config.get("agents", {})
    defaults = agents.get("defaults", {})
    if isinstance(defaults, dict):
        defaults.pop("mcp", None)

    # Remove tools.exec.enabled (not recognized by OpenClaw schema)
    tools = config.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if isinstance(exec_cfg, dict):
        exec_cfg.pop("enabled", None)

    # Only strip "tools" from model input arrays; preserve "image" for vision models.
    providers = config.get("models", {}).get("providers", {})
    for provider_name, provider in providers.items():
        if isinstance(provider, dict):
            provider.pop("provider", None)
            for model in provider.get("models", []):
                if isinstance(model, dict) and "input" in model:
                    model["input"] = [x for x in model["input"] if x in ("text", "image")]

    # Clean channels sub-keys
    channels = config.get("channels", {})
    for key in ["telegram", "slack"]:
        if key in channels:
            channel_cfg = channels[key]
            if isinstance(channel_cfg, dict):
                channel_cfg.pop("groups", None)

    # Clean up incomplete providers - remove providers without baseUrl or models
    providers = config.get("models", {}).get("providers", {})
    incomplete = []
    for provider_name, provider in providers.items():
        if isinstance(provider, dict):
            if not provider.get("baseUrl") or not provider.get("models"):
                incomplete.append(provider_name)
    for name in incomplete:
        del providers[name]
    if incomplete:
        logger.info(f"Cleaned up incomplete providers during activation: {incomplete}")

    # Fix schema-invalid values so the gateway doesn't reject the config.
    # Only corrects known schema violations — preserves user's intended values
    # wherever they don't conflict with the gateway's strict validation.
    _fix_schema_values(config)

    # Ensure a provider key matching the model prefix exists so the gateway
    # can find the API key. The gateway resolves provider by the first part
    # of the model string (e.g. "groq/llama-..." -> looks for "groq" key).
    # The gateway strips unknown keys on its own "config overwrite", so we
    # must RENAME the provider key rather than creating an alias.
    agents = config.get("agents", {})
    defaults = agents.get("defaults", {})
    primary_model = None
    if isinstance(defaults, dict):
        model_cfg = defaults.get("model", {})
        if isinstance(model_cfg, dict):
            primary_model = model_cfg.get("primary", "")
    if isinstance(primary_model, str) and "/" in primary_model:
        model_prefix = primary_model.split("/")[0]
        if model_prefix and model_prefix not in providers:
            # Find a valid source provider and rename it
            source_name = None
            ui_key = "custom-api-openrouter-ai"
            if ui_key in providers and providers[ui_key].get("baseUrl"):
                source_name = ui_key
            else:
                for pname, pcfg in providers.items():
                    if isinstance(pcfg, dict) and pcfg.get("baseUrl") and pcfg.get("apiKey"):
                        source_name = pname
                        break
            if source_name and source_name != model_prefix:
                providers[model_prefix] = providers.pop(source_name)

    return config


def _normalize_runtime_cost_controls(config: dict):
    """Avoid loading disabled tools/plugins when writing runtime config."""
    if not isinstance(config, dict):
        return

    models_cfg = config.setdefault("models", {})
    if models_cfg.get("mode") in (None, "merge"):
        models_cfg["mode"] = "replace"

    tools = config.setdefault("tools", {})
    exec_cfg = tools.setdefault("exec", {})
    elevated_cfg = tools.setdefault("elevated", {})
    web_cfg = tools.setdefault("web", {})
    search_cfg = web_cfg.setdefault("search", {})
    fetch_cfg = web_cfg.setdefault("fetch", {})

    if exec_cfg.get("enabled") is not True:
        exec_cfg["enabled"] = False
    if elevated_cfg.get("enabled") is not True:
        elevated_cfg["enabled"] = False
    if search_cfg.get("enabled") is not True:
        search_cfg["enabled"] = False
    if fetch_cfg.get("enabled") is not True:
        fetch_cfg["enabled"] = False

    if tools.get("profile") == "minimal":
        allowed = tools.get("alsoAllow")
        if not isinstance(allowed, list):
            allowed = []
        if exec_cfg.get("enabled") is True:
            for item in ("exec", "process"):
                if item not in allowed:
                    allowed.append(item)
        else:
            allowed = [item for item in allowed if item not in {"exec", "process", "fs"}]
        tools["alsoAllow"] = allowed

    slack = config.setdefault("channels", {}).setdefault("slack", {})
    plugins = config.setdefault("plugins", {}).setdefault("entries", {})
    if slack.get("enabled") is not True:
        plugins.setdefault("slack", {})["enabled"] = False
    # Duckduckgo: respect the user's plugin toggle; only disable if both
    # the plugin AND web search are explicitly off.
    if search_cfg["enabled"] is not True and plugins.get("duckduckgo", {}).get("enabled") is not True:
        plugins.setdefault("duckduckgo", {})["enabled"] = False
    # When duckduckgo plugin is explicitly enabled, align web search toggle.
    if plugins.get("duckduckgo", {}).get("enabled") is True:
        search_cfg["enabled"] = True
    # memory-core: respect user's explicit setting; default off if unset.
    if plugins.get("memory-core", {}).get("enabled") is not True:
        plugins.setdefault("memory-core", {})["enabled"] = False
    for plugin_id in ("browser", "file-transfer"):
        plugins.setdefault(plugin_id, {})["enabled"] = False
    plugins.pop("image", None)


def activate_instance(instance_name: str, instance_id: str, config_json: dict):
    """
    Writes the provided configuration JSON to the instance's working directory.
    This is the source of truth for the Node.js gateway process.
    
    Args:
        instance_name: Human-readable instance name (used for folder path)
        instance_id: Database UUID (used for config references)
        config_json: Configuration dictionary
    """
    instance_dir = get_instance_dir(instance_name)
    os.makedirs(instance_dir, exist_ok=True)

    # Ensure each instance has its own workspace directory
    if "agents" not in config_json:
        config_json["agents"] = {}
    if "defaults" not in config_json["agents"]:
        config_json["agents"]["defaults"] = {}
    # Always set workspace to instance_dir so uploaded data files
    # (stored at instance_dir/data/) are accessible from the workspace.
    config_json["agents"]["defaults"]["workspace"] = instance_dir

    config_json = _sanitize_config(config_json, instance_dir)

    # Ensure meta section exists to prevent gateway config integrity warnings
    config_json["meta"] = {
        "lastTouchedVersion": "2026.5.6",
        "lastTouchedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.utcnow().microsecond:06d}Z"
    }

    # 1. Write promptclaw.json (updated from openclaw.json)
    config_path = os.path.join(instance_dir, "promptclaw.json")
    with open(config_path, "w") as f:
        json.dump(config_json, f, indent=2)

    # 1b. Mirror config into isolated PROMPTCLAW_HOME locations so runtime never
    # falls back to global per-user state.
    home_paths = [
        os.path.join(instance_dir, ".promptclaw-home"),
        os.path.join(str(settings.promptclaw_home_root), sanitize_instance_name(instance_name)),
    ]
    for instance_home in home_paths:
        try:
            os.makedirs(instance_home, exist_ok=True)
            home_config_path = os.path.join(instance_home, "promptclaw.json")

            # Copy AGENTS.md to home path workspace if it exists
            home_config = copy.deepcopy(config_json)
            workspace = config_json.get("agents", {}).get("defaults", {}).get("workspace")
            if workspace and os.path.exists(os.path.join(workspace, "AGENTS.md")):
                home_workspace = os.path.join(instance_home, "workspace")
                os.makedirs(home_workspace, exist_ok=True)
                shutil.copy2(os.path.join(workspace, "AGENTS.md"),
                           os.path.join(home_workspace, "AGENTS.md"))

            with open(home_config_path, "w") as f:
                json.dump(home_config, f, indent=2)
        except Exception:
            # Some environments may not allow writing outside sandbox roots.
            pass

    # 2. Create an empty package.json to trick find-up (prevents it from going to root)
    pkg_path = os.path.join(instance_dir, "package.json")
    if not os.path.exists(pkg_path):
        with open(pkg_path, "w") as f:
            f.write('{"name": "promptclaw-instance", "private": true}')

    # 3. Ensure a basic .env exists if needed
    env_path = os.path.join(instance_dir, ".env")
    if not os.path.exists(env_path):
        with open(env_path, "w") as f:
            f.write("# Instance Environment\n")

    logger.info(f"Instance {instance_name} ({instance_id}) activated at {instance_dir}")
    return config_path


def load_disk_config(instance_name: str):
    """Read the promptclaw.json from the instance directory, if it exists.
    Returns the parsed dict or None if the file doesn't exist or is invalid.
    """
    instance_dir = get_instance_dir(instance_name)
    config_path = os.path.join(instance_dir, "promptclaw.json")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"Failed to read disk config for {instance_name}: {exc}")
        return None


def delete_instance_files(instance_name: str, instance_id: str = None):
    """Cleanup the instance working directory."""
    instance_dir = get_instance_dir(instance_name)
    if os.path.exists(instance_dir):
        shutil.rmtree(instance_dir)
        logger.info(f"Cleaned up files for instance {instance_name}")

    # Cleanup isolated per-instance PromptClaw state outside workspace (and fallback path).
    safe_name = sanitize_instance_name(instance_name)
    homes = [
        os.path.join(str(settings.promptclaw_home_root), safe_name),
        os.path.join(instance_dir, ".promptclaw-home"),
    ]
    for instance_home in homes:
        if os.path.exists(instance_home):
            try:
                shutil.rmtree(instance_home)
                logger.info(f"Cleaned up isolated home for instance {instance_name}: {instance_home}")
            except Exception:
                pass

def get_instance_logs(instance_name: str) -> str:
    """Read the gateway.log file for the instance."""
    instance_dir = get_instance_dir(instance_name)
    log_path = os.path.join(instance_dir, "gateway.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            return f"Error reading logs: {str(e)}"
    return "No logs available yet."

def clear_instance_logs(instance_name: str) -> bool:
    """Clear the gateway.log file for the instance."""
    instance_dir = get_instance_dir(instance_name)
    log_path = os.path.join(instance_dir, "gateway.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, "w") as f:
                f.write("")  # Truncate file
            logger.info(f"Cleared logs for instance {instance_name}")
            return True
        except Exception as e:
            logger.warning(f"Error clearing logs for instance {instance_name}: {str(e)}")
            return False
    return True
