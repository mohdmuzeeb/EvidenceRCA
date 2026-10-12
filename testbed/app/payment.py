"""Payment service: idempotent charge per order (in-memory ledger; no database)."""

import logging
import threading
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.common import create_app

app, tel = create_app("payment")
log = logging.getLogger("evidencerca.payment")

MAX_AMOUNT_CENTS = 1_000_000
_ledger: dict[str, str] = {}
_lock = threading.Lock()


class Charge(BaseModel):
    order_id: str
    amount_cents: int = Field(gt=0)


@app.post("/charge")
def charge(req: Charge):
    if req.amount_cents > MAX_AMOUNT_CENTS:
        log.warning("charge declined", extra={"order_id": req.order_id, "amount_cents": req.amount_cents})
        raise HTTPException(status_code=402, detail="amount over limit")
    with _lock:
        payment_id = _ledger.setdefault(req.order_id, str(uuid.uuid4()))
    log.info("payment authorised", extra={"order_id": req.order_id, "payment_id": payment_id,
                                          "amount_cents": req.amount_cents})
    return {"order_id": req.order_id, "payment_id": payment_id, "status": "authorised"}
