import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import User, Order
from schema import UserCreate
router = APIRouter(tags=['User'])

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@router.post("/users/")
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.username == user.username).first()
    if existing_user:
        logger.error(f"User with username {user.username} already exists.")
        raise HTTPException(status_code=400, detail="Username already exists.")
    db_user = User(
        username=user.username,
        email = user.email,
        full_name=user.full_name
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    logger.info(f"User {user.username} created successfully.")
    return db_user

@router.get("/users/{user_id}")
def get_user_by_id(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.error(f"User with ID {user_id} not found.")
        raise HTTPException(status_code=404, detail="User not found.")
    
    logger.info(f"Fetched user with ID {user_id}.")
    return user

@router.get("/users/{user_id}/orders")
def get_user_orders(user_id: int, db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.user_id == user_id).all()
    if not orders:
        logger.error(f"No orders found for user with ID {user_id}.")
        raise HTTPException(status_code=404, detail="No orders found for this user.")
    
    logger.info(f"Fetched {len(orders)} orders for user with ID {user_id}.")
    return orders
