"""Unit tests for pathway embedding pipeline helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import embed_pathways


def _mock_select_chain(rows: list[dict[str, object]]) -> tuple[MagicMock, MagicMock]:
    supabase = MagicMock()
    query = MagicMock()
    supabase.table.return_value = query
    query.select.return_value = query
    query.is_.return_value = query
    query.limit.return_value = query
    query.execute.return_value = SimpleNamespace(data=rows)
    return supabase, query


def test_fetch_pending_pathways_filters_blank_content() -> None:
    """Only rows with id and non-empty content should be returned."""
    supabase, query = _mock_select_chain(
        [
            {"id": 1, "content": "Pathway content"},
            {"id": 2, "content": ""},
            {"id": None, "content": "Missing id"},
        ]
    )

    rows = embed_pathways.fetch_pending_pathways(supabase, batch_size=100)

    assert rows == [{"id": 1, "content": "Pathway content"}]
    query.limit.assert_called_once_with(100)


def test_generate_embeddings_returns_vectors() -> None:
    """Embedding helper should return vectors from local model in order."""
    mock_model = MagicMock()
    vector_a = [0.1] * 384
    vector_b = [0.2] * 384
    mock_model.encode.return_value = SimpleNamespace(tolist=lambda: [vector_a, vector_b])

    vectors = embed_pathways.generate_embeddings(mock_model, ["first", "second"])

    assert vectors == [vector_a, vector_b]
    mock_model.encode.assert_called_once_with(["first", "second"])


def test_process_batch_updates_each_row(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Each row in a batch should trigger one Supabase update."""
    rows = [{"id": 11, "content": "alpha"}, {"id": 12, "content": "beta"}]
    vectors = [[0.1] * 384, [0.2] * 384]
    update_calls: list[tuple[object, list[float]]] = []

    def _fake_generate(_client: object, _texts: list[str]) -> list[list[float]]:
        return vectors

    def _fake_update(_supabase: object, row_id: object, vector: list[float]) -> None:
        update_calls.append((row_id, vector))

    monkeypatch.setattr(embed_pathways, "generate_embeddings", _fake_generate)
    monkeypatch.setattr(embed_pathways, "update_embedding", _fake_update)

    success, failed = embed_pathways.process_batch(MagicMock(), MagicMock(), rows)

    assert success == 2
    assert failed == 0
    assert update_calls == [(11, vectors[0]), (12, vectors[1])]


def test_run_embedding_pipeline_processes_until_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Pipeline should continue fetching batches until no pending rows remain."""
    batch_one = [{"id": 1, "content": "first"}]
    pending_batches = [batch_one, []]

    def _fake_fetch(_supabase: object, batch_size: int) -> list[dict[str, object]]:
        assert batch_size == 100
        return pending_batches.pop(0)

    monkeypatch.setattr(embed_pathways, "create_supabase_client", lambda: MagicMock())
    monkeypatch.setattr(embed_pathways, "create_embedding_model", lambda: MagicMock())
    monkeypatch.setattr(embed_pathways, "fetch_pending_pathways", _fake_fetch)
    monkeypatch.setattr(embed_pathways, "process_batch", lambda _s, _o, _r: (1, 0))

    exit_code = embed_pathways.run_embedding_pipeline(batch_size=100)

    assert exit_code == 0
