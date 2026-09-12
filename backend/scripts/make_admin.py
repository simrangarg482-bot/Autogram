"""
Promote (or demote) a user by email. The ONLY way to grant the admin role —
there is deliberately no API endpoint for it (an endpoint that mints admins is
a privilege-escalation target; a shell script on the deployment host is not).

    python scripts/make_admin.py <email>                # -> admin
    python scripts/make_admin.py <email> --role moderator
    python scripts/make_admin.py <email> --role member  # demote

See app/models/db_models.py::VALID_USER_ROLES.
"""

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal  # noqa: E402
from app.models.db_models import User, VALID_USER_ROLES  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Set a user's role by email.")
    parser.add_argument("email")
    parser.add_argument("--role", default="admin", choices=sorted(VALID_USER_ROLES))
    args = parser.parse_args()

    email = args.email.lower().strip()
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            print(f"No user with email {email!r}.")
            raise SystemExit(1)
        previous = user.role
        if previous == args.role:
            print(f"{email} is already {args.role!r}. Nothing to do.")
            return
        user.role = args.role
        db.commit()
        print(f"{email}: role {previous!r} -> {args.role!r}.")


if __name__ == "__main__":
    main()
