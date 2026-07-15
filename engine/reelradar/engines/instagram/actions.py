"""Engagement action policy (PRD departure: read-only is the default).

Likes/follows are the #1 trigger for Instagram action-blocks, so this gate is
deliberately conservative: opt-in per campaign, hard per-session caps, like only
on relevance, follow only on a real lead. The Session enforces human pacing and
halts on any action-block; this class only answers "are we still under cap?".
"""
from __future__ import annotations

from dataclasses import dataclass

from ...core.config import Campaign


@dataclass
class ActionPolicy:
    enabled: bool = False
    max_likes: int = 8
    max_follows: int = 4
    likes: int = 0
    follows: int = 0

    @classmethod
    def from_campaign(cls, campaign: Campaign) -> "ActionPolicy":
        return cls(
            enabled=campaign.enable_actions,
            max_likes=campaign.max_likes_per_session,
            max_follows=campaign.max_follows_per_session,
        )

    def can_like(self) -> bool:
        return self.enabled and self.likes < self.max_likes

    def can_follow(self) -> bool:
        return self.enabled and self.follows < self.max_follows

    def record_like(self) -> None:
        self.likes += 1

    def record_follow(self) -> None:
        self.follows += 1
