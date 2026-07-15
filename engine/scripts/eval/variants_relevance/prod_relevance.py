"""Eval the ACTUAL shipping relevance prompt from reelradar.prompts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from reelradar.engines.instagram.prompts import SYSTEM_RELEVANCE as SYSTEM  # noqa: E402,F401
from reelradar.core.prompts import USER_TEMPLATE  # noqa: E402,F401
