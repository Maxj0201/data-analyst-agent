"""Hypothesis profiles: ``ci`` (default, bounded and deterministic) and ``dev`` (more examples).

Select with ``HYPOTHESIS_PROFILE=dev``.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=150,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("dev", max_examples=1000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
