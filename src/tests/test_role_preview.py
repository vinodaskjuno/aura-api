"""Seeing another role's dashboard — the one exception to reading role off the token.

`routers/dashboard_view.py` opens with the rule this endpoint otherwise keeps: "a client
cannot ask for another role's dashboard, and there is nothing to spoof." That is correct
for using the product and impossible to work with when changing it — there are seven
distinct payloads, and two roles whose ENTIRE product is this one screen, so verifying a
UI change across all of them meant provisioning directory accounts.

These tests guard the two properties that make the exception safe: it is gated in exactly
one place, and it never becomes an impersonation.
"""
from __future__ import annotations

import pytest

from src.services import role_metrics


@pytest.fixture
def quiet(monkeypatch):
    """No graph, no Dynamo — every source degrades and the builders still run."""
    import src.database.dynamo_client as db
    monkeypatch.setattr(db, "scan_items", lambda *a, **k: [])
    monkeypatch.setattr(db, "query_items", lambda *a, **k: [])
    import src.graph.neo4j_client as neo
    monkeypatch.setattr(neo, "is_available", lambda: False)
    import src.services.ontology_version_service as ovs
    monkeypatch.setattr(ovs, "list_versions", lambda limit=50, **k: [])


def _user(role="admin", **extra):
    return {"userId": "u1", "username": "u1", "role": role,
            "permissions": ["dashboard", "user_management"], **extra}


# ── It actually renders the other role's shape ──────────────────────────────

def test_preview_renders_the_previewed_roles_blocks(quiet):
    """The whole point: an admin can see what a Project Manager sees.

    `pipeline` is rendered for exactly one role, so it is the sharpest possible
    check that the builder really was swapped.
    """
    own = role_metrics.build_view(_user("admin"))
    preview = role_metrics.build_view(_user("admin"), preview_role="project_manager")

    assert "pipeline" not in {b.get("kind") for b in own["blocks"]}
    assert "pipeline" in {b.get("kind") for b in preview["blocks"]}


def test_preview_says_it_is_a_preview(quiet):
    """A preview indistinguishable from the real thing is how someone files a bug
    about a dashboard they were never looking at."""
    view = role_metrics.build_view(_user("admin"), preview_role="product_owner")

    assert view["previewedRole"] == "product_owner"
    assert view["actualRole"] == "admin"
    assert view["roleLabel"] == "Product Owner"


def test_no_preview_marker_when_previewing_your_own_role(quiet):
    view = role_metrics.build_view(_user("admin"), preview_role="admin")
    assert "previewedRole" not in view


def test_an_unknown_role_falls_through_like_a_real_one(quiet):
    """A directory user can carry any role id their org invented, which is why
    DEFAULT_VIEW exists. A preview must not be a second code path for that."""
    view = role_metrics.build_view(_user("admin"), preview_role="group:something")
    assert view["blocks"]                      # rendered, not blank


@pytest.mark.parametrize("role", sorted(role_metrics.ROLE_VIEWS))
def test_every_role_is_previewable(quiet, role):
    """If a role has a view, it must be reachable — otherwise the roles nobody can
    provision are exactly the roles nobody can check."""
    view = role_metrics.build_view(_user("admin"), preview_role=role)
    assert view["role"] == role
    assert "headline" in view


# ── It is gated, and it is not impersonation ────────────────────────────────

def test_preview_is_refused_without_user_management(quiet):
    from fastapi import HTTPException
    from src.routers import dashboard_view

    with pytest.raises(HTTPException) as raised:
        dashboard_view.get_dashboard_view(
            previewRole="admin",
            user={"userId": "u2", "role": "user_dev", "permissions": ["dashboard"]})
    assert raised.value.status_code == 403


def test_preview_does_not_widen_what_the_data_layer_sees(quiet, monkeypatch):
    """Only the BUILDER is swapped. `_Data` must still be constructed from the real
    caller, or a preview would become a way to read another user's rows."""
    seen: list = []
    real = role_metrics._Data
    monkeypatch.setattr(role_metrics, "_Data",
                        lambda user: seen.append(dict(user)) or real(user))

    role_metrics.build_view(_user("admin"), preview_role="project_manager")

    assert len(seen) == 1
    assert seen[0]["userId"] == "u1"
    assert seen[0]["role"] == "admin"          # NOT the previewed role
