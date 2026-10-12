"""Inventory service: stock lookup, idempotent reservation and release (owns `items`, `reservations`)."""

import logging

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.common import create_app, create_db_engine

app, tel = create_app("inventory")
engine = create_db_engine(tel)
log = logging.getLogger("evidencerca.inventory")


class Reservation(BaseModel):
    order_id: str
    item_id: str
    quantity: int = Field(gt=0)


@app.get("/stock/{item_id}")
def stock(item_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT item_id, stock, unit_price_cents FROM items WHERE item_id = :item_id"),
            {"item_id": item_id},
        ).one_or_none()
    if row is None:
        log.warning("unknown item", extra={"item_id": item_id})
        raise HTTPException(status_code=404, detail="unknown item")
    return {"item_id": row.item_id, "stock": row.stock, "unit_price_cents": row.unit_price_cents}


@app.post("/reserve")
def reserve(req: Reservation):
    with engine.begin() as conn:
        inserted = conn.execute(
            text("INSERT INTO reservations (order_id, item_id, quantity) VALUES (:order_id, :item_id, :quantity) "
                 "ON CONFLICT (order_id) DO NOTHING RETURNING order_id"),
            req.model_dump(),
        ).one_or_none()
        if inserted is not None:
            updated = conn.execute(
                text("UPDATE items SET stock = stock - :quantity WHERE item_id = :item_id AND stock >= :quantity "
                     "RETURNING stock"),
                req.model_dump(),
            ).one_or_none()
            if updated is None:  # raising rolls back the reservation insert
                log.warning("insufficient stock", extra={"order_id": req.order_id, "item_id": req.item_id})
                raise HTTPException(status_code=409, detail="insufficient stock")
        price = conn.execute(
            text("SELECT unit_price_cents FROM items WHERE item_id = :item_id"), {"item_id": req.item_id}
        ).scalar_one()
    log.info("stock reserved", extra={"order_id": req.order_id, "item_id": req.item_id,
                                      "quantity": req.quantity, "replayed": inserted is None})
    return {"order_id": req.order_id, "unit_price_cents": price}


@app.post("/release")
def release(req: Reservation):
    with engine.begin() as conn:
        released = conn.execute(
            text("DELETE FROM reservations WHERE order_id = :order_id RETURNING item_id, quantity"),
            {"order_id": req.order_id},
        ).one_or_none()
        if released is not None:
            conn.execute(text("UPDATE items SET stock = stock + :quantity WHERE item_id = :item_id"),
                         {"quantity": released.quantity, "item_id": released.item_id})
    log.info("reservation released", extra={"order_id": req.order_id, "found": released is not None})
    return {"order_id": req.order_id, "released": released is not None}
