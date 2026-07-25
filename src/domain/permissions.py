import uuid
from src.models.user import User, AccessGroup
from src.models.article import Article

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
        if user.role == "Admin":
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
        
        # If no access groups are specified but it's not public (e.g. default internal),
        # we default to public bit to avoid locking it out completely, or we handle it based on role.
        if bitmask == 0:
            bitmask = 1 << cls.get_public_bit()
            
        return bitmask

    @classmethod
    def can_view_article(cls, user: User, article: Article) -> bool:
        if user.role == "Admin":
            return True
            
        if user.role == "Department Owner" and user.dept == article.dept:
            return True

        if article.owner_id == user.id:
            return True

        # Check drafts access
        if article.status == "draft" and user.role != "Reviewer":
            return False

        # Bitwise match
        user_mask = cls.calculate_user_bitmask(user)
        art_mask = cls.calculate_article_bitmask(article)
        return (user_mask & art_mask) != 0

    @classmethod
    def can_edit_article(cls, user: User, article: Article) -> bool:
        if user.role == "Admin":
            return True

        if user.role == "Department Owner" and user.dept == article.dept:
            return True

        if article.owner_id == user.id:
            return True

        return False

    @classmethod
    def can_delete_article(cls, user: User, article: Article) -> bool:
        return cls.can_edit_article(user, article)
