"""
Account-level role/status mutations (Phase 0 RBAC).

Kept in its own module — not in `app/api/auth.py` — because the writers are the
admin console (Phase 12: `POST /admin/users/{id}/role` / `/status`) and
community moderation (Phase 11: suspend/ban), never the auth endpoints
themselves. `scripts/make_admin.py` is the only bootstrap path for the very
first admin.

Audit: these actions are privileged and must be recorded. The existing
`audit_log_repository` is application-scoped (it requires an `application_id`
or `autonomous_task_id`), so account actions don't fit it. Phase 12 introduces
an account-scoped audit trail and wires it in here; until then every call logs
a WARNING line so the change is at least traceable in the process log.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.db_models import User, VALID_USER_ROLES, VALID_USER_STATUSES

logger = logging.getLogger(__name__)


def set_role(db: Session, *, user_id: str, role: str, actor: str) -> User:
    if role not in VALID_USER_ROLES:
        raise ValueError(f"{role!r} is not a valid role; expected one of {sorted(VALID_USER_ROLES)}")
    user = db.query(User).filter(User.user_id == user_id).first()
    if user is None:
        raise LookupError(f"No user {user_id!r}")
    previous, user.role = user.role, role
    db.commit()
    logger.warning("RBAC: %s changed user %s role %r -> %r", actor, user_id, previous, role)
    return user


def set_status(db: Session, *, user_id: str, status: str, actor: str, reason: str | None = None) -> User:
    if status not in VALID_USER_STATUSES:
        raise ValueError(
            f"{status!r} is not a valid status; expected one of {sorted(VALID_USER_STATUSES)}"
        )
    user = db.query(User).filter(User.user_id == user_id).first()
    if user is None:
        raise LookupError(f"No user {user_id!r}")
    previous, user.status = user.status, status
    db.commit()
    logger.warning(
        "RBAC: %s changed user %s status %r -> %r (reason: %s)",
        actor, user_id, previous, status, reason or "—",
    )
    return user
