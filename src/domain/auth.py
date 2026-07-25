import uuid
from datetime import timedelta
from fastapi import HTTPException, status
from src.core.security import verify_password, get_password_hash, create_access_token
from src.core.config import settings
from src.models.user import User
from src.repositories.user import UserRepository

class AuthService:
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo

    async def authenticate_user(self, email: str, password: str) -> User:
        user = await self.user_repo.get_by_email(email)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    async def register_user(self, email: str, name: str, password: str, dept: str | None = None, role: str = "Staff") -> User:
        existing = await self.user_repo.get_by_email(email)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
            
        hashed_password = get_password_hash(password)
        user = User(
            email=email,
            name=name,
            password_hash=hashed_password,
            dept=dept,
            role=role
        )
        
        # Seed access group mapping
        # Automatically assign every user to a "public" access group (index 0) if it exists,
        # or create it if it doesn't exist yet (very useful for local bootstrapping!).
        from src.models.user import AccessGroup
        public_group = await self.user_repo.get_group_by_name("public")
        if not public_group:
            public_group = AccessGroup(name="public", bitmask_position=0)
            public_group = await self.user_repo.create_group(public_group)
            
        user.groups.append(public_group)
        
        # If user is in a department, auto create/assign department group too (e.g. at bit position based on dept name length or random)
        if dept:
            dept_group_name = f"dept_{dept.lower()}"
            dept_group = await self.user_repo.get_group_by_name(dept_group_name)
            if not dept_group:
                # Find max bitmask position and increment
                all_groups = await self.user_repo.get_all_groups()
                max_pos = max([g.bitmask_position for g in all_groups]) if all_groups else 0
                dept_group = AccessGroup(name=dept_group_name, bitmask_position=max_pos + 1)
                dept_group = await self.user_repo.create_group(dept_group)
            user.groups.append(dept_group)

        return await self.user_repo.create(user)

    def create_token(self, user: User) -> str:
        # Save email as subject
        expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        return create_access_token(subject=user.email, expires_delta=expires)
