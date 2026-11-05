"""scripts/check_docs.py: the Markdown link / path / setting / CLI / TODO checker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_docs", REPO / "scripts/check_docs.py")
assert _spec and _spec.loader
cd = importlib.util.module_from_spec(_spec)
sys.modules["check_docs"] = cd
_spec.loader.exec_module(cd)


def doc(text: str, rel: str = "docs/_probe.md") -> cd.Doc:
    return cd.parse(rel, text)


def messages(check, text: str, rel: str = "docs/_probe.md") -> list[str]:
    return [f.message for f in check(doc(text, rel))]


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Interrupted runs (`--resume`)", "interrupted-runs---resume"),
        ("Judge prompt v2 (`rubric_judge@2.0.0`)", "judge-prompt-v2-rubric_judge200"),
        ("Round 5: #23, #22 (2026-11-04)", "round-5-23-22-2026-11-04"),
        ("Audit of docs/review.md (round 4)", "audit-of-docsreviewmd-round-4"),
        ("Tomorrow's run, step by step", "tomorrows-run-step-by-step"),
        ("Embedding and hybrid: not measured yet", "embedding-and-hybrid-not-measured-yet"),
        ("[Linked](x.md) heading", "linked-heading"),
    ],
)
def test_github_slug(heading: str, slug: str) -> None:
    assert cd.github_slug(heading) == slug


def test_repeated_headings_get_numbered_anchors() -> None:
    d = doc("# T\n\n## Still open\n\n## Still open\n\n```\n## not a heading\n```\n")
    assert cd.anchors_of(d) == {"t", "still-open", "still-open-1"}


def test_links_resolve_files_and_anchors() -> None:
    text = (
        "[ok](configuration.md#feature-flags) [self](#t) [ext](https://x.example/nope)\n"
        "[gone](nope.md) [bad](configuration.md#nope) [selfbad](#nope)\n"
        "[wrapped\ntext](../README.md#no-such) ![img](assets/ui-approve-cost.png)\n"
        "`[in code](nope.md)`\n\n```\n[in a fence](nope.md)\n```\n\n# T\n"
    )
    found = messages(cd.check_links, text)
    assert found == [
        "nope.md: docs/nope.md does not exist",
        "configuration.md#nope: no heading #nope in docs/configuration.md",
        "#nope: no heading #nope in docs/_probe.md",
        "../README.md#no-such: no heading #no-such in README.md",
    ]


def test_finding_lines_count_fenced_lines() -> None:
    d = doc("```bash\na\nb\n```\n[gone](nope.md)\n")
    assert [f.line for f in cd.check_links(d)] == [5]


@pytest.mark.parametrize(
    ("span", "path"),
    [
        ("core/config.py:Settings", "core/config.py"),
        ("tests/test_cli.py::test_x", "tests/test_cli.py"),
        ("benchmarks/metrics/", "benchmarks/metrics/"),
        ("vocabulary.yaml", "vocabulary.yaml"),
        ("pass | revise", None),
        ("L1/L2/L3", None),
        ("/app/data", None),
        ("runs/<timestamp>/", None),
        ("vendor_payments__NOTE_..._0.csv", None),
        (".npz", None),
        ("sonnet/", None),
        ("--data", None),
    ],
)
def test_path_candidate(span: str, path: str | None) -> None:
    assert cd.path_candidate(span) == path


def test_paths_must_exist_unless_ignored_or_output() -> None:
    text = (
        "`core/loop.py` `_child/bootstrap.py` `scripts/ui-e2e/README.md` `vocabulary.yaml`\n"
        "`runs/summary.md` `meta.json` `core/nope.py` `tests/test_nope.py::t` `nope.yaml`\n"
    )
    assert messages(cd.check_paths, text) == [
        "`core/nope.py`: no such file or directory",
        "`tests/test_nope.py::t`: no such file or directory",
        "`nope.yaml`: no such file or directory",
    ]


def test_historical_paths_only_in_their_dated_records() -> None:
    text = "`docs/interview-notes.md`\n"
    assert messages(cd.check_paths, text, "docs/integration-notes.md") == []
    assert messages(cd.check_paths, text, "README.md") == [
        "`docs/interview-notes.md`: no such file or directory"
    ]


def test_settings_names() -> None:
    text = (
        "AGENT_MODEL, `AGENT_MODEL_L1`, AGENT_*_ENABLED, AGENT_MODEL_L*, AGENT_<FEATURE>_ENABLED,\n"
        "AGENT_API_URL, `AGENT_NOPE`, AGENT_FOO_ENABLED, AGENT_ROUTING_ENABLED=true\n"
    )
    assert messages(cd.check_settings, text) == [
        "AGENT_NOPE: not a Settings field",
        "AGENT_FOO_ENABLED: not a Settings field",
    ]


@pytest.mark.parametrize(
    "tokens",
    [
        ["analyst-agent", "ask", "--dry-run", "--data", "x.csv", "q", "--routing/--no-routing"],
        ["analyst-agent", "trace", "show", "run_1", "--compact"],
        ["python", "-m", "analyst_agent.eval", "run", "--agent", "oracle", "--fail-under", "1.0"],
        ["python", "-m", "analyst_agent.semantic", "eval", "-b", "bm25", "--no-rerank"],
        ["analyst-agent-mcp", "--data", "x.csv", "--no-inline-images"],
        ["scripts/run_experiments.sh", "--print-commands"],
        ["scripts/preflight.sh", "--limit", "8", "--batch"],
        ["scripts/sandbox-probes/run.sh", "img", "--cap-drop", "ALL"],
        ["make", "lint", "RUNS=/tmp/x"],
        ["analyst-agent"],
    ],
)
def test_cli_commands_that_parse(tokens: list[str]) -> None:
    assert cd.check_command(tokens).ok, cd.check_command(tokens).message


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        (["analyst-agent", "ask", "--bogus"], "analyst-agent ask: no option --bogus"),
        (["analyst-agent", "frob"], "analyst-agent: no command 'frob'"),
        (["python", "-m", "analyst_agent.eval", "nope"], "no command 'nope'"),
        (["python", "-m", "analyst_agent.eval.runner"], "not a known project command"),
        (["analyst-agent-mcp", "--zap"], "analyst-agent-mcp: no option --zap"),
        (["scripts/run_experiments.sh", "--zz"], "scripts/run_experiments.sh: no option --zz"),
        (["scripts/preflight.sh", "--zz"], "python -m analyst_agent.eval.preflight: no option"),
        (["scripts/nope.sh"], "scripts/nope.sh: no such script"),
        (["make", "nonesuch"], "make: no target nonesuch"),
    ],
)
def test_cli_commands_that_do_not(tokens: list[str], message: str) -> None:
    result = cd.check_command(tokens)
    assert not result.ok
    assert message in result.message


def test_commands_come_from_shell_fences_and_inline_code() -> None:
    text = (
        "Run `eval run --budget-usd 2` or `ask --dry-run`; `eval` alone is a word, `make` is not.\n"
        "`analyst-agent ...` `scripts/run_experiments.sh ... --batch`\n"
        "`claude mcp add a -- uv --directory /d run --extra mcp analyst-agent-mcp --data x`\n\n"
        "```bash\n"
        "AGENT_X=1 uv run --extra mcp python -m analyst_agent.semantic eval \\\n  -b bm25\n"
        "cat > f <<'EOF'\nanalyst-agent nope\nEOF\n"
        "git status && make lint  # comment\n"
        "```\n\n```text\nanalyst-agent ignored\n```\n"
    )
    got = [c.tokens for c in cd.commands(doc(text))]
    assert got == [
        ["python", "-m", "analyst_agent.semantic", "eval", "-b", "bm25"],
        ["make", "lint"],
        ["python", "-m", "analyst_agent.eval", "run", "--budget-usd", "2"],
        ["analyst-agent", "ask", "--dry-run"],
        ["make"],  # parses: the default target
        ["analyst-agent"],  # the ellipsis is dropped
        ["scripts/run_experiments.sh", "--batch"],
        ["analyst-agent-mcp", "--data", "x"],
    ]


def test_todo_and_placeholders() -> None:
    text = (
        "Done.\nThis is TBD for now. A placeholder here.\n```mermaid\nA{{hex}}\n```\n"
        "Flags `TODO` and `placeholder` (mentions). Fill `{{ACC}}` (a slot).\n"
    )
    assert [(f.line, f.message.split(" in:")[0]) for f in cd.check_todo(doc(text))] == [
        (2, "'TBD'"),
        (2, "'placeholder'"),
        (6, "'{{'"),
    ]
    assert list(cd.check_todo(doc(text, "docs/resume.md"))) == []
    assert list(cd.check_todo(doc(text, "docs/eval-runbook.md"))) == []


def test_ignore_marker_and_historical_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "A TODO here <!-- check-docs: ignore -->\n<!-- check-docs: ignore -->\nTBD\nTBD\n"
    docs = {
        "docs/_probe.md": doc(text),
        "docs/history/x.md": doc("TBD [x](nope.md)\n", "docs/history/x.md"),
    }
    monkeypatch.setattr(cd, "load", docs.__getitem__)
    assert [(f.path, f.line) for f in cd.run(docs)] == [
        ("docs/_probe.md", 4),
        ("docs/history/x.md", 1),  # links only: the broken link, not the TBD
    ]
