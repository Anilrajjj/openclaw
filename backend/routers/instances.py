import logging
from typing import List, Dict, Any
import secrets
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy.orm import Session
from database import get_db
import crud, schemas, models
from auth import get_current_user
from services import launcher, config_builder, file_storage_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/instances", tags=["instances"])


# ─── Helper: ensure instance belongs to current user's tenant ─────────────────
def _owned_instance(instance_id: str, current_user: models.User, db: Session) -> models.Instance:
    db_instance = crud.get_instance(db, instance_id)
    if not db_instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if db_instance.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return db_instance


def _config_update_payload(db: Session, instance_id: str, message: str) -> Dict[str, Any]:
    """Return the canonical saved config and refreshed UI helper fields."""
    config = crud.get_instance_config(db, instance_id, mask=True)
    instance = crud.get_instance(db, instance_id)
    return {
        "success": True,
        "message": message,
        "config": config,
        "instance": schemas.Instance.model_validate(instance) if instance else None,
    }


def _activate_or_restart_instance(db: Session, db_instance: models.Instance, config_json: Dict[str, Any]) -> None:
    """Write runtime config and restart only when the instance is actually running."""
    was_running = db_instance.status == "running" and launcher.is_instance_running(db_instance.pid)
    if db_instance.status == "running" and not was_running:
        crud.update_instance(db, db_instance.id, schemas.InstanceUpdate(status="stopped", pid=None))

    if was_running:
        launcher.stop_instance(db_instance.pid)
        crud.update_instance(db, db_instance.id, schemas.InstanceUpdate(status="stopped", pid=None))

    config_builder.activate_instance(db_instance.name, db_instance.id, config_json)

    if was_running:
        pid = launcher.start_instance(db_instance.name, db_instance.id, db_instance.port)
        crud.update_instance(db, db_instance.id, schemas.InstanceUpdate(status="running", pid=pid))


@router.get("", response_model=List[schemas.Instance])
def list_instances(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return crud.get_instances(db, current_user.tenant_id)


@router.post("", response_model=schemas.Instance)
def create_instance(
    instance: schemas.InstanceCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Force tenant_id to the authenticated user's tenant (ignore whatever frontend sends)
    instance.tenant_id = current_user.tenant_id

    # Ensure tenant exists (it always should after auth, but belt-and-suspenders)
    tenant = crud.get_tenant(db, current_user.tenant_id)
    if not tenant:
        crud.create_tenant(
            db,
            schemas.TenantCreate(name=f"{current_user.name}'s Workspace"),
            tenant_id=current_user.tenant_id,
        )

    if crud.instance_folder_exists_for_name(db, current_user.tenant_id, instance.name):
        raise HTTPException(status_code=409, detail="An instance with this name already exists")

    created = crud.create_instance(db, instance)
    config_json = crud.get_instance_config(db, created.id, mask=False)
    if config_json:
        try:
            config_builder.activate_instance(created.name, created.id, config_json)
        except Exception as exc:
            logger.warning(f"Created instance {created.id}, but activation failed: {exc}")
    return created


@router.get("/{instance_id}", response_model=schemas.Instance)
def get_instance(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _owned_instance(instance_id, current_user, db)


@router.delete("/{instance_id}")
def delete_instance(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete an instance and all associated data."""
    db_instance = _owned_instance(instance_id, current_user, db)
    instance_name = db_instance.name
    logger.info(f"Starting deletion of instance: {instance_id} ({instance_name})")

    # 1. Stop if running
    if db_instance.status == "running" and db_instance.pid:
        try:
            logger.info(f"Stopping running instance {instance_id} (PID: {db_instance.pid})")
            launcher.stop_instance(db_instance.pid)
            logger.info(f"Successfully stopped instance {instance_id}")
        except Exception as e:
            logger.warning(f"Error stopping instance {instance_id}: {str(e)}")

    # 2. Delete from database
    try:
        if not crud.delete_instance(db, instance_id):
            raise HTTPException(status_code=404, detail="Instance not found")
        logger.info(f"Deleted instance record from database: {instance_id}")
    except Exception as e:
        logger.error(f"Failed to delete instance from database: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete instance from database")

    # 3. Cleanup all instance files
    try:
        config_builder.delete_instance_files(instance_name, instance_id)
        file_storage_service.delete_instance_files(instance_name)
        logger.info(f"Cleaned up files for instance {instance_id}")
    except Exception as e:
        logger.warning(f"Error cleaning up files for instance {instance_id}: {str(e)}")

    logger.info(f"Successfully completed deletion of instance: {instance_id} ({instance_name})")
    return {"success": True, "message": f"Instance '{instance_name}' deleted successfully"}


@router.post("/{instance_id}/start")
def start_instance(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)

    if db_instance.status == "running" and launcher.is_instance_running(db_instance.pid):
        return {"success": True, "message": "Already running"}
    if db_instance.status == "running":
        crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="stopped", pid=None))

    # 1. Load config from disk promptclaw.json (user's manual edits take priority).
    #    Falls back to DB if disk file doesn't exist or is invalid.
    disk_config = config_builder.load_disk_config(db_instance.name)
    if isinstance(disk_config, dict) and disk_config:
        # Sync disk config to DB so manual edits are the source of truth, while
        # preserving DB-only fields stripped from runtime promptclaw.json.
        db_config = crud.get_instance_config(db, instance_id, mask=False)
        disk_config = crud.merge_runtime_config_with_db(disk_config, db_config)
        crud.update_instance_config(db, instance_id, disk_config)
        config_json = crud.get_instance_config(db, instance_id, mask=False)
    else:
        config_json = crud.get_instance_config(db, instance_id, mask=False)
    if not isinstance(config_json, dict) or not config_json:
        config_json = crud.reset_instance_config(db, instance_id)
        if not isinstance(config_json, dict):
            raise HTTPException(status_code=500, detail="Failed to prepare instance configuration")

    # Always keep runtime gateway port aligned with the assigned instance port.
    config_json.setdefault("gateway", {})
    config_json["gateway"]["port"] = db_instance.port
    config_json["gateway"].setdefault("auth", {})
    if not config_json["gateway"]["auth"].get("token"):
        config_json["gateway"]["auth"]["token"] = secrets.token_hex(32)

    # Persist normalized config before activation.
    if not crud.update_instance_config(db, instance_id, config_json):
        raise HTTPException(status_code=500, detail="Failed to persist instance configuration")

    # 2. Activate (write promptclaw.json)
    config_builder.activate_instance(db_instance.name, instance_id, config_json)

    # 3. Launch
    pid = launcher.start_instance(db_instance.name, instance_id, db_instance.port)

    # 4. Update DB
    crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="running", pid=pid))

    return {"success": True, "pid": pid}


@router.post("/{instance_id}/stop")
def stop_instance(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)

    if db_instance.status == "stopped" or not launcher.is_instance_running(db_instance.pid):
        crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="stopped", pid=None))
        return {"success": True, "message": "Already stopped"}

    launcher.stop_instance(db_instance.pid)
    crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="stopped", pid=None))

    # Clear logs after stopping
    try:
        config_builder.clear_instance_logs(db_instance.name)
        logger.info(f"Cleared logs for stopped instance {instance_id}")
    except Exception as e:
        logger.warning(f"Error clearing logs for instance {instance_id}: {str(e)}")

    return {"success": True, "message": "Instance stopped and logs cleared"}


@router.get("/{instance_id}/config")
def get_config(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)
    config = crud.get_instance_config(db, instance_id, mask=True)

    # Auto-create config from template if missing
    if config is None:
        config = crud.reset_instance_config(db, instance_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Instance not found")

    # Migrate legacy telegram channels to slack if present
    config = crud.migrate_telegram_to_slack(config)
    crud.sync_related_config_sections(config)

    return config


@router.post("/{instance_id}/config")
def update_config(
    instance_id: str,
    config: Dict[str, Any],
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)

    # Migrate any legacy telegram entries in incoming config
    config = crud.migrate_telegram_to_slack(config)
    crud.sync_related_config_sections(config)

    # Clean up incomplete providers - remove providers that don't have baseUrl or models
    if 'models' in config and 'providers' in config['models']:
        providers = config['models']['providers']
        incomplete = []
        for name, provider in providers.items():
            if not provider.get('baseUrl') or not provider.get('models'):
                incomplete.append(name)
        for name in incomplete:
            del providers[name]
        if incomplete:
            logger.info(f"Cleaned up incomplete providers: {incomplete}")

    if not crud.update_instance_config(db, instance_id, config):
        raise HTTPException(status_code=404, detail="Instance not found")

    # Always write the updated config to promptclaw.json on disk so it's
    # ready the next time the instance starts — even if currently stopped.
    config_json = crud.get_instance_config(db, instance_id, mask=False)
    if config_json and db_instance:
        _activate_or_restart_instance(db, db_instance, config_json)

    return _config_update_payload(db, instance_id, "Configuration saved successfully")


@router.post("/{instance_id}/config/apply-fixes")
def apply_config_fixes(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Applies the canonical runtime fixes to the instance's DB config and re-writes
    promptclaw.json to disk. This ensures the UI and the runtime are always in sync.

    Fixes applied:
    - tools.alsoAllow: ["exec", "process", "fs"]  (required since OpenClaw #47487)
    - channels.slack.historyLimit / dmHistoryLimit → 50
    - plugins.entries.browser.enabled → false       (eliminates EPERM symlink error)
    - tools.profile → "full" (if was "minimal")
    """
    db_instance = _owned_instance(instance_id, current_user, db)

    config = crud.get_instance_config(db, instance_id, mask=False)
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")

    # ── Fix 1: tools.alsoAllow ───────────────────────────────────────────────
    tools = config.setdefault("tools", {})
    existing_allow = tools.get("alsoAllow", [])
    required = {"exec", "process"} if tools.get("exec", {}).get("enabled") is True else set()
    merged = list(required | set(existing_allow))
    tools["alsoAllow"] = merged

    # Keep simple chat cheap by default; tools can be enabled from Advanced.
    tools.setdefault("profile", "minimal")
    if tools.get("profile") == "minimal" and tools.get("exec", {}).get("enabled") is not True:
        tools["alsoAllow"] = [item for item in tools["alsoAllow"] if item not in {"exec", "process"}]

    # ── Fix 2: Slack history limits ──────────────────────────────────────────
    slack = config.setdefault("channels", {}).setdefault("slack", {})
    if slack.get("historyLimit", 0) == 0:
        slack["historyLimit"] = 50
    if slack.get("dmHistoryLimit", 0) == 0:
        slack["dmHistoryLimit"] = 50

    # ── Fix 3: Disable browser plugin (eliminates EPERM symlink error) ───────
    plugins = config.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    entries.setdefault("browser", {})["enabled"] = False
    entries.pop("image", None)
    if slack.get("enabled") is not True:
        entries.setdefault("slack", {})["enabled"] = False
    if tools.get("web", {}).get("search", {}).get("enabled") is not True:
        entries.setdefault("duckduckgo", {})["enabled"] = False

    # Persist the patched config to DB
    if not crud.update_instance_config(db, instance_id, config):
        raise HTTPException(status_code=500, detail="Failed to update config in database")

    # Re-read unmasked and write to disk (both promptclaw.json locations)
    config_unmasked = crud.get_instance_config(db, instance_id, mask=False)
    if config_unmasked and db_instance:
        _activate_or_restart_instance(db, db_instance, config_unmasked)

    return _config_update_payload(db, instance_id, "Config fixes applied to database and disk")


@router.post("/{instance_id}/reset")
def reset_config(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned_instance(instance_id, current_user, db)
    new_config = crud.reset_instance_config(db, instance_id)
    if new_config is None:
        raise HTTPException(status_code=404, detail="Instance not found")

    db_instance = crud.get_instance(db, instance_id)
    config_unmasked = crud.get_instance_config(db, instance_id, mask=False)
    if config_unmasked and db_instance:
        _activate_or_restart_instance(db, db_instance, config_unmasked)

    return _config_update_payload(db, instance_id, "Configuration reset to default")


@router.get("/{instance_id}/runtime", response_model=schemas.InstanceRuntime)
def get_runtime(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)
    config = crud.get_instance_config(db, instance_id, mask=False)
    if not config:
        raise HTTPException(status_code=404, detail="Instance not found")

    return {
        "id": instance_id,
        "port": db_instance.port,
        "token": config.get("gateway", {}).get("auth", {}).get("token", ""),
    }


@router.get("/{instance_id}/logs")
def get_logs(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)
    log_content = config_builder.get_instance_logs(db_instance.name)
    return {"log": log_content}


@router.post("/{instance_id}/upload", response_model=schemas.UploadResponse)
async def upload_files(
    instance_id: str,
    files: List[UploadFile] = File(...),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)

    saved_records: List[models.InstanceFile] = []
    errors: List[str] = []

    for upload in files:
        if not upload.filename:
            errors.append("A file had no name")
            continue
        try:
            content = await upload.read()
            file_name, file_path, file_size = file_storage_service.save_file(
                db_instance.name, upload.filename, content
            )
            file_type = file_storage_service.MIME_TYPE_MAP.get(
                file_storage_service.Path(upload.filename).suffix.lower(),
                "application/octet-stream",
            )
            record = models.InstanceFile(
                instance_id=instance_id,
                file_name=file_name,
                file_path=file_path,
                file_size=file_size,
                file_type=file_type,
            )
            db.add(record)
            saved_records.append(record)
        except ValueError as e:
            errors.append(f"{upload.filename}: {str(e)}")
        except Exception as e:
            errors.append(f"{upload.filename}: {str(e)}")

    db.commit()
    for rec in saved_records:
        db.refresh(rec)

    out = [schemas.InstanceFileOut.model_validate(r) for r in saved_records]
    msg = f"{len(out)} file(s) uploaded to '{db_instance.name}'"
    if errors:
        msg += f". {len(errors)} failed: {'; '.join(errors)}"

    was_running = db_instance.status == "running" and launcher.is_instance_running(db_instance.pid)
    if db_instance.status == "running" and not was_running:
        crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="stopped", pid=None))
    if was_running:
        launcher.stop_instance(db_instance.pid)
        crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="stopped", pid=None))

    # Dynamically update AGENTS.md so the agent knows about new data files
    try:
        file_storage_service.update_agents_data_section(db_instance.name)
    except Exception as e:
        logger.warning(f"Failed to update AGENTS.md after upload for {db_instance.name}: {e}")

    # Uploaded data is only useful if the instance can inspect its own
    # workspace. Enable the minimal allow-list needed for file/process tools.
    try:
        config = crud.get_instance_config(db, instance_id, mask=False)
        if config:
            tools = config.setdefault("tools", {})
            tools.setdefault("profile", "minimal")
            exec_cfg = tools.setdefault("exec", {})
            exec_cfg["enabled"] = True
            exec_cfg.setdefault("security", "full")
            allow = tools.get("alsoAllow")
            if not isinstance(allow, list):
                allow = []
            for item in ("exec", "process"):
                if item not in allow:
                    allow.append(item)
            tools["alsoAllow"] = allow
            crud.update_instance_config(db, instance_id, config)
            runtime_config = crud.get_instance_config(db, instance_id, mask=False)
            config_builder.activate_instance(db_instance.name, instance_id, runtime_config)
    except Exception as e:
        logger.warning(f"Failed to enable data access after upload for {db_instance.name}: {e}")
    finally:
        if was_running:
            pid = launcher.start_instance(db_instance.name, instance_id, db_instance.port)
            crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="running", pid=pid))

    return {"success": True, "message": msg, "files": out}


@router.get("/{instance_id}/files", response_model=List[schemas.InstanceFileOut])
def list_files(
    instance_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _owned_instance(instance_id, current_user, db)
    return db.query(models.InstanceFile).filter(
        models.InstanceFile.instance_id == instance_id
    ).order_by(models.InstanceFile.uploaded_at.desc()).all()


@router.delete("/{instance_id}/files/{file_id}")
def delete_file(
    instance_id: str,
    file_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_instance = _owned_instance(instance_id, current_user, db)
    record = db.query(models.InstanceFile).filter(
        models.InstanceFile.id == file_id,
        models.InstanceFile.instance_id == instance_id,
    ).first()
    if not record:
        raise HTTPException(status_code=404, detail="File record not found")

    was_running = db_instance.status == "running" and launcher.is_instance_running(db_instance.pid)
    if db_instance.status == "running" and not was_running:
        crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="stopped", pid=None))
    if was_running:
        launcher.stop_instance(db_instance.pid)
        crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="stopped", pid=None))

    try:
        file_storage_service.delete_file(db_instance.name, record.file_path)
    except ValueError:
        pass
    except Exception as e:
        logger.warning(f"Error deleting file on disk: {e}")

    db.delete(record)
    db.commit()

    # Update AGENTS.md so the agent knows about the remaining data files
    try:
        file_storage_service.update_agents_data_section(db_instance.name)
        config = crud.get_instance_config(db, instance_id, mask=False)
        if config:
            config_builder.activate_instance(db_instance.name, instance_id, config)
    except Exception as e:
        logger.warning(f"Failed to update AGENTS.md after delete for {db_instance.name}: {e}")
    finally:
        if was_running:
            pid = launcher.start_instance(db_instance.name, instance_id, db_instance.port)
            crud.update_instance(db, instance_id, schemas.InstanceUpdate(status="running", pid=pid))

    return {"success": True, "message": f"File '{record.file_name}' deleted"}
