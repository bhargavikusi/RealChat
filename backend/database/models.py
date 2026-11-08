"""Imports every model module so Base.metadata is complete.

Alembic's env.py imports this; add each new model module here.
"""

from backend.modules.admin import models as admin  # noqa: F401
from backend.modules.chat import models as chat  # noqa: F401
from backend.modules.chat_requests import models as chat_requests  # noqa: F401
from backend.modules.coins import models as coins  # noqa: F401
from backend.modules.direct_messages import models as direct_messages  # noqa: F401
from backend.modules.moderation import models as moderation  # noqa: F401
from backend.modules.people import models as people  # noqa: F401
from backend.modules.users import models as users  # noqa: F401
