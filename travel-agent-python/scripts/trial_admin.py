"""Trusted operator CLI: issue invites or promote an existing user, without HTTP backdoors."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.models import SysUser
from app.db.session import session_scope
from app.services import trial_invites


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("invites")
    issue.add_argument("--count", type=int, default=1)
    promote = commands.add_parser("promote")
    promote.add_argument("username")
    args = parser.parse_args()
    if args.command == "invites":
        for code in trial_invites.issue(args.count, "operator-cli"):
            print(code)
        return
    with session_scope() as session:
        user = session.execute(select(SysUser).where(SysUser.username == args.username)).scalar_one_or_none()
        if user is None:
            parser.error("user does not exist; register with an invite first")
        user.role = "admin"
    print("Existing user promoted. Log in again and visit /admin.")


if __name__ == "__main__":
    main()
