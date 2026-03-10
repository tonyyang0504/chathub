import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class SkillDefinition:
    def __init__(
        self,
        name,
        display_name,
        description,
        instructions,
        requires_os=None,
        requires_binaries=None,
        requires_env=None,
        user_invocable=False,
        auto_activate=False,
        tools=None,
        tier="bundled",
        path="",
    ):
        self.name = name
        self.display_name = display_name
        self.description = description
        self.instructions = instructions
        self.requires_os = requires_os or []
        self.requires_binaries = requires_binaries or []
        self.requires_env = requires_env or []
        self.user_invocable = user_invocable
        self.auto_activate = auto_activate
        self.tools = tools or []
        self.tier = tier
        self.path = path


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}

    def load_all(self, workspace_path: str = None):
        """Load skills from 3 tiers (workspace wins on name collision):
        1. Bundled: app/chathub_agent/skills/bundled/*/SKILL.md
        2. Managed: ~/.chathub-agent/skills/*/SKILL.md
        3. Workspace: {workspace}/.chathub-agent/skills/*/SKILL.md
        """
        bundled_dir = Path(__file__).parent / "bundled"
        self._load_tier(bundled_dir, "bundled")

        managed_dir = Path.home() / ".chathub-agent" / "skills"
        if managed_dir.exists():
            self._load_tier(managed_dir, "managed")

        if workspace_path:
            ws_dir = Path(workspace_path) / ".chathub-agent" / "skills"
            if ws_dir.exists():
                self._load_tier(ws_dir, "workspace")

    def _load_tier(self, base_dir: Path, tier: str):
        if not base_dir.exists():
            return
        for skill_dir in base_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            try:
                skill = self._parse_skill_file(skill_file, tier)
                if skill:
                    self._skills[skill.name] = skill
            except Exception as e:
                logger.warning(f"Failed to load skill from {skill_file}: {e}")

    def _parse_skill_file(self, path: Path, tier: str) -> SkillDefinition | None:
        content = path.read_text()
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        frontmatter = yaml.safe_load(parts[1])
        instructions = parts[2].strip()
        return SkillDefinition(
            name=frontmatter.get("name", path.parent.name),
            display_name=frontmatter.get("display_name", frontmatter.get("name", "")),
            description=frontmatter.get("description", ""),
            instructions=instructions,
            requires_os=frontmatter.get("requires_os"),
            requires_binaries=frontmatter.get("requires_binaries"),
            requires_env=frontmatter.get("requires_env"),
            user_invocable=frontmatter.get("user_invocable", False),
            auto_activate=frontmatter.get("auto_activate", False),
            tools=frontmatter.get("tools"),
            tier=tier,
            path=str(path),
        )

    def get_all(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def get_skill(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def get_system_prompt_injection(self, enabled_skills: list[str] = None) -> str:
        parts = []
        for skill in self._skills.values():
            parts.append(f"## Skill: {skill.display_name}\n{skill.instructions}")
        return "\n\n".join(parts)

    def get_slash_commands(self) -> list[dict]:
        return [
            {"name": s.name, "display_name": s.display_name, "description": s.description}
            for s in self._skills.values()
            if s.user_invocable
        ]
