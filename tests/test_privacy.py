import pytest

from fleet_pipeline.privacy import Role, authenticate, mask_vehicle_id, resolve_role


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("VHC-8472-ABC", "VHC-****-ABC"),
        ("VHC-0001-XYZ", "VHC-****-XYZ"),
        ("TRK-12-AB", "TRK-****-AB"),
    ],
)
def test_mask_hides_only_the_numeric_block(raw, masked):
    assert mask_vehicle_id(raw) == masked


@pytest.mark.parametrize("raw", ["", "8472", "VHC8472ABC", "vhc-8472-abc", "VHC-84A2-ABC", None])
def test_unexpected_formats_are_fully_masked(raw):
    assert mask_vehicle_id(raw) == "****"


def test_masked_value_never_contains_the_digits():
    assert "8472" not in mask_vehicle_id("VHC-8472-ABC")


def test_masking_is_idempotent_on_already_masked_ids():
    assert mask_vehicle_id(mask_vehicle_id("VHC-8472-ABC")) == "****"


@pytest.mark.parametrize(
    ("key", "expected"),
    [("secret", Role.ADMIN), ("wrong", Role.ANALYST), ("", Role.ANALYST), (None, Role.ANALYST)],
)
def test_only_the_exact_admin_key_grants_admin(key, expected):
    assert resolve_role(key, admin_api_key="secret") is expected


def test_empty_admin_key_never_grants_admin():
    assert resolve_role("", admin_api_key="") is Role.ANALYST


USERS = {"admin": ("s3cret", Role.ADMIN), "analista": ("view", Role.ANALYST)}


@pytest.mark.parametrize(
    ("username", "password", "role"),
    [
        ("admin", "s3cret", Role.ADMIN),
        ("  Admin ", "s3cret", Role.ADMIN),  # user names are not case sensitive
        ("analista", "view", Role.ANALYST),
        ("admin", "view", None),  # another user's password
        ("admin", "", None),
        ("admin", "S3CRET", None),  # passwords are case sensitive
        ("nobody", "s3cret", None),
        ("", "", None),
    ],
)
def test_authenticate(username, password, role):
    assert authenticate(username, password, USERS) is role
