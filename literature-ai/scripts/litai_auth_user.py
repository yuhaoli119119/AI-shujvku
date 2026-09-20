#!/usr/bin/env python3
"""Manage the Literature AI workbench accounts (Apache htpasswd store).

The workbench web login and the backend both read the same file:

    /opt/literature-ai/deploy/nginx/owner.htpasswd

It was previously used for the nginx HTTP Basic gate, so existing credentials
keep working after the upgrade.  Passwords are stored as Apache ``$apr1$``
hashes; this script never writes a clear-text password.

Usage (run on the server as 2401liyuhao, no dependencies beyond python3):

    python3 /opt/literature-ai/scripts/litai_auth_user.py list
    python3 /opt/literature-ai/scripts/litai_auth_user.py add alice            # prompts twice
    python3 /opt/literature-ai/scripts/litai_auth_user.py set-password alice   # prompts twice
    printf '%s\\n' 'secret' | python3 .../litai_auth_user.py set-password alice --password-stdin
    python3 /opt/literature-ai/scripts/litai_auth_user.py verify alice --password-stdin
    python3 /opt/literature-ai/scripts/litai_auth_user.py delete alice --yes

The file is bind-mounted into the backend and read on every login, so changes
take effect immediately -- no container restart needed.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import os
import sys
from pathlib import Path

DEFAULT_HTPASSWD = Path("/opt/literature-ai/deploy/nginx/owner.htpasswd")
MIN_PASSWORD_LENGTH = 10


def _load_htpasswd_module():
    """Import app.security.htpasswd without importing the whole application."""
    candidates = [
        Path(__file__).resolve().parents[1] / "backend" / "app" / "security" / "htpasswd.py",
        Path("/opt/literature-ai/backend/app/security/htpasswd.py"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("litai_htpasswd", candidate)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            return module
    raise SystemExit("ERROR: cannot locate backend/app/security/htpasswd.py")


hp = _load_htpasswd_module()


def read_store(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"ERROR: {path} does not exist")
    return hp.parse_htpasswd(path.read_text(encoding="utf-8"))


def read_password(args) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SystemExit("ERROR: empty password on stdin")
        return password
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise SystemExit("ERROR: passwords do not match")
    return first


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"ERROR: password must be at least {MIN_PASSWORD_LENGTH} characters")


def command_list(args) -> int:
    store = read_store(Path(args.file))
    if not store:
        print("(no accounts)")
        return 0
    for username, hash_field in store.items():
        scheme = hp.hash_scheme(hash_field)
        warning = "" if hp.is_supported_hash(hash_field) else "  <-- UNSUPPORTED, re-hash it"
        print(f"{username}\t{scheme}{warning}")
    return 0


def command_add(args) -> int:
    path = Path(args.file)
    store = read_store(path)
    if args.username in store:
        raise SystemExit(f"ERROR: account {args.username!r} already exists (use set-password)")
    password = read_password(args)
    validate_password(password)
    store[args.username] = hp.hash_password(password)
    hp.write_htpasswd(path, store)
    print(f"OK: account {args.username!r} added to {path}")
    print("Reminder: it can sign in at https://dft.researchlife.top/login immediately.")
    return 0


def command_set_password(args) -> int:
    path = Path(args.file)
    store = read_store(path)
    if args.username not in store:
        raise SystemExit(f"ERROR: account {args.username!r} does not exist (use add)")
    password = read_password(args)
    validate_password(password)
    scheme = hp.hash_scheme(store[args.username])
    store[args.username] = hp.hash_password(password)
    hp.write_htpasswd(path, store)
    print(f"OK: password for {args.username!r} updated ({scheme} -> apr1) in {path}")
    print("Note: outstanding sessions for this account were invalidated automatically.")
    return 0


def command_delete(args) -> int:
    path = Path(args.file)
    store = read_store(path)
    if args.username not in store:
        raise SystemExit(f"ERROR: account {args.username!r} does not exist")
    if not args.yes:
        raise SystemExit(
            "Refusing to delete without --yes. "
            f"Exact change: remove the single line '{args.username}:***' from {path}"
        )
    del store[args.username]
    hp.write_htpasswd(path, store)
    print(f"OK: account {args.username!r} removed from {path}")
    return 0


def command_verify(args) -> int:
    store = read_store(Path(args.file))
    hash_field = store.get(args.username)
    if not hash_field:
        print(f"FAIL: account {args.username!r} not found")
        return 1
    password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Password: ")
    if hp.verify_password(hash_field, password):
        print(f"OK: password matches for {args.username!r}")
        return 0
    print(f"FAIL: password does not match for {args.username!r}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=os.environ.get("LITAI_AUTH_HTPASSWD_FILE", str(DEFAULT_HTPASSWD)),
                        help=f"htpasswd file (default: {DEFAULT_HTPASSWD})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="list accounts and their hash scheme").set_defaults(func=command_list)

    add = subparsers.add_parser("add", help="create a new account")
    add.add_argument("username")
    add.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    add.set_defaults(func=command_add)

    update = subparsers.add_parser("set-password", help="change a password (or re-hash a legacy entry)")
    update.add_argument("username")
    update.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    update.set_defaults(func=command_set_password)

    delete = subparsers.add_parser("delete", help="remove an account")
    delete.add_argument("username")
    delete.add_argument("--yes", action="store_true", help="confirm the exact line to delete")
    delete.set_defaults(func=command_delete)

    verify = subparsers.add_parser("verify", help="check a password against the stored hash")
    verify.add_argument("username")
    verify.add_argument("--password-stdin", action="store_true")
    verify.set_defaults(func=command_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
