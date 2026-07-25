import pytest
import uuid
from src.models.user import User, AccessGroup
from src.models.article import Article
from src.domain.permissions import PermissionService

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

def test_can_view_article_rules():
    # Admin can always view
    admin = User(role="Admin")
    g1 = AccessGroup(name="secret", bitmask_position=5)
    restricted_article = Article(sensitivity="restricted", access_groups=[g1], owner_id=uuid.uuid4())
    assert PermissionService.can_view_article(admin, restricted_article) is True

    # Staff without group cannot view restricted article
    staff = User(role="Staff", groups=[], id=uuid.uuid4())
    assert PermissionService.can_view_article(staff, restricted_article) is False

    # Staff with group can view restricted article
    staff_with_access = User(role="Staff", groups=[g1], id=uuid.uuid4())
    assert PermissionService.can_view_article(staff_with_access, restricted_article) is True
