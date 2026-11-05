"""A missing optional extra is one line, never a traceback (docs/fresh-clone-audit.md)."""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys

import pytest

from analyst_agent import extras
from analyst_agent.core import langfuse_export
from analyst_agent.extras import MissingExtraError
from analyst_agent.semantic import embeddings

# Runs in a child interpreter with ``mcp`` made unimportable (a ``None`` entry in sys.modules
# makes ``import mcp`` raise ImportError), so the test works whether or not the extra is here.
NO_MCP = "import sys; sys.modules['mcp'] = None; sys.argv = ['analyst-agent-mcp', '--help']\n"


def _child(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)


def test_message_names_the_extra_and_both_install_commands() -> None:
    err = MissingExtraError("mcp", "the MCP server")
    assert isinstance(err, ImportError) and err.extra == "mcp"
    assert str(err) == (
        "the MCP server needs the optional 'mcp' extra: uv sync --extra mcp "
        "(or pip install 'analyst-agent[mcp]')"
    )


@pytest.mark.parametrize(
    "entry",
    [
        "from analyst_agent.extras import mcp_main; mcp_main()",  # the console script
        "import runpy; runpy.run_module('analyst_agent.mcp_server', run_name='__main__')",
    ],
    ids=["analyst-agent-mcp", "python -m analyst_agent.mcp_server"],
)
def test_mcp_entry_points_without_the_extra_print_one_line(entry: str) -> None:
    out = _child(NO_MCP + entry)
    assert out.returncode == 2, out.stderr
    assert out.stdout == ""
    assert out.stderr == (
        "analyst-agent-mcp: the MCP server needs the optional 'mcp' extra: uv sync --extra mcp "
        "(or pip install 'analyst-agent[mcp]')\n"
    )


def test_importing_mcp_server_without_the_extra_raises_missing_extra() -> None:
    out = _child(
        "import sys; sys.modules['mcp'] = None\n"
        "from analyst_agent.extras import MissingExtraError\n"
        "try:\n    import analyst_agent.mcp_server\n"
        "except MissingExtraError as e:\n    print(e.extra)\n"
    )
    assert out.returncode == 0 and out.stdout == "mcp\n", out.stderr


def test_mcp_main_delegates_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp", reason="needs the 'mcp' extra: uv sync --extra mcp")
    from analyst_agent import mcp_server

    seen: list[object] = []
    monkeypatch.setattr(mcp_server, "main", seen.append)
    extras.mcp_main(["--help"])
    assert seen == [["--help"]]


def test_sentence_transformers_missing_raises_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(MissingExtraError) as info:
        embeddings.load_sentence_transformer("any-model")
    assert info.value.extra == "embeddings" and str(info.value) == embeddings.INSTALL_HINT


def test_langfuse_missing_is_one_warning_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "langfuse", None)
    with caplog.at_level(logging.WARNING, logger=langfuse_export.__name__):
        assert langfuse_export.make_langfuse_client() is None
    [rec] = caplog.records
    assert rec.levelno == logging.WARNING and rec.exc_info is None
    assert "Langfuse tracing needs the optional 'api' extra: uv sync --extra api" in rec.message


@pytest.mark.skipif(importlib.util.find_spec("langfuse") is None, reason="needs the 'api' extra")
def test_langfuse_init_failure_still_logs_the_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import langfuse

    def boom() -> None:
        raise RuntimeError("bad LANGFUSE_HOST")

    monkeypatch.setattr(langfuse, "Langfuse", boom)
    with caplog.at_level(logging.ERROR, logger=langfuse_export.__name__):
        assert langfuse_export.make_langfuse_client() is None
    assert caplog.records[0].exc_info is not None
