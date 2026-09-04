import api.models as models
from api.wsbound import MemoryBudgetExceeded


def _force_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(models, "_stale_snapshot_metadata_refresh_ids", lambda _rows: set())
    monkeypatch.setattr(models, "_row_may_need_sidecar_metadata_refresh", lambda *_a, **_k: True)


def test_bad_sidecar_json_keeps_sidebar_row(monkeypatch, tmp_path):
    row = {"session_id": "bad-json", "message_count": 2}
    _force_refresh(monkeypatch, tmp_path)
    (tmp_path / "bad-json.json").write_text("{invalid", encoding="utf-8")

    assert models._refresh_index_rows_from_sidecar_metadata([row]) == [row]


def test_sidecar_budget_refusal_keeps_sidebar_row(monkeypatch, tmp_path):
    row = {"session_id": "too-large", "message_count": 2}
    _force_refresh(monkeypatch, tmp_path)

    def refuse(_cls, _sid, **_kwargs):
        raise MemoryBudgetExceeded()

    monkeypatch.setattr(models.Session, "load_metadata_only", classmethod(refuse))

    assert models._refresh_index_rows_from_sidecar_metadata([row]) == [row]
