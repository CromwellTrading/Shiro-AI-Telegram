from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import Coupon, User
from app.services.economy import create_coupon


def parse_milestones(raw: str) -> list[tuple[int, float]]:
    items: list[tuple[int, float]] = []
    for item in raw.split(","):
        try:
            stars, percent = item.strip().split(":", 1)
            items.append((int(stars), min(float(percent), 10.0)))
        except Exception:
            continue
    return sorted(items)


async def grant_milestone_coupons(session: AsyncSession, user: User, settings: Settings) -> list[Coupon]:
    granted: list[Coupon] = []
    rules = parse_milestones(settings.coupon_milestones)
    existing = {
        (c.milestone_stars, c.discount_percent)
        for c in (await session.execute(select(Coupon).where(Coupon.user_id == user.id))).scalars().all()
    }
    for stars_needed, percent in rules:
        if user.stars >= stars_needed and (stars_needed, percent) not in existing:
            granted.append(await create_coupon(session, user, percent, stars_needed))
    return granted
