"""Imports every model module so Base.metadata is complete.

Alembic's env.py imports this; add each new model module here.
"""

from backend.modules.admin import models as admin  # noqa: F401
from backend.modules.chat import models as chat  # noqa: F401
from backend.modules.moderation import models as moderation  # noqa: F401
from backend.modules.users import models as users  # noqa: F401
