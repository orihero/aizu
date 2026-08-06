"""The prompt that actually ships — imported from aizu.prompts so the
eval always measures production behavior, never a stale copy."""
from aizu.engines.instagram.prompts import SYSTEM_MATCH as SYSTEM  # noqa: F401
from aizu.core.prompts import USER_TEMPLATE  # noqa: F401
