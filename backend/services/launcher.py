import os
import subprocess
import signal
import logging
import psutil
import shutil
from datetime import datetime
from settings import settings
from services.config_builder import get_instance_dir, sanitize_instance_name

logger = logging.getLogger(__name__)

# ─── Constants ───────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCES_ROOT = str(settings.instances_root)

# ─── Process Management ───────────────────────

def start_instance(instance_name: str, instance_id: str, port: int) -> int:
    """
    Launch a detached PromptClaw gateway process for the instance.
    
    Args:
        instance_name: Human-readable instance name
        instance_id: Database UUID
        port: Gateway port to use
    """
    settings.validate_runtime(require_openclaw=True)

    instance_dir = get_instance_dir(instance_name)
    os.makedirs(instance_dir, exist_ok=True)

    # Keep per-instance state isolated from the user's default ~/.promptclaw.
    # Prefer a non-OneDrive local path to avoid strict sync-folder safety checks.
    safe_name = sanitize_instance_name(instance_name)
    preferred_home = os.path.join(str(settings.promptclaw_home_root), safe_name)
    fallback_home = os.path.join(instance_dir, ".promptclaw-home")
    try:
        os.makedirs(preferred_home, exist_ok=True)
        instance_home = preferred_home
    except Exception:
        os.makedirs(fallback_home, exist_ok=True)
        instance_home = fallback_home

    log_file_path = os.path.join(instance_dir, "gateway.log")

    config_src = os.path.join(instance_dir, "promptclaw.json")
    config_dst = os.path.join(instance_home, "promptclaw.json")
    if os.path.exists(config_src):
        try:
            shutil.copyfile(config_src, config_dst)
        except Exception:
            pass
    
    cmd = settings.gateway_command(port)
    env = os.environ.copy()
    env["OPENCLAW_HOME"] = instance_home
    env["OPENCLAW_CONFIG_PATH"] = config_dst
    env["NODE_OPTIONS"] = "--dns-result-order=ipv4first"
    
    log_file = open(log_file_path, "a", buffering=1, encoding="utf-8", errors="replace")

    # On Windows, properly spawn a detached process with stdout/stderr redirection
    if os.name == 'nt':
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            creation_flags |= subprocess.CREATE_NO_WINDOW
        else:
            creation_flags |= 0x08000000

        process = subprocess.Popen(
            cmd,
            cwd=instance_dir,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=False
        )
    else:
        # Unix/Linux: use start_new_session for detachment
        process = subprocess.Popen(
            cmd,
            cwd=instance_dir,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )

    try:
        log_file.write(f"[{process.pid}] Process started at {datetime.now()}\n")
        log_file.flush()
    except Exception:
        pass

    try:
        log_file.close()
    except Exception:
        pass
    
    logger.info(f"Started instance {instance_name} ({instance_id}) on port {port} with PID {process.pid}")
    return process.pid

def is_instance_running(pid: int) -> bool:
    """Return True only when the saved PID points to a live process."""
    if not pid:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return False

def stop_instance(pid: int):
    """
    Kill the process and all its children.
    """
    if not pid:
        return
    
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
        logger.info(f"Stopped process {pid}")
    except psutil.NoSuchProcess:
        logger.warning(f"Process {pid} not found, already stopped?")
    except Exception as e:
        logger.error(f"Error stopping process {pid}: {str(e)}")
