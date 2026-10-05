# Shiro Synthesis Two — Telegram + IA + pagos + tienda + memoria

Proyecto de bot de Telegram con personalidad IA, moderación, memoria, XP, estrellas, cupones, tienda privada, pedidos, wallet y recepción de pagos de SynthesisOne Parser-bot.

## Cambios importantes de este build

### Pagos y tickets

- Antes de crear una compra o una recarga de wallet, el usuario debe vincular el número de teléfono que utilizará para pagar. El bot usa el botón nativo de Telegram para compartir el contacto y verifica que el contacto pertenezca al propio usuario.
- La pantalla de checkout utiliza un botón tipo casilla: `☐ Entiendo y acepto` → `✅ Entiendo y acepto`. Sin activar la casilla no se crea el ticket.
- El ticket exige haber sido creado **antes** de hacer la transferencia.
- Los tickets de pago **no caducan automáticamente**. Permanecen abiertos hasta que el usuario o el administrador los cierre.
- Un usuario tiene como máximo un ticket pendiente a la vez para evitar que dos órdenes con el mismo teléfono e importe creen ambigüedad.
- El usuario puede cerrar su ticket si todavía no ha pagado. Se avisa expresamente que, una vez cerrado, un pago nuevo no se asociará automáticamente a esa orden.

### Estado ONLINE / OFFLINE

- `ONLINE`: un pago recibido puede asociarse automáticamente a un ticket pendiente si coincide el teléfono vinculado, el importe y la moneda.
- `OFFLINE`: el webhook sigue recibiendo y guardando los pagos, pero **no acredita wallet ni marca una compra como pagada**. El pago queda retenido.
- Al ejecutar `/online`, el bot cambia a ONLINE y procesa automáticamente los pagos retenidos que tengan un único ticket abierto coincidente.
- No existe TTL de pago. Un pago recibido offline puede procesarse posteriormente mientras el ticket siga abierto.
- Si el usuario cerró la orden, el pago no se reasigna de forma automática; queda visible para revisión administrativa.

## Webhook de SynthesisOne Parser-bot

Endpoint:

```text
POST https://TU_DOMINIO/payments/transfermovil/webhook
```

Parser-bot envía estos headers de seguridad:

```text
X-Webhook-Event-Id
X-Webhook-Timestamp
X-Webhook-Signature
X-Webhook-Signature-V2
```

El bot usa `X-Webhook-Signature-V2`. La firma se calcula sobre:

```text
HMAC_SHA256(PARSER_WEBHOOK_SECRET, `${timestamp}.${raw_body}`)
```

Esto coincide con el formato de entrega v2 del proyecto Parser-bot.

El payload esperado para una transferencia recibida tiene esta forma general:

```json
{
  "schema_version": "1.0",
  "event": "TRANSFER_DETECTED",
  "event_id": "...",
  "occurred_at": "2026-10-02T15:30:00.000Z",
  "client": {
    "id": "...",
    "name": "...",
    "phone_number": "..."
  },
  "merchant_accounts": {
    "card1": "...",
    "card2": null,
    "card3": null,
    "wallet": null
  },
  "sms": {
    "sender": "...",
    "body": "...",
    "received_at": "...",
    "message_id": "...",
    "log_id": "..."
  },
  "verification": {
    "is_financial_transfer": true,
    "has_amount": true,
    "has_currency": true,
    "has_transaction_id": true,
    "has_destination_account": true,
    "has_destination_phone": true,
    "has_counterparty_phone": true
  },
  "transaction": {
    "direction": "RECIBIDO",
    "amount": 100.00,
    "currency": "CUP",
    "sender_phone": "53512345678",
    "receiver_phone": "...",
    "receiver_account": "...",
    "transaction_id": "TX-123"
  }
}
```

El bot toma como datos relevantes del pago `transaction.direction`, `transaction.amount`, `transaction.currency`, `transaction.sender_phone` y `transaction.transaction_id`. Las comisiones externas no se convierten en otra cifra de pago.

### Configuración en Parser-bot

En el cliente de Parser-bot que recibe los SMS/pagos, configura como webhook una URL de este bot:

```text
https://TU_DOMINIO/payments/transfermovil/webhook
```

Después copia el `webhook_secret` de ese cliente a:

```text
PARSER_WEBHOOK_SECRET=...
```

No necesitas programar un adaptador distinto para cada pago: el bot consume el payload estándar que Parser-bot ya genera.

## Flujo de compra

```text
Usuario
  ↓
Buscar ofertas
  ↓
Elegir producto
  ↓
¿Teléfono vinculado? — No → compartir contacto
  ↓
Resumen + cupón opcional
  ↓
☐ Entiendo y acepto
  ↓
Crear ticket
  ↓
Ticket enviado al admin
  ↓
Usuario paga importe exacto
  ↓
Parser-bot → webhook
  ↓
ONLINE?
 ├─ Sí → asociar automáticamente
 └─ No → guardar retenido
  ↓
Admin procesa la recarga
```

## Flujo de wallet

Es el mismo flujo de ticket, pero el ticket representa un depósito. Cuando llega un pago coincidente: `ONLINE` → se acredita wallet + estrellas + cupones de hito. `OFFLINE` → se retiene y se procesa al volver a ONLINE.

## XP

- Los mensajes válidos otorgan XP, con cooldown y límite diario.
- Los mensajes basura/repetitivos no otorgan XP.
- Las búsquedas de actualidad, clima, noticias, eventos, fechas, resultados, etc. están excluidas del XP para impedir farming.
- XP no decide a quién responde Shiro. Un usuario nuevo puede conversar con ella normalmente.

## Personalidad / visión

La personalidad se encuentra en `data/shiro_personality.txt`. Shiro puede ser Genki, adulta, caótica, sarcástica, dramática y muy expresiva. Puede usar emojis y kaomojis.

**Visión desactivada:** esta versión no envía fotos, videos ni GIF a un modelo con visión. Si llega una imagen/video, Shiro no finge haberlo visto. Puede comentar una descripción escrita por el usuario.

## Búsqueda web

OpenRouter permite usar `openrouter:web_search` dentro de la solicitud; el modelo puede decidir cuándo buscar cuando la herramienta está habilitada. Esto se usa para actualidad, noticias, clima, efemérides y otra información que necesite verificación actual.

## Fuentes de juegos

El código es el motor; los juegos y fuentes son datos. Desde `/admin` puedes añadir juegos y fuentes RSS/API/web sin editar Python. También puedes utilizar el descubrimiento de fuentes para encontrar candidatas y luego agregarlas.

## Arranque

```bash
cp .env.example .env
# edita .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app
```

Panel administrativo:

```text
http://localhost:8080/admin?token=TU_WEBAPP_ADMIN_TOKEN
```

En producción utiliza HTTPS y protege el token del panel.

## Comandos de admin

`/online` · activa servicio y reconcilia pagos retenidos

`/offline` · desactiva acreditación automática

`/blacklist ID` · añade a blacklist

`/unblacklist ID` · quita de blacklist

`/warn ID` · registra advertencia

`/mute ID [segundos]` · mute administrativo

`/tickets` · tickets recientes

`/payments` · pagos no asociados/retenidos

`/close_ticket ID` · cierra un ticket pendiente

`/coupons [ID|@username]` · consulta cupones

`/stats` · estadísticas bajo demanda

## Comandos públicos

`/inspect @usuario` o `/inspect ID` · perfil público

`/sugerencias texto` · sugerencia

`/pedido texto` · pedido en privado

`/vincular` · iniciar vinculación de teléfono

## IA y capacidades de información

- Shiro usa `openrouter:web_search` para información actual y `openrouter:web_fetch` cuando necesita leer una fuente con más profundidad; `openrouter:datetime` aporta fecha/hora actual. Estas son herramientas del servidor de OpenRouter y requieren un modelo/ruta con soporte de tool calling. El bot tiene fallback a una respuesta sin herramientas si la ruta elegida rechaza las herramientas.
- Esta versión **NO tiene visión**. Puede recibir una foto, vídeo o GIF como mensaje de Telegram, pero no puede inspeccionar su contenido y nunca debe afirmar que lo vio. Solo puede trabajar con una descripción/caption textual o cuando en el futuro se integre una herramienta de visión real.
- Consultas de clima, noticias, efemérides, eventos de juegos y búsquedas similares no otorgan XP por sí mismas.
