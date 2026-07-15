"""The prompt that actually ships — imported from reelradar.prompts so the
eval always measures production behavior, never a stale copy."""
from reelradar.engines.instagram.prompts import SYSTEM_MATCH as SYSTEM  # noqa: F401
from reelradar.core.prompts import USER_TEMPLATE  # noqa: F401
