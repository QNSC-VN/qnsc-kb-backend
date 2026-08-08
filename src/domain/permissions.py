import uuid
from src.models.user import User, AccessGroup
from src.models.article import Article
from src.domain.rbac import AuthorizationService

class PermissionService:
    @staticmethod
    def get_public_bit() -> int:
        # Bit position 0 represents public access (always available to everyone)
        return 0

    @classmethod
    def calculate_user_bitmask(cls, user: User) -> int:
        """
        Calculates user's bitmask from their access groups.
        If user is Admin, we return a fully set bitmask (e.g. all 1s).
        All users get the public bit (position 0) automatically.
        """
        if AuthorizationService.has_permission(user, "article.read", requested_scope="global"):
            # Enable first 62 bits
            return (1 << 62) - 1
            
        bitmask = 1 << cls.get_public_bit()
        for group in user.groups:
            if group.bitmask_position is not None:
                bitmask |= (1 << group.bitmask_position)
        return bitmask

    @classmethod
    def calculate_article_bitmask(cls, article: Article) -> int:
        """
        Calculates an article's access group bitmask.
        If sensitivity is public, it returns just the public bit.
        Otherwise, it returns the bitwise OR of all allowed access group bitmask positions.
        """
        if article.sensitivity == "public":
            return 1 << cls.get_public_bit()

        bitmask = 0
        for group in article.access_groups:
            if group.bitmask_position is not None:
                bitmask |= (1 << group.bitmask_position)
        
        # Restricted/internal articles without an explicit access group must
        # fail closed. Treating them as public leaks documents whenever an
        # editor forgets to select a group.
        return bitmask

    @classmethod
    def can_view_article(cls, user: User, article: Article) -> bool:
        if article.status == "deleted" or getattr(article, "lifecycle_status", "active") not in (None, "active"):
            return False
        if not AuthorizationService.can_access_article_departments(user, article):
            return False
        if article.status in {"draft", "pending_review", "archived"}:
            # Unpublished content is never ordinary knowledge-base content.
            # Owners and governance users may inspect it for review/history,
            # but a reader must not discover it through direct IDs or search.
            governance_access = any(
                AuthorizationService.has_permission(user, permission, article, scope)
                for permission in ("article.review", "article.publish", "article.edit", "article.delete")
                for scope in ("own", "department", "company", "global")
            )
            if article.owner_id != user.id and not governance_access:
                return False
        if not any(AuthorizationService.has_permission(user, "article.read", article, scope) for scope in ("own", "department", "company")):
            return False
        if AuthorizationService.has_full_company_article_access(user):
            return True
        if AuthorizationService.has_narrow_article_access(user, article):
            return True
        user_mask = cls.calculate_user_bitmask(user)
        art_mask = cls.calculate_article_bitmask(article)
        return (user_mask & art_mask) != 0

    @classmethod
    def can_edit_article(cls, user: User, article: Article) -> bool:
        return any(AuthorizationService.has_permission(user, "article.edit", article, scope) for scope in ("own", "department", "company"))

    @classmethod
    def can_delete_article(cls, user: User, article: Article) -> bool:
        return any(AuthorizationService.has_permission(user, "article.delete", article, scope) for scope in ("own", "department", "company"))
