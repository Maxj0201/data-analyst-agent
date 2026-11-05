<!-- Fill every section; delete a line only if it truly does not apply. Rules: CLAUDE.md, CONTRIBUTING.md -->

## What changed

<!-- What and why, in a few sentences. Branch name should follow the task (feat/..., fix/..., docs/..., chore/...). -->

## How I verified it

<!-- Commands and results, e.g. `make check` -> passed. Do not paste secrets or API keys. -->

## Edits outside my area

<!-- CLAUDE.md: stay inside your package (core / tools / sandbox / eval / api / semantic). List any file you had to touch outside it and why, or write "none". -->

## Checklist

- [ ] `make check` passes (what CI runs: lint, format check, tests, coverage gate, offline eval gate)
- [ ] Tests mock the LLM (`FakeLLM`); nothing calls the real API
- [ ] All LLM calls go through `core/llm.py`
- [ ] New `AGENT_*` setting: field on `core/config.py:Settings` with a default, a row in `docs/configuration.md`, a line in `.env.example`
- [ ] New feature sits behind an `AGENT_<FEATURE>_ENABLED` flag that defaults to `false`; with it off, behaviour is unchanged (added to the `ask --dry-run` matrix in `tests/test_cli.py` and the "every flag on" step of `make eval`, which CI runs)
- [ ] Concurrent callers (API jobs, eval, MCP) use `tools.create_sandbox(...)` and `ToolContext(sandbox=sb)`, not the module-level `sandbox.run_*`
- [ ] Type hints on new code; ruff clean (line length 100)
- [ ] Prompt text changed? Version bumped in `core/prompts.py`, pin updated in `tests/test_prompts.py`, and an `eval compare` before/after is below (or N/A)
- [ ] Documented behaviour changed? `README.md` and the matching `docs/*.md` updated
- [ ] Security checklist in CONTRIBUTING.md reviewed for what this touches (untrusted data fencing, UI rendering, sandbox layers)

## Eval compare (only if prompts or agent behaviour changed)

<!-- `python -m analyst_agent.eval compare <before> <after>` output, or "N/A". -->
