import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import Product, DiscountCode, Order

router = APIRouter(tags=["Admin"])

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCOUNT_PERCENTAGE = 0.10  # 10% discount
NTH_ORDER_FOR_DISCOUNT = 5

@router.get("/discounts/")
def view_all_discount_codes(db: Session = Depends(get_db)):
    discounts = db.query(DiscountCode).all()
    if not discounts:
        logger.error("No discount codes found.")
        raise HTTPException(status_code=404, detail="No discount codes found.")
    
    logger.info(f"Fetched all discount codes.")
    return discounts

@router.get("/statistics/")
def view_statistics(db: Session = Depends(get_db)):
    total_items_purchased = sum([order.total_price for order in db.query(Order).all()])
    total_purchase_amount = db.query(Order).count()
    discount_codes = db.query(DiscountCode).all()
    total_discount_amount = total_purchase_amount * DISCOUNT_PERCENTAGE * len([code for code in discount_codes if code.is_used])

    statistics = {
        "total_items_purchased": total_items_purchased,
        "total_purchase_amount": total_purchase_amount,
        "discount_codes": [code.code for code in discount_codes],
        "total_discount_amount": total_discount_amount
    }

    logger.info(f"Fetched statistics.")
    return statistics
