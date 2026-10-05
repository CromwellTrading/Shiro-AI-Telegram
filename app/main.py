from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import random
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn

from app.config import BASE_DIR, Settings
import app.db as db
from app.db import (
    CheckoutSession,
    Coupon,
    Game,
    GameEvent,
    IncomingPayment,
    Order,
    OrderTicket,
    Payment,
    PaymentIdentity,
    Product,
    Source,
    Suggestion,
    Subscription,
    User,
    ChatMessage,
    create_tables,
    get_or_create_user,
    init_db,
    get_setting,
    set_setting,
)
from app.services.ai import OpenRouterAI
from app.services.economy import (
    active_products,
    create_order,
    grant_message_xp,
    level_for_xp,
    user_coupons,
    valid_message_score,
    xp_to_next_level,
)
from app.services.events import EventCollector
from app.services.memory import build_context, remember_message
from app.services.moderation import contains_link, bot_invocation, media_explanation, is_blacklisted
from app.services.payments import (
    bind_phone,
    clear_checkout,
    create_checkout,
    get_identity,
    normalize_transfer_number,
    open_ticket_for_checkout,
    parser_transfer_from_payload,
    reconcile_held_payments,
    record_and_process_payment,
    verify_hmac_v2,
)
from app.services.rewards import grant_milestone_coupons
from app.services.search import discover_sources

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shiro")
settings = Settings.from_env()

PROMPT_PATH = BASE_DIR / "data" / "shiro_personality.txt"
PERSONALITY = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else "Eres Shiro Synthesis Two."
init_db(settings.database_url)

bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)
ai = OpenRouterAI(settings, PERSONALITY)
collector = EventCollector(settings, db.SessionLocal, ai)

BOT_USERNAME = ""
payment_lock = asyncio.Lock()

PURCHASE_RE = re.compile(
    r"(?i)\b(quiero\s+(comprar|recargar)|quiero\s+\d+|comprar|recargar|precio\s+de|cu[aá]nto\s+cuesta|diamantes|robux|c[oó]digos?\s+de|saldo)\b"
)
LOOKUP_RE = re.compile(
    r"(?i)\b(clima|tiempo|temperatura|pron[oó]stico|noticia|noticias|efem[eé]ride|qu[eé]\s+pas[oó]\s+el|qu[eé]\s+ocurri[oó]\s+el|fecha\s+de|evento[s]?|skin|parche|actualizaci[oó]n|lanzamiento|hoy|ma[ñn]ana|ayer|[ú]ltim[ao]s?|qui[eé]n\s+gan[oó]|resultado[s]?)\b"
)

TERMS_TEXT = (
    "<b>Condiciones del ticket</b>\n\n"
    "1. Debes pagar desde el número de teléfono que tienes vinculado.\n"
    "2. Crea el ticket <b>antes</b> de realizar la transferencia.\n"
    "3. Debes enviar exactamente el importe indicado.\n"
    "4. El ticket <b>no tiene fecha de vencimiento</b>; permanece abierto hasta que tú o el administrador lo cierren.\n"
    "5. Si el servicio está OFFLINE, puedes pagar igualmente. El pago quedará guardado y no se acreditará automáticamente hasta que el servicio vuelva a ONLINE.\n"
    "6. Si ya pagaste, <b>no cierres el ticket</b>; al cerrarlo, el pago no podrá asociarse automáticamente a esa orden.\n"
    "7. La acreditación y el estado del pago dependen del sistema de pagos; decir 'ya pagué' no confirma una operación."
)


def admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def money(value: float) -> str:
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


async def service_online(session: AsyncSession) -> bool:
    return (await get_setting(session, "service_online", "true")).lower() == "true"


def private_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Buscar ofertas", callback_data="shop")],
        [InlineKeyboardButton(text="💰 Mi wallet", callback_data="wallet"), InlineKeyboardButton(text="💳 Añadir saldo", callback_data="deposit")],
        [InlineKeyboardButton(text="⭐ Mis estrellas", callback_data="stars"), InlineKeyboardButton(text="🎟️ Mis cupones", callback_data="coupons")],
        [InlineKeyboardButton(text="📦 Hacer pedido", callback_data="request")],
        [InlineKeyboardButton(text="📱 Mi número", callback_data="phone")],
        [InlineKeyboardButton(text="🧾 Mis tickets", callback_data="mytickets")],
    ])


def group_shop_keyboard() -> InlineKeyboardMarkup:
    url = f"https://t.me/{BOT_USERNAME}?start=shop" if BOT_USERNAME else "https://t.me/"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Buscar ofertas en mi chat", url=url)]])


def phone_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Vincular mi número", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Comparte el número con el que pagarás",
    )


def ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cerrar orden", callback_data=f"ticket:close:{ticket_id}")],
        [InlineKeyboardButton(text="⬅️ Menú", callback_data="menu")],
    ])


def checkout_keyboard(draft: CheckoutSession, has_coupon: bool) -> InlineKeyboardMarkup:
    accepted = draft.terms_accepted
    rows = [
        [InlineKeyboardButton(text=("✅ Entiendo y acepto" if accepted else "☐ Entiendo y acepto"), callback_data="checkout:terms")],
    ]
    if draft.kind == "purchase" and has_coupon:
        rows.append([InlineKeyboardButton(text="🎟️ Elegir cupón", callback_data="checkout:coupon")])
    rows.append([InlineKeyboardButton(text="🧾 Crear ticket de pago" if accepted else "🔒 Acepta primero las condiciones", callback_data="checkout:create")])
    rows.append([InlineKeyboardButton(text="❌ Cancelar", callback_data="checkout:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_ticket_keyboard(ticket: OrderTicket) -> InlineKeyboardMarkup:
    rows = []
    if ticket.status == "pending":
        rows.append([InlineKeyboardButton(text="❌ Cerrar ticket", callback_data=f"admin:close:{ticket.id}")])
    if ticket.status == "paid" and ticket.kind == "purchase" and ticket.order_id:
        rows.append([InlineKeyboardButton(text="✅ Marcar recarga procesada", callback_data=f"admin:done:{ticket.order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(inline_keyboard=[])


async def send_shiro(chat_id: int, text: str, **kwargs):
    if not text or text.strip() == "SKIP":
        return
    await bot.send_message(chat_id, text, **kwargs)


async def user_context(session: AsyncSession, tg_user) -> User:
    return await get_or_create_user(session, tg_user.id, tg_user.username, tg_user.full_name)


def decide_group_reply(message: Message) -> bool:
    text = message.text or message.caption or ""
    if bot_invocation(text):
        return True
    if text.endswith("?") or "¿" in text:
        return True
    if len(text) >= 100:
        return random.random() < 0.30
    return random.random() < 0.04


def is_lookup_request(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if any(mark in lowered for mark in ("http://", "https://")):
        return False
    return bool(LOOKUP_RE.search(text)) and ("?" in text or "¿" in text or "qué" in lowered or "cual" in lowered)


async def reply_with_ai(message: Message, user: User, session: AsyncSession, private: bool = False, prefix_context: str = ""):
    context = await build_context(session, user, message.chat.id)
    if prefix_context:
        context += f"\n\nINFORMACIÓN DE SISTEMA:\n{prefix_context}"
    text = message.text or message.caption or ""
    ctype = "text"
    if message.photo:
        ctype = "photo"
    elif message.video:
        ctype = "video"
    elif message.animation:
        ctype = "animation"
    if ctype != "text" and not text:
        await send_shiro(message.chat.id, "Me llegó, pero no puedo ver esa imagen/video todavía 😭. Si me cuentas qué aparece, lo comentamos.", reply_to_message_id=message.message_id)
        return
    if ctype != "text":
        context += "\n\nAVISO SOBRE MEDIA:\n" + media_explanation(ctype)
    response = await ai.chat(text or "El usuario envió contenido multimedia.", context, private=private)
    if response.strip() == "SKIP":
        return
    await send_shiro(message.chat.id, response, reply_to_message_id=message.message_id)
    session.add(ChatMessage(
        telegram_message_id=message.message_id,
        chat_id=message.chat.id,
        user_id=user.id,
        role="assistant",
        text=response,
        content_type="text",
    ))


async def notify_admins(text: str, reply_markup=None):
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as exc:
            log.warning("admin notify failed: %s", exc)


async def render_checkout(target_message: Message, session: AsyncSession, user: User):
    draft = (await session.execute(select(CheckoutSession).where(CheckoutSession.user_id == user.id))).scalar_one_or_none()
    if not draft:
        await target_message.answer("No hay ninguna operación preparada.", reply_markup=private_keyboard())
        return
    identity = await get_identity(session, user)
    if not identity:
        await target_message.answer(
            "📱 Antes de crear cualquier compra o recarga de wallet necesito que vincules el número de teléfono desde el que vas a pagar.\n\n"
            "Pulsa el botón de Telegram para compartirlo.",
            reply_markup=phone_request_keyboard(),
        )
        return
    product = None
    coupon = None
    if draft.kind == "purchase":
        product = (await session.execute(select(Product).where(Product.id == draft.product_id))).scalar_one_or_none()
        if not product or not product.active:
            await clear_checkout(session, user)
            await session.commit()
            await target_message.answer("Esa oferta ya no está disponible. Busca las ofertas otra vez.", reply_markup=private_keyboard())
            return
        if draft.coupon_id:
            coupon = (await session.execute(select(Coupon).where(Coupon.id == draft.coupon_id, Coupon.user_id == user.id, Coupon.used.is_(False)))).scalar_one_or_none()
            if coupon and coupon.expires_at and coupon.expires_at <= datetime.now(timezone.utc):
                coupon = None
                draft.coupon_id = None
        subtotal = float(product.price)
        discount = float(coupon.discount_percent) if coupon else 0.0
        total = round(subtotal * (1 - discount / 100), 2)
        draft.amount = total
        label = f"🎮 <b>{esc(product.game)}</b>\n📦 {esc(product.name)}\n💰 Precio: <b>{money(subtotal)} {esc(product.currency)}</b>"
        if coupon:
            label += f"\n🎟️ Cupón: <b>{esc(coupon.code)}</b> · -{money(discount)}%\n💵 Total: <b>{money(total)} {esc(product.currency)}</b>"
        else:
            label += f"\n💵 Total: <b>{money(total)} {esc(product.currency)}</b>"
    else:
        draft.amount = round(float(draft.amount), 2)
        label = f"💳 <b>Añadir {money(draft.amount)} CUP a la wallet</b>"

    status_text = "🟢 ONLINE: un pago coincidente podrá acreditarse/procesarse automáticamente." if await service_online(session) else "🔴 OFFLINE: puedes pagar, pero el pago quedará retenido y no se acreditará automáticamente hasta que vuelva a ONLINE."
    text = (
        f"🧾 <b>Preparando operación</b>\n\n{label}\n\n"
        f"📱 Número vinculado: <code>{esc(identity.transfer_number)}</code>\n\n"
        f"{TERMS_TEXT}\n\n{status_text}\n\n"
        f"Marca la casilla para poder crear el ticket."
    )
    coupons = await user_coupons(session, user) if draft.kind == "purchase" else []
    await session.commit()
    await target_message.answer(text, reply_markup=checkout_keyboard(draft, bool(coupons)))


async def finish_ticket(ticket: OrderTicket, user: User, session: AsyncSession):
    identity = await get_identity(session, user)
    online = await service_online(session)
    status = "🟢 ONLINE" if online else "🔴 OFFLINE"
    if ticket.kind == "wallet":
        title = f"💳 <b>Ticket de recarga de wallet #{ticket.id}</b>"
        extra = "El saldo se acreditará automáticamente cuando el pago sea detectado." if online else "Puedes pagar ahora. El pago se guardará y el saldo se acreditará automáticamente cuando el servicio vuelva a ONLINE."
    else:
        order = (await session.execute(select(Order).where(Order.id == ticket.order_id))).scalar_one_or_none()
        product = (await session.execute(select(Product).where(Product.id == order.product_id))).scalar_one_or_none() if order and order.product_id else None
        title = f"🛒 <b>Ticket de compra #{ticket.id}</b>"
        extra = "Cuando llegue el pago coincidente, se marcará como pagado y se avisará al administrador para procesar la recarga." if online else "Puedes pagar estando OFFLINE. El pago quedará guardado y se procesará cuando el servicio pase a ONLINE."
        if product:
            title += f"\n\n🎮 {esc(product.game)} · {esc(product.name)}"
    destination = settings.transfermovil_destination or "el destino indicado por el administrador"
    text = (
        f"{title}\n\n"
        f"💰 Importe exacto: <b>{money(ticket.amount)} {esc(ticket.currency)}</b>\n"
        f"📱 Paga desde: <code>{esc(identity.transfer_number if identity else ticket.transfer_number)}</code>\n"
        f"🏦 Destino: <code>{esc(destination)}</code>\n"
        f"📌 Estado del servicio: {status}\n\n"
        f"{extra}\n\n"
        f"⚠️ No hay límite de tiempo para pagar mientras el ticket siga abierto. Si ya pagaste, no cierres la orden."
    )
    await bot.send_message(user.telegram_id, text, reply_markup=ticket_keyboard(ticket.id))


async def notify_admin_ticket(ticket: OrderTicket, user: User, session: AsyncSession, event_note: str = ""):
    identity = await get_identity(session, user)
    online = await service_online(session)
    order = (await session.execute(select(Order).where(Order.id == ticket.order_id))).scalar_one_or_none() if ticket.order_id else None
    product = (await session.execute(select(Product).where(Product.id == order.product_id))).scalar_one_or_none() if order and order.product_id else None
    if ticket.kind == "wallet":
        body = f"💳 <b>Nuevo ticket de wallet #{ticket.id}</b>"
    else:
        body = f"🛒 <b>Nuevo ticket de compra #{ticket.id}</b>"
        if product:
            body += f"\n🎮 {esc(product.game)} · {esc(product.name)}"
    body += (
        f"\n\n👤 {esc(user.full_name)} (@{esc(user.username or 'sin_username')})"
        f"\n🆔 <code>{user.telegram_id}</code>"
        f"\n📱 <code>{esc(identity.transfer_number if identity else ticket.transfer_number)}</code>"
        f"\n💰 <b>{money(ticket.amount)} {esc(ticket.currency)}</b>"
        f"\n📌 Creado con servicio: {'ONLINE' if ticket.service_online_at_creation else 'OFFLINE'}"
        f"\n📡 Ahora: {'ONLINE' if online else 'OFFLINE'}"
    )
    if ticket.discount_percent:
        body += f"\n🎟️ Descuento: {money(ticket.discount_percent)}%"
    if event_note:
        body += f"\n\n{event_note}"
    await notify_admins(body, admin_ticket_keyboard(ticket))


@router.message(CommandStart())
async def cmd_start(message: Message):
    async with db.SessionLocal() as session:
        user = await user_context(session, message.from_user)
        await session.commit()
    if message.chat.type == ChatType.PRIVATE:
        deep = (message.text or "").split(maxsplit=1)
        suffix = deep[1].strip().lower() if len(deep) > 1 else ""
        if suffix == "shop":
            await message.answer("🛒 Vamos a buscar ofertas. Elige una categoría:", reply_markup=private_keyboard())
            return
        await message.answer(
            "<b>Shiro Synthesis Two</b> 🫡\n\n"
            "Bienvenido a mi rincón privado. Aquí puedes hablar conmigo, buscar ofertas, consultar tu wallet, estrellas, cupones o hacer un pedido.",
            reply_markup=private_keyboard(),
        )
    else:
        await message.answer("Estoy aquí (⁠☞⁠ ಠ_ಠ⁠)⁠☞")


@router.message(F.contact)
async def contact_received(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    contact = message.contact
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer("Ese contacto no corresponde a tu cuenta de Telegram. Comparte tu propio número.", reply_markup=phone_request_keyboard())
        return
    async with db.SessionLocal() as session:
        user = await user_context(session, message.from_user)
        ok, value = await bind_phone(session, user, contact.phone_number)
        await session.commit()
        if not ok:
            await message.answer(value, reply_markup=private_keyboard())
            return
        normalized = value
        await message.answer(
            f"✅ Número vinculado: <code>{esc(normalized)}</code>\n\nEse será el número que usaré para asociar tus pagos recibidos.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await render_checkout(message, session, user)


@router.message(Command("vincular"))
async def cmd_vincular(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Para vincular el número, escríbeme al privado.")
        return
    await message.answer("📱 Comparte tu propio número con el botón de Telegram. Es el número que utilizarás para pagar.", reply_markup=phone_request_keyboard())


@router.message(Command("inspect"))
async def cmd_inspect(message: Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Uso: /inspect @usuario o /inspect ID")
        return
    target = args[1].strip()
    async with db.SessionLocal() as session:
        if target.startswith("@"):
            uname = target[1:].lower()
            result = await session.execute(select(User).where(func.lower(User.username) == uname))
        else:
            try:
                tg_id = int(target)
            except ValueError:
                await message.answer("Necesito un @username o un Telegram ID válido.")
                return
            result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if not user:
            await message.answer("No encuentro a esa persona en mi memoria todavía.")
            return
        xp_now, xp_needed = xp_to_next_level(user.xp)
        display = f"@{user.username}" if user.username else user.full_name
        text = (
            f"👤 <b>{esc(display)}</b>\n"
            f"🏷️ Nivel: <b>{user.level}</b>\n"
            f"✨ XP: <b>{xp_now}/{xp_needed}</b>\n"
            f"💬 Mensajes válidos: <b>{user.valid_messages}</b>\n"
            f"⭐ Estrellas: <b>{user.stars}</b>\n"
            f"📅 En el grupo desde: <b>{user.joined_at.date()}</b>\n"
            f"🔥 Racha: <b>{user.streak}</b>"
        )
        if admin(message.from_user.id):
            identity = await get_identity(session, user)
            coupons = await user_coupons(session, user)
            text += f"\n💰 Wallet: <b>{money(user.wallet_balance)} CUP</b>\n📱 Teléfono: <code>{esc(identity.transfer_number if identity else 'no vinculado')}</code>\n🎟️ Cupones activos: <b>{len(coupons)}</b>\n⚠️ Advertencias: <b>{user.warnings}</b>"
        await message.answer(text)


@router.message(Command("sugerencias"))
async def cmd_suggestion(message: Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or len(args[1].strip()) < 5:
        await message.answer("Uso: /sugerencias texto_de_tu_sugerencia")
        return
    async with db.SessionLocal() as session:
        user = await user_context(session, message.from_user)
        session.add(Suggestion(user_id=user.id, text=args[1].strip()))
        await session.commit()
    await message.answer("📝 Guardada. La dejaré junto a las demás sugerencias del grupo.")


@router.message(Command("pedido"))
async def cmd_request(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        await message.answer("📦 Para hacer un pedido, escríbeme al privado y usa /pedido allí.", reply_markup=group_shop_keyboard())
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Uso: /pedido lo que estás buscando")
        return
    async with db.SessionLocal() as session:
        user = await user_context(session, message.from_user)
        order = Order(user_id=user.id, request_text=args[1].strip(), total=0, currency="CUP", status="request")
        session.add(order)
        await session.commit()
        oid = order.id
    await message.answer(f"📦 Pedido #{oid} registrado. Ya quedó visible para el administrador.")
    await notify_admins(f"📦 <b>Nuevo pedido #{oid}</b>\n👤 {esc(user.full_name)}\n🆔 <code>{user.telegram_id}</code>\n📝 {esc(args[1].strip())}")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Ese comando es solo para el administrador.")
        return
    async with db.SessionLocal() as session:
        users = await session.scalar(select(func.count(User.id))) or 0
        valid = await session.scalar(select(func.sum(User.valid_messages))) or 0
        suggestions = await session.scalar(select(func.count(Suggestion.id)).where(Suggestion.status == "new")) or 0
        orders = await session.scalar(select(func.count(Order.id)).where(Order.status.in_(["pending", "paid", "processing", "request"]))) or 0
        payments = await session.scalar(select(func.sum(Payment.amount)).where(Payment.status == "confirmed")) or 0
        pending_tickets = await session.scalar(select(func.count(OrderTicket.id)).where(OrderTicket.status == "pending")) or 0
        held = await session.scalar(select(func.count(IncomingPayment.id)).where(IncomingPayment.status.in_(["held_offline", "needs_review", "unmatched"]))) or 0
        online = await service_online(session)
    await message.answer(
        f"📊 <b>Estadísticas</b>\n\n"
        f"👥 Usuarios: {users}\n💬 Mensajes válidos: {valid}\n📝 Sugerencias nuevas: {suggestions}\n"
        f"📦 Órdenes/tickets: {orders}/{pending_tickets}\n💰 Pagos confirmados: {money(payments)} CUP\n"
        f"🧾 Pagos pendientes de revisión: {held}\n📌 Servicio: {'🟢 ONLINE' if online else '🔴 OFFLINE'}"
    )


async def set_online_and_reconcile() -> int:
    processed = 0
    async with db.SessionLocal() as session:
        await set_setting(session, "service_online", "true")
        await session.commit()
    async with db.SessionLocal() as session:
        async with payment_lock:
            results = await reconcile_held_payments(session, settings)
            for result in results:
                processed += 1
                ticket: OrderTicket = result["ticket"]
                user: User = result["user"]
                coupons = result.get("coupons") or []
                if ticket.kind == "wallet":
                    coupon_text = ""
                    if coupons:
                        coupon_text = "\n🎟️ Nuevos cupones: " + ", ".join(f"{esc(c.code)} (-{money(c.discount_percent)}%)" for c in coupons)
                    await bot.send_message(user.telegram_id, f"✅ Pago recibido mientras estaba OFFLINE.\n💳 Ticket #{ticket.id}\n💰 Wallet: <b>{money(user.wallet_balance)} CUP</b>\n⭐ +{result.get('stars',0)} estrellas{coupon_text}")
                else:
                    await bot.send_message(user.telegram_id, f"✅ Pago del ticket #{ticket.id} detectado y procesado. El administrador ya recibió el aviso para continuar con la recarga.")
                await notify_admin_ticket(ticket, user, session, "✅ Este pago estaba retenido por OFFLINE y acaba de procesarse automáticamente al volver ONLINE.")
            await session.commit()
    return processed


@router.message(Command("online"))
async def cmd_online(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede cambiar el estado.")
        return
    processed = await set_online_and_reconcile()
    await message.answer(f"🟢 <b>ONLINE</b>. Pagos retenidos procesados automáticamente: <b>{processed}</b>.")


@router.message(Command("offline"))
async def cmd_offline(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede cambiar el estado.")
        return
    async with db.SessionLocal() as session:
        await set_setting(session, "service_online", "false")
        await session.commit()
    await message.answer("🔴 <b>OFFLINE</b>. Los usuarios pueden seguir creando tickets y pagando; los pagos recibidos quedarán retenidos y se procesarán automáticamente cuando vuelvas a ONLINE.")


@router.message(Command("blacklist"))
async def cmd_blacklist(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede usar esto.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Uso: /blacklist ID")
        return
    try:
        target = int(args[1])
    except ValueError:
        await message.answer("ID inválido.")
        return
    async with db.SessionLocal() as session:
        user = (await session.execute(select(User).where(User.telegram_id == target))).scalar_one_or_none()
        if user is None:
            user = User(telegram_id=target, full_name="Unknown")
            session.add(user)
        user.is_blacklisted = True
        await session.commit()
    await message.answer(f"🚫 {target} añadido a blacklist.")
    try:
        await bot.ban_chat_member(settings.group_id, target)
    except Exception as exc:
        log.warning("Blacklist action failed: %s", exc)


@router.message(Command("unblacklist"))
async def cmd_unblacklist(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede usar esto.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Uso: /unblacklist ID")
        return
    try:
        target = int(args[1])
    except ValueError:
        await message.answer("ID inválido.")
        return
    async with db.SessionLocal() as session:
        user = (await session.execute(select(User).where(User.telegram_id == target))).scalar_one_or_none()
        if not user:
            await message.answer("Usuario no encontrado.")
            return
        user.is_blacklisted = False
        await session.commit()
    try:
        await bot.unban_chat_member(settings.group_id, target, only_if_banned=True)
    except Exception:
        pass
    await message.answer(f"✅ {target} retirado de blacklist.")


@router.message(Command("warn"))
async def cmd_warn(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede usar esto.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Uso: /warn ID")
        return
    try:
        target = int(args[1])
    except ValueError:
        await message.answer("ID inválido.")
        return
    async with db.SessionLocal() as session:
        user = (await session.execute(select(User).where(User.telegram_id == target))).scalar_one_or_none()
        if not user:
            await message.answer("Usuario no encontrado.")
            return
        user.warnings += 1
        await session.commit()
        total = user.warnings
        name = user.username or user.full_name
    await message.answer(f"⚠️ Advertencia registrada para {esc(name)}. Total: {total}")


@router.message(Command("mute"))
async def cmd_mute(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede usar esto.")
        return
    args = (message.text or "").split()
    if len(args) < 2:
        await message.answer("Uso: /mute ID [segundos]")
        return
    try:
        target = int(args[1])
        seconds = int(args[2]) if len(args) > 2 else 300
    except ValueError:
        await message.answer("ID/segundos inválidos.")
        return
    from datetime import timedelta
    from aiogram.types import ChatPermissions
    try:
        await bot.restrict_chat_member(settings.group_id, target, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now(timezone.utc) + timedelta(seconds=max(30, min(seconds, 86400))))
        await message.answer(f"🔇 Usuario {target} silenciado temporalmente.")
    except Exception as exc:
        await message.answer(f"No pude aplicar el mute: {esc(exc)}")


@router.message(Command("tickets"))
async def cmd_tickets(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede consultar tickets.")
        return
    async with db.SessionLocal() as session:
        tickets = (await session.execute(select(OrderTicket).order_by(desc(OrderTicket.created_at)).limit(30))).scalars().all()
    if not tickets:
        await message.answer("🧾 No hay tickets.")
        return
    lines = ["🧾 <b>Tickets recientes</b>"]
    for t in tickets:
        lines.append(f"#{t.id} · {t.kind} · {money(t.amount)} {esc(t.currency)} · {esc(t.status)} · <code>{esc(t.transfer_number)}</code>")
    await message.answer("\n".join(lines))


@router.message(Command("payments"))
async def cmd_payments(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede consultar pagos.")
        return
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(IncomingPayment).where(IncomingPayment.status.in_(["held_offline", "unmatched", "needs_review"])).order_by(desc(IncomingPayment.received_at)).limit(25)
        )).scalars().all()
    if not rows:
        await message.answer("✅ No hay pagos pendientes de revisión.")
        return
    text = "🧾 <b>Pagos pendientes</b>\n\n" + "\n".join(
        f"#{p.id} · {money(p.amount)} {esc(p.currency)} · {esc(p.status)} · <code>{esc(p.transfer_number)}</code> · ref {esc(p.provider_reference or p.event_id)}"
        for p in rows
    )
    await message.answer(text)


@router.message(Command("close_ticket"))
async def cmd_close_ticket(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede cerrar tickets.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Uso: /close_ticket ID")
        return
    tid = int(args[1])
    async with db.SessionLocal() as session:
        ticket = (await session.execute(select(OrderTicket).where(OrderTicket.id == tid))).scalar_one_or_none()
        if not ticket:
            await message.answer("Ticket no encontrado.")
            return
        if ticket.status != "pending":
            await message.answer(f"Ese ticket ya está en estado {esc(ticket.status)}.")
            return
        ticket.status = "cancelled"
        ticket.closed_at = datetime.now(timezone.utc)
        if ticket.order_id:
            order = (await session.execute(select(Order).where(Order.id == ticket.order_id))).scalar_one_or_none()
            if order and order.status == "pending":
                order.status = "cancelled"
        user = (await session.execute(select(User).where(User.id == ticket.user_id))).scalar_one_or_none()
        await session.commit()
    await message.answer(f"✅ Ticket #{tid} cerrado.")
    if user:
        try:
            await bot.send_message(user.telegram_id, f"❌ Tu ticket #{tid} fue cerrado por el administrador. Si ya habías pagado, contacta al admin antes de crear otro ticket.")
        except Exception:
            pass


@router.message(F.new_chat_members)
async def on_join(message: Message):
    async with db.SessionLocal() as session:
        for member in message.new_chat_members:
            u = await user_context(session, member)
            if u.is_blacklisted:
                try:
                    await bot.ban_chat_member(message.chat.id, member.id)
                except Exception as exc:
                    log.warning("Blacklist join action failed: %s", exc)
        await session.commit()


@router.message(F.left_chat_member)
async def on_leave(message: Message):
    member = message.left_chat_member
    if not member or random.random() >= 0.35:
        return
    variants = [
        f"Se fue @{member.username}. Bueno... adiós 🫡" if member.username else f"Se fue {member.full_name}. 🫡",
        "Otro que abandona la partida. 👻",
        "Y yo que pensaba que hoy venía drama...",
    ]
    await message.answer(random.choice(variants))


@router.message(F.content_type.in_({"photo", "video", "animation"}))
async def media_message(message: Message):
    caption = message.caption or ""
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP} and contains_link(caption):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        await message.answer("🚫 Los enlaces tampoco pasan aunque vengan con una imagen o video. (⁠¬⁠_⁠¬⁠)")
        return
    if bot_invocation(caption):
        await message.answer("Puedo recibir una imagen/video, pero ahora mismo no tengo visión activada 🫡. Si me describes qué aparece, lo comentamos.")


@router.message(F.text)
async def on_text(message: Message):
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}:
        return
    if not message.from_user:
        return
    text_value = message.text or ""
    async with db.SessionLocal() as session:
        user = await user_context(session, message.from_user)
        if user.is_blacklisted and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return
        if contains_link(text_value) and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            try:
                await bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
            user.warnings += 1
            await session.commit()
            await message.answer("🚫 Los enlaces no están permitidos aquí. Nada personal (⁠¬⁠_⁠¬⁠)")
            return

        await remember_message(session, message.chat.id, user, message.message_id, text_value, "text")

        if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            ok, reason = valid_message_score(text_value)
            if ok and not is_lookup_request(text_value):
                await grant_message_xp(session, user, settings)
            elif len(text_value) >= 5 and reason in {"repetitivo", "sin_contenido"}:
                await session.commit()
                # Deliberately warn rather than mute/ban: farming XP is not, by itself, a severe moderation violation.
                if random.random() < 0.70:
                    await message.answer("⚠️ Eso no cuenta para XP. Participa normal, que no me engañas (⁠☞⁠ ಠ_ಠ⁠)⁠☞")
                return
            await session.commit()
            if PURCHASE_RE.search(text_value):
                await message.answer("🛒 Para compras y recargas, búscame en privado y te enseño las ofertas.", reply_markup=group_shop_keyboard())
                return
            if not decide_group_reply(message):
                return
            await reply_with_ai(message, user, session, private=False)
            await session.commit()
            return

        await session.commit()
        await reply_with_ai(message, user, session, private=True)
        await session.commit()


@router.callback_query(F.data == "shop")
async def cb_shop(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        products = await active_products(session)
    if not products:
        await callback.message.edit_text("Todavía no hay ofertas activas. Usa /pedido aquí mismo para pedir algo específico.", reply_markup=private_keyboard())
        return
    rows = []
    for p in products[:30]:
        rows.append([InlineKeyboardButton(text=f"{p.game} · {p.name} · {money(p.price)} {p.currency}", callback_data=f"buy:{p.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Menú", callback_data="menu")])
    await callback.message.edit_text("🛒 <b>Ofertas</b>\n\nElige lo que estás buscando:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "phone")
async def cb_phone(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        identity = await get_identity(session, user)
    if identity:
        await callback.message.answer(f"📱 Número vinculado: <code>{esc(identity.transfer_number)}</code>")
    else:
        await callback.message.answer("📱 Todavía no tienes un número vinculado. Compártelo desde el botón de abajo.", reply_markup=phone_request_keyboard())


@router.callback_query(F.data == "wallet")
async def cb_wallet(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        online = await service_online(session)
        identity = await get_identity(session, user)
        pending = (await session.execute(select(OrderTicket).where(OrderTicket.user_id == user.id, OrderTicket.status == "pending"))).scalar_one_or_none()
    text = f"💰 Wallet: <b>{money(user.wallet_balance)} CUP</b>\n\nEstado: {'🟢 ONLINE' if online else '🔴 OFFLINE'}"
    if identity:
        text += f"\n📱 Número: <code>{esc(identity.transfer_number)}</code>"
    if pending:
        text += f"\n\n🧾 Tienes un ticket pendiente #{pending.id}."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Añadir saldo", callback_data="deposit")],
        [InlineKeyboardButton(text="⬅️ Menú", callback_data="menu")],
    ]))


@router.callback_query(F.data == "deposit")
async def cb_deposit(callback: CallbackQuery):
    await callback.answer()
    amounts = [10, 50, 100, 250, 500, 1000]
    rows = []
    for i in range(0, len(amounts), 2):
        rows.append([InlineKeyboardButton(text=f"{a} CUP", callback_data=f"depamt:{a}") for a in amounts[i:i+2]])
    rows.append([InlineKeyboardButton(text="⬅️ Menú", callback_data="menu")])
    await callback.message.edit_text("💳 <b>Añadir saldo a la wallet</b>\n\nElige el importe:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("depamt:"))
async def cb_deposit_amount(callback: CallbackQuery):
    await callback.answer()
    amount = float(callback.data.split(":", 1)[1])
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        pending = (await session.execute(
            select(OrderTicket).where(OrderTicket.user_id == user.id, OrderTicket.status == "pending").limit(1)
        )).scalar_one_or_none()
        if pending:
            await callback.message.edit_text(
                f"🧾 Ya tienes un ticket pendiente #{pending.id}. Ciérralo antes de añadir saldo.",
                reply_markup=ticket_keyboard(pending.id),
            )
            return
        await create_checkout(session, user, kind="wallet", amount=amount)
        await render_checkout(callback.message, session, user)


@router.callback_query(F.data == "stars")
async def cb_stars(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
    await callback.message.edit_text(f"⭐ Tienes <b>{user.stars}</b> estrellas.\n\nLos descuentos dependen de los hitos configurados por el administrador.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Menú", callback_data="menu")]]))


@router.callback_query(F.data == "coupons")
async def cb_coupons(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        coupons = await user_coupons(session, user)
    if not coupons:
        text = "🎟️ No tienes cupones disponibles ahora mismo."
    else:
        text = "🎟️ <b>Tus cupones</b>\n\n" + "\n".join(f"• <code>{esc(c.code)}</code> — -{money(c.discount_percent)}%" for c in coupons)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Menú", callback_data="menu")]]))


@router.callback_query(F.data == "request")
async def cb_request(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("📦 Escribe en este chat privado: /pedido lo que buscas\n\nEjemplo:\n/pedido 1000 Robux\n/pedido Recarga de 500 diamantes de X")


@router.callback_query(F.data == "mytickets")
async def cb_mytickets(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        tickets = (await session.execute(select(OrderTicket).where(OrderTicket.user_id == user.id).order_by(desc(OrderTicket.created_at)).limit(10))).scalars().all()
    if not tickets:
        await callback.message.edit_text("🧾 No tienes tickets todavía.", reply_markup=private_keyboard())
        return
    text = "🧾 <b>Mis tickets</b>\n\n" + "\n".join(f"#{t.id} · {esc(t.kind)} · {money(t.amount)} {esc(t.currency)} · {esc(t.status)}" for t in tickets)
    await callback.message.edit_text(text, reply_markup=private_keyboard())


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("<b>Shiro Synthesis Two</b> 🫡\n\nElige lo que necesitas:", reply_markup=private_keyboard())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    await callback.answer()
    pid = int(callback.data.split(":", 1)[1])
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        pending = (await session.execute(
            select(OrderTicket).where(OrderTicket.user_id == user.id, OrderTicket.status == "pending").limit(1)
        )).scalar_one_or_none()
        if pending:
            await callback.message.edit_text(
                f"🧾 Ya tienes un ticket pendiente #{pending.id}. Ciérralo antes de iniciar otra operación.",
                reply_markup=ticket_keyboard(pending.id),
            )
            return
        product = (await session.execute(select(Product).where(Product.id == pid, Product.active.is_(True)))).scalar_one_or_none()
        if not product:
            await callback.message.edit_text("Esa oferta ya no está disponible.", reply_markup=private_keyboard())
            return
        await create_checkout(session, user, kind="purchase", product_id=pid, amount=product.price)
        await render_checkout(callback.message, session, user)


@router.callback_query(F.data == "checkout:terms")
async def cb_checkout_terms(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        draft = (await session.execute(select(CheckoutSession).where(CheckoutSession.user_id == user.id))).scalar_one_or_none()
        if not draft:
            await callback.message.edit_text("La sesión de compra ya no está disponible.", reply_markup=private_keyboard())
            return
        draft.terms_accepted = not draft.terms_accepted
        draft.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await callback.message.edit_text("<b>Condiciones del ticket</b>\n\n" + TERMS_TEXT + "\n\n" + ("✅ Casilla activada. Ya puedes crear el ticket." if draft.terms_accepted else "☐ Casilla desactivada. Debes activarla para continuar."), reply_markup=checkout_keyboard(draft, bool(await user_coupons(session, user)) if draft.kind == "purchase" else False))


@router.callback_query(F.data == "checkout:coupon")
async def cb_checkout_coupon(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        draft = (await session.execute(select(CheckoutSession).where(CheckoutSession.user_id == user.id))).scalar_one_or_none()
        if not draft or draft.kind != "purchase":
            await callback.message.edit_text("No hay una compra activa.", reply_markup=private_keyboard())
            return
        coupons = await user_coupons(session, user)
    rows = [[InlineKeyboardButton(text=f"{money(c.discount_percent)}% · {c.code}", callback_data=f"checkout:coupon:{c.id}")] for c in coupons]
    rows.append([InlineKeyboardButton(text="Sin cupón", callback_data="checkout:coupon:none")])
    rows.append([InlineKeyboardButton(text="⬅️ Volver", callback_data="checkout:back")])
    await callback.message.edit_text("🎟️ <b>Elige un cupón</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("checkout:coupon:"))
async def cb_checkout_coupon_select(callback: CallbackQuery):
    await callback.answer()
    raw = callback.data.split(":", 2)[2]
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        draft = (await session.execute(select(CheckoutSession).where(CheckoutSession.user_id == user.id))).scalar_one_or_none()
        if not draft:
            return
        if raw == "none":
            draft.coupon_id = None
        else:
            try:
                cid = int(raw)
            except ValueError:
                return
            coupon = (await session.execute(select(Coupon).where(Coupon.id == cid, Coupon.user_id == user.id, Coupon.used.is_(False)))).scalar_one_or_none()
            if not coupon or (coupon.expires_at and coupon.expires_at <= datetime.now(timezone.utc)):
                await callback.message.edit_text("Ese cupón ya no está disponible.", reply_markup=private_keyboard())
                return
            draft.coupon_id = cid
        await session.commit()
        await render_checkout(callback.message, session, user)


@router.callback_query(F.data == "checkout:back")
async def cb_checkout_back(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        await render_checkout(callback.message, session, user)


@router.callback_query(F.data == "checkout:cancel")
async def cb_checkout_cancel(callback: CallbackQuery):
    await callback.answer()
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        await clear_checkout(session, user)
        await session.commit()
    await callback.message.edit_text("✅ Operación cancelada.", reply_markup=private_keyboard())


@router.callback_query(F.data == "checkout:create")
async def cb_checkout_create(callback: CallbackQuery):
    await callback.answer()
    try:
        async with db.SessionLocal() as session:
            user = await user_context(session, callback.from_user)
            online = await service_online(session)
            try:
                ticket = await open_ticket_for_checkout(session, user, settings, online)
            except ValueError as exc:
                await session.rollback()
                await callback.message.edit_text(f"⚠️ {esc(exc)}", reply_markup=private_keyboard())
                return
            await session.commit()
            await notify_admin_ticket(ticket, user, session)
            await callback.message.edit_text(
                "✅ <b>Ticket creado</b>\n\n"
                "Te envié las indicaciones de pago y el botón para cerrarlo si todavía no has pagado."
            )
            await finish_ticket(ticket, user, session)
    except Exception:
        log.exception("checkout ticket creation failed")
        try:
            await callback.message.edit_text("No pude crear el ticket ahora mismo. Inténtalo de nuevo en unos segundos.", reply_markup=private_keyboard())
        except Exception:
            pass


@router.callback_query(F.data.startswith("ticket:close:"))
async def cb_ticket_close(callback: CallbackQuery):
    await callback.answer()
    tid = int(callback.data.rsplit(":", 1)[1])
    async with db.SessionLocal() as session:
        user = await user_context(session, callback.from_user)
        ticket = (await session.execute(select(OrderTicket).where(OrderTicket.id == tid, OrderTicket.user_id == user.id))).scalar_one_or_none()
        if not ticket:
            await callback.message.edit_text("No encuentro ese ticket.", reply_markup=private_keyboard())
            return
        if ticket.status != "pending":
            await callback.message.edit_text(f"Ese ticket ya está en estado <b>{esc(ticket.status)}</b>.", reply_markup=private_keyboard())
            return
        ticket.status = "cancelled"
        ticket.closed_at = datetime.now(timezone.utc)
        if ticket.order_id:
            order = (await session.execute(select(Order).where(Order.id == ticket.order_id))).scalar_one_or_none()
            if order and order.status == "pending":
                order.status = "cancelled"
        await session.commit()
    await callback.message.edit_text("❌ <b>Orden cerrada.</b>\n\nNo hay ningún límite de tiempo para pagar mientras una orden siga abierta, pero una vez cerrada no se asociará automáticamente un pago nuevo a ella.", reply_markup=private_keyboard())


@router.callback_query(F.data.startswith("admin:close:"))
async def cb_admin_close(callback: CallbackQuery):
    if not admin(callback.from_user.id):
        await callback.answer("Sin permisos", show_alert=True)
        return
    await callback.answer()
    tid = int(callback.data.rsplit(":", 1)[1])
    async with db.SessionLocal() as session:
        ticket = (await session.execute(select(OrderTicket).where(OrderTicket.id == tid))).scalar_one_or_none()
        if not ticket or ticket.status != "pending":
            await callback.message.edit_reply_markup(reply_markup=None)
            return
        ticket.status = "cancelled"
        ticket.closed_at = datetime.now(timezone.utc)
        if ticket.order_id:
            order = (await session.execute(select(Order).where(Order.id == ticket.order_id))).scalar_one_or_none()
            if order and order.status == "pending":
                order.status = "cancelled"
        user = (await session.execute(select(User).where(User.id == ticket.user_id))).scalar_one_or_none()
        await session.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Ticket #{tid} cerrado por el admin.")
    if user:
        try:
            await bot.send_message(user.telegram_id, f"❌ El administrador cerró tu ticket #{tid}.")
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:done:"))
async def cb_admin_done(callback: CallbackQuery):
    if not admin(callback.from_user.id):
        await callback.answer("Sin permisos", show_alert=True)
        return
    await callback.answer()
    oid = int(callback.data.rsplit(":", 1)[1])
    async with db.SessionLocal() as session:
        order = (await session.execute(select(Order).where(Order.id == oid))).scalar_one_or_none()
        if not order:
            await callback.message.answer("Orden no encontrada.")
            return
        order.status = "fulfilled"
        ticket = (await session.execute(select(OrderTicket).where(OrderTicket.order_id == oid))).scalar_one_or_none()
        if ticket and ticket.status == "paid":
            ticket.status = "fulfilled"
            ticket.closed_at = datetime.now(timezone.utc)
        user = (await session.execute(select(User).where(User.id == order.user_id))).scalar_one_or_none()
        await session.commit()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Orden #{oid} marcada como procesada.")
    if user:
        try:
            await bot.send_message(user.telegram_id, f"✅ Tu orden #{oid} ha sido marcada como procesada por el administrador. 🫡")
        except Exception:
            pass


@router.message(Command("coupons"))
async def cmd_coupons_admin(message: Message):
    if not admin(message.from_user.id):
        await message.answer("Solo el admin puede consultar eso.")
        return
    args = (message.text or "").split(maxsplit=1)
    async with db.SessionLocal() as session:
        if len(args) == 2:
            target = args[1].strip().lstrip("@")
            try:
                q = select(User).where(User.telegram_id == int(target))
            except ValueError:
                q = select(User).where(func.lower(User.username) == target.lower())
            user = (await session.execute(q)).scalar_one_or_none()
            if not user:
                await message.answer("Usuario no encontrado.")
                return
            coupons = await user_coupons(session, user)
            text = "\n".join(f"{esc(c.code)} — -{money(c.discount_percent)}%" for c in coupons) or "Sin cupones activos."
            await message.answer(f"🎟️ <b>Cupones de {esc(user.full_name)}</b>\n{text}")
            return
        coupons = (await session.execute(select(Coupon).where(Coupon.used.is_(False)).order_by(desc(Coupon.created_at)).limit(50))).scalars().all()
    text = "🎟️ <b>Cupones activos</b>\n\n" + ("\n".join(f"{esc(c.code)} — -{money(c.discount_percent)}% · user {c.user_id}" for c in coupons) or "ninguno")
    await message.answer(text)


async def process_parser_payment(body: dict[str, Any]) -> dict[str, Any]:
    parsed = parser_transfer_from_payload(body)
    if not parsed:
        return {"ok": True, "ignored": True, "reason": "not_a_received_transfer"}
    # Optional recipient validation: the Parser client itself is already tied to your merchant account.
    if settings.transfermovil_receiving_account and parsed.get("receiver_account"):
        if normalize_transfer_number(parsed["receiver_account"]) != normalize_transfer_number(settings.transfermovil_receiving_account):
            return {"ok": True, "ignored": True, "reason": "recipient_account_mismatch"}
    if settings.transfermovil_receiving_phone and parsed.get("receiver_phone"):
        if normalize_transfer_number(parsed["receiver_phone"]) != normalize_transfer_number(settings.transfermovil_receiving_phone):
            return {"ok": True, "ignored": True, "reason": "recipient_phone_mismatch"}
    async with payment_lock:
        async with db.SessionLocal() as session:
            online = await service_online(session)
            incoming, result = await record_and_process_payment(session, parsed, settings, online)
            await session.commit()
            if incoming.status == "held_offline":
                await notify_admins(
                    f"🟡 <b>Pago recibido mientras OFFLINE</b>\n\n"
                    f"💰 {money(incoming.amount)} {esc(incoming.currency)}\n"
                    f"📱 <code>{esc(incoming.transfer_number)}</code>\n"
                    f"🔖 <code>{esc(incoming.provider_reference or incoming.event_id)}</code>\n\n"
                    f"Queda retenido. Se procesará automáticamente al pasar ONLINE."
                )
                return {"ok": True, "held_offline": True, "payment_id": incoming.id}
            if result and not result.get("duplicate"):
                ticket: OrderTicket = result["ticket"]
                user: User = result["user"]
                coupons = result.get("coupons") or []
                if ticket.kind == "wallet":
                    coupon_text = ""
                    if coupons:
                        coupon_text = "\n🎟️ Nuevo(s) cupón(es): " + ", ".join(f"{esc(c.code)} (-{money(c.discount_percent)}%)" for c in coupons)
                    await bot.send_message(user.telegram_id, f"✅ Pago detectado.\n💳 Ticket #{ticket.id}\n💰 Wallet: <b>{money(user.wallet_balance)} CUP</b>\n⭐ +{result.get('stars',0)} estrellas{coupon_text}")
                else:
                    await bot.send_message(user.telegram_id, f"✅ Pago detectado para tu ticket #{ticket.id}. El administrador ya recibió el aviso para procesar la recarga.")
                await notify_admin_ticket(ticket, user, session, "✅ Pago detectado automáticamente y asociado al ticket.")
                return {"ok": True, "matched": True, "ticket_id": ticket.id, "incoming_id": incoming.id}
            if result and result.get("duplicate"):
                return {"ok": True, "duplicate": True, "status": incoming.status, "incoming_id": incoming.id, "ticket_id": incoming.matched_ticket_id}
            if incoming.status in {"unmatched", "needs_review"} and settings.admin_notify_unmatched_payments:
                await notify_admins(
                    f"⚠️ <b>Pago sin asociación automática</b>\n\n"
                    f"💰 {money(incoming.amount)} {esc(incoming.currency)}\n"
                    f"📱 <code>{esc(incoming.transfer_number)}</code>\n"
                    f"🔖 <code>{esc(incoming.provider_reference or incoming.event_id)}</code>\n"
                    f"Estado: {esc(incoming.status)}\n\nUsa /payments para revisarlo."
                )
            return {"ok": True, "matched": False, "status": incoming.status, "incoming_id": incoming.id}



@router.callback_query(F.data.startswith("admin:ping:"))
async def _unused_admin_ping(callback: CallbackQuery):
    await callback.answer()


async def collector_loop():
    while True:
        try:
            events = await collector.run_once()
            for ev in events:
                await publish_new_event_if_needed(ev)
        except Exception:
            log.exception("event collector failed")
        await asyncio.sleep(max(5, settings.event_poll_minutes * 60))


async def publish_new_event_if_needed(ev: GameEvent):
    async with db.SessionLocal() as session:
        game = (await session.execute(select(Game).where(Game.id == ev.game_id))).scalar_one_or_none() if ev.game_id else None
        if not game or not game.active or ev.published:
            return
        if game.auto_publish:
            msg = f"🎮 <b>{esc(game.name)}</b>\n\n🆕 {esc(ev.title)}\n\n{esc(ev.summary[:900])}\n\nFuente: {esc(ev.url)}"
            try:
                await bot.send_message(settings.group_id, msg, disable_web_page_preview=True)
            except Exception as exc:
                log.warning("autopublish failed: %s", exc)
            if game.alert_subscribers:
                subs = (await session.execute(select(Subscription.user_id).where(Subscription.game == game.name, Subscription.active.is_(True)))).scalars().all()
                for uid in subs:
                    u = (await session.execute(select(User).where(User.id == uid))).scalar_one_or_none()
                    if u:
                        try:
                            await bot.send_message(u.telegram_id, msg, disable_web_page_preview=True)
                        except Exception:
                            pass
            ev.published = True
            await session.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await create_tables()
    task = asyncio.create_task(collector_loop())
    yield
    task.cancel()
    await collector.close()
    await ai.close()


app = FastAPI(title="Shiro Synthesis Two Admin", lifespan=lifespan)


def check_token(token: str | None):
    if not token or not hmac.compare_digest(token, settings.webapp_admin_token):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
async def health():
    return {"ok": True, "service": "shiro-synthesis-two", "payment_webhook": "/payments/transfermovil/webhook"}


@app.get("/payments/transfermovil/webhook")
async def payment_webhook_get():
    return {"ok": True, "method": "POST expected", "provider": "synthesisone-parser"}


@app.post("/payments/transfermovil/webhook")
async def payment_webhook_route(request: Request, x_webhook_signature_v2: str | None = Header(default=None, alias="X-Webhook-Signature-V2"), x_webhook_timestamp: str | None = Header(default=None, alias="X-Webhook-Timestamp"), x_webhook_event_id: str | None = Header(default=None, alias="X-Webhook-Event-Id")):
    body = await request.body()
    # Parser-bot sends a v2 HMAC over `${timestamp}.${raw_body}`.
    if settings.parser_webhook_secret:
        if not x_webhook_signature_v2 or not x_webhook_timestamp:
            raise HTTPException(status_code=401, detail="signature_required")
        if not verify_hmac_v2(body, settings.parser_webhook_secret, x_webhook_signature_v2, x_webhook_timestamp, settings.webhook_max_skew_seconds):
            raise HTTPException(status_code=401, detail="invalid_signature")
    else:
        raise HTTPException(status_code=500, detail="parser_webhook_secret_missing")
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid_json")
    if x_webhook_event_id and not data.get("event_id"):
        data["event_id"] = x_webhook_event_id
    return await process_parser_payment(data)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(token: str | None = None):
    check_token(token)
    page = (BASE_DIR / "app" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    return page.replace("__ADMIN_TOKEN__", esc(token or ""))


@app.get("/api/admin/overview")
async def overview(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        return {
            "users": await session.scalar(select(func.count(User.id))) or 0,
            "games": await session.scalar(select(func.count(Game.id))) or 0,
            "sources": await session.scalar(select(func.count(Source.id)).where(Source.active.is_(True))) or 0,
            "events": await session.scalar(select(func.count(GameEvent.id))) or 0,
            "products": await session.scalar(select(func.count(Product.id)).where(Product.active.is_(True))) or 0,
            "orders": await session.scalar(select(func.count(Order.id))) or 0,
            "pending_tickets": await session.scalar(select(func.count(OrderTicket.id)).where(OrderTicket.status == "pending")) or 0,
            "held_payments": await session.scalar(select(func.count(IncomingPayment.id)).where(IncomingPayment.status.in_(["held_offline", "unmatched", "needs_review"]))) or 0,
            "service_online": await service_online(session),
        }


@app.get("/api/admin/games")
async def api_games(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        games = (await session.execute(select(Game).order_by(Game.name))).scalars().all()
        return [{"id": g.id, "name": g.name, "aliases": g.aliases, "active": g.active, "auto_publish": g.auto_publish, "alert_subscribers": g.alert_subscribers} for g in games]


@app.post("/api/admin/games")
async def create_game(payload: dict[str, Any], token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        g = Game(name=str(payload.get("name", "")).strip(), aliases=str(payload.get("aliases", "")), active=bool(payload.get("active", True)), auto_publish=bool(payload.get("auto_publish", False)), alert_subscribers=bool(payload.get("alert_subscribers", False)))
        if not g.name:
            raise HTTPException(status_code=400, detail="name required")
        session.add(g)
        await session.commit()
        return {"id": g.id}


@app.get("/api/admin/sources")
async def api_sources(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(Source).order_by(desc(Source.id)))).scalars().all()
        return [{"id": s.id, "game_id": s.game_id, "name": s.name, "url": s.url, "kind": s.kind, "active": s.active, "prompt_hint": s.prompt_hint, "last_checked": s.last_checked.isoformat() if s.last_checked else None} for s in rows]


@app.post("/api/admin/sources")
async def create_source(payload: dict[str, Any], token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        s = Source(game_id=int(payload["game_id"]) if payload.get("game_id") else None, name=str(payload.get("name", "source")), url=str(payload.get("url", "")), kind=str(payload.get("kind", "web")), prompt_hint=str(payload.get("prompt_hint", "")))
        if not s.url:
            raise HTTPException(status_code=400, detail="url required")
        session.add(s)
        await session.commit()
        return {"id": s.id}


@app.post("/api/admin/discover_sources")
async def discover_source_api(payload: dict[str, Any], token: str | None = None):
    check_token(token)
    game_name = str(payload.get("game", "")).strip()
    if not game_name:
        raise HTTPException(status_code=400, detail="game required")
    try:
        return await discover_sources(settings, game_name)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"source discovery failed: {exc}")


@app.get("/api/admin/products")
async def api_products(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(Product).order_by(Product.game, Product.sort_order))).scalars().all()
        return [{"id": p.id, "game": p.game, "category": p.category, "name": p.name, "description": p.description, "price": p.price, "currency": p.currency, "active": p.active} for p in rows]


@app.post("/api/admin/products")
async def create_product(payload: dict[str, Any], token: str | None = None):
    check_token(token)
    try:
        price = float(payload["price"])
    except Exception:
        raise HTTPException(status_code=400, detail="price invalid")
    if price <= 0:
        raise HTTPException(status_code=400, detail="price invalid")
    async with db.SessionLocal() as session:
        p = Product(game=str(payload.get("game", "")), category=str(payload.get("category", "game")), name=str(payload.get("name", "")), description=str(payload.get("description", "")), price=price, currency=str(payload.get("currency", "CUP")))
        if not p.game or not p.name:
            raise HTTPException(status_code=400, detail="game and name required")
        session.add(p)
        await session.commit()
        return {"id": p.id}


@app.get("/api/admin/users")
async def api_users(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(User).order_by(desc(User.last_activity)).limit(200))).scalars().all()
        return [{"telegram_id": u.telegram_id, "username": u.username, "full_name": u.full_name, "level": u.level, "xp": u.xp, "valid_messages": u.valid_messages, "stars": u.stars, "wallet": u.wallet_balance, "blacklisted": u.is_blacklisted} for u in rows]


@app.get("/api/admin/coupons")
async def api_coupons(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(Coupon).order_by(desc(Coupon.created_at)).limit(200))).scalars().all()
        return [{"code": c.code, "user_id": c.user_id, "discount_percent": c.discount_percent, "used": c.used, "expires_at": c.expires_at.isoformat() if c.expires_at else None, "milestone_stars": c.milestone_stars} for c in rows]


@app.get("/api/admin/tickets")
async def api_tickets(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(OrderTicket).order_by(desc(OrderTicket.created_at)).limit(300))).scalars().all()
        return [{
            "id": t.id, "user_id": t.user_id, "order_id": t.order_id, "kind": t.kind,
            "transfer_number": t.transfer_number, "amount": t.amount, "currency": t.currency,
            "terms_accepted": t.terms_accepted, "service_online_at_creation": t.service_online_at_creation,
            "status": t.status, "provider_reference": t.provider_reference,
            "created_at": t.created_at.isoformat(), "paid_at": t.paid_at.isoformat() if t.paid_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        } for t in rows]


@app.get("/api/admin/payments")
async def api_incoming_payments(token: str | None = None):
    check_token(token)
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(IncomingPayment).order_by(desc(IncomingPayment.received_at)).limit(300))).scalars().all()
        return [{
            "id": p.id, "event_id": p.event_id, "provider_reference": p.provider_reference,
            "transfer_number": p.transfer_number, "receiver_phone": p.receiver_phone,
            "receiver_account": p.receiver_account, "amount": p.amount, "currency": p.currency,
            "status": p.status, "matched_ticket_id": p.matched_ticket_id,
            "received_at": p.received_at.isoformat(), "matched_at": p.matched_at.isoformat() if p.matched_at else None,
        } for p in rows]


@app.post("/api/admin/settings")
async def api_settings(payload: dict[str, Any], token: str | None = None):
    check_token(token)
    if "service_online" in payload:
        desired = str(payload["service_online"]).lower() == "true"
        if desired:
            processed = await set_online_and_reconcile()
            return {"ok": True, "service_online": True, "processed": processed}
    async with db.SessionLocal() as session:
        for key, value in payload.items():
            if key not in {"service_online", "group_id"}:
                continue
            await set_setting(session, key, str(value).lower() if isinstance(value, bool) else str(value))
        await session.commit()
    return {"ok": True}


async def run_bot():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def run_web():
    config = uvicorn.Config(app, host=settings.webapp_host, port=settings.webapp_port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_all():
    await create_tables()
    async with db.SessionLocal() as session:
        if await get_setting(session, "service_online", "") == "":
            await set_setting(session, "service_online", "true")
            await session.commit()
    await asyncio.gather(run_bot(), run_web())


if __name__ == "__main__":
    settings.validate()
    asyncio.run(run_all())
