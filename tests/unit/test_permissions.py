import pytest
import uuid
from src.models.user import User, AccessGroup, Department, DepartmentManager
from src.models.article import Article
from src.domain.permissions import PermissionService
from src.domain.rbac import AuthorizationService

def test_public_bit_position():
    assert PermissionService.get_public_bit() == 0

def test_admin_bitmask():
    admin = User(role="Admin")
    mask = PermissionService.calculate_user_bitmask(admin)
    # Admin gets all bits
    assert mask == (1 << 62) - 1

def test_staff_default_bitmask():
    staff = User(role="Staff", groups=[])
    mask = PermissionService.calculate_user_bitmask(staff)
    # Default staff with no groups still gets public access (bit 0)
    assert mask == 1  # 1 << 0

def test_staff_with_groups_bitmask():
    g1 = AccessGroup(name="hr", bitmask_position=1)
    g2 = AccessGroup(name="legal", bitmask_position=3)
    
    staff = User(role="Staff", groups=[g1, g2])
    mask = PermissionService.calculate_user_bitmask(staff)
    
    # Expected: (1 << 0) | (1 << 1) | (1 << 3) = 1 | 2 | 8 = 11
    assert mask == 11

def test_article_public_bitmask():
    article = Article(sensitivity="public", access_groups=[])
    mask = PermissionService.calculate_article_bitmask(article)
    assert mask == 1

def test_article_restricted_bitmask():
    g1 = AccessGroup(name="security", bitmask_position=2)
    article = Article(sensitivity="restricted", access_groups=[g1])
    mask = PermissionService.calculate_article_bitmask(article)
    assert mask == 4  # 1 << 2

def test_non_public_article_without_group_fails_closed():
    article = Article(sensitivity="internal", access_groups=[])
    assert PermissionService.calculate_article_bitmask(article) == 0

def test_can_view_article_rules():
    # Admin can always view
    admin = User(role="Admin")
    g1 = AccessGroup(name="secret", bitmask_position=5)
    engineering = Department(company_domain="local", name="Engineering", active=True)
    restricted_article = Article(company_domain="local", dept="Engineering", departments=[engineering], sensitivity="restricted", access_groups=[g1], owner_id=uuid.uuid4())
    assert PermissionService.can_view_article(admin, restricted_article) is True

    # Staff without group cannot view restricted article
    staff = User(role="Staff", company_domain="local", groups=[], departments=[engineering], id=uuid.uuid4())
    assert PermissionService.can_view_article(staff, restricted_article) is False

    # Staff with group can view restricted article
    staff_with_access = User(role="Staff", company_domain="local", groups=[g1], departments=[engineering], id=uuid.uuid4())
    assert PermissionService.can_view_article(staff_with_access, restricted_article) is True


def test_ceo_company_access_is_consistent_with_article_and_chat_scope():
    ceo = User(role="CEO", company_domain="acme.test", groups=[])
    article = Article(company_domain="acme.test", sensitivity="internal", access_groups=[])
    assert PermissionService.can_view_article(ceo, article) is True


def test_department_scope_uses_explicit_ownership_not_a_role_name():
    owner = User(role="Staff", company_domain="acme.test", groups=[])
    department = Department(company_domain="acme.test", name="Operations", active=True)
    owner.departments.append(department)
    owner.department_ownerships.append(DepartmentManager(department=department, active=True))
    from src.models.rbac import Permission, Role, RolePermission
    role = Role(name="Department content manager", company_domain="acme.test")
    role.permissions.append(RolePermission(permission=Permission(key="article.read", name="Read"), scope="department"))
    owner.roles.append(role)
    article = Article(company_domain="acme.test", dept="Operations", sensitivity="internal", access_groups=[])
    assert PermissionService.can_view_article(owner, article) is True


def test_unpublished_articles_are_not_reader_visible():
    group = AccessGroup(name="public", bitmask_position=0)
    department = Department(company_domain="local", name="Engineering", active=True)
    reader = User(role="Staff", company_domain="local", groups=[group], departments=[department], id=uuid.uuid4())
    owner = User(role="Staff", company_domain="local", groups=[group], departments=[department], id=uuid.uuid4())
    for state in ("draft", "pending_review", "archived"):
        article = Article(
            status=state,
            dept="Engineering",
            departments=[department],
            sensitivity="public",
            access_groups=[group],
            owner_id=owner.id,
            lifecycle_status="active",
        )
        assert PermissionService.can_view_article(reader, article) is False
    published = Article(company_domain="local", status="published", dept="Engineering", departments=[department], sensitivity="public", access_groups=[group], lifecycle_status="active")
    assert PermissionService.can_view_article(reader, published) is True


def test_department_membership_blocks_article_from_another_department():
    engineering = Department(company_domain="acme.test", name="Engineering", active=True)
    security = Department(company_domain="acme.test", name="Security", active=True)
    member = User(role="Staff", company_domain="acme.test", departments=[engineering])
    engineering_article = Article(company_domain="acme.test", dept="Engineering", departments=[engineering], sensitivity="public")
    security_article = Article(company_domain="acme.test", dept="Security", departments=[security], sensitivity="public")

    assert PermissionService.can_view_article(member, engineering_article) is True
    assert PermissionService.can_view_article(member, security_article) is False


def test_article_response_hides_non_member_department_labels():
    engineering = Department(company_domain="acme.test", name="Engineering", active=True)
    security = Department(company_domain="acme.test", name="Security", active=True)
    member = User(role="Staff", company_domain="acme.test", departments=[engineering])
    article = Article(company_domain="acme.test", dept="Security", departments=[engineering, security], sensitivity="public")

    assert PermissionService.can_view_article(member, article) is True
    AuthorizationService.restrict_article_metadata(member, article)
    assert article.dept == "Engineering"
    assert [department.name for department in article.departments] == ["Engineering"]
