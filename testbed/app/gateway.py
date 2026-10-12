"""API gateway: public entry point; checks stock with inventory, then forwards orders to order."""

import logging
import os

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.common import Downstream, create_app

app, tel = create_app("gateway")
order = Downstream("order", os.environ["ORDER_URL"])
inventory = Downstream("inventory", os.environ["INVENTORY_URL"])
log = logging.getLogger("evidencerca.gateway")


class PlaceOrder(BaseModel):
    item_id: str
    quantity: int = Field(gt=0)


def passthrough(response):
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail"))
    return response.json()


@app.get("/api/stock/{item_id}")
def get_stock(item_id: str):
    return passthrough(inventory.request("GET", f"/stock/{item_id}"))


@app.post("/api/orders", status_code=201)
def place_order(req: PlaceOrder):
    stock = passthrough(inventory.request("GET", f"/stock/{req.item_id}"))
    if stock["stock"] < req.quantity:
        log.warning("order refused: insufficient stock",
                    extra={"item_id": req.item_id, "quantity": req.quantity, "stock": stock["stock"]})
        raise HTTPException(status_code=409, detail="insufficient stock")
    placed = passthrough(order.request("POST", "/orders", json=req.model_dump()))
    log.info("order placed", extra={"order_id": placed["order_id"], "item_id": req.item_id})
    return placed


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    return passthrough(order.request("GET", f"/orders/{order_id}"))
