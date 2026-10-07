"""File storage service with strict per-instance isolation.

Files are stored inside the instance's own working directory:
  C:\Openclaw_SAAS\instances\{instance_name}\data\

This module also manages the AGENTS.md data section so the agent
is always aware of available data files.
"""
import logging
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from settings import settings

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

ALLOWED_EXTENSIONS = {".csv", ".json", ".xlsx", ".txt"}

MIME_TYPE_MAP = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCES_ROOT = settings.instances_root


def sanitize_instance_name(name: str) -> str:
    name = name.replace(' ', '-').replace('_', '-')
    name = re.sub(r'[^a-zA-Z0-9\-]', '', name)
    name = name.strip('-')
    return name if name else 'instance'


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = "".join(c for c in name if c.isalnum() or c in "._- ")
    return name.strip() or "unnamed_file"


def _instance_data_dir(instance_name: str) -> Path:
    safe = sanitize_instance_name(instance_name)
    d = INSTANCES_ROOT / safe / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_file(filename: str, size: int) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"File type '{ext}' not allowed. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    if size > MAX_FILE_SIZE:
        raise ValueError(f"File '{filename}' exceeds 10MB limit ({size} bytes)")
    if size == 0:
        raise ValueError(f"File '{filename}' is empty")
    return ext


def save_file(instance_name: str, filename: str, content: bytes) -> tuple[str, str, int]:
    ext = validate_file(filename, len(content))
    safe_name = _safe_filename(filename)
    dest_dir = _instance_data_dir(instance_name)
    dest_path = dest_dir / safe_name

    resolved = dest_path.resolve()
    expected_root = dest_dir.resolve()
    if not str(resolved).startswith(str(expected_root)):
        raise ValueError("Invalid file path detected")

    counter = 1
    while dest_path.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        safe_name = f"{stem}_{counter}{suffix}"
        dest_path = dest_dir / safe_name
        counter += 1

    dest_path.write_bytes(content)
    file_type = MIME_TYPE_MAP.get(ext, "application/octet-stream")
    rel_path = str(dest_path.relative_to(INSTANCES_ROOT))
    logger.info(f"Saved file '{safe_name}' ({len(content)} bytes) for instance {instance_name}")
    return safe_name, rel_path, len(content)


def delete_file(instance_name: str, file_path: str) -> None:
    full = INSTANCES_ROOT / file_path
    resolved = full.resolve()
    expected = (INSTANCES_ROOT / sanitize_instance_name(instance_name)).resolve()
    if not str(resolved).startswith(str(expected)):
        raise ValueError("Access denied: file does not belong to this instance")
    if resolved.exists():
        resolved.unlink()
        logger.info(f"Deleted file '{file_path}' for instance {instance_name}")


def delete_instance_files(instance_name: str) -> None:
    safe = sanitize_instance_name(instance_name)
    data_dir = INSTANCES_ROOT / safe / "data"
    if data_dir.exists():
        import shutil
        shutil.rmtree(data_dir)
        logger.info(f"Deleted data directory for instance {instance_name}")


def _agents_md_path(instance_name: str) -> Path:
    safe = sanitize_instance_name(instance_name)
    return INSTANCES_ROOT / safe / "AGENTS.md"


def _mirror_agents_md(instance_name: str) -> None:
    """Mirror AGENTS.md from instance dir to .promptclaw-home/workspace/."""
    safe = sanitize_instance_name(instance_name)
    src = _agents_md_path(instance_name)
    if not src.exists():
        return
    home_dirs = [
        INSTANCES_ROOT / safe / ".promptclaw-home" / "workspace",
    ]
    for home_dir in home_dirs:
        try:
            home_dir.mkdir(parents=True, exist_ok=True)
            dest = home_dir / "AGENTS.md"
            import shutil
            shutil.copy2(str(src), str(dest))
        except Exception as exc:
            logger.warning(f"Could not mirror AGENTS.md to {home_dir}: {exc}")


def update_agents_data_section(instance_name: str) -> None:
    """Rewrite the AGENTS.md for an instance with a current listing of data files.

    Reads the existing AGENTS.md, preserves the system prompt header, and
    appends a fresh ## Available Data Files section. If no files exist the
    data section is removed entirely so the agent is not confused.
    """
    safe = sanitize_instance_name(instance_name)
    data_dir = INSTANCES_ROOT / safe / "data"
    agents_path = _agents_md_path(instance_name)

    # Collect data files on disk
    files_info: List[Dict[str, Any]] = []
    if data_dir.exists():
        for p in sorted(data_dir.iterdir()):
            if p.is_file():
                ext = p.suffix.lower()
                ftype = MIME_TYPE_MAP.get(ext, "application/octet-stream")
                files_info.append({
                    "file_name": p.name,
                    "file_size": p.stat().st_size,
                    "file_type": ftype,
                })

    # Read existing content, strip any old data section
    content = ""
    if agents_path.exists():
        content = agents_path.read_text(encoding="utf-8")

    import re as _re
    content = _re.split(r'\n## Available Data Files\n', content)[0]
    content = content.rstrip()

    # Append fresh data section
    if files_info:
        section_parts = ["", "## Available Data Files", ""]
        section_parts.append("You have access to the following data files in the `data/` directory:")
        section_parts.append("")
        for f in files_info:
            size_str = f"{f['file_size']:,} bytes" if f['file_size'] < 1048576 else f"{f['file_size'] / 1048576:.1f} MB"
            section_parts.append(f"- `data/{f['file_name']}` ({size_str}, type: {f['file_type']})")
        section_parts.append("")
        section_parts.append("Use relative paths like `data/filename.csv` when reading files from the `data/` directory located at your workspace root.")
        section_parts.append("")
        section_parts.append("When asked about employee data, HR records, or any uploaded information, first inspect the available data files to understand the schema.")
        content += "\n".join(section_parts)

    agents_path.write_text(content, encoding="utf-8")
    logger.info(f"Updated AGENTS.md data section for instance {instance_name} ({len(files_info)} files)")

    # Mirror to .promptclaw-home/workspace so the running gateway picks it up
    _mirror_agents_md(instance_name)
