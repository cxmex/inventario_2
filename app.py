import os
import io
import secrets
import requests
import pytz
from datetime import datetime
from typing import List, Optional


from fastapi import FastAPI, HTTPException, APIRouter,Request,Form
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")

# PDF / QR
from reportlab.lib.pagesizes import letter, A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
import qrcode

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
SUPABASE_URL = "https://gbkhkbfbarsnpbdkxzii.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imdia"
    "2hrYmZiYXJzbnBiZGt4emlpIiwicm9sZSI6ImFu"
    "b24iLCJpYXQiOjE3MzQzODAzNzMsImV4cCI6MjA"
    "0OTk1NjM3M30.mcOcC2GVEu_wD3xNBzSCC3MwDck3CIdmz4D8adU-bpI"
)
LOCAL_CAMERA_SERVICE = "https://fred-nonchalky-fatally.ngrok-free.dev"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# Store identifier (terex2 = second store)
STORE_ID = "terex2"
INVENTORY_COL = "terex2"         # column in inventario1
VENTAS_TABLE = "ventas_terex2"   # sales table

# ─────────────────────────────────────────────
# APP & ROUTER
# ─────────────────────────────────────────────
app = FastAPI(title="Nota Terex2")
router = APIRouter()


# ─────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────
class ProductItem(BaseModel):
    qty: int = 1
    name: str = ""
    codigo: str = ""
    price: float = 0.0
    customer_email: Optional[str] = None


class SavePayload(BaseModel):
    products: List[ProductItem]
    payment_method: str = "efectivo"



class ConteoEfectivoResponse(BaseModel):
    id: int
    nombre: str
    tipo: str
    amount: float
    balance: float
    created_at: str
    order_id: Optional[int] = None
    descripcion: Optional[str] = None
    diferencia: Optional[float] = None

class ConteoEfectivoCreate(BaseModel):
    nombre: str
    tipo: str
    amount: float

# ─────────────────────────────────────────────
# SUPABASE HELPER
# ─────────────────────────────────────────────
async def supabase_request(
    method: str,
    endpoint: str,
    params: dict = None,
    json_data: dict = None,
) -> list | dict | None:
    url = f"{SUPABASE_URL}{endpoint}"
    try:
        resp = requests.request(
            method.upper(), url, headers=HEADERS, params=params, json=json_data, timeout=10
        )
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None
    except requests.exceptions.HTTPError as e:
        print(f"Supabase HTTP error [{method} {endpoint}]: {e} — {resp.text}")
        raise
    except Exception as e:
        print(f"Supabase request error: {e}")
        raise


# ─────────────────────────────────────────────
# ORDER ID
# ─────────────────────────────────────────────
async def get_next_order_id() -> int:
    rows = await supabase_request(
        method="GET",
        endpoint=f"/rest/v1/{VENTAS_TABLE}",
        params={"select": "order_id", "order": "order_id.desc", "limit": "1"},
    )
    if rows:
        return (rows[0].get("order_id") or 0) + 1
    return 1


# ─────────────────────────────────────────────
# CASH BALANCE
# ─────────────────────────────────────────────
async def get_current_balance() -> float:
    rows = await supabase_request(
        method="GET",
        endpoint="/rest/v1/conteo_efectivo",
        params={"select": "balance", "order": "id.desc", "limit": "1"},
    )
    if rows:
        return float(rows[0].get("balance") or 0)
    return 0.0


# ─────────────────────────────────────────────
# LOYALTY
# ─────────────────────────────────────────────
async def process_loyalty_deduction(p_dict: dict, order_id: int, fecha: str, hora: str) -> dict:
    """Deduct loyalty points and log the transaction."""
    codigo = p_dict.get("codigo", "")
    amount = abs(float(p_dict.get("price", 0)))
    customer_email = p_dict.get("customer_email", "")

    # Find customer by barcode
    rows = await supabase_request(
        method="GET",
        endpoint="/rest/v1/loyalty_customers",
        params={"select": "id,email,balance", "barcode": f"eq.{codigo}", "limit": "1"},
    )
    if not rows:
        print(f"Loyalty barcode {codigo} not found — skipping deduction")
        return {"status": "not_found", "codigo": codigo}

    customer = rows[0]
    current_balance = float(customer.get("balance") or 0)
    new_balance = max(0, current_balance - amount)

    # Update balance
    await supabase_request(
        method="PATCH",
        endpoint=f"/rest/v1/loyalty_customers?id=eq.{customer['id']}",
        json_data={"balance": new_balance},
    )

    # Log deduction
    await supabase_request(
        method="POST",
        endpoint="/rest/v1/loyalty_transactions",
        json_data={
            "customer_id": customer["id"],
            "barcode": codigo,
            "amount": -amount,
            "balance_after": new_balance,
            "order_id": order_id,
            "fecha": fecha,
            "hora": hora,
            "store": STORE_ID,
        },
    )

    return {
        "status": "ok",
        "codigo": codigo,
        "email": customer.get("email", customer_email),
        "deducted": amount,
        "new_balance": new_balance,
    }

@app.get("/api/search_barcode")
async def api_search_barcode(barcode: str):
    """Enhanced barcode search that handles both products and loyalty barcodes"""
    
    # Check if this is a customer loyalty barcode (starts with 8000)
    if barcode.startswith('8000') and len(barcode) == 13:
        return await handle_loyalty_barcode(barcode)
    
    # Handle regular product barcode search
    rows = await supabase_request(
        method="GET",
        endpoint="/rest/v1/inventario1",
        params={"select": "*", "barcode": f"eq.{barcode}", "limit": 1}
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    row = rows[0]
    name = row.get("name") or row.get("modelo") or ""
    price = float(row.get("precio") or 0.0)
    send_telegram_message(
    f"🔍 Código escaneado\n"
    f"📦 Producto: {name}\n"
    f"💰 Precio: ${price:.2f}"
    )
    return {"name": name, "price": price, "codigo": barcode}


@app.get("/api/sync_prices")
async def sync_prices():
    """Sync prices from inventario_estilos to inventario1"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/rpc/sync_inventario_prices"
        response = requests.post(url, headers=HEADERS)
        response.raise_for_status()
        
        result = response.json()
        updated_count = result[0].get('updated_count', 0) if result else 0
        
        print(f"✅ Synced {updated_count} prices")
        return {"success": True, "updated": updated_count}
    except Exception as e:
        print(f"❌ Sync error: {e}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────
# REDEMPTION TOKEN
# ─────────────────────────────────────────────
def generate_redemption_token() -> str:
    return secrets.token_urlsafe(16)


async def store_redemption_token(order_id: int, token: str, total: float):
    try:
        await supabase_request(
            method="POST",
            endpoint="/rest/v1/redemption_tokens",
            json_data={
                "order_id": order_id,
                "token": token,
                "total": total,
                "store": STORE_ID,
                "used": False,
            },
        )
    except Exception as e:
        print(f"Could not store redemption token: {e}")


# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────
def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception as e:
        print(f"Telegram message error: {e}")


async def send_telegram_picture(barcode: str = None, order_id: int = None):
    """Forward a captured camera image for the order to Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        img_url = f"{LOCAL_CAMERA_SERVICE}/api/last_capture"
        if order_id:
            img_url += f"?order_id={order_id}"
        img_resp = requests.get(img_url, timeout=5)
        if img_resp.status_code == 200:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            requests.post(
                tg_url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": f"Venta #{order_id} — {STORE_ID}"},
                files={"photo": ("capture.jpg", img_resp.content, "image/jpeg")},
                timeout=10,
            )
    except Exception as e:
        print(f"Telegram picture error: {e}")


# ─────────────────────────────────────────────
# PDF RECEIPT WITH QR
# ─────────────────────────────────────────────
def _build_receipt_pdf_with_qr(
    items: list, total: float, order_id: int, redemption_token: str
) -> io.BytesIO:
    buf = io.BytesIO()
    width, height = 80 * mm, 200 * mm  # thermal-ish size
    c = canvas.Canvas(buf, pagesize=(width, height))

    y = height - 10 * mm

    # Header
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(width / 2, y, f"TEREX2 — Ticket #{order_id}")
    y -= 7 * mm

    mexico_tz = pytz.timezone("America/Mexico_City")
    now_str = datetime.now(mexico_tz).strftime("%Y-%m-%d %H:%M")
    c.setFont("Helvetica", 8)
    c.drawCentredString(width / 2, y, now_str)
    y -= 8 * mm

    c.line(5 * mm, y, width - 5 * mm, y)
    y -= 6 * mm

    # Items
    c.setFont("Helvetica-Bold", 8)
    c.drawString(5 * mm, y, "Producto")
    c.drawRightString(width - 5 * mm, y, "Subtotal")
    y -= 5 * mm

    c.setFont("Helvetica", 8)
    for item in items:
        line = f"{item['qty']}x {item['name'][:28]}"
        c.drawString(5 * mm, y, line)
        c.drawRightString(width - 5 * mm, y, f"${item['subtotal']:.0f}")
        y -= 5 * mm
        if y < 40 * mm:
            c.showPage()
            y = height - 10 * mm

    c.line(5 * mm, y, width - 5 * mm, y)
    y -= 6 * mm

    # Total
    c.setFont("Helvetica-Bold", 10)
    c.drawString(5 * mm, y, "TOTAL")
    c.drawRightString(width - 5 * mm, y, f"${total:.0f}")
    y -= 10 * mm

    # QR code
    qr_url = f"https://terex.mx/redeem?token={redemption_token}&order={order_id}"
    qr = qrcode.make(qr_url)
    qr_buf = io.BytesIO()
    qr.save(qr_buf, format="PNG")
    qr_buf.seek(0)

    qr_size = 25 * mm
    qr_x = (width - qr_size) / 2
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(qr_buf), qr_x, y - qr_size, width=qr_size, height=qr_size)
    y -= qr_size + 4 * mm

    c.setFont("Helvetica", 7)
    c.drawCentredString(width / 2, y, "Escanea para puntos de lealtad")

    c.save()
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "store": STORE_ID}


@router.get("/api/sync_prices")
async def sync_prices():
    """Refresh local price cache from Supabase (returns count of rows)."""
    try:
        rows = await supabase_request(
            method="GET",
            endpoint="/rest/v1/inventario1",
            params={"select": "barcode,name,price", "limit": "10000"},
        )
        return {"updated": len(rows) if rows else 0}
    except Exception as e:
        return {"updated": 0, "error": str(e)}


@router.get("/api/search_barcode")
async def search_barcode(barcode: str):
    """
    Returns product data for a barcode.
    Loyalty barcodes start with 8000 (13 digits).
    """
    if not barcode:
        raise HTTPException(status_code=400, detail="Barcode requerido")

    # ── Loyalty card ──────────────────────────────────────────────────────
    if barcode.startswith("8000") and len(barcode) == 13:
        rows = await supabase_request(
            method="GET",
            endpoint="/rest/v1/loyalty_customers",
            params={"select": "email,balance", "barcode": f"eq.{barcode}", "limit": "1"},
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        customer = rows[0]
        balance = float(customer.get("balance") or 0)
        return {
            "name": f"Saldo lealtad ({customer.get('email', '')})",
            "codigo": barcode,
            "price": -balance,          # negative → discount row
            "is_loyalty": True,
            "customer_email": customer.get("email", ""),
        }

    # ── Regular product ───────────────────────────────────────────────────
    rows = await supabase_request(
        method="GET",
        endpoint="/rest/v1/inventario1",
        params={
            "select": f"barcode,name,price,{INVENTORY_COL}",
            "barcode": f"eq.{barcode}",
            "limit": "1",
        },
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Producto {barcode} no encontrado")

    product = rows[0]
    stock = int(product.get(INVENTORY_COL) or 0)
    if stock <= 0:
        raise HTTPException(status_code=400, detail=f"Sin stock en {STORE_ID}")

    return {
        "name": product.get("name", ""),
        "codigo": barcode,
        "price": float(product.get("price") or 0),
        "is_loyalty": False,
    }


@router.post("/api/start_camera_capture")
async def start_camera_capture(barcode: str = ""):
    """Forward camera capture request to local camera service."""
    try:
        resp = requests.post(
            f"{LOCAL_CAMERA_SERVICE}/capture",
            params={"barcode": barcode},
            timeout=3,
        )
        return {"status": "ok", "camera_status": resp.status_code}
    except Exception as e:
        print(f"Camera capture error: {e}")
        return {"status": "error", "detail": str(e)}


@router.post("/api/save")
async def api_save(payload: SavePayload):
    """Process and persist a sale for store terex2."""
    if not payload.products:
        raise HTTPException(status_code=400, detail="No products provided")

    payment_method = payload.payment_method or "efectivo"
    print(f"DEBUG: payment_method={payment_method}")

    next_order_id = await get_next_order_id()

    mexico_tz = pytz.timezone("America/Mexico_City")
    now = datetime.now(mexico_tz)
    fecha = now.strftime("%Y-%m-%d")
    hora = now.strftime("%H:%M:%S")

    items_for_ticket: list = []
    loyalty_deductions: list = []

    for p in payload.products:
        p_dict = p.model_dump() if hasattr(p, "model_dump") else p.dict()
        codigo = p_dict.get("codigo", "")

        # ── Loyalty redemption ───────────────────────────────────────────
        if codigo.startswith("8000") and len(codigo) == 13:
            result = await process_loyalty_deduction(p_dict, next_order_id, fecha, hora)
            loyalty_deductions.append(result)
            items_for_ticket.append({
                "qty": p_dict.get("qty", 1),
                "name": p_dict.get("name", ""),
                "price": p_dict.get("price", 0),
                "subtotal": p_dict.get("qty", 1) * p_dict.get("price", 0),
            })
            continue

        # ── Regular product ──────────────────────────────────────────────
        inv_rows = await supabase_request(
            method="GET",
            endpoint="/rest/v1/inventario1",
            params={
                "select": f"modelo,modelo_id,estilo,estilo_id,{INVENTORY_COL}",
                "barcode": f"eq.{codigo}",
                "limit": "1",
            },
        )
        if not inv_rows:
            raise HTTPException(
                status_code=400,
                detail=f"Producto con barcode {codigo} no existe en inventario1",
            )
        inv = inv_rows[0]

        # Insert sale record
        record = {
            "qty": p_dict.get("qty", 1),
            "name": p_dict.get("name", ""),
            "name_id": codigo,
            "price": p_dict.get("price", 0),
            "fecha": fecha,
            "hora": hora,
            "order_id": next_order_id,
            "modelo": inv.get("modelo", ""),
            "modelo_id": inv.get("modelo_id", ""),
            "estilo": inv.get("estilo", ""),
            "estilo_id": inv.get("estilo_id", ""),
            "payment_method": payment_method,
        }
        await supabase_request(
            method="POST",
            endpoint=f"/rest/v1/{VENTAS_TABLE}",
            json_data=record,
        )

        # Decrement inventory
        current_qty = int(inv.get(INVENTORY_COL) or 0)
        new_qty = current_qty - p_dict.get("qty", 1)
        await supabase_request(
            method="PATCH",
            endpoint=f"/rest/v1/inventario1?barcode=eq.{codigo}",
            json_data={INVENTORY_COL: new_qty},
        )

        items_for_ticket.append({
            "qty": p_dict.get("qty", 1),
            "name": p_dict.get("name", ""),
            "price": p_dict.get("price", 0),
            "subtotal": p_dict.get("qty", 1) * p_dict.get("price", 0),
        })

    total = sum(i["subtotal"] for i in items_for_ticket)

    # ── Telegram notification ────────────────────────────────────────────
    try:
        payment_emoji = "💵" if payment_method == "efectivo" else "💳"
        total_pieces = sum(i["qty"] for i in items_for_ticket)
        send_telegram_message(
            f"🎉 VENTA #{next_order_id} [{STORE_ID.upper()}]\n"
            f"📊 {total_pieces} piezas\n"
            f"💰 ${total:.2f}\n"
            f"{payment_emoji} {payment_method.title()}"
        )
        import asyncio
        asyncio.create_task(send_telegram_picture(order_id=next_order_id))
    except Exception as e:
        print(f"Telegram error: {e}")

    # ── Cash register ────────────────────────────────────────────────────
    if payment_method == "efectivo":
        try:
            current_balance = await get_current_balance2()
            new_balance = current_balance + total
            url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2"
            conteo_payload = {
                "nombre": f"Venta #{next_order_id} [terex2]",
                "tipo": "credito",
                "amount": total,
                "balance": new_balance,
                "order_id": next_order_id,
            }
            resp = requests.post(url, headers=HEADERS, json=conteo_payload, timeout=10)
            resp.raise_for_status()
            print(f"Cash entry added for order {next_order_id}: ${total}")
        except Exception as e:
            print(f"Error adding conteo_efectivo entry: {e}")
    else:
        print(f"DEBUG: Skipping conteo_efectivo (payment_method={payment_method})")

    # ── Redemption token & PDF ───────────────────────────────────────────
    redemption_token = generate_redemption_token()
    await store_redemption_token(next_order_id, redemption_token, total)
    pdf_buf = _build_receipt_pdf_with_qr(items_for_ticket, total, next_order_id, redemption_token)

    filename = f"ticket_{STORE_ID}_{next_order_id}_{int(datetime.now().timestamp()*1000)}.pdf"
    return StreamingResponse(
        pdf_buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────
# SERVE FRONTEND
# ─────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Frontend not found — place index.html in /static</h1>")


@app.get("/nota1", response_class=HTMLResponse)
async def nota(request: Request):
    return templates.TemplateResponse("nota1.html", {"request": request})



@app.get("/entradamercancia2", response_class=HTMLResponse)
async def get_entrada_mercancia_2_form(request: Request):
    """Render the merchandise entry form for store 2"""
    try:
        print("Loading entrada mercancia 2 form", flush=True)
        return templates.TemplateResponse("entrada_mercancia_2.html", {
            "request": request
        })
    except Exception as e:
        print(f"Error loading entrada mercancia 2 form: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=f"Error loading form: {str(e)}")


@app.post("/entradamercancia2")
async def process_entrada_mercancia_2(
    request: Request,
    qty: int = Form(...),
    barcode: str = Form(...)
):
    """Process merchandise entry form and save to entrada_mercancia_2 / update terex2"""
    try:
        print(f"Processing entrada mercancia 2: qty={qty}, barcode={barcode}", flush=True)

        if qty <= 0:
            raise HTTPException(status_code=400, detail="La cantidad debe ser mayor a 0")

        if not barcode or barcode.strip() == "":
            raise HTTPException(status_code=400, detail="El código de barras es requerido")

        barcode = barcode.strip()

        try:
            barcode_int = int(barcode)
        except ValueError:
            raise HTTPException(status_code=400, detail="El código de barras debe ser numérico")

        # Fetch product info and current terex2 stock
        product_info = None
        current_terex2 = 0
        try:
            product_response = await supabase_request(
                method="GET",
                endpoint="/rest/v1/inventario1",
                params={
                    "select": "name,estilo_id,marca,terex2",
                    "barcode": f"eq.{barcode}",
                    "limit": "1"
                }
            )
            if product_response and len(product_response) > 0:
                product_info = product_response[0]
                current_terex2 = product_info.get("terex2", 0) or 0
                print(f"Found product: {product_info}, current terex2: {current_terex2}", flush=True)
            else:
                print(f"No product found with barcode {barcode}", flush=True)
        except Exception as product_error:
            print(f"Error fetching product info: {str(product_error)}", flush=True)

        # Build insert payload
        entrada_data = {
            "qty": qty,
            "barcode": barcode_int,
        }
        if product_info:
            if product_info.get("name"):
                entrada_data["estilo"] = product_info.get("name", "")
            if product_info.get("estilo_id"):
                entrada_data["estilo_id"] = product_info.get("estilo_id")

        print(f"Inserting entrada_mercancia_2 data: {entrada_data}", flush=True)

        # Insert into entrada_mercancia_2
        entrada_success = False
        try:
            response = await supabase_request(
                method="POST",
                endpoint="/rest/v1/entrada_mercancia_2",
                json_data=entrada_data
            )
            print(f"Insert response: {response}", flush=True)
            entrada_success = True
        except Exception as insert_error:
            print(f"Insert error: {str(insert_error)}, trying minimal insert", flush=True)
            try:
                response = await supabase_request(
                    method="POST",
                    endpoint="/rest/v1/entrada_mercancia_2",
                    json_data={"qty": qty, "barcode": barcode_int}
                )
                print(f"Minimal insert successful: {response}", flush=True)
                entrada_success = True
            except Exception as minimal_error:
                print(f"Minimal insert failed: {str(minimal_error)}", flush=True)
                raise HTTPException(status_code=500, detail=f"Database error: {str(minimal_error)}")

        # Update terex2 in inventario1
        if entrada_success and product_info:
            try:
                new_terex2 = current_terex2 + qty
                print(f"Updating terex2: {current_terex2} → {new_terex2} for barcode {barcode}", flush=True)
                update_response = await supabase_request(
                    method="PATCH",
                    endpoint=f"/rest/v1/inventario1?barcode=eq.{barcode_int}",
                    json_data={"terex2": new_terex2}
                )
                print(f"terex2 update response: {update_response}", flush=True)
            except Exception as update_error:
                print(f"Error updating terex2: {str(update_error)}", flush=True)
                import traceback
                traceback.print_exc()
                # Non-fatal — log and continue

        if entrada_success:
            return {
                "success": True,
                "message": "Entrada registrada exitosamente",
                "qty": qty,
                "barcode": barcode,
                "product_name": product_info.get("name", "Producto no identificado") if product_info else "Producto no identificado",
                "terex2_updated": product_info is not None
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to insert entrada")

    except Exception as e:
        print(f"Error in entrada mercancia 2: {str(e)}", flush=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error processing entrada: {str(e)}")


@app.get("/entradamercancia2/recientes")
async def get_recent_entries_2():
    """Get recent merchandise entries for store 2"""
    try:
        print("Fetching recent entrada_mercancia_2 records", flush=True)
        entries = await supabase_request(
            method="GET",
            endpoint="/rest/v1/entrada_mercancia_2",
            params={
                "select": "*",
                "order": "created_at.desc",
                "limit": "20"
            }
        )
        print(f"Retrieved {len(entries)} recent entries", flush=True)
        return {"success": True, "entries": entries}
    except Exception as e:
        print(f"Error fetching recent entries 2: {str(e)}", flush=True)
        return {"success": False, "error": str(e), "entries": []}

@app.get("/api/conteo2", response_model=List[ConteoEfectivoResponse])
async def get_conteo2(limit: Optional[int] = 100):
    """Get cash movement entries for store 2 (most recent first)"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2?order=created_at.desc&limit={limit}"
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()

        if not data:
            return []

        return [
            ConteoEfectivoResponse(
                id=entry["id"],
                nombre=entry["nombre"],
                tipo=entry["tipo"],
                amount=entry["amount"],
                balance=entry["balance"],
                created_at=entry["created_at"],
                order_id=entry.get("order_id"),
                descripcion=entry.get("descripcion"),
                diferencia=entry.get("diferencia")
            )
            for entry in data
        ]
    except Exception as e:
        print(f"Error fetching conteo2: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/conteo2", response_model=ConteoEfectivoResponse)
async def create_conteo2(data: ConteoEfectivoCreate):
    """Save a new cash movement entry for store 2"""
    try:
        current_balance = await get_current_balance2()

        new_balance = current_balance
        diferencia = None

        if data.tipo == 'credito':
            new_balance = current_balance + data.amount
        elif data.tipo == 'debito':
            new_balance = current_balance - data.amount
        elif data.tipo == 'conteo':
            diferencia = data.amount - current_balance
            new_balance = data.amount
        else:
            raise HTTPException(status_code=400, detail="Tipo must be 'credito', 'debito', or 'conteo'")

        url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2"
        payload = {
            "nombre": data.nombre,
            "tipo": data.tipo,
            "amount": data.amount,
            "balance": new_balance,
            "diferencia": diferencia
        }

        response = requests.post(url, headers=HEADERS, json=payload)
        response.raise_for_status()
        entry = response.json()[0]

        if data.tipo == 'conteo':
            if diferencia == 0:
                print(f"✅ Conteo correcto: ${current_balance:.2f} = ${data.amount:.2f}")
            elif diferencia > 0:
                print(f"💰 Sobrante: ${diferencia:.2f} (Esperado: ${current_balance:.2f}, Contado: ${data.amount:.2f})")
            else:
                print(f"⚠️ Faltante: ${abs(diferencia):.2f} (Esperado: ${current_balance:.2f}, Contado: ${data.amount:.2f})")

        return ConteoEfectivoResponse(
            id=entry["id"],
            nombre=entry["nombre"],
            tipo=entry["tipo"],
            amount=entry["amount"],
            balance=entry["balance"],
            created_at=entry["created_at"],
            order_id=entry.get("order_id"),
            descripcion=entry.get("descripcion"),
            diferencia=entry.get("diferencia")
        )
    except Exception as e:
        print(f"Error saving conteo2: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/conteo2/{conteo_id}")
async def delete_conteo2(conteo_id: int):
    """Delete a cash movement entry from store 2 and recalculate balances"""
    try:
        check_url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2?id=eq.{conteo_id}"
        check_response = requests.get(check_url, headers=HEADERS)
        check_response.raise_for_status()
        entry_data = check_response.json()

        if entry_data and entry_data[0].get('tipo') == 'inicial':
            raise HTTPException(status_code=400, detail="Cannot delete initial balance")

        url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2?id=eq.{conteo_id}"
        response = requests.delete(url, headers=HEADERS)
        response.raise_for_status()

        await recalculate_balances2()

        return {"success": True, "message": "Entry deleted and balances recalculated"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error deleting conteo2: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def get_current_balance2() -> float:
    """Get the current balance from the last entry in conteo_efectivo2"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2?order=created_at.desc&limit=1"
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()

        if not data:
            return 0.0

        return float(data[0].get('balance', 0.0))
    except Exception as e:
        print(f"Error getting current balance2: {e}")
        return 0.0


async def recalculate_balances2():
    """Recalculate all balances in conteo_efectivo2 after a deletion"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2?order=created_at.asc"
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        entries = response.json()

        running_balance = 0.0

        for entry in entries:
            if entry['tipo'] == 'inicial':
                running_balance = float(entry['amount'])
            elif entry['tipo'] == 'credito':
                running_balance += float(entry['amount'])
            elif entry['tipo'] == 'debito':
                running_balance -= float(entry['amount'])

            update_url = f"{SUPABASE_URL}/rest/v1/conteo_efectivo2?id=eq.{entry['id']}"
            update_response = requests.patch(update_url, headers=HEADERS, json={"balance": running_balance})
            update_response.raise_for_status()

        return running_balance
    except Exception as e:
        print(f"Error recalculating balances2: {e}")
        raise

@app.get("/conteoefectivo", response_class=HTMLResponse)
async def get_conteo_efectivo(request: Request):
    return templates.TemplateResponse("conteo_efectivo2.html", {"request": request})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index2.html", {"request": request})

# Register router
app.include_router(router)

# Optional: serve static files
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass

# ─────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=True)