"""Order service: creates an order, reserves stock, charges payment (owns `orders`)."""

import logging
import os
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.common import Downstream, create_app, create_db_engine

app, tel = create_app("order")
engine = create_db_engine(tel)
inventory = Downstream("inventory", os.environ["INVENTORY_URL"])
payment = Downstream("payment", os.environ["PAYMENT_URL"])
log = logging.getLogger("evidencerca.order")


class NewOrder(BaseModel):
    item_id: str
    quantity: int = Field(gt=0)


def set_status(order_id: str, status: str, **fields) -> None:
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE orders SET status = :status, updated_at = now(){', ' + assignments if fields else ''} "
                 "WHERE order_id = :order_id"),
            {"order_id": order_id, "status": status, **fields},
        )


@app.post("/orders", status_code=201)
def create_order(req: NewOrder):
    order_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO orders (order_id, item_id, quantity, status) "
                 "VALUES (:order_id, :item_id, :quantity, 'pending')"),
            {"order_id": order_id, **req.model_dump()},
        )
    log.info("order created", extra={"order_id": order_id, "item_id": req.item_id, "quantity": req.quantity})

    reservation = {"order_id": order_id, **req.model_dump()}
    reserved = inventory.request("POST", "/reserve", json=reservation)
    if reserved.status_code != 200:
        set_status(order_id, "rejected")
        log.warning("reservation failed", extra={"order_id": order_id, "status_code": reserved.status_code})
        raise HTTPException(status_code=reserved.status_code, detail=reserved.json().get("detail"))

    amount = reserved.json()["unit_price_cents"] * req.quantity
    charged = payment.request("POST", "/charge", json={"order_id": order_id, "amount_cents": amount})
    if charged.status_code != 200:
        inventory.request("POST", "/release", json=reservation)
        set_status(order_id, "payment_failed", amount_cents=amount)
        log.warning("payment failed", extra={"order_id": order_id, "status_code": charged.status_code})
        raise HTTPException(status_code=402 if charged.status_code == 402 else 502, detail="payment failed")

    payment_id = charged.json()["payment_id"]
    set_status(order_id, "confirmed", amount_cents=amount, payment_id=payment_id)
    log.info("order confirmed", extra={"order_id": order_id, "payment_id": payment_id, "amount_cents": amount})
    return {"order_id": order_id, "status": "confirmed", "amount_cents": amount, "payment_id": payment_id}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT order_id, item_id, quantity, status, amount_cents, payment_id FROM orders "
                 "WHERE order_id = :order_id"),
            {"order_id": order_id},
        ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown order")
    return {**row, "order_id": str(row["order_id"])}
