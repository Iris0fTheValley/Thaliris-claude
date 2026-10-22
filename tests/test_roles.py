from __future__ import annotations

import json
from pathlib import Path
import re

from thaliris_claude import cli, roles

ROOT = Path(__file__).resolve().parents[1]


def _frontmatter(text: str) -> dict[str, str]:
    block = text.split("---\n", 2)[1]
    values = {}
    for line in block.strip().splitlines():
        key, _, value = line.partition(": ")
        # Plain YAML scalars may not contain ": "; generated values are JSON-quoted.
        assert not (": " in value and not value.startswith('"')), line
        values[key] = json.loads(value) if value.startswith('"') else value
    return values


def test_generated_files_match_canonical_roles():
    assert cli._render(check=True) == {"ok": True, "drift": []}


def test_agent_frontmatter_binds_execution_facts_only():
    for role in roles.child_roles():
        binding = roles.get_binding(role)
        meta = _frontmatter((ROOT / f"agents/{binding.agent_name}.md").read_text(encoding="utf-8"))
        assert meta["name"] == role and meta["description"] == binding.description
        # Plugin agents drop these fields in Claude Code; never rely on them.
        assert not {"permissionMode", "hooks", "mcpServers"} & set(meta)
        if not roles.delegation_allowed(role, "investigator"):
            assert "Agent" in meta.get("disallowedTools", "")


def test_reviewer_is_non_writing_and_only_delegates_scanner():
    binding = roles.get_binding("reviewer")
    assert not binding.repo_write_allowed
    assert {"Edit", "Write", "MultiEdit", "NotebookEdit"} <= set(binding.disallowed_tools)
    assert binding.allowed_delegation_targets == frozenset({"investigator"})
    assert roles.role_for_agent_type("thaliris:reviewer") == "reviewer"
    assert roles.role_for_agent_type("reviewer") is None  # bare names are generic agents


# Assert protocol-bearing tokens (commands, markers, report headings, agent types),
# never prose wording: wording edits must not break CI (history H084).
def test_child_prompts_carry_the_protocol_tokens():
    for role in roles.child_roles():
        text = roles.get_role(role).instructions
        for token in ("observe --json", "**Conclusion**", "**Decision-changing unknowns**", "**Verification performed**"):
            assert token in text, (role, token)
        assert ("thaliris:investigator" in text) == roles.delegation_allowed(role, "investigator")


def test_children_do_not_choose_successors():
    """H143: routing after a result belongs to the Controller."""
    for role in roles.child_roles():
        text = roles.get_role(role).instructions.casefold()
        assert "fresh ordinary implementer" not in text, role


def test_controller_skill_carries_the_protocol_tokens():
    skill = (ROOT / "skills/thaliris/SKILL.md").read_text(encoding="utf-8")
    meta = _frontmatter(skill)
    assert meta["name"] == "thaliris" and "main-thread" in meta["description"]
    for token in ("thaliris-evidence:", "task-start", "register", "judge", "dispose", "close", "guidance --section recovery",
                  "execution_constraint", "delegated", "controller-direct", "single-agent"):
        assert token in skill, token
    assert re.search(r"\| Reviewer \| `thaliris:reviewer` \| `inherit` \| no \| investigator \|", skill)
    assert (ROOT / "skills/thaliris/recovery.md").is_file()
