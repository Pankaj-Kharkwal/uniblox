import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import Cart, Product, Order, DiscountCode, User

router = APIRouter(tags=['Checkout'])

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCOUNT_PERCENTAGE = 0.10  # 10% discount
NTH_ORDER_FOR_DISCOUNT = 3

@router.post("/users/{user_id}/checkout")
def checkout(user_id: int, discount_code: str = None, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.error(f"User with ID {user_id} not found.")
        raise HTTPException(status_code=404, detail="User not found.")

    cart_items = db.query(Cart).filter(Cart.user_id == user_id).all()
    if not cart_items:
        logger.error(f"No items in cart for user with ID {user_id}.")
        raise HTTPException(status_code=400, detail="Cart is empty.")

    total_price = 0
    for item in cart_items:
        product = db.query(Product).filter(Product.id == item.item_id).first()
        if product.stock < item.quantity:
            logger.error(f"Insufficient stock for product with ID {item.item_id}.")
            raise HTTPException(status_code=400, detail=f"Insufficient stock for {product.name}.")
        product.stock -= item.quantity
        total_price += product.price * item.quantity

    if discount_code:
        discount = db.query(DiscountCode).filter(DiscountCode.code == discount_code).first()
        if not discount or discount.is_used:
            logger.error(f"Invalid or already used discount code {discount_code}.")
            raise HTTPException(status_code=400, detail="Invalid or already used discount code.")
        total_price -= total_price * DISCOUNT_PERCENTAGE
        discount.is_used = True

    order = Order(user_id=user_id, total_price=total_price)
    db.add(order)
    db.commit()
    db.refresh(order)
    for item in cart_items:
        db.delete(item)
    db.commit()

       # Check if we should generate a new discount code
    total_orders = db.query(Order).filter(Order.user_id == user_id).count()
    if (total_orders + 1) % NTH_ORDER_FOR_DISCOUNT == 0:  # assuming nth_order is the interval for discount codes
        discount_code = generate_discount_code()  # a function to generate a unique code
        db_discount_code = DiscountCode(code=discount_code, is_used=False)
        db.add(db_discount_code)
        db.commit()
        logger.info(f"Generated new discount code {discount_code} for order ID {order.id}.")
        return {"order_id": order.id, "total_price": total_price, "discount code for next order": discount_code}

    logger.info(f"User with ID {user_id} checked out successfully.")
    return {"order_id": order.id, "total_price": total_price}

def generate_discount_code():
    # Generate a unique alphanumeric code
    # This is a simple example; consider using more robust methods
    import random
    import string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
