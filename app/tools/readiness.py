import re
from typing import Dict, List, Optional


TOOL_FRONTMATTER_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---", re.MULTILINE)
YAML_LINE_RE = re.compile(r"^(\w+)\s*:\s*(.+)$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_tool_frontmatter(tool_md_content: Optional[str]) -> Dict[str, str]:
    if not tool_md_content:
        return {}

    match = TOOL_FRONTMATTER_RE.match(tool_md_content.strip())
    if not match:
        return {}

    fields: Dict[str, str] = {}
    for line in match.group(1).split("\n"):
        parsed = YAML_LINE_RE.match(line.strip())
        if not parsed:
            continue
        fields[parsed.group(1)] = parsed.group(2).strip().strip('"\'')
    return fields


def build_publish_readiness(
    changed_files: List[str],
    metadata: Optional[Dict[str, str]] = None,
    skill_md_content: Optional[str] = None,
) -> Dict:
    metadata = metadata or {}
    frontmatter = parse_tool_frontmatter(skill_md_content)

    has_tool_file = any(path.upper().endswith("TOOL.MD") for path in changed_files)

    name = (metadata.get("name") or frontmatter.get("name") or "").strip()
    display_name = (metadata.get("display_name") or frontmatter.get("display_name") or "").strip()
    description = (metadata.get("description") or frontmatter.get("description") or "").strip()
    icon = (metadata.get("icon") or frontmatter.get("icon") or "").strip()
    trigger = (frontmatter.get("trigger") or metadata.get("name") or "").strip()

    checks = [
        {
            "key": "skill_file",
            "label": "TOOL.md file present",
            "ok": has_tool_file,
            "hard": False,
            "hint": "Create a TOOL.md file in your changes.",
        },
        {
            "key": "name",
            "label": "Tool name (kebab-case)",
            "ok": bool(name) and bool(SLUG_RE.match(name)),
            "hard": True,
            "hint": "Use lowercase kebab-case (example: contact-followup-helper).",
        },
        {
            "key": "icon",
            "label": "Bootstrap icon",
            "ok": bool(icon) and icon.startswith("bi-"),
            "hard": True,
            "hint": "Use a Bootstrap icon class like bi-robot.",
        },
        {
            "key": "trigger",
            "label": "Frontmatter trigger",
            "ok": bool(trigger),
            "hard": False,
            "hint": "Add a trigger in TOOL.md frontmatter.",
        },
        {
            "key": "display_name",
            "label": "Display name",
            "ok": bool(display_name),
            "hard": False,
            "hint": "Add a human-readable display name.",
        },
        {
            "key": "description",
            "label": "Short description",
            "ok": len(description) >= 10,
            "hard": False,
            "hint": "Add a short description (at least 10 characters).",
        },
    ]

    hard_failures = [item for item in checks if item["hard"] and not item["ok"]]
    soft_failures = [item for item in checks if not item["hard"] and not item["ok"]]

    if hard_failures:
        status = "not_ready"
        label = "Not Ready"
    elif soft_failures:
        status = "almost_ready"
        label = "Almost Ready"
    else:
        status = "ready"
        label = "Ready"

    return {
        "status": status,
        "status_label": label,
        "hard_requirements_met": len(hard_failures) == 0,
        "hard_failures": [item["key"] for item in hard_failures],
        "soft_failures": [item["key"] for item in soft_failures],
        "checks": checks,
        "frontmatter": frontmatter,
    }
