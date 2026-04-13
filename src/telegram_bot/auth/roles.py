from pydantic import BaseModel

from src.config import app_config


class RoleResolver(BaseModel):
    admin_ids: set[int]
    allowed_user_ids: set[int]

    @classmethod
    def from_config(cls) -> "RoleResolver":
        return cls(
            admin_ids=set(app_config.admin_users),
            allowed_user_ids=set(app_config.allowed_users),
        )

    def resolve_role(self, user_id: int) -> str:
        if user_id in self.admin_ids:
            return "admin"
        if user_id in self.allowed_user_ids:
            return "user"
        return "guest"

    def can_chat(self, user_id: int) -> bool:
        # If no users are configured, deny all.
        if not self.admin_ids and not self.allowed_user_ids:
            return False
        return user_id in self.admin_ids or user_id in self.allowed_user_ids