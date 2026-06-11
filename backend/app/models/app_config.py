from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AppConfig(Base, TimestampMixin):
    """Single-row application configuration (white-label branding, etc.).

    There is always exactly one row, with id=1. Read by the PUBLIC
    ``GET /api/config`` endpoint (so the unauthenticated login/register pages
    can show the brand name) and mutated by admins via
    ``PATCH /api/admin/config``.
    """

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    business_name: Mapped[str] = mapped_column(
        String(120), nullable=False, default="The Option Haven",
        server_default="The Option Haven",
    )
