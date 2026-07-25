import uuid
from typing import Sequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.models.user import User, AccessGroup

class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self.db.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.groups))
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(
            select(User)
            .where(User.email == email)
            .options(selectinload(User.groups))
        )
        return result.scalar_one_or_none()

    async def create(self, user: User) -> User:
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def get_group_by_name(self, name: str) -> AccessGroup | None:
        result = await self.db.execute(
            select(AccessGroup).where(AccessGroup.name == name)
        )
        return result.scalar_one_or_none()

    async def create_group(self, group: AccessGroup) -> AccessGroup:
        self.db.add(group)
        await self.db.commit()
        await self.db.refresh(group)
        return group

    async def get_all_groups(self) -> Sequence[AccessGroup]:
        result = await self.db.execute(select(AccessGroup).order_by(AccessGroup.bitmask_position))
        return result.scalars().all()

    async def get_groups_by_ids(self, group_ids: list[uuid.UUID]) -> Sequence[AccessGroup]:
        result = await self.db.execute(
            select(AccessGroup).where(AccessGroup.id.in_(group_ids))
        )
        return result.scalars().all()

    async def update_user_groups(self, user: User, groups: list[AccessGroup]) -> User:
        user.groups = groups
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user
