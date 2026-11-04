from analyst_agent.eval.cli import app

# Guarded: with the ``spawn`` start method, task processes re-import ``__main__``.
if __name__ == "__main__":
    app()
