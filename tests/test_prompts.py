"""Prompt versioning: every prompt has a version, and text changes must bump it."""

from __future__ import annotations

from analyst_agent.core import prompts
from analyst_agent.core.prompts import PROMPTS, prompt_versions

# When you edit a prompt: bump its version in core/prompts.py, then update its line here.
PINNED = {
    "planner": ("1.0.0", "08761e9f772b"),
    "critic": ("1.0.0", "bcb25459552e"),
    "report_writer": ("1.0.0", "7f3d5ed6c379"),
    "semantic_layer_rules": ("1.0.0", "d2ab91bc9847"),
    "router": ("1.0.0", "333c0c32c72c"),
}


def test_prompt_text_changes_require_a_version_bump() -> None:
    current = {name: (p.version, p.sha256) for name, p in PROMPTS.items()}
    for name, (version, sha) in PINNED.items():
        cur_version, cur_sha = current[name]
        if cur_sha != sha:
            assert cur_version != version, (
                f"prompt {name!r} text changed ({sha} -> {cur_sha}) but its version is still "
                f"{version}: bump it in core/prompts.py and update PINNED in this test"
            )
    assert current == PINNED, "update PINNED in tests/test_prompts.py to the new versions"


def test_prompt_objects_wrap_the_system_constants() -> None:
    assert prompts.PLANNER.text is prompts.PLANNER_SYSTEM
    assert prompts.CRITIC.text is prompts.CRITIC_SYSTEM
    assert prompts.REPORT_WRITER.text is prompts.REPORT_WRITER_SYSTEM
    assert prompts.SEMANTIC_LAYER.text is prompts.SEMANTIC_LAYER_RULES
    assert prompts.ROUTER.text is prompts.ROUTER_SYSTEM


def test_semantic_layer_planner_prompt_is_planner_plus_versioned_rules() -> None:
    assert prompts.planner_system(False) is prompts.PLANNER_SYSTEM
    assert (
        prompts.planner_system(True) == f"{prompts.PLANNER_SYSTEM}\n{prompts.SEMANTIC_LAYER_RULES}"
    )


def test_prompt_versions_metadata_shape() -> None:
    v = prompt_versions()
    assert set(v) == set(PINNED)
    assert all(set(x) == {"version", "sha256"} and len(x["sha256"]) == 12 for x in v.values())
