"""Real API + AgentLoop + sandbox + ApprovalBroker; only the LLM is scripted (dry_run_llm).

uvicorn --app-dir <this dir> dryrun_api:app
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import analyst_agent.core as core
from analyst_agent.api import agent as api_agent
from analyst_agent.api.app import create_app
from analyst_agent.core.fake_llm import dry_run_llm
from analyst_agent.tools import create_sandbox, table_name_for

_local = threading.local()


def _scripted_llm(settings: Any) -> Any:
    return dry_run_llm(_local.question, _local.table, _local.sb.run_sql)


def dry_runner(*, dataset_path: Path, question: str, emit: Any, approver: Any = None):
    name = table_name_for(dataset_path)
    with create_sandbox({name: dataset_path}) as sb:  # planning sandbox for the script
        _local.question, _local.table, _local.sb = question, name, sb
        return api_agent.core_runner(
            dataset_path=dataset_path, question=question, emit=emit, approver=approver
        )


# core_runner resolves LLMClient from analyst_agent.core at call time.
core.LLMClient = _scripted_llm
app = create_app(runner=dry_runner)
