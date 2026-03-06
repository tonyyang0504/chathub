import os
import platform
import shutil


def check_gating(skill) -> dict:
    """Check if skill prerequisites are met. Returns {met: bool, missing: list[str]}."""
    missing = []

    if skill.requires_os:
        current_os = platform.system().lower()
        os_map = {"darwin": "darwin", "linux": "linux", "windows": "windows"}
        if os_map.get(current_os) not in skill.requires_os:
            missing.append(f"OS: requires {skill.requires_os}, got {current_os}")

    if skill.requires_binaries:
        for binary in skill.requires_binaries:
            if not shutil.which(binary):
                missing.append(f"Binary not found: {binary}")

    if skill.requires_env:
        for var in skill.requires_env:
            if not os.environ.get(var):
                missing.append(f"Env var not set: {var}")

    return {"met": len(missing) == 0, "missing": missing}
