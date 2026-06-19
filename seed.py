"""Insert a few sample products so you can try the POS immediately.

Run:  python seed.py   (safe to skip; safe to re-run — it won't duplicate)
"""
from decimal import Decimal

from app import app
from models import db, Product, StockMovement

SAMPLES = [
    # barcode, name, price, cost, qty, reorder
    ("6001240001", "Maize Flour 2kg",      180, 150, 40, 10),
    ("6001240002", "Cooking Oil 1L",       320, 270, 25, 8),
    ("6001240003", "Sugar 1kg",            160, 135, 30, 10),
    ("6001240004", "Bread 400g",            65,  50, 18, 6),
    ("6001240005", "Milk 500ml",            60,  48, 50, 12),
    ("6001240006", "Rice 2kg",             290, 245, 20, 6),
    ("6001240007", "Tea Leaves 250g",      150, 120, 15, 5),
    ("6001240008", "Soap Bar",              45,  32,  4, 6),   # low stock on purpose
    ("6001240009", "Salt 1kg",              40,  28, 35, 8),
    ("6001240010", "Soda 500ml",            70,  55, 60, 15),
]


def run():
    with app.app_context():
        added = 0
        for barcode, name, price, cost, qty, reorder in SAMPLES:
            if Product.query.filter_by(barcode=barcode).first():
                continue
            p = Product(
                barcode=barcode, name=name,
                price=Decimal(price), cost_price=Decimal(cost),
                quantity=qty, reorder_level=reorder,
            )
            db.session.add(p)
            db.session.flush()
            db.session.add(StockMovement(
                product_id=p.id, change_qty=qty,
                movement_type="receive", note="Seed opening stock",
            ))
            added += 1
        db.session.commit()
        print(f"Seeded {added} new products ({len(SAMPLES) - added} already existed).")


if __name__ == "__main__":
    run()
