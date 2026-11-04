"""Run one agent call in a child process that is really killed on timeout.

``asyncio.wait_for`` around a worker thread only stops *waiting*: the thread (and its LLM
calls, sandbox subprocesses and token spend) keeps going. Here each call gets its own process,
started with ``forkserver`` where available (``spawn`` otherwise), in its own session/process
group. On timeout, cancellation or after the result arrives, the whole group is SIGKILLed, so
grandchildren such as the sandbox's Python runner die with it.

Constraints: the agent and its input are pickled into the child, so the agent must be a
module-level function or an instance of a module-level class (``core_agent``, ``OracleAgent``,
``module:callable`` specs). Closures need ``isolation="inline"``.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import pickle
import signal
import sys
import traceback
from multiprocessing.connection import Connection
from typing import Any

from analyst_agent.eval.agent import (
    AgentFn,
    AgentInput,
    AgentOutput,
    SpendMeter,
    call_agent,
    metering,
)


class AgentTimeout(TimeoutError):
    """The agent did not answer within the timeout; its process group was killed."""


class AgentCrashed(RuntimeError):
    """The agent process died without sending a result (segfault, OOM kill, os._exit, ...)."""


_JOIN_TIMEOUT_S = 5.0
_PRELOAD = ["analyst_agent.eval.agent", "analyst_agent.core", "analyst_agent.tools"]
_ctx: Any = None


def _context() -> Any:
    global _ctx
    if _ctx is None:
        methods = multiprocessing.get_all_start_methods()
        _ctx = multiprocessing.get_context("forkserver" if "forkserver" in methods else "spawn")
        if "forkserver" in methods:
            # Imported once in the fork server (the anthropic SDK alone takes ~2s), so each
            # task's process starts warm. Imports only: no threads or connections exist yet.
            _ctx.set_forkserver_preload(_PRELOAD)
    return _ctx


def check_picklable(agent: AgentFn) -> None:
    try:
        pickle.dumps(agent)
    except Exception as e:
        raise TypeError(
            f"isolation='process' runs each task in a child process and needs a picklable agent "
            f"(a module-level function or an instance of a module-level class); "
            f"{agent!r} is not picklable ({type(e).__name__}: {e}). "
            f"Use isolation='inline' for closures."
        ) from e


def _apply_env(env: dict[str, str]) -> None:
    """Make the child's environment the parent's environment *at task start*.

    A forkserver child inherits the environment the fork server was started with (the first
    task of the process), not the parent's current one, so AGENT_* changes made later (a test's
    monkeypatch, a second run in the same process) would be silently ignored.
    """
    os.environ.clear()
    os.environ.update(env)
    try:  # drop a Settings object cached during the fork server's preload
        from analyst_agent.core.config import get_settings

        get_settings.cache_clear()
    except ImportError:  # pragma: no cover - core not installed
        pass


def _child_main(
    conn: Connection,
    agent: AgentFn,
    inp: AgentInput,
    env: dict[str, str] | None = None,
    spend: Any = None,
) -> None:
    if hasattr(os, "setsid"):
        os.setsid()  # own process group: the parent can kill everything we spawn at once
    if env is not None:
        _apply_env(env)
    try:
        # Spend reported while the agent runs lands in ``spend`` (shared memory), which the
        # parent reads even if it has to kill this process.
        with metering(SpendMeter(spend)):
            out = asyncio.run(call_agent(agent, inp))
        msg: tuple[str, Any] = ("ok", out)
    except BaseException as e:  # report everything, including SystemExit from agent code
        msg = ("error", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
    try:
        conn.send(msg)
    except Exception as e:  # e.g. an unpicklable field in the agent's output
        conn.send(("error", f"could not send agent result: {type(e).__name__}: {e}"))
    conn.close()
    # Skip interpreter shutdown: it would wait for any non-daemon threads the agent leaked.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _kill(proc: Any) -> None:
    if proc.pid is None:
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # group already gone, or setsid() had not run yet: fall through
    if proc.is_alive():
        proc.kill()


async def _wait_readable(conn: Connection, timeout_s: float) -> bool:
    loop = asyncio.get_running_loop()
    try:
        fut: asyncio.Future[None] = loop.create_future()
        loop.add_reader(conn.fileno(), lambda: fut.done() or fut.set_result(None))
    except NotImplementedError:  # Windows proactor loop: bounded poll in a thread instead
        return await asyncio.to_thread(conn.poll, timeout_s)
    try:
        await asyncio.wait_for(fut, timeout_s)
        return True
    except TimeoutError:
        return False
    finally:
        loop.remove_reader(conn.fileno())


async def run_in_subprocess(
    agent: AgentFn, inp: AgentInput, timeout_s: float, spend: SpendMeter | None = None
) -> AgentOutput:
    """Run ``agent(inp)`` in a fresh child process; kill it (and its children) on timeout.

    Raises :class:`AgentTimeout`, :class:`AgentCrashed`, or ``RuntimeError`` carrying the
    child's traceback if the agent raised. ``spend`` receives the USD the child reported
    (:func:`~analyst_agent.eval.agent.report_spend`), also when it timed out or crashed.
    """
    ctx = _context()
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    env = dict(os.environ)
    shared = ctx.Value("d", 0.0)
    proc = ctx.Process(target=_child_main, args=(send_conn, agent, inp, env, shared), daemon=True)
    try:
        await asyncio.to_thread(proc.start)
        send_conn.close()  # only the child holds the write end now: EOF if it dies
        if not await _wait_readable(recv_conn, timeout_s):
            raise AgentTimeout(f"agent exceeded {timeout_s}s")
        try:
            kind, value = recv_conn.recv()
        except EOFError:
            await asyncio.to_thread(proc.join, _JOIN_TIMEOUT_S)
            raise AgentCrashed(
                f"agent process exited with code {proc.exitcode} without a result"
            ) from None
    finally:
        _kill(proc)
        await asyncio.to_thread(proc.join, _JOIN_TIMEOUT_S)
        recv_conn.close()
        send_conn.close()
        if proc.exitcode is not None:
            proc.close()
        if spend is not None:
            spend.add(shared.value)
    if kind == "error":
        raise RuntimeError(value)
    if not isinstance(value, AgentOutput):
        raise TypeError(f"agent returned {type(value).__name__}, expected AgentOutput")
    return value
