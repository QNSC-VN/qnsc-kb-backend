import uuid
from typing import Sequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.user import User, AccessGroup, DepartmentManager
from src.models.rbac import Role, RolePermission

class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self.db.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.groups))
            .options(selectinload(User.departments))
            .options(selectinload(User.department_ownerships).selectinload(DepartmentManager.department))
            .options(selectinload(User.roles).selectinload(Role.permissions).selectinload(RolePermission.permission))
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(
            select(User)
            .where(User.email == email)
            .options(selectinload(User.groups))
            .options(selectinload(User.departments))
            .options(selectinload(User.department_ownerships).selectinload(DepartmentManager.department))
            .options(selectinload(User.roles).selectinload(Role.permissions).selectinload(RolePermission.permission))
        )
        return result.scalar_one_or_none()

    async def create(self, user: User) -> User:
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def get_group_by_name(self, name: str, company_domain: str | None = None) -> AccessGroup | None:
        stmt = select(AccessGroup).where(AccessGroup.name == name)
        if company_domain is not None:
            stmt = stmt.where(AccessGroup.company_domain == company_domain)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_group(self, group: AccessGroup, *, commit: bool = True) -> AccessGroup:
        self.db.add(group)
        if commit:
            await self.db.commit()
            await self.db.refresh(group)
        else:
            await self.db.flush()
        return group

    async def get_all_groups(self, company_domain: str | None = None) -> Sequence[AccessGroup]:
        stmt = select(AccessGroup).order_by(AccessGroup.bitmask_position)
        if company_domain:
            stmt = stmt.where(AccessGroup.company_domain == company_domain)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_groups_by_ids(self, group_ids: list[uuid.UUID], company_domain: str | None = None) -> Sequence[AccessGroup]:
        stmt = select(AccessGroup).where(AccessGroup.id.in_(group_ids))
        if company_domain:
            stmt = stmt.where(AccessGroup.company_domain == company_domain)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def list_users(self, offset: int = 0, limit: int = 100) -> Sequence[User]:
        result = await self.db.execute(
            select(User).options(
                selectinload(User.groups),
                selectinload(User.departments),
                selectinload(User.department_ownerships).selectinload(DepartmentManager.department),
                selectinload(User.roles).selectinload(Role.permissions).selectinload(RolePermission.permission),
            ).order_by(User.company_domain, User.name).offset(offset).limit(limit)
        )
        return result.scalars().all()

    async def update(self, user: User) -> User:
        self.db.add(user)
        await self.db.commit()
        # Re-FETCH rather than refresh(). refresh() expires the instance and reloads its
        # COLUMNS only, so every relationship loaded by get_by_id is dropped — and each
        # caller of this method hands the result to a response builder that walks
        # user.roles -> role.permissions. Those then lazy-load inside an async request,
        # which asyncpg cannot do, and the endpoint 500s with MissingGreenlet AFTER the
        # write has already committed.
        return await self.get_by_id(user.id) or user

    async def update_user_groups(self, user: User, groups: list[AccessGroup]) -> User:
        user.groups = groups
        self.db.add(user)
        await self.db.commit()
        # Same reason as update() above.
        return await self.get_by_id(user.id) or user
