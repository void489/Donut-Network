"""Single source of truth for the bot's tone lives in the top-level personality.py.
This module just re-exports it so `from utils.personality import ...` keeps working
and there is only ONE personality to edit."""
from personality import PERSONALITY, OWNER_SPECIAL_CASE, OWNER_USER_ID, OWNER_NAME  # noqa: F401
