"""
Phase 0 RBAC — role gates and account-status enforcement.

Mock-DB unit tests, matching this repo's convention (see
tests/test_applications_api.py): no live Postgres, the User row and the token
decode are stubbed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import HTTPException

from app.core import auth as auth_module
from app.core.auth import (
    create_access_token,
    get_current_user,
    require_role,
)
from app.core.config import JWT_SECRET


def _user(**overrides):
    base = dict(user_id="u1", email="a@b.com", role="member", status="active")
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------- token ----------

def test_access_token_carries_role():
    token = create_access_token("u1", "admin")
    decoded = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert decoded["sub"] == "u1"
    assert decoded["role"] == "admin"


def test_access_token_defaults_role_to_member():
    decoded = jwt.decode(create_access_token("u1"), JWT_SECRET, algorithms=["HS256"])
    assert decoded["role"] == "member"


# ---------- get_current_user: DB row is the authority ----------

def _call_get_current_user(monkeypatch, db_user, token_role="member"):
    monkeypatch.setattr(
        auth_module, "read_with_reconnect", lambda db, fn: db_user
    )
    token = create_access_token("u1", token_role)
    return get_current_user(token=token, db=MagicMock())


def test_tampered_token_role_is_ignored_db_row_wins(monkeypatch):
    # Token claims admin, DB says member -> the returned user is a member.
    user = _call_get_current_user(monkeypatch, _user(role="member"), token_role="admin")
    assert user.role == "member"


def test_unknown_user_rejected(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _call_get_current_user(monkeypatch, None)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("status", ["suspended", "banned"])
def test_suspended_or_banned_user_rejected_with_403(monkeypatch, status):
    with pytest.raises(HTTPException) as exc:
        _call_get_current_user(monkeypatch, _user(status=status))
    assert exc.value.status_code == 403
    assert status in exc.value.detail.lower()


def test_active_user_passes(monkeypatch):
    user = _call_get_current_user(monkeypatch, _user(status="active"))
    assert user.status == "active"


# ---------- require_role ----------

@pytest.mark.parametrize(
    "user_role, allowed, ok",
    [
        ("admin", ("admin",), True),
        ("moderator", ("moderator", "admin"), True),
        ("admin", ("moderator", "admin"), True),
        ("member", ("moderator", "admin"), False),
        ("moderator", ("admin",), False),
    ],
)
def test_require_role_matrix(user_role, allowed, ok):
    dep = require_role(*allowed)
    if ok:
        assert dep(user=_user(role=user_role)).role == user_role
    else:
        with pytest.raises(HTTPException) as exc:
            dep(user=_user(role=user_role))
        assert exc.value.status_code == 403


def test_require_role_hide_returns_404_not_403():
    dep = require_role("admin", hide=True)
    with pytest.raises(HTTPException) as exc:
        dep(user=_user(role="member"))
    assert exc.value.status_code == 404


def test_get_current_admin_hides_surface_from_non_admin():
    with pytest.raises(HTTPException) as exc:
        auth_module.get_current_admin(user=_user(role="moderator"))
    assert exc.value.status_code == 404


def test_get_current_moderator_admits_admin():
    assert auth_module.get_current_moderator(user=_user(role="admin")).role == "admin"
