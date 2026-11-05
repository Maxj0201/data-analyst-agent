#!/usr/bin/env python3
"""Check that the Markdown docs agree with the repository (links, paths, settings, CLI).

Runs over every tracked ``*.md`` file and reports, as ``path:line: [check] message``:

1. ``link``     a relative link or ``#anchor`` that does not resolve (anchors use GitHub's
                heading slugs, so ``## Interrupted runs (`--resume`)`` is
                ``#interrupted-runs---resume``);
2. ``path``     a file path in backticks (inline code) that exists nowhere in the repository;
3. ``setting``  an ``AGENT_*`` name that is not a field of ``core/config.py:Settings`` (or one
                of the UI-only variables documented in ``docs/configuration.md``);
4. ``cli``      a project command (``analyst-agent``, ``python -m analyst_agent.*``,
                ``scripts/*``, ``make``) whose subcommand does not parse with ``--help`` or
                that passes an option the command does not have;
5. ``todo``     a sentence containing TODO, TBD, "placeholder" or ``{{`` (outside
                ``docs/resume.md`` and ``docs/eval-runbook.md``).

Commands come from ``bash``/``sh``/``shell``/``console`` code blocks and from inline code that
starts with a project command, including the short forms the docs use (``eval run ...``,
``ask --dry-run``, ``trace show``, ``semantic eval``).

``docs/history/`` holds the original session briefs, kept unedited on purpose (CONTRIBUTING.md);
only links are checked there. Dated records may name a path as it was at the time; those are
listed in ``HISTORICAL_PATHS``, each with the file allowed to cite it.

Exit status 1 if anything is found. Offline: no network, no API key, no LLM calls.

    uv run python scripts/check_docs.py            # every tracked Markdown file
    uv run python scripts/check_docs.py README.md  # just these
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# Files exempt from the TODO / placeholder check (check 5): both legitimately carry templates.
TODO_EXEMPT = {"docs/resume.md", "docs/eval-runbook.md"}
# Historical briefs, kept unedited (CONTRIBUTING.md): links only.
HISTORICAL_DIRS = ("docs/history/",)
# Paths that no longer exist but that a dated record cites as they were then: path -> files.
HISTORICAL_PATHS: dict[str, set[str]] = {
    "docs/kickoff/": {"docs/integration-notes.md", "docs/final-review.md", "CHANGELOG.md"},
    "docs/kickoff/*.md": {"docs/integration-notes.md"},
    "api/settings.py": {"docs/integration-notes.md"},
    "eval/config.py": {"docs/integration-notes.md"},
    "docs/interview-notes.md": {
        "docs/integration-notes.md",
        "docs/final-review.md",
        "docs/review-2.md",
    },
    "interview-notes.md": {
        "docs/integration-notes.md",
        "docs/final-review.md",
        "docs/review-2.md",
    },
}
# Environment variables named like settings but read outside Settings (docs/configuration.md,
# "Other environment variables": the Streamlit UI image has no agent package).
NON_SETTINGS_ENV = {"AGENT_API_URL", "AGENT_API_ID_TOKEN"}
# Files that are written at run time or live outside the repository (an MCP client's config):
# fine to mention by name.
OUTPUT_NAMES = {
    "result.json",
    "claude_desktop_config.json",
    ".mcp.json",
    "meta.json",
    "meta.json.tmp",
    "results.jsonl",
    "report.md",
    "summary.md",
    "manifest.tsv",
    "trace.json",
    "compose.log",
    "jobs.sqlite3",
    "memory.sqlite3",
    ".env",
    ".env.cloudrun",
    "metrics.yaml",
}
FILE_EXT = re.compile(
    r"\.(py|md|ya?ml|json|jsonl|toml|lock|txt|csv|tsv|parquet|sqlite3?|duckdb|db|sh|js|png|"
    r"jpe?g|svg|ini|cfg|log|tgz|npz|example)$"
)
SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh"}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.check}] {self.message}"


# ---- Markdown model ----------------------------------------------------------------------------


@dataclass
class Fence:
    lang: str
    start: int  # 1-based line of the first content line
    lines: list[str]


@dataclass
class Doc:
    rel: str
    text: str
    prose: str  # fenced blocks blanked (same offsets as text); inline code kept
    fences: list[Fence] = field(default_factory=list)
    spans: list[tuple[int, str]] = field(default_factory=list)  # (offset, code span content)

    def line_of(self, offset: int) -> int:
        return self.text.count("\n", 0, offset) + 1


FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+-]*)")
# A code span: matching backtick runs, never across a blank line (a paragraph break).
SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.S)


def parse(rel: str, text: str) -> Doc:
    out: list[str] = []
    fences: list[Fence] = []
    cur: Fence | None = None
    marker = ""
    for i, line in enumerate(text.split("\n"), start=1):
        m = FENCE_RE.match(line)
        if cur is None and m:
            marker = m.group(2)
            cur = Fence(m.group(3).lower(), i + 1, [])
            out.append(" " * len(line))
        elif cur is not None:
            if line.strip().startswith(marker[0] * len(marker)) and not line.strip().strip(
                marker[0]
            ):
                fences.append(cur)
                cur = None
            else:
                cur.lines.append(line)
            out.append(" " * len(line))
        else:
            out.append(line)
    if cur is not None:
        fences.append(cur)
    prose = "\n".join(out)
    doc = Doc(rel, text, prose, fences)
    for m in SPAN_RE.finditer(prose):
        content = m.group(2)
        # CommonMark: strip one leading and trailing space when both are present.
        if content.startswith(" ") and content.endswith(" ") and content.strip():
            content = content[1:-1]
        doc.spans.append((m.start(), content.replace("\n", " ")))
    return doc


def strip_spans(doc: Doc) -> str:
    """Prose with inline code replaced by spaces (offsets and newlines preserved)."""

    return SPAN_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), doc.prose)


def github_slug(heading: str) -> str:
    """GitHub's anchor for a heading: rendered text, lowercased, punctuation dropped."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links -> their text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def anchors_of(doc: Doc) -> frozenset[str]:
    """Every ``#anchor`` a document defines: heading slugs (``-1``, ``-2`` for repeats) and
    explicit ``<a name=...>`` / ``id=...`` targets."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    for line in doc.prose.split("\n"):
        m = HEADING_RE.match(line)
        if not m:
            continue
        slug = github_slug(m.group(2))
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    out.update(re.findall(r"""<a\s+(?:name|id)=["']([^"']+)["']""", doc.text))
    return frozenset(out)


@cache
def anchors(rel: str) -> frozenset[str]:
    return anchors_of(load(rel))


@cache
def load(rel: str) -> Doc:
    return parse(rel, (REPO / rel).read_text(encoding="utf-8"))


@cache
def tracked_files() -> tuple[str, ...]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True, text=True
        ).stdout
        files = [f for f in out.split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        skip = {".git", ".venv", "node_modules", "__pycache__"}
        files = [
            p.relative_to(REPO).as_posix()
            for p in REPO.rglob("*")
            if p.is_file() and not skip & set(p.relative_to(REPO).parts)
        ]
    return tuple(f for f in files if (REPO / f).exists())


@cache
def tracked_dirs() -> frozenset[str]:
    dirs: set[str] = set()
    for f in tracked_files():
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return frozenset(dirs)


def markdown_files() -> list[str]:
    return sorted(f for f in tracked_files() if f.endswith(".md"))


def is_historical(rel: str) -> bool:
    return rel.startswith(HISTORICAL_DIRS)


# ---- 1. links and anchors ----------------------------------------------------------------------

LINK_RE = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.M)


def check_links(doc: Doc) -> Iterator[Finding]:
    prose = strip_spans(doc)
    targets = [(m.start(), m.group(1)) for m in LINK_RE.finditer(prose)]
    targets += [(m.start(), m.group(1)) for m in REF_DEF_RE.finditer(prose)]
    targets += [
        (m.start(), m.group(1))
        for m in re.finditer(r"""<(?:a|img)\s[^>]*?(?:href|src)="([^"]+)\"""", prose)
    ]
    for offset, target in targets:
        if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):  # http:, mailto:, ...
            continue
        path_part, _, anchor = target.partition("#")
        path_part = path_part.split("?", 1)[0]
        line = doc.line_of(offset)
        if path_part:
            base = REPO if path_part.startswith("/") else (REPO / doc.rel).parent
            dest = (base / path_part.lstrip("/")).resolve()
            try:
                rel = dest.relative_to(REPO).as_posix()
            except ValueError:
                yield Finding(doc.rel, line, "link", f"{target}: points outside the repository")
                continue
            if not dest.exists():
                yield Finding(doc.rel, line, "link", f"{target}: {rel} does not exist")
                continue
        else:
            rel = doc.rel
        defined = (
            anchors_of(doc) if rel == doc.rel else anchors(rel) if rel.endswith(".md") else None
        )
        if anchor and defined is not None and anchor not in defined:
            yield Finding(doc.rel, line, "link", f"{target}: no heading #{anchor} in {rel}")


# ---- 2. file paths in backticks ----------------------------------------------------------------

PATH_CHARS = re.compile(r"^[\w.\-/]+$")


def path_candidate(span: str) -> str | None:
    """The path a code span names, or None if it is not (or not only) a repository path."""
    s = span.strip()
    if " " in s or not s or s.startswith(("/", "~", "-", "http", "$", ".venv")):
        return None
    s = re.split(r"::|:(?=[A-Za-z_]|\d+$)", s, maxsplit=1)[0]  # file.py:Symbol, test.py::name
    s = s.removeprefix("./")
    if not PATH_CHARS.match(s) or ".." in s or re.match(r"^\.\w{1,5}$", s):
        return None  # elided (`a_..._b.csv`) or just an extension (`.npz`)
    if s.endswith("/"):
        return s if "/" in s[:-1] else None  # a bare `name/` is usually a run-output directory
    if FILE_EXT.search(s) and not re.match(r"^\d", s):
        return s
    return None


@cache
def ignored(path: str) -> bool:
    r = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path], cwd=REPO, capture_output=True
    )
    return r.returncode == 0


def path_exists(path: str, doc_rel: str) -> bool:
    bare = path.rstrip("/")
    roots = [(REPO / doc_rel).parent, REPO, REPO / "src", REPO / "src/analyst_agent"]
    if any((root / bare).exists() for root in roots):
        return True
    files, dirs = tracked_files(), tracked_dirs()
    if "/" not in bare:  # a bare file name: any tracked file of that name
        return any(f.rsplit("/", 1)[-1] == bare for f in files) or any(
            d.rsplit("/", 1)[-1] == bare for d in dirs
        )
    # a path relative to some package or directory (``_child/bootstrap.py``)
    return any(f.endswith("/" + bare) for f in files) or any(d.endswith("/" + bare) for d in dirs)


def check_paths(doc: Doc) -> Iterator[Finding]:
    for offset, span in doc.spans:
        path = path_candidate(span)
        if path is None or path in OUTPUT_NAMES:
            continue
        if doc.rel in HISTORICAL_PATHS.get(path, set()):
            continue
        if path_exists(path, doc.rel) or ignored(path.rstrip("/")) or ignored(path):
            continue
        yield Finding(doc.rel, doc.line_of(offset), "path", f"`{span}`: no such file or directory")


# ---- 3. AGENT_* names --------------------------------------------------------------------------

AGENT_RE = re.compile(r"(?<![A-Za-z0-9_])AGENT_([A-Z0-9_]*)([*<{]?)")


@cache
def settings_env() -> frozenset[str]:
    sys.path.insert(0, str(REPO / "src"))
    from analyst_agent.core.config import Settings

    prefix = Settings.model_config.get("env_prefix", "AGENT_")
    return frozenset(f"{prefix}{name}".upper() for name in Settings.model_fields)


def check_settings(doc: Doc) -> Iterator[Finding]:
    known = settings_env() | NON_SETTINGS_ENV
    seen: set[tuple[int, str]] = set()
    for m in AGENT_RE.finditer(doc.text):
        name, wildcard = "AGENT_" + m.group(1), m.group(2)
        line = doc.line_of(m.start())
        if wildcard or name.endswith("_") and not m.group(1).strip("_"):
            # a pattern (AGENT_*_ENABLED, AGENT_MODEL_L*, AGENT_<FEATURE>_ENABLED)
            if any(k.startswith(name) for k in known):
                continue
        elif name.rstrip("_") in known:
            continue
        if (line, name) not in seen:
            seen.add((line, name))
            yield Finding(doc.rel, line, "setting", f"{name}{wildcard}: not a Settings field")


# ---- 4. CLI commands ---------------------------------------------------------------------------


@dataclass
class Command:
    line: int
    text: str
    tokens: list[str]


ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
UV_RUN_VALUE_OPTS = {"--extra", "--with", "--directory", "--group", "--python", "-p", "--project"}
SHORTHAND = {
    "eval": ["python", "-m", "analyst_agent.eval"],
    "ask": ["analyst-agent", "ask"],
    "trace": ["analyst-agent", "trace"],
    "semantic": ["python", "-m", "analyst_agent.semantic"],
    "judge-calibrate": ["python", "-m", "analyst_agent.eval", "judge-calibrate"],
}


def split_commands(text: str) -> Iterator[list[str]]:
    """Simple commands of a shell line (split at && || ; |), tokenised, comments dropped."""
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        lex.commenters = "#"
        tokens = list(lex)
    except ValueError:
        tokens = text.split()
    cmd: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= set(";&|"):
            if cmd:
                yield cmd
            cmd = []
        else:
            cmd.append(tok)
    if cmd:
        yield cmd


def normalise(tokens: list[str]) -> list[str] | None:
    """``[prog..., args...]`` for a project command, else None."""
    t = list(tokens)
    while t and (ENV_ASSIGN.match(t[0]) or t[0] in {"env", "time", "exec"}):
        t.pop(0)
    if t[:1] == ["uv"]:
        t.pop(0)
        while t and t[0].startswith("-"):  # uv --directory DIR run ...
            t = t[2:] if t[0] in UV_RUN_VALUE_OPTS else t[1:]
        if t[:1] != ["run"]:
            return None
        t.pop(0)
        while t and t[0].startswith("-"):
            t = t[2:] if t[0] in UV_RUN_VALUE_OPTS else t[1:]
    if t and t[0].startswith(".venv/bin/"):
        t[0] = t[0].removeprefix(".venv/bin/")
    if t[:1] == ["bash"] and len(t) > 1:
        t.pop(0)
    t = [tok for tok in t if tok.strip(".…")]  # `analyst-agent ...`: an ellipsis, not an argument
    if not t:
        return None
    if t[0] not in {"uv", "python", "python3", "make"} and "--" in t:
        # a wrapper that runs a project command: `claude mcp add analyst -- uv run ...`
        return normalise(t[t.index("--") + 1 :])
    head = t[0].removeprefix("./")
    if head in {"python", "python3"}:
        if len(t) > 2 and t[1] == "-m" and t[2].startswith("analyst_agent"):
            return ["python", "-m", *t[2:]]
        if len(t) > 1 and t[1].startswith("scripts/"):
            return t[1:]
        return None
    if head in {"analyst-agent", "analyst-agent-mcp", "make"}:
        return [head, *t[1:]]
    if head in script_names():
        return [script_names()[head], *t[1:]]
    if head.startswith("scripts/") and not head.endswith("/") and "*" not in head:
        return [head, *t[1:]]
    return None


@cache
def script_names() -> dict[str, str]:
    """``run_experiments.sh`` -> ``scripts/run_experiments.sh`` for executable scripts."""
    out: dict[str, str] = {}
    for f in tracked_files():
        if f.startswith("scripts/") and f.endswith((".sh", ".py")):
            out.setdefault(f.rsplit("/", 1)[-1], f)
            out[f] = f
    return out


def shell_lines(fence: Fence) -> Iterator[tuple[int, str]]:
    buf, start = "", 0
    for i, raw in enumerate(fence.lines):
        line = re.sub(r"^\s*\$ ", "", raw)
        if not buf:
            start = fence.start + i
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        buf += line
        if buf.strip():
            yield start, buf
        buf = ""
    if buf.strip():
        yield start, buf


def commands(doc: Doc) -> Iterator[Command]:
    heredoc: str | None = None
    for fence in doc.fences:
        if fence.lang not in SHELL_LANGS:
            continue
        for line, text in shell_lines(fence):
            if heredoc:
                if text.strip() == heredoc:
                    heredoc = None
                continue
            m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", text)
            if m:
                heredoc = m.group(1)
            for toks in split_commands(text):
                norm = normalise(toks)
                if norm:
                    yield Command(line, text.strip(), norm)
    for offset, span in doc.spans:
        for toks in split_commands(span):
            norm = normalise(toks)
            if norm is None and toks[0] in SHORTHAND and len(toks) > 1:
                if re.match(r"^(-|[a-z][a-z-]*$)", toks[1]):
                    norm = SHORTHAND[toks[0]] + toks[1:]
            if norm:
                yield Command(doc.line_of(offset), span, norm)


# Programs -> how to introspect them.
TYPER_APPS = {
    "analyst-agent": ("analyst_agent.cli", "app"),
    "python -m analyst_agent.cli": ("analyst_agent.cli", "app"),
    "python -m analyst_agent.eval": ("analyst_agent.eval.cli", "app"),
    "python -m analyst_agent.eval.preflight": ("analyst_agent.eval.preflight", "app"),
    "python -m analyst_agent.eval.experiments": ("analyst_agent.eval.experiments", "app"),
    "python -m analyst_agent.semantic": ("analyst_agent.semantic.__main__", "app"),
}
ARGPARSE_APPS = {
    "analyst-agent-mcp": ("analyst_agent.mcp_server", "_parser"),
    "python -m analyst_agent.mcp_server": ("analyst_agent.mcp_server", "_parser"),
}
# Shell wrappers whose options are those of a Python entry point.
SCRIPT_DELEGATES = {"scripts/preflight.sh": "python -m analyst_agent.eval.preflight"}
# Scripts that pass their extra arguments to another program (`docker run`).
SCRIPT_PASSTHROUGH = {"scripts/sandbox-probes/run.sh"}


def option_tokens(args: Iterable[str]) -> Iterator[str]:
    for a in args:
        if not a.startswith("-") or a in {"-", "--"} or re.match(r"^-\d", a):
            continue
        for part in a.rstrip("/").split("/") if "/" in a else [a]:
            if part.startswith("-"):
                yield part.split("=", 1)[0]


@dataclass
class Checked:
    ok: bool
    message: str = ""


@cache
def typer_app(prog: str) -> Any:  # a typer.Typer
    module, attr = TYPER_APPS[prog]
    return getattr(importlib.import_module(module), attr)


@cache
def click_root(prog: str) -> Any:  # a click Command (typer vendors click as typer._click)
    import typer

    return typer.main.get_command(typer_app(prog))


def _options(cmd: Any) -> dict[str, Any]:
    table: dict[str, Any] = {"--help": None}
    for p in cmd.params:
        for o in [*p.opts, *getattr(p, "secondary_opts", [])]:
            if o.startswith("-"):
                table[o] = p
    return table


def check_click(prog: str, args: list[str]) -> Checked:
    from typer.testing import CliRunner

    cmd = click_root(prog)
    path: list[str] = []
    allowed: set[str] = set()
    rest = list(args)
    while True:
        table = _options(cmd)
        allowed |= set(table)
        if not hasattr(cmd, "commands"):  # a leaf command, not a group
            break
        while rest and rest[0].startswith("-"):
            tok, eq, _ = rest.pop(0).partition("=")
            if tok not in table:
                return Checked(False, f"{' '.join([prog, *path])}: no option {tok}")
            p = table[tok]
            takes_value = p is not None and not getattr(p, "is_flag", True)
            if takes_value and not eq and rest:
                rest.pop(0)
        if not rest:
            break
        sub = cmd.commands.get(rest[0])
        if sub is None:
            return Checked(False, f"{' '.join([prog, *path])}: no command {rest[0]!r}")
        path.append(rest.pop(0))
        cmd = sub
    result = CliRunner().invoke(typer_app(prog), [*path, "--help"])
    if result.exit_code != 0:
        return Checked(False, f"{' '.join([prog, *path, '--help'])} exited {result.exit_code}")
    bad = sorted({o for o in option_tokens(rest) if o not in allowed})
    if bad:
        return Checked(False, f"{' '.join([prog, *path])}: no option {', '.join(bad)}")
    return Checked(True)


def check_argparse(prog: str, args: list[str]) -> Checked:
    module, attr = ARGPARSE_APPS[prog]
    parser = getattr(importlib.import_module(module), attr)()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(["--help"])
        except SystemExit as exc:
            if exc.code not in (0, None):
                return Checked(False, f"{prog} --help exited {exc.code}")
    known = set(parser._option_string_actions)
    bad = [o for o in option_tokens(args) if o not in known]
    if bad:
        return Checked(False, f"{prog}: no option {', '.join(sorted(set(bad)))}")
    return Checked(True)


@cache
def script_help(script: str) -> tuple[int, str]:
    runner = [sys.executable] if script.endswith(".py") else ["bash"]
    r = subprocess.run(
        [*runner, script, "--help"], cwd=REPO, capture_output=True, text=True, timeout=120
    )
    return r.returncode, r.stdout + r.stderr


def check_script(script: str, args: list[str]) -> Checked:
    path = REPO / script
    if not path.is_file():
        return Checked(False, f"{script}: no such script")
    source = path.read_text(encoding="utf-8")
    if script in SCRIPT_DELEGATES:
        code, _ = script_help(script)
        if code != 0:
            return Checked(False, f"{script} --help exited {code}")
        return check_command([*SCRIPT_DELEGATES[script].split(), *args])
    if script in SCRIPT_PASSTHROUGH:
        return Checked(True)
    if "--help" in source:
        code, text = script_help(script)
        if code != 0:
            return Checked(False, f"{script} --help exited {code}")
    else:
        # No --help (scripts/demo.sh starts Docker): its options must appear in the source.
        text = source
    bad = [
        o for o in option_tokens(args) if not re.search(rf"(?<![\w-]){re.escape(o)}(?![\w-])", text)
    ]
    if bad:
        return Checked(False, f"{script}: no option {', '.join(sorted(set(bad)))}")
    return Checked(True)


@cache
def make_targets() -> frozenset[str]:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    return frozenset(re.findall(r"^([A-Za-z0-9_.-]+):(?!=)", text, re.M))


def check_make(args: list[str]) -> Checked:
    bad = [a for a in args if not a.startswith("-") and "=" not in a and a not in make_targets()]
    if bad:
        return Checked(False, f"make: no target {', '.join(bad)}")
    return Checked(True)


def check_command(tokens: list[str]) -> Checked:
    head = tokens[0]
    if head == "python":
        prog, args = " ".join(tokens[:3]), tokens[3:]
    else:
        prog, args = head, tokens[1:]
    if prog in TYPER_APPS:
        return check_click(prog, args)
    if prog in ARGPARSE_APPS:
        return check_argparse(prog, args)
    if head == "make":
        return check_make(args)
    if head.startswith("scripts/"):
        return check_script(head, args)
    return Checked(False, f"{prog}: not a known project command")


def check_cli(doc: Doc) -> Iterator[Finding]:
    for cmd in commands(doc):
        result = check_command(cmd.tokens)
        if not result.ok:
            yield Finding(doc.rel, cmd.line, "cli", f"`{cmd.text}`: {result.message}")


# ---- 5. TODO / placeholder ---------------------------------------------------------------------

TODO_RE = re.compile(r"\bTODO\b|\bTBD\b|placeholder|\{\{", re.I)


def check_todo(doc: Doc) -> Iterator[Finding]:
    """Prose only: fenced code is code (mermaid's ``{{`` is a hexagon). A word in inline code
    (`` `TODO` ``) is a mention, not a marker; ``{{`` in inline code is still a template slot."""
    if doc.rel in TODO_EXEMPT:
        return
    no_spans = strip_spans(doc)
    for m in TODO_RE.finditer(doc.prose):
        if m.group(0) != "{{" and not no_spans[m.start() : m.end()].strip():
            continue
        start = max(doc.text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
        ends = [i for i in (doc.text.find(c, m.end()) for c in ".!?\n") if i != -1]
        sentence = " ".join(doc.text[start : min(ends, default=len(doc.text))].split())
        yield Finding(doc.rel, doc.line_of(m.start()), "todo", f"{m.group(0)!r} in: {sentence}")


# ---- driver ------------------------------------------------------------------------------------

CHECKS: dict[str, Callable[[Doc], Iterator[Finding]]] = {
    "link": check_links,
    "path": check_paths,
    "setting": check_settings,
    "cli": check_cli,
    "todo": check_todo,
}
LINKS_ONLY = {"link"}


IGNORE_MARK = "<!-- check-docs: ignore -->"


def ignored_lines(doc: Doc) -> set[int]:
    """Lines that carry the ignore marker, and the line after a marker on its own line."""
    out: set[int] = set()
    for i, line in enumerate(doc.text.split("\n"), start=1):
        if IGNORE_MARK in line:
            out.add(i)
            if line.strip() == IGNORE_MARK:
                out.add(i + 1)
    return out


def run(files: Iterable[str], checks: Iterable[str] = CHECKS) -> list[Finding]:
    findings: list[Finding] = []
    for rel in files:
        doc = load(rel)
        skip = ignored_lines(doc)
        for name in checks:
            if is_historical(rel) and name not in LINKS_ONLY:
                continue
            findings.extend(f for f in CHECKS[name](doc) if f.line not in skip)
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="Markdown files (default: every tracked *.md)")
    ap.add_argument(
        "--check", action="append", choices=sorted(CHECKS), help="run only these (repeatable)"
    )
    ns = ap.parse_args(argv)
    files = [Path(f).resolve().relative_to(REPO).as_posix() for f in ns.files] or markdown_files()
    findings = run(files, ns.check or list(CHECKS))
    for f in findings:
        print(f)
    n = len(files)
    if findings:
        print(f"check_docs: {len(findings)} finding(s) in {n} Markdown file(s)", file=sys.stderr)
        return 1
    print(f"check_docs: {n} Markdown file(s) OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
