"""Create and manage instructor accounts for the UI sign-in.

    docker compose exec web python -m app.manage_instructors add ada@pitt.edu --name "Ada Lovelace"
    docker compose exec web python -m app.manage_instructors passwd ada@pitt.edu
    docker compose exec web python -m app.manage_instructors deactivate ada@pitt.edu
    docker compose exec web python -m app.manage_instructors activate ada@pitt.edu
    docker compose exec web python -m app.manage_instructors list

add and passwd prompt for the password twice. For scripts, pass
--password-stdin and pipe it in (use `docker compose exec -T`).
Deactivating or changing a password signs that instructor out everywhere.
"""

import argparse
import getpass
import sys

from app.auth import InstructorStore, SqlInstructorStore, check_new_password
from app.db import SessionLocal


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        return check_new_password(sys.stdin.readline().rstrip("\r\n"))
    password = check_new_password(getpass.getpass("Password: "))
    if getpass.getpass("Repeat password: ") != password:
        raise ValueError("passwords do not match")
    return password


def main(argv: list[str] | None = None, store: InstructorStore | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="create an instructor account")
    add.add_argument("email")
    add.add_argument("--name", help="shown in the page header instead of the email")
    add.add_argument("--password-stdin", action="store_true")

    passwd = commands.add_parser("passwd", help="set a new password")
    passwd.add_argument("email")
    passwd.add_argument("--password-stdin", action="store_true")

    for name, help_text in (("activate", "allow signing in again"),
                            ("deactivate", "block signing in and end current sessions")):
        commands.add_parser(name, help=help_text).add_argument("email")

    commands.add_parser("list", help="show every account")

    args = parser.parse_args(argv)
    store = store or SqlInstructorStore(SessionLocal)
    try:
        if args.command == "add":
            store.add(args.email, read_password(args.password_stdin), args.name)
            print(f"Added {args.email.strip().lower()}.")
        elif args.command == "passwd":
            if store.find_by_email(args.email) is None:
                raise ValueError(f"no instructor with email {args.email}")
            store.set_password(args.email, read_password(args.password_stdin))
            print(f"Changed the password for {args.email}.")
        elif args.command in ("activate", "deactivate"):
            store.set_active(args.email, args.command == "activate")
            print(f"{args.command.capitalize()}d {args.email}.")
        else:
            accounts = store.list_all()
            if not accounts:
                print("No instructors yet. Add one with: python -m app.manage_instructors add EMAIL")
            for a in accounts:
                status = "active" if a.is_active else "deactivated"
                last = a.last_login_at.strftime("%Y-%m-%d %H:%M UTC") if a.last_login_at else "never"
                print(f"{a.email}\t{a.display_name or ''}\t{status}\tlast sign-in: {last}")
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
