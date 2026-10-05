from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import (
    CheckoutSession,
    Coupon,
    IncomingPayment,
    Order,
    OrderTicket,
    Payment,
    PaymentIdentity,
    Product,
    User,
)
from app.services.economy import add_wallet_and_rewards, create_order, user_coupons
from app.services.rewards import grant_milestone_coupons


def normalize_transfer_number(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("535") and len(digits) == 11:
        return digits[3:]
    if digits.startswith("53") and len(digits) == 10:
        return digits[2:]
    return digits


def valid_transfer_number(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{6,15}", normalize_transfer_number(value)))


def verify_hmac_v2(raw_body: bytes, secret: str, signature: str, timestamp: str, max_skew_seconds: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(datetime.now(timezone.utc).timestamp()) - ts) > max_skew_seconds:
        return False
    sig = signature.strip().lower().removeprefix("sha256=")
    if not sig:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def verify_legacy_signature(raw_body: bytes, secret: str, signature: str, timestamp: str, max_skew_seconds: int = 300) -> bool:
    if not signature or not timestamp:
        return False
    return verify_hmac_v2(raw_body, secret, signature, timestamp, max_skew_seconds)


def parser_transfer_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    if str(payload.get("event", "")) != "TRANSFER_DETECTED":
        return None
    tx = payload.get("transaction") or {}
    if not isinstance(tx, dict):
        return None
    direction = str(tx.get("direction") or "").upper()
    if direction != "RECIBIDO":
        return None
    try:
        amount = round(float(tx.get("amount")), 2)
    except (TypeError, ValueError):
        return None
    transfer_number = normalize_transfer_number(tx.get("sender_phone"))
    if not valid_transfer_number(transfer_number) or amount <= 0:
        return None
    return {
        "event_id": str(payload.get("event_id") or "").strip(),
        "provider_reference": str(tx.get("transaction_id") or payload.get("event_id") or "").strip(),
        "transfer_number": transfer_number,
        "receiver_phone": normalize_transfer_number(tx.get("receiver_phone")) or None,
        "receiver_account": str(tx.get("receiver_account") or "").strip() or None,
        "amount": amount,
        "currency": str(tx.get("currency") or "CUP").upper(),
        "raw_json": json.dumps(payload, ensure_ascii=False),
    }


async def get_identity(session: AsyncSession, user: User) -> PaymentIdentity | None:
    return (await session.execute(
        select(PaymentIdentity).where(PaymentIdentity.user_id == user.id)
    )).scalar_one_or_none()


async def bind_phone(session: AsyncSession, user: User, transfer_number: str) -> tuple[bool, str]:
    phone = normalize_transfer_number(transfer_number)
    if not valid_transfer_number(phone):
        return False, "Número de teléfono inválido."
    owner = (await session.execute(
        select(PaymentIdentity).where(PaymentIdentity.transfer_number == phone)
    )).scalar_one_or_none()
    if owner and owner.user_id != user.id:
        return False, "Ese número ya está vinculado a otra cuenta de Telegram."
    existing = await get_identity(session, user)
    if existing:
        if existing.transfer_number != phone:
            pending = (await session.execute(
                select(OrderTicket).where(
                    OrderTicket.user_id == user.id,
                    OrderTicket.status == "pending",
                ).limit(1)
            )).scalar_one_or_none()
            if pending:
                return False, "No puedes cambiar el número mientras tienes un ticket pendiente. Ciérralo antes de vincular otro número."
        existing.transfer_number = phone
        existing.updated_at = datetime.now(timezone.utc)
    else:
        session.add(PaymentIdentity(user_id=user.id, transfer_number=phone))
    return True, phone


async def create_checkout(session: AsyncSession, user: User, *, kind: str, product_id: int | None = None, amount: float = 0.0) -> CheckoutSession:
    draft = (await session.execute(select(CheckoutSession).where(CheckoutSession.user_id == user.id))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if draft is None:
        draft = CheckoutSession(user_id=user.id, kind=kind, product_id=product_id, amount=round(float(amount), 2), terms_accepted=False)
        session.add(draft)
    else:
        draft.kind = kind
        draft.product_id = product_id
        draft.amount = round(float(amount), 2)
        draft.coupon_id = None
        draft.terms_accepted = False
        draft.updated_at = now
    await session.flush()
    return draft


async def clear_checkout(session: AsyncSession, user: User) -> None:
    draft = (await session.execute(select(CheckoutSession).where(CheckoutSession.user_id == user.id))).scalar_one_or_none()
    if draft:
        await session.delete(draft)


async def open_ticket_for_checkout(session: AsyncSession, user: User, settings: Settings, service_online: bool) -> OrderTicket:
    draft = (await session.execute(select(CheckoutSession).where(CheckoutSession.user_id == user.id))).scalar_one_or_none()
    if not draft:
        raise ValueError("No hay una operación preparada.")
    identity = await get_identity(session, user)
    if not identity:
        raise ValueError("Debes vincular tu número de teléfono antes de crear la orden.")
    if not draft.terms_accepted:
        raise ValueError("Debes aceptar las condiciones antes de crear el ticket.")
    existing = (await session.execute(
        select(OrderTicket).where(OrderTicket.user_id == user.id, OrderTicket.status == "pending").limit(1)
    )).scalar_one_or_none()
    if existing:
        raise ValueError(f"Ya tienes un ticket pendiente: #{existing.id}. Cierra esa orden antes de crear otra.")

    if draft.kind == "wallet":
        amount = round(float(draft.amount), 2)
        order = None
    else:
        product = (await session.execute(
            select(Product).where(Product.id == draft.product_id, Product.active.is_(True))
        )).scalar_one_or_none()
        if not product:
            raise ValueError("La oferta ya no está disponible.")
        coupon = None
        if draft.coupon_id:
            coupon = (await session.execute(
                select(Coupon).where(Coupon.id == draft.coupon_id, Coupon.user_id == user.id, Coupon.used.is_(False))
            )).scalar_one_or_none()
            if coupon and coupon.expires_at and coupon.expires_at <= datetime.now(timezone.utc):
                coupon = None
        order = await create_order(session, user, product, coupon)
        amount = order.total

    ticket = OrderTicket(
        user_id=user.id,
        order_id=order.id if order else None,
        kind=draft.kind,
        product_id=product.id if draft.kind == "purchase" and product else None,
        transfer_number=identity.transfer_number,
        amount=amount,
        subtotal=float(product.price) if draft.kind == "purchase" and product else amount,
        discount_percent=float(coupon.discount_percent) if draft.kind == "purchase" and coupon else 0.0,
        coupon_id=coupon.id if draft.kind == "purchase" and coupon else None,
        currency="CUP" if draft.kind == "wallet" else order.currency,
        terms_accepted=True,
        service_online_at_creation=service_online,
        status="pending",
    )
    session.add(ticket)
    await session.flush()
    await clear_checkout(session, user)
    return ticket


async def match_incoming_payment(session: AsyncSession, incoming: IncomingPayment, online: bool) -> OrderTicket | None:
    if not online:
        incoming.status = "held_offline"
        return None
    q = select(OrderTicket).where(
        OrderTicket.status == "pending",
        OrderTicket.transfer_number == incoming.transfer_number,
        OrderTicket.currency == incoming.currency,
    ).order_by(OrderTicket.created_at.asc())
    candidates = list((await session.execute(q)).scalars().all())
    tickets = [t for t in candidates if round(float(t.amount), 2) == round(float(incoming.amount), 2)]
    if len(tickets) == 1:
        ticket = tickets[0]
        ticket.status = "processing"
        ticket.provider_reference = incoming.provider_reference or incoming.event_id
        incoming.matched_ticket_id = ticket.id
        incoming.status = "matched"
        incoming.matched_at = datetime.now(timezone.utc)
        return ticket
    if len(tickets) > 1:
        incoming.status = "needs_review"
        return None
    incoming.status = "unmatched"
    return None


async def apply_matched_payment(session: AsyncSession, incoming: IncomingPayment, ticket: OrderTicket, settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    external_id = incoming.provider_reference or incoming.event_id
    payment = (await session.execute(select(Payment).where(Payment.external_id == external_id))).scalar_one_or_none()
    if payment is None:
        payment = Payment(
            external_id=external_id,
            telegram_id=None,
            amount=incoming.amount,
            currency=incoming.currency,
            status="confirmed",
            raw_json=incoming.raw_json,
        )
        session.add(payment)
        await session.flush()

    user = (await session.execute(select(User).where(User.id == ticket.user_id))).scalar_one()
    payment.telegram_id = user.telegram_id
    ticket.payment_id = payment.id
    ticket.status = "paid"
    ticket.paid_at = now
    if ticket.provider_reference is None:
        ticket.provider_reference = external_id
    incoming.status = "matched"
    incoming.matched_ticket_id = ticket.id
    incoming.matched_at = now

    result: dict[str, Any] = {"ticket": ticket, "user": user, "payment": payment, "stars": 0}
    if ticket.kind == "wallet":
        stars = await add_wallet_and_rewards(session, user, incoming.amount, settings, external_id, payment.id)
        result["stars"] = stars
        coupons = await grant_milestone_coupons(session, user, settings)
        result["coupons"] = coupons
    else:
        order = None
        if ticket.order_id:
            order = (await session.execute(select(Order).where(Order.id == ticket.order_id))).scalar_one_or_none()
            if order:
                order.status = "paid"
                if ticket.coupon_id:
                    coupon = (await session.execute(select(Coupon).where(Coupon.id == ticket.coupon_id))).scalar_one_or_none()
                    if coupon:
                        coupon.used = True
        result["order"] = order
    return result


async def record_and_process_payment(session: AsyncSession, parsed: dict[str, Any], settings: Settings, service_online: bool) -> tuple[IncomingPayment, dict[str, Any] | None]:
    event_id = parsed["event_id"]
    existing = (await session.execute(select(IncomingPayment).where(IncomingPayment.event_id == event_id))).scalar_one_or_none()
    if existing:
        return existing, {"duplicate": True, "payment": existing}
    incoming = IncomingPayment(
        event_id=event_id,
        provider_reference=parsed["provider_reference"] or None,
        transfer_number=parsed["transfer_number"],
        receiver_phone=parsed.get("receiver_phone"),
        receiver_account=parsed.get("receiver_account"),
        amount=parsed["amount"],
        currency=parsed.get("currency", "CUP"),
        status="held_offline" if not service_online else "unmatched",
        raw_json=parsed["raw_json"],
        received_at=datetime.now(timezone.utc),
    )
    session.add(incoming)
    await session.flush()
    ticket = await match_incoming_payment(session, incoming, service_online)
    if ticket:
        return incoming, await apply_matched_payment(session, incoming, ticket, settings)
    return incoming, None


async def reconcile_held_payments(session: AsyncSession, settings: Settings) -> list[dict[str, Any]]:
    held = list((await session.execute(
        select(IncomingPayment).where(IncomingPayment.status == "held_offline").order_by(IncomingPayment.received_at.asc())
    )).scalars().all())
    results: list[dict[str, Any]] = []
    for incoming in held:
        ticket = await match_incoming_payment(session, incoming, True)
        if ticket:
            results.append(await apply_matched_payment(session, incoming, ticket, settings))
    await session.flush()
    return results
