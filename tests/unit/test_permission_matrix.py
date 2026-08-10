import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_permission_matrix import CASES, ROLES


def test_permission_matrix_covers_all_seeded_roles_and_resource_families():
    assert set(ROLES) == {"Admin", "CEO", "Reviewer", "Staff"}
    assert {case.resource for case in CASES} >= {
        "articles",
        "hybrid search",
        "AI answer",
        "source catalog",
        "draft review queue",
        "SharePoint connectors",
        "audit log",
        "dependency health",
        "users",
        "roles",
    }
    assert all(case.allowed_roles <= set(ROLES) for case in CASES)


def test_permission_matrix_has_distinct_allow_and_deny_cases():
    assert any(case.allowed_roles == frozenset(ROLES) for case in CASES)
    assert any(case.allowed_roles != frozenset(ROLES) for case in CASES)
