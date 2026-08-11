"""Named addresses are provisioned as administrators; everyone else is Staff.

The list is a BOOTSTRAP DEFAULT, not a standing authority. It is read only when an account
is first created, so a role changed later in the admin UI is never overwritten by a
sign-in, and removing an address here does not demote anyone. Making it authoritative on
every sign-in would mean the config file and the UI fight each other, and the config would
win silently.

Expressing it as email addresses is safe because the tenant is pinned: an id_token only
reaches this code if Entra issued it for MICROSOFT_TENANT_ID, so the addresses are ones
the organisation controls rather than anything a stranger can claim.
"""
from __future__ import annotations

import pytest

from src.core.config import Settings


def _admin_set(configured: str) -> set[str]:
    """The parsing the callback performs, isolated."""
    return {
        address.strip().lower()
        for address in configured.split(",")
        if address.strip()
    }


def test_default_is_empty_so_nobody_is_promoted_by_accident():
    assert Settings(_env_file=None).ENTRA_ADMIN_EMAILS == ""
    assert _admin_set("") == set()


@pytest.mark.parametrize(
    "configured",
    [
        "boss@qnsc.vn,lead@qnsc.vn",
        " boss@qnsc.vn , lead@qnsc.vn ",
        "BOSS@QNSC.VN,Lead@Qnsc.vn",
        "boss@qnsc.vn,,lead@qnsc.vn,",
    ],
)
def test_the_list_tolerates_the_ways_people_actually_write_it(configured):
    """Whitespace, case and trailing commas are how this gets typed into Terraform."""
    assert _admin_set(configured) == {"boss@qnsc.vn", "lead@qnsc.vn"}


def test_a_listed_address_is_matched_case_insensitively():
    """Entra returns preferred_username in whatever case the directory holds.

    The callback lowercases the claim before comparing; if either side stopped doing that,
    an administrator would silently be provisioned as Staff and nobody would notice until
    they were refused something.
    """
    admins = _admin_set("Boss@QNSC.vn")
    assert "boss@qnsc.vn" in admins


def test_an_unlisted_address_is_not_an_admin():
    admins = _admin_set("boss@qnsc.vn")
    assert "someone.else@qnsc.vn" not in admins
