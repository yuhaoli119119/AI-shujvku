"""Apache htpasswd store: read, verify, and (re)hash user records.

The owner gateway already keeps its user list in an Apache ``htpasswd`` file
(``deploy/nginx/owner.htpasswd``, mounted read-only into the backend).  This
module is the single place that parses and verifies that file so the web login
can reuse the exact same credentials the HTTP Basic gate used to check.

Only stdlib is used (no bcrypt/passlib in the image), therefore:

* ``$apr1$`` (Apache MD5 crypt)  -> verified and generated here.
* ``$1$``   (MD5 crypt)          -> verified (same algorithm, different magic).
* ``{SHA}`` (base64 SHA1)        -> verified.
* md5crypt ``$apr1$`` is the hash we write for new/updated accounts.
* ``$2y$``/``$2b$`` bcrypt and cleartext entries are refused (a warning is
  emitted once) instead of being silently accepted or mis-verified.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
APR1_MAGIC = "$apr1$"
MD5_MAGIC = "$1$"
BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2x$", "$2y$")

_unsupported_warned: set[str] = set()


def _b64_from_24bit(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(ITOA64[value & 0x3F])
        value >>= 6
    return "".join(out)


def md5_crypt_raw(password: bytes, salt: bytes, magic: bytes) -> str:
    """MD5 crypt (``$1$``) / Apache MD5 crypt (``$apr1$``) implementation."""
    salt = salt[:8]
    context = hashlib.md5(password + magic + salt)

    alternate = hashlib.md5(password + salt + password).digest()
    remaining = len(password)
    while remaining > 0:
        context.update(alternate[: min(16, remaining)])
        remaining -= 16

    index = len(password)
    while index > 0:
        context.update(b"\x00" if index & 1 else password[:1])
        index >>= 1

    digest = context.digest()
    for index in range(1000):
        round_hash = hashlib.md5()
        round_hash.update(password if index & 1 else digest)
        if index % 3:
            round_hash.update(salt)
        if index % 7:
            round_hash.update(password)
        round_hash.update(digest if index & 1 else password)
        digest = round_hash.digest()

    encoded = ""
    for first, second, third in ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5)):
        encoded += _b64_from_24bit((digest[first] << 16) | (digest[second] << 8) | digest[third], 4)
    encoded += _b64_from_24bit(digest[11], 2)
    return f"{magic.decode('ascii')}{salt.decode('ascii')}${encoded}"


def hash_password(password: str, salt: str | None = None) -> str:
    """Return an Apache ``$apr1$`` hash for ``password``."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    raw_salt = salt if salt is not None else secrets.token_urlsafe(12)
    clean_salt = "".join(
        character
        for character in str(raw_salt)
        if character in ITOA64
    )[:8]
    if len(clean_salt) < 8:
        clean_salt = (clean_salt + secrets.token_urlsafe(12)).replace("$", "")[:8]
        clean_salt = "".join(character for character in clean_salt if character in ITOA64)[:8]
    return md5_crypt_raw(
        password.encode("utf-8"),
        clean_salt.encode("ascii"),
        APR1_MAGIC.encode("ascii"),
    )


def hash_scheme(hash_field: str) -> str:
    """Classify an htpasswd hash field."""
    field = (hash_field or "").strip()
    if field.startswith(APR1_MAGIC):
        return "apr1"
    if field.startswith(MD5_MAGIC):
        return "md5crypt"
    if field.startswith("{SHA}"):
        return "sha1"
    if field.startswith(BCRYPT_PREFIXES):
        return "bcrypt"
    if field.startswith("$"):
        return "unknown"
    return "cleartext"


def is_supported_hash(hash_field: str) -> bool:
    return hash_scheme(hash_field) in {"apr1", "md5crypt", "sha1"}


def verify_password(hash_field: str, password: str) -> bool:
    """Constant-time-ish verification of ``password`` against ``hash_field``."""
    field = (hash_field or "").strip()
    if not field or not isinstance(password, str):
        return False
    scheme = hash_scheme(field)
    if scheme in {"apr1", "md5crypt"}:
        try:
            magic, remainder = field.split("$", 2)[1], field.split("$", 2)[2]
            salt = remainder.split("$", 1)[0]
        except (IndexError, ValueError):
            return False
        candidate = md5_crypt_raw(
            password.encode("utf-8"),
            salt.encode("ascii", "ignore"),
            f"${magic}$".encode("ascii"),
        )
        return hmac.compare_digest(candidate, field)
    if scheme == "sha1":
        digest = base64.b64encode(hashlib.sha1(password.encode("utf-8")).digest()).decode("ascii")
        return hmac.compare_digest(field[len("{SHA}") :], digest)
    if scheme == "bcrypt":
        if "bcrypt" not in _unsupported_warned:
            _unsupported_warned.add("bcrypt")
            logger.warning(
                "htpasswd entry uses bcrypt ($2y$/$2b$) which this build cannot verify; "
                "re-hash the account with scripts/litai_auth_user.py set-password"
            )
        return False
    if scheme == "cleartext":
        if "cleartext" not in _unsupported_warned:
            _unsupported_warned.add("cleartext")
            logger.error(
                "htpasswd entry is stored in clear text and is refused; "
                "re-hash it with scripts/litai_auth_user.py set-password"
            )
        return False
    if "unknown" not in _unsupported_warned:
        _unsupported_warned.add("unknown")
        logger.warning("htpasswd entry uses an unsupported hash scheme and is refused")
    return False


def parse_htpasswd(text: str) -> dict[str, str]:
    """Parse htpasswd file content into ``{username: hash_field}``."""
    users: dict[str, str] = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        username, hash_field = stripped.split(":", 1)
        username = username.strip()
        if username:
            users[username] = hash_field.strip()
    return users


def write_htpasswd(path: str | Path, users: dict[str, str]) -> None:
    """Rewrite an htpasswd file atomically (0644) preserving username order."""
    target = Path(path)
    body = "".join(f"{username}:{hash_field}\n" for username, hash_field in users.items())
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(target)


class HtpasswdStore:
    """Read-through cache of an htpasswd file (reloaded when mtime changes)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._mtime: float | None = None
        self._size: int | None = None
        self._users: dict[str, str] = {}

    def reload(self, *, force: bool = False) -> dict[str, str]:
        try:
            stat = self.path.stat()
        except OSError:
            if force or self._users:
                logger.warning("htpasswd file is not readable: %s", self.path)
            self._mtime = None
            self._size = None
            self._users = {}
            return self._users
        if not force and self._mtime == stat.st_mtime and self._size == stat.st_size:
            return self._users
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("htpasswd file read failed: %s", self.path)
            return self._users
        self._users = parse_htpasswd(text)
        self._mtime = stat.st_mtime
        self._size = stat.st_size
        return self._users

    def users(self) -> dict[str, str]:
        return self.reload()

    def hash_for(self, username: str) -> str | None:
        return self.users().get(str(username or "").strip())

    def verify(self, username: str, password: str) -> bool:
        hash_field = self.hash_for(username)
        if not hash_field:
            return False
        return verify_password(hash_field, password)
