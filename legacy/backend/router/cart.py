import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import Cart, Product, User

router = APIRouter(tags=['Cart'])

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@router.post("/users/{user_id}/cart")
def add_to_cart(user_id: int, product_id: int, quantity: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.error(f"User with ID {user_id} not found.")
        raise HTTPException(status_code=404, detail="User not found.")

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        logger.error(f"Product with ID {product_id} not found.")
        raise HTTPException(status_code=404, detail="Product not found.")
    
    if product.stock < quantity:
        logger.error(f"Insufficient stock for product with ID {product_id}.")
        raise HTTPException(status_code=400, detail="Insufficient stock.")

    cart_item = Cart(user_id=user_id, item_id=product_id, quantity=quantity)
    db.add(cart_item)
    db.commit()
    db.refresh(cart_item)
    logger.info(f"Added {quantity} of product ID {product_id} to cart of user ID {user_id}.")
    return cart_item

@router.get("/users/{user_id}/cart")
def view_cart(user_id: int, db: Session = Depends(get_db)):
    cart_items = db.query(Cart).filter(Cart.user_id == user_id).all()
    if not cart_items:
        logger.error(f"No items in cart for user with ID {user_id}.")
        raise HTTPException(status_code=404, detail="Cart is empty.")

    logger.info(f"Fetched cart for user with ID {user_id}.")
    return cart_items

@router.delete("/users/{user_id}/cart/{product_id}")
def remove_from_cart(user_id: int, product_id: int, db: Session = Depends(get_db)):
    cart_item = db.query(Cart).filter(Cart.user_id == user_id, Cart.item_id == product_id).first()
    if not cart_item:
        logger.error(f"Product with ID {product_id} not found in cart of user with ID {user_id}.")
        raise HTTPException(status_code=404, detail="Product not found in cart.")

    db.delete(cart_item)
    db.commit()
    logger.info(f"Removed product with ID {product_id} from cart of user with ID {user_id}.")
    return {"message": "Product removed from cart successfully."}
