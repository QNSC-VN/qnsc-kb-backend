import uuid
from datetime import datetime
from sqlalchemy import Table, Column, ForeignKey, String, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.models.base import Base, UUIDPrimaryKeyMixin, TimestampMixin

# Association table for User <-> AccessGroup (Many-to-Many)
user_groups = Table(
    "user_groups",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("access_groups.id", ondelete="CASCADE"), primary_key=True),
)

class AccessGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "access_groups"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # position of the bit in the bitmask (0, 1, 2, ... up to 63 for 64-bit integer, or higher if using numeric)
    bitmask_position: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship(
        "User", secondary=user_groups, back_populates="groups"
    )

class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    company_domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False, default="local")
    dept: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Admin, CEO, Department Owner, Reviewer, Staff
    role: Mapped[str] = mapped_column(String(50), default="Staff", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    groups: Mapped[list[AccessGroup]] = relationship(
        "AccessGroup", secondary=user_groups, back_populates="users"
    )
