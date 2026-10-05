from __future__ import annotations

from datetime import datetime, timezone
from math import floor
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import Coupon, DailyXP, Order, Product, User, WalletTransaction


def level_for_xp(xp: int) -> int:
    level = 1
    threshold = 120
    remaining = max(0, xp)
    while remaining >= threshold and level < 100:
        remaining -= threshold
        level += 1
        threshold = 120 + (level - 1) * 65
    return level


def xp_to_next_level(xp: int) -> tuple[int, int]:
    level = level_for_xp(xp)
    base = 0
    threshold = 120
    for current in range(1, level):
        base += threshold
        threshold = 120 + current * 65
    return max(0, xp - base), threshold


def valid_message_score(text: str) -> tuple[bool, str]:
    normalized = " ".join(text.strip().split())
    if len(normalized) < 3:
        return False, "demasiado_corto"
    compact = normalized.lower().replace(" ", "")
    alnum = sum(ch.isalnum() for ch in compact)
    if alnum < 3:
        return False, "sin_contenido"
    unique = len(set(compact))
    if len(compact) >= 8 and unique <= 3:
        return False, "repetitivo"
    # Repeated short chunks such as asdasdasdasd / qweqweqwe.
    for n in (2, 3, 4):
        if len(compact) >= n * 4 and compact[:n] * (len(compact) // n) == compact:
            return False, "repetitivo"
    return True, "ok"


async def grant_message_xp(session: AsyncSession, user: User, settings: Settings) -> int:
    now = datetime.now(timezone.utc)
    today = now.date()
    row = (await session.execute(
        select(DailyXP).where(DailyXP.user_id == user.id, DailyXP.day == today)
    )).scalar_one_or_none()
    if row is None:
        row = DailyXP(user_id=user.id, day=today, amount=0)
        session.add(row)
        await session.flush()
        user.streak = user.streak + 1 if user.last_activity and (today - user.last_activity.date()).days == 1 else 1
    if row.amount >= settings.xp_daily_cap:
        user.last_activity = now
        return 0
    if user.last_xp_at and (now - user.last_xp_at).total_seconds() < settings.xp_cooldown_seconds:
        user.last_activity = now
        return 0
    grant = min(settings.xp_per_valid_message, settings.xp_daily_cap - row.amount)
    row.amount += grant
    user.xp += grant
    user.valid_messages += 1
    user.level = level_for_xp(user.xp)
    user.last_xp_at = now
    user.last_xp_day = today
    user.last_activity = now
    return grant


async def add_wallet_and_rewards(session: AsyncSession, user: User, amount: float, settings: Settings, external_id: str, payment_id: int | None = None) -> int:
    existing = (await session.execute(
        select(WalletTransaction).where(WalletTransaction.external_id == external_id)
    )).scalar_one_or_none()
    if existing:
        return existing.stars
    amount = round(float(amount), 2)
    user.wallet_balance = round(float(user.wallet_balance) + amount, 2)
    stars = floor(amount / settings.star_amount_step) * settings.star_per_step
    user.stars += stars
    session.add(WalletTransaction(
        user_id=user.id,
        payment_id=payment_id,
        external_id=external_id,
        amount=amount,
        stars=stars,
        reason="wallet_deposit",
    ))
    return stars


async def active_products(session: AsyncSession, game: str | None = None) -> list[Product]:
    q = select(Product).where(Product.active.is_(True)).order_by(Product.game, Product.sort_order, Product.id)
    if game:
        q = q.where(Product.game == game)
    return list((await session.execute(q)).scalars().all())


async def create_order(session: AsyncSession, user: User, product: Product, coupon: Coupon | None = None) -> Order:
    subtotal = round(float(product.price), 2)
    discount = min(max(float(coupon.discount_percent), 0.0), 10.0) if coupon else 0.0
    total = round(subtotal * (1 - discount / 100), 2)
    order = Order(
        user_id=user.id,
        product_id=product.id,
        total=total,
        currency=product.currency,
        status="pending",
    )
    session.add(order)
    await session.flush()
    return order


async def user_coupons(session: AsyncSession, user: User) -> list[Coupon]:
    now = datetime.now(timezone.utc)
    q = select(Coupon).where(Coupon.user_id == user.id, Coupon.used.is_(False))
    rows = list((await session.execute(q)).scalars().all())
    return [c for c in rows if c.expires_at is None or c.expires_at > now]


async def create_coupon(session: AsyncSession, user: User, percent: float, milestone_stars: int | None = None) -> Coupon:
    code = f"SHIRO-{token_hex(4).upper()}"
    coupon = Coupon(user_id=user.id, code=code, discount_percent=min(percent, 10.0), milestone_stars=milestone_stars)
    session.add(coupon)
    await session.flush()
    return coupon
