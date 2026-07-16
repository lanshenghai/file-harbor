"""Load SMB/SFTP credentials from environment variables or a credentials file."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

DEFAULT_CREDS_FILE = Path.home() / ".smbcredentials"


@dataclass(frozen=True)
class SavedCredentials:
    username: str
    password: str
    domain: str
    source: str


def load_saved_credentials(path: Path | None = None) -> SavedCredentials | None:
    """Return credentials from env and/or ~/.smbcredentials, or None if no password found."""
    creds_path = path or Path(os.environ.get("SMB_CREDENTIALS", DEFAULT_CREDS_FILE))
    user = os.environ.get("SMB_USER", "")
    password = os.environ.get("SMB_PASS", "")
    domain = os.environ.get("SMB_DOMAIN", "")
    source = "env"

    if creds_path.is_file():
        with creds_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                key = key.strip().lower()
                value = value.strip()
                if key == "username":
                    user = value
                elif key == "password":
                    password = value
                elif key == "domain":
                    domain = value
        source = str(creds_path)

    if not password:
        return None
    if not user:
        user = os.environ.get("USER", "") or os.environ.get("USERNAME", "")
    if not user:
        return None
    return SavedCredentials(username=user, password=password, domain=domain, source=source)
