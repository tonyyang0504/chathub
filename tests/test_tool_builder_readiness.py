from app.tools.readiness import build_publish_readiness, parse_skill_frontmatter


def test_parse_skill_frontmatter_extracts_required_fields():
    skill_md = """---
name: demo-tool
display_name: Demo Tool
description: A demo tool for testing
icon: bi-robot
trigger: demo
---

# System Prompt
You are a demo assistant.
"""

    fields = parse_skill_frontmatter(skill_md)

    assert fields["name"] == "demo-tool"
    assert fields["display_name"] == "Demo Tool"
    assert fields["icon"] == "bi-robot"
    assert fields["trigger"] == "demo"


def test_readiness_is_not_ready_when_hard_requirements_fail():
    readiness = build_publish_readiness(
        changed_files=["app/tools/routes.py"],
        metadata={
            "name": "Bad Name",
            "display_name": "Bad Name",
            "description": "short",
            "icon": "robot",
        },
        skill_md_content=None,
    )

    assert readiness["status"] == "not_ready"
    assert readiness["hard_requirements_met"] is False
    assert "skill_file" in readiness["hard_failures"]
    assert "name" in readiness["hard_failures"]
    assert "icon" in readiness["hard_failures"]
    assert "trigger" in readiness["hard_failures"]


def test_readiness_is_almost_ready_when_only_soft_requirements_fail():
    skill_md = """---
name: demo-tool
display_name: Demo Tool
description: short
icon: bi-stars
trigger: demo
---
"""

    readiness = build_publish_readiness(
        changed_files=["SKILL.md"],
        metadata={},
        skill_md_content=skill_md,
    )

    assert readiness["status"] == "almost_ready"
    assert readiness["hard_requirements_met"] is True
    assert readiness["soft_failures"] == ["description"]


def test_readiness_is_ready_when_all_checks_pass():
    readiness = build_publish_readiness(
        changed_files=["tools/my_tool/SKILL.md", "app/tools/routes.py"],
        metadata={
            "name": "demo-tool",
            "display_name": "Demo Tool",
            "description": "This tool validates publish readiness.",
            "icon": "bi-check2-circle",
        },
        skill_md_content="""---
name: demo-tool
display_name: Demo Tool
description: This tool validates publish readiness.
icon: bi-check2-circle
trigger: demo
---
""",
    )

    assert readiness["status"] == "ready"
    assert readiness["hard_requirements_met"] is True
    assert readiness["hard_failures"] == []
    assert readiness["soft_failures"] == []
