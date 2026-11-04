from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _never_load_a_real_embedding_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests mock the encoder; any attempt to load sentence-transformers fails loudly."""

    def refuse(model_name: str) -> Any:
        raise AssertionError(f"test tried to load a real embedding model ({model_name})")

    monkeypatch.setattr("analyst_agent.semantic.embeddings.load_sentence_transformer", refuse)
