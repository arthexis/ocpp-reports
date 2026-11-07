"""Read-only inventory of Nginx htpasswd identities, never credential material."""
from __future__ import annotations

from pathlib import Path


def inventory(path: Path) -> list[dict[str, str]]:
    """Return usernames and recognized hash families without exposing hash values.

    This is an inventory, not a credential importer. The caller must explicitly
    associate each htpasswd file with an authorized dashboard tenant.
    """
    if not path.is_file() or path.is_symlink():
        raise ValueError("Expected an existing regular htpasswd file")
    accounts: list[dict[str, str]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, 1):
            line = raw.rstrip("\r\n")
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise ValueError(f"Malformed htpasswd entry at line {number}")
            username, digest = line.split(":", 1)
            if not username or not digest or username in seen or any(c.isspace() for c in username):
                raise ValueError(f"Invalid or duplicate htpasswd identity at line {number}")
            seen.add(username)
            if digest.startswith("$2"):
                family = "bcrypt"
            elif digest.startswith("$apr1$"):
                family = "apr1"
            elif digest.startswith("{SHA}"):
                family = "sha1"
            elif digest.startswith("$5$"):
                family = "sha256-crypt"
            elif digest.startswith("$6$"):
                family = "sha512-crypt"
            else:
                family = "unknown"
            accounts.append({"username": username, "hash_family": family})
    return accounts
