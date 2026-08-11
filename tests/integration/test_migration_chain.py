from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migrations_have_one_current_head():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "migrations" / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["20260810_51"]


def test_production_compose_is_explicitly_hardened():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "AUTO_CREATE_SCHEMA: \"false\"" in compose
    assert "ALLOW_SELF_REGISTRATION: \"false\"" in compose
    assert "JOB_MODE: celery" in compose
    assert "MIGRATION_DATABASE_URL" in compose
    assert "APP_DATABASE_ROLE" in compose
    assert "ENABLE_RLS: \"true\"" in compose
    assert "5432:5432" not in compose
    assert "6379:6379" not in compose
    assert "caddy:2.8-alpine" in compose
    assert "PUBLIC_HOSTNAME" in compose
    assert "GEMINI_MODEL" in compose
    assert "MICROSOFT_CLIENT_ID: ${MICROSOFT_CLIENT_ID:?set MICROSOFT_CLIENT_ID}" in compose
    assert "MICROSOFT_CLIENT_SECRET: ${MICROSOFT_CLIENT_SECRET:?set MICROSOFT_CLIENT_SECRET}" in compose
    assert "MICROSOFT_TENANT_ID: ${MICROSOFT_TENANT_ID:?set MICROSOFT_TENANT_ID}" in compose
    assert "MICROSOFT_LOGIN_REDIRECT_URI: ${MICROSOFT_LOGIN_REDIRECT_URI:?set MICROSOFT_LOGIN_REDIRECT_URI}" in compose
    assert compose.count("MICROSOFT_CLIENT_ID: ${MICROSOFT_CLIENT_ID:?set MICROSOFT_CLIENT_ID}") == 2
    assert compose.count("MICROSOFT_CLIENT_SECRET: ${MICROSOFT_CLIENT_SECRET:?set MICROSOFT_CLIENT_SECRET}") == 2
    assert compose.count("MICROSOFT_TENANT_ID: ${MICROSOFT_TENANT_ID:?set MICROSOFT_TENANT_ID}") == 2
    # The bootstrap administrator is a GLOBAL admin. An unset password must fail the
    # compose invocation rather than fall back to the one published in .env.example.
    assert "BOOTSTRAP_ADMIN_PASSWORD: ${BOOTSTRAP_ADMIN_PASSWORD:?set BOOTSTRAP_ADMIN_PASSWORD}" in compose
    assert "BOOTSTRAP_ADMIN_PASSWORD:-" not in compose
    # Migrations are not run from any service's entrypoint any more — the `migrator`
    # image target owns them, behind a compose profile, so a scale-out cannot fire N
    # concurrent migrations. Locally that means `alembic upgrade head` by hand.
    assert "target: migrator" in compose
    dev_compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "entrypoint:" not in dev_compose
    assert "AUTO_CREATE_SCHEMA=false" in dev_compose


def _chain() -> list[str]:
    """Revisions from base to head, in the order Alembic will apply them."""
    root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(root / "migrations" / "alembic.ini")))
    head = script.get_heads()[0]
    order = []
    for revision in script.walk_revisions(base="base", head=head):
        order.append(revision.revision)
    order.reverse()
    return order


# Pairs where a later revision id appears BEFORE an earlier one in the chain.
#
# Each entry is a scar, not a convention. 20260810_36 was authored against 20260807_35
# and DEPLOYED, then a merge re-parented it onto 20260810_50. Alembic then read the
# deployed version (20260810_36) as "everything before me is applied" and skipped FIFTEEN
# migrations — the database reported head while missing six tables and four columns, and
# the API crashed on the first one it touched. A fresh database was fine, which is why CI
# never noticed.
#
# Do not add to this list to make a build pass. A new entry means a released migration has
# been re-parented, and every environment that already applied it will silently skip the
# migrations now behind it.
KNOWN_OUT_OF_ORDER = {
    # The re-parenting scar described above.
    ("20260810_50", "20260810_36"),
    ("20260810_36", "20260810_51"),
    # Not a scar: 20260802_00 is the bootstrap revision (extensions, base schema) and was
    # numbered _00 to sit first, while the migrations after it carry their authoring date.
    # It has never been re-parented.
    ("20260802_00", "20260726_01"),
}


def test_the_chain_applies_in_id_order():
    """Revision ids must increase along the chain.

    Ids here are date-prefixed and sequential, so the chain order and the id order should
    agree. When they disagree, a migration has been moved after it was written — the
    failure mode above.
    """
    order = _chain()
    inversions = [
        (previous, current)
        for previous, current in zip(order, order[1:])
        if current < previous and (previous, current) not in KNOWN_OUT_OF_ORDER
    ]
    assert not inversions, (
        f"migration(s) re-parented out of id order: {inversions}. A revision that is "
        "already deployed must keep its down_revision — moving it makes Alembic treat "
        "everything now behind it as already applied."
    )


def test_the_chain_is_linear():
    """One head, and no revision claimed by two children.

    A fork does not fail loudly: Alembic picks a head and the other branch is never
    applied, which is indistinguishable from the drift above.
    """
    root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(root / "migrations" / "alembic.ini")))
    assert len(script.get_heads()) == 1, f"forked chain: {script.get_heads()}"

    parents: dict[str, list[str]] = {}
    for revision in script.walk_revisions(base="base", head=script.get_heads()[0]):
        for parent in revision._all_down_revisions:
            parents.setdefault(parent, []).append(revision.revision)
    forks = {parent: kids for parent, kids in parents.items() if len(kids) > 1}
    assert not forks, f"more than one migration revises the same parent: {forks}"
