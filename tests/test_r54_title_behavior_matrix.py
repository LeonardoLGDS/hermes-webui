"""Reproduce the accepted seven-case R47/R54 title matrix against R62."""

import ast
import collections
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes


@pytest.fixture(autouse=True)
def isolated_title_matrix(monkeypatch, tmp_path):
    """Keep all model globals and route session state hermetic."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    original_save = models.Session.__dict__["save"]
    monkeypatch.setattr(models, "SESSIONS", collections.OrderedDict())
    monkeypatch.setattr(routes, "SESSIONS", collections.OrderedDict())
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db")
    models._SIDECAR_METADATA_CACHE.clear()
    models._LEGACY_SIDECAR_FACTS.clear()
    saved = []
    published = []

    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda _sid, session: session)
    monkeypatch.setattr(routes, "_sync_session_title_to_insights", lambda _session: None)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *_args, **_kwargs: published.append((_args, _kwargs)))

    def record_save(self, *_args, **_kwargs):
        saved.append(
            (
                self.session_id,
                self.title,
                self.manual_title,
                self.llm_title_generated,
            )
        )

    monkeypatch.setattr(models.Session, "save", record_save)
    yield SimpleNamespace(
        saved=saved,
        published=published,
        session_dir=session_dir,
        original_save=original_save,
    )
    models._SIDECAR_METADATA_CACHE.clear()
    models._LEGACY_SIDECAR_FACTS.clear()


def _install(session):
    routes.SESSIONS[session.session_id] = session
    return session


def _prepare_regeneration(monkeypatch, sid):
    class NoDiagnostics:
        @staticmethod
        def maybe_start(*_args, **_kwargs):
            return None

    responses = []
    monkeypatch.setattr(routes, "RequestDiagnostics", NoDiagnostics)
    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda requested_id: routes.SESSIONS[requested_id])
    monkeypatch.setattr(
        routes,
        "generate_session_title_for_session",
        lambda _session, prefer_latest=False: ("Regenerated Title", "ok", "raw-preview"),
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: (responses.append(payload), True)[1])
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda _handler, *_args, **_kwargs: {"session_id": sid, "prefer_latest": True},
    )
    return responses


def test_matrix_1_default_automatic_generation_allowed():
    session = _install(
        models.Session(
            session_id="matrix-default",
            title="Untitled",
            manual_title=False,
            llm_title_generated=False,
        )
    )

    returned = routes._persist_generated_session_title(
        session,
        "Generated Default Title",
        event_reason="matrix_automatic_default",
        require_default_title=True,
    )

    assert returned == "Generated Default Title"
    assert (session.title, session.manual_title, session.llm_title_generated) == (
        "Generated Default Title",
        False,
        True,
    )


def test_matrix_2_manual_title_blocks_automatic_generation(isolated_title_matrix):
    _install(
        models.Session(
            session_id="matrix-manual",
            title="User Owned Title",
            manual_title=True,
            llm_title_generated=False,
        )
    )

    returned = routes._persist_generated_session_title(
        routes.SESSIONS["matrix-manual"],
        "Automatic Must Not Win",
        event_reason="matrix_automatic",
        require_default_title=True,
    )

    assert returned == "User Owned Title"
    assert isolated_title_matrix.saved == []
    assert isolated_title_matrix.published == []


def test_matrix_3_explicit_regeneration_keeps_manual_ownership(monkeypatch):
    manual = _install(
        models.Session(
            session_id="matrix-manual",
            title="User Owned Title",
            manual_title=True,
            llm_title_generated=False,
        )
    )
    responses = _prepare_regeneration(monkeypatch, "matrix-manual")

    routes.handle_post(object(), SimpleNamespace(path="/api/session/title/regenerate", query=""))

    response = responses[-1]["session"]
    assert manual.title == "Regenerated Title"
    assert manual.manual_title is True
    assert manual.llm_title_generated is False
    assert response["manual_title"] is True
    assert response["llm_title_generated"] is False


def test_matrix_4_nonmanual_explicit_regeneration_preserves_existing_behavior(monkeypatch):
    nonmanual = _install(
        models.Session(
            session_id="matrix-nonmanual",
            title="Old Generated Title",
            manual_title=False,
            llm_title_generated=True,
        )
    )
    responses = _prepare_regeneration(monkeypatch, "matrix-nonmanual")

    routes.handle_post(object(), SimpleNamespace(path="/api/session/title/regenerate", query=""))

    response = responses[-1]["session"]
    assert nonmanual.title == "Regenerated Title"
    assert nonmanual.manual_title is False
    assert nonmanual.llm_title_generated is True
    assert response["manual_title"] is False
    assert response["llm_title_generated"] is True


def test_matrix_5_compact_and_save_serialize_generated_title_state(isolated_title_matrix):
    session = models.Session(
        session_id="matrix-compact-save",
        title="Generated Persisted",
        llm_title_generated=True,
        manual_title=False,
    )

    isolated_title_matrix.original_save(session, touch_updated_at=False)

    sidecar_path = isolated_title_matrix.session_dir / "matrix-compact-save.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["llm_title_generated"] is True
    assert session.compact()["llm_title_generated"] is True


def test_matrix_6_old_sidecar_without_generated_field_loads_valid(isolated_title_matrix):
    legacy_id = "matrix-legacy-no-field"
    payload = {
        "session_id": legacy_id,
        "title": "Legacy Title",
        "workspace": "/tmp/hermetic-r62-title-matrix",
        "created_at": 1.0,
        "updated_at": 2.0,
        "messages": [],
    }
    (isolated_title_matrix.session_dir / f"{legacy_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    loaded = models.Session.load(legacy_id)

    assert loaded is not None
    assert loaded.title == "Legacy Title"
    assert loaded.llm_title_generated is False


def test_matrix_7_forced_title_write_has_only_regeneration_callsite():
    source = (Path(routes.__file__).parent / "routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    force_true = []

    def visit(node, containing_functions):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            containing_functions = containing_functions + [node.name]
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "_persist_generated_session_title"
        ):
            forced = any(
                keyword.arg == "force"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            if forced:
                force_true.append((node.lineno, containing_functions[-1]))
        for child in ast.iter_child_nodes(node):
            visit(child, containing_functions)

    visit(tree, ["<module>"])
    assert force_true
    assert len(force_true) == 1
    assert force_true[0][1] == "handle_post"
