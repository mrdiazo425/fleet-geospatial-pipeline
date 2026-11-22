"""Vehicle id masking. Mirrors ``bronze.mask_vehicle_id`` in SQL."""

from __future__ import annotations

import hmac
import re
from enum import StrEnum

VEHICLE_ID_PATTERN = r"^VHC-\d{4}-[A-Z]{3}$"
_MASKABLE = re.compile(r"^([A-Z]+)-[0-9]+-([A-Z]+)$")
FULL_MASK = "****"


class Role(StrEnum):
    """Who is asking for the data."""

    ADMIN = "admin"
    ANALYST = "analyst"


def mask_vehicle_id(vehicle_id: str) -> str:
    """Hide the numeric block: ``VHC-8472-ABC`` -> ``VHC-****-ABC``.

    Values that do not look like a vehicle id are fully masked, so an
    unexpected format can never leak through (fail closed).
    """
    match = _MASKABLE.match(vehicle_id or "")
    if match is None:
        return FULL_MASK
    return f"{match.group(1)}-{FULL_MASK}-{match.group(2)}"


def resolve_role(api_key: str | None, admin_api_key: str) -> Role:
    """Only the exact admin key grants admin; everything else is analyst."""
    if api_key and admin_api_key and api_key == admin_api_key:
        return Role.ADMIN
    return Role.ANALYST


def authenticate(
    username: str, password: str, credentials: dict[str, tuple[str, Role]]
) -> Role | None:
    """Return the role of a user, or None when the credentials are wrong.

    ``credentials`` maps username -> (password, role). The comparison is
    constant-time so response times do not leak how much of a password matched.
    """
    expected = credentials.get(username.strip().lower())
    if expected is None or not password:
        return None
    stored_password, role = expected
    if hmac.compare_digest(password.encode(), stored_password.encode()):
        return role
    return None
