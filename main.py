import os
import json
import hashlib
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from database import (
    DB_ERRORS,
    begin_write_transaction,
    get_connection as get_db,
)


logger = logging.getLogger(__name__)

# =========================================================
# ENVIRONMENT
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

RINGAI_API_KEY = os.getenv("RINGAI_API_KEY")
RINGAI_WORKFLOW_ID = os.getenv("RINGAI_WORKFLOW_ID")
RINGAI_API_BASE_URL = os.getenv(
    "RINGAI_API_BASE_URL",
    "https://api.ringai.com",
).rstrip("/")

# =========================================================
# FASTAPI APP
# =========================================================

app = FastAPI(
    title="AI Order Manager",
    description="AI-powered voice order management system",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def database_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("Database request failed (%s)", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "The database is temporarily unavailable."},
    )


for database_error in DB_ERRORS:
    app.add_exception_handler(database_error, database_error_handler)


# =========================================================
# PASSWORD HASHING
# =========================================================

def hash_password(password: str) -> str:

    salt = secrets.token_hex(16)

    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt.encode(),
        100000
    )

    return f"{salt}${hashed.hex()}"


def verify_password(
    password: str,
    stored_password: str
) -> bool:

    try:

        salt, stored_hash = stored_password.split("$")

        hashed = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            salt.encode(),
            100000
        )

        return secrets.compare_digest(
            hashed.hex(),
            stored_hash
        )

    except Exception:
        return False


# =========================================================
# PYDANTIC MODELS
# =========================================================

class ShopkeeperRegister(BaseModel):

    name: str
    shop_name: str
    phone_number: str
    email: str
    password: str


class ShopkeeperLogin(BaseModel):

    email: str
    password: str


class ShopkeeperLogout(BaseModel):

    shopkeeper_id: int


class ToggleCallingRequest(BaseModel):

    shopkeeper_id: int
    active: bool


class PreCallRequest(BaseModel):

    shopkeeper_id: int


class OrderItem(BaseModel):

    product: str
    quantity: int = Field(gt=0)


class CreateOrderRequest(BaseModel):

    customer_name: str
    shopkeeper_id: int
    items: List[OrderItem]


# =========================================================
# ROOT
# =========================================================

@app.get("/")
def root():

    return {
        "status": "online",
        "application": "AI Order Manager",
        "version": "2.1.0"
    }


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
def health():
    conn = get_db()
    try:
        conn.execute("SELECT 1 FROM shopkeepers, products, orders LIMIT 0")
    finally:
        conn.close()

    return {
        "status": "healthy",
        "database": "connected",
        "ring_ai": "configured" if RINGAI_API_KEY else "not_configured",
        "workflow": "configured" if RINGAI_WORKFLOW_ID else "not_configured"
    }


# =========================================================
# SHOPKEEPER REGISTER
# =========================================================

@app.post("/shopkeeper/register")
def register_shopkeeper(data: ShopkeeperRegister):

    conn = get_db()
    cursor = conn.cursor()

    existing = cursor.execute(
        """
        SELECT id
        FROM shopkeepers
        WHERE email = ? OR phone_number = ?
        """,
        (
            data.email,
            data.phone_number
        )
    ).fetchone()

    if existing:

        conn.close()

        raise HTTPException(
            status_code=400,
            detail="Email or phone number already registered"
        )

    password_hash = hash_password(data.password)

    cursor.execute(
        """
        INSERT INTO shopkeepers
        (
            name,
            shop_name,
            phone_number,
            email,
            password,
            ai_calling_active,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            data.name,
            data.shop_name,
            data.phone_number,
            data.email,
            password_hash,
            1,
            datetime.now().isoformat()
        )
    )

    shopkeeper_id = cursor.fetchone()["id"]

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "Shopkeeper registered successfully",
        "shopkeeper_id": shopkeeper_id,
        "ai_calling_active": True
    }


# =========================================================
# SHOPKEEPER LOGIN
# =========================================================

@app.post("/shopkeeper/login")
def login_shopkeeper(data: ShopkeeperLogin):

    conn = get_db()

    shopkeeper = conn.execute(
        """
        SELECT *
        FROM shopkeepers
        WHERE email = ?
        """,
        (data.email,)
    ).fetchone()

    if not shopkeeper:

        conn.close()

        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    if not verify_password(
        data.password,
        shopkeeper["password"]
    ):

        conn.close()

        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    # Login -> enable AI calling
    conn.execute(
        """
        UPDATE shopkeepers
        SET ai_calling_active = 1
        WHERE id = ?
        """,
        (shopkeeper["id"],)
    )

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "Login successful",
        "shopkeeper": {
            "id": shopkeeper["id"],
            "name": shopkeeper["name"],
            "shop_name": shopkeeper["shop_name"],
            "phone_number": shopkeeper["phone_number"],
            "email": shopkeeper["email"],
            "ai_calling_active": True
        }
    }


# =========================================================
# SHOPKEEPER LOGOUT
# =========================================================

@app.post("/shopkeeper/logout")
def logout_shopkeeper(data: ShopkeeperLogout):

    conn = get_db()

    shopkeeper = conn.execute(
        """
        SELECT id
        FROM shopkeepers
        WHERE id = ?
        """,
        (data.shopkeeper_id,)
    ).fetchone()

    if not shopkeeper:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="Shopkeeper not found"
        )

    # IMPORTANT:
    # Phone number database se delete nahi hota.
    # Sirf AI calling disable hoti hai.

    conn.execute(
        """
        UPDATE shopkeepers
        SET ai_calling_active = 0
        WHERE id = ?
        """,
        (data.shopkeeper_id,)
    )

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "Logged out successfully. AI calling disabled.",
        "ai_calling_active": False
    }


# =========================================================
# SHOPKEEPER STATUS
# =========================================================

@app.get("/shopkeeper/{shopkeeper_id}/status")
def shopkeeper_status(shopkeeper_id: int):

    conn = get_db()

    shopkeeper = conn.execute(
        """
        SELECT
            id,
            name,
            shop_name,
            phone_number,
            email,
            ai_calling_active,
            created_at
        FROM shopkeepers
        WHERE id = ?
        """,
        (shopkeeper_id,)
    ).fetchone()

    conn.close()

    if not shopkeeper:

        raise HTTPException(
            status_code=404,
            detail="Shopkeeper not found"
        )

    return {
        "success": True,
        "shopkeeper": dict(shopkeeper)
    }


# =========================================================
# ENABLE / DISABLE AI CALLING
# =========================================================

@app.post("/shopkeeper/toggle-calling")
def toggle_calling(data: ToggleCallingRequest):

    conn = get_db()

    shopkeeper = conn.execute(
        """
        SELECT id
        FROM shopkeepers
        WHERE id = ?
        """,
        (data.shopkeeper_id,)
    ).fetchone()

    if not shopkeeper:

        conn.close()

        raise HTTPException(
            status_code=404,
            detail="Shopkeeper not found"
        )

    conn.execute(
        """
        UPDATE shopkeepers
        SET ai_calling_active = ?
        WHERE id = ?
        """,
        (
            1 if data.active else 0,
            data.shopkeeper_id
        )
    )

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": (
            "AI calling enabled"
            if data.active
            else "AI calling disabled"
        ),
        "ai_calling_active": data.active
    }


# =========================================================
# RING AI - PRE CALL DATA
# =========================================================
#
# Ring AI / public workflow can call this endpoint BEFORE
# starting the voice interaction.
#
# Public URL example:
#
# https://YOUR-NGROK-URL.ngrok-free.dev/voice/pre-call
#
# Body:
#
# {
#   "shopkeeper_id": 1
# }
#
# =========================================================

@app.post("/voice/pre-call")
def voice_pre_call(data: PreCallRequest):

    conn = get_db()

    shopkeeper = conn.execute(
        """
        SELECT
            id,
            name,
            shop_name,
            phone_number,
            email,
            ai_calling_active
        FROM shopkeepers
        WHERE id = ?
        """,
        (data.shopkeeper_id,)
    ).fetchone()

    conn.close()

    if not shopkeeper:

        raise HTTPException(
            status_code=404,
            detail="Shopkeeper not found"
        )

    # Logout ke baad Ring AI ko data nahi milega.
    if shopkeeper["ai_calling_active"] != 1:

        raise HTTPException(
            status_code=403,
            detail="AI calling is disabled for this shopkeeper"
        )

    return {
        "success": True,
        "shopkeeper_id": shopkeeper["id"],
        "name": shopkeeper["name"],
        "shop_name": shopkeeper["shop_name"],
        "phone_number": shopkeeper["phone_number"],
        "ai_calling_active": True
    }


# =========================================================
# RING AI - FETCH WORKFLOWS
# =========================================================

@app.get("/ringai/workflows")
def get_ringai_workflows():

    if not RINGAI_API_KEY:

        raise HTTPException(
            status_code=500,
            detail="RINGAI_API_KEY is missing in backend/.env"
        )

    try:

        response = requests.get(
            f"{RINGAI_API_BASE_URL}/api/v1/workflow/fetch",
            headers={
                "Authorization": f"Bearer {RINGAI_API_KEY}",
                "Content-Type": "application/json"
            },
            timeout=30
        )

        try:
            data = response.json()

        except Exception:

            data = {
                "raw_response": response.text
            }

        return {
            "success": response.ok,
            "ringai_status_code": response.status_code,
            "data": data
        }

    except requests.exceptions.RequestException as e:

        raise HTTPException(
            status_code=502,
            detail=f"Unable to connect to Ring AI: {str(e)}"
        )


# =========================================================
# RING AI - INITIATE CALL
# =========================================================

def initiate_ring_ai_call(
    phone_number: str,
    shopkeeper_id: int,
    shopkeeper_name: str,
    shop_name: str
):

    if not RINGAI_API_KEY:

        return {
            "success": False,
            "message": "RINGAI_API_KEY is missing"
        }

    if not RINGAI_WORKFLOW_ID:

        return {
            "success": False,
            "message": "RINGAI_WORKFLOW_ID is missing"
        }

    try:

        response = requests.post(
            f"{RINGAI_API_BASE_URL}/api/v1/telephony/initiate-call",
            headers={
                "Authorization": f"Bearer {RINGAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "workflow_id": RINGAI_WORKFLOW_ID,
                "phone_number": phone_number,
                "context": {
                    "shopkeeper_id": shopkeeper_id,
                    "shopkeeper_name": shopkeeper_name,
                    "shop_name": shop_name
                }
            },
            timeout=30
        )

        try:
            response_data = response.json()

        except Exception:

            response_data = {
                "raw_response": response.text
            }

        return {
            "success": response.ok,
            "ringai_status_code": response.status_code,
            "data": response_data
        }

    except requests.exceptions.RequestException as e:

        return {
            "success": False,
            "message": f"Ring AI request failed: {str(e)}"
        }


# =========================================================
# MANUALLY START AI CALL
# =========================================================

@app.post("/shopkeeper/{shopkeeper_id}/call")
def call_shopkeeper(shopkeeper_id: int):

    conn = get_db()

    shopkeeper = conn.execute(
        """
        SELECT *
        FROM shopkeepers
        WHERE id = ?
        """,
        (shopkeeper_id,)
    ).fetchone()

    conn.close()

    if not shopkeeper:

        raise HTTPException(
            status_code=404,
            detail="Shopkeeper not found"
        )

    # Logout ke baad call nahi jayegi.
    if shopkeeper["ai_calling_active"] != 1:

        return {
            "success": False,
            "message": "AI calling is disabled for this shopkeeper",
            "ai_calling_active": False
        }

    result = initiate_ring_ai_call(
        phone_number=shopkeeper["phone_number"],
        shopkeeper_id=shopkeeper["id"],
        shopkeeper_name=shopkeeper["name"],
        shop_name=shopkeeper["shop_name"]
    )

    return result


# =========================================================
# PRODUCTS
# =========================================================

@app.get("/products")
def get_products():

    conn = get_db()

    products = conn.execute(
        """
        SELECT *
        FROM products
        ORDER BY id
        """
    ).fetchall()

    conn.close()

    return {
        "success": True,
        "products": [dict(product) for product in products]
    }


# =========================================================
# CREATE ORDER
# =========================================================

@app.post("/order/create")
def create_order(data: CreateOrderRequest):
    conn = get_db()
    try:
        begin_write_transaction(conn)
        cursor = conn.cursor()

        shopkeeper = cursor.execute(
            "SELECT id FROM shopkeepers WHERE id = ?",
            (data.shopkeeper_id,),
        ).fetchone()
        if not shopkeeper:
            raise HTTPException(status_code=404, detail="Shopkeeper not found")

        if not data.items:
            raise HTTPException(
                status_code=400,
                detail="Order must contain at least one item",
            )

        requested_items = {}
        for item in data.items:
            product_name = item.product.strip()
            key = product_name.casefold()
            if not key:
                raise HTTPException(status_code=404, detail="Product not found")
            if key in requested_items:
                requested_items[key][1] += item.quantity
            else:
                requested_items[key] = [product_name, item.quantity]

        total = 0
        order_items = []
        for product_name, quantity in requested_items.values():
            product = cursor.execute(
                "SELECT id, name, price, stock FROM products "
                "WHERE LOWER(name) = LOWER(?)",
                (product_name,),
            ).fetchone()
            if not product:
                raise HTTPException(
                    status_code=404,
                    detail=f"Product not found: {product_name}",
                )
            if product["stock"] < quantity:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock for {product['name']}",
                )

            item_total = product["price"] * quantity
            total += item_total
            order_items.append({
                "product": product["name"],
                "quantity": quantity,
                "price": product["price"],
                "subtotal": item_total,
            })

        for item in order_items:
            cursor.execute(
                "UPDATE products SET stock = stock - ? "
                "WHERE name = ? AND stock >= ?",
                (item["quantity"], item["product"], item["quantity"]),
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient stock for {item['product']}",
                )

        created_at = datetime.now().isoformat()
        cursor.execute(
            """
            INSERT INTO orders
                (customer_name, shopkeeper_id, items, total, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                data.customer_name.strip(),
                data.shopkeeper_id,
                json.dumps(order_items),
                total,
                "pending",
                created_at,
            ),
        )
        order_id = cursor.fetchone()["id"]
        conn.commit()

        return {
            "success": True,
            "message": "Order created successfully",
            "order": {
                "id": order_id,
                "customer_name": data.customer_name.strip(),
                "shopkeeper_id": data.shopkeeper_id,
                "items": order_items,
                "total": total,
                "status": "pending",
                "created_at": created_at,
            },
        }
    except HTTPException:
        conn.rollback()
        raise
    except DB_ERRORS as error:
        conn.rollback()
        logger.error("Order creation failed (%s)", type(error).__name__)
        raise HTTPException(
            status_code=500,
            detail="The order could not be saved. Please try again.",
        ) from error
    finally:
        conn.close()


# =========================================================
# GET ORDERS
# =========================================================

@app.get("/orders")
def get_orders(
    shopkeeper_id: Optional[int] = None
):

    conn = get_db()

    if shopkeeper_id is not None:

        orders = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE shopkeeper_id = ?
            ORDER BY id DESC
            """,
            (shopkeeper_id,)
        ).fetchall()

    else:

        orders = conn.execute(
            """
            SELECT *
            FROM orders
            ORDER BY id DESC
            """
        ).fetchall()

    conn.close()

    result = []

    for order in orders:

        order_data = dict(order)

        try:

            order_data["items"] = json.loads(
                order_data["items"]
            )

        except Exception:
            pass

        result.append(order_data)

    return {
        "success": True,
        "orders": result
    }


# =========================================================
# RING AI WEBHOOK
# =========================================================

@app.post("/voice/webhook")
def voice_webhook(data: dict):
    logger.info("Ring AI webhook received")

    return {
        "success": True,
        "message": "Webhook received",
        "data": data
    }


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000
    )