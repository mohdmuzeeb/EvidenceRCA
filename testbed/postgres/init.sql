-- Schema and seed data for the EvidenceRCA testbed (applied on first start of an empty data dir).
-- Phase 2: stock raised so that ~5 min of sustained workload never runs an item out (see docs/phase2_validation.md).
CREATE TABLE items (
    item_id          text PRIMARY KEY,
    name             text NOT NULL,
    unit_price_cents integer NOT NULL CHECK (unit_price_cents > 0),
    stock            integer NOT NULL CHECK (stock >= 0)
);

CREATE TABLE reservations (
    order_id uuid PRIMARY KEY,
    item_id  text NOT NULL REFERENCES items (item_id),
    quantity integer NOT NULL CHECK (quantity > 0)
);

CREATE TABLE orders (
    order_id     uuid PRIMARY KEY,
    item_id      text NOT NULL,
    quantity     integer NOT NULL CHECK (quantity > 0),
    status       text NOT NULL,
    amount_cents integer,
    payment_id   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO items (item_id, name, unit_price_cents, stock) VALUES
    ('sku-001', 'widget',   1250, 1000000),
    ('sku-002', 'gadget',   4999, 1000000),
    ('sku-003', 'gizmo',     799, 1000000),
    ('sku-004', 'doohickey', 2500,      50),  -- kept small: insufficient-stock rejections
    ('sku-005', 'sprocket',  150, 1000000);
