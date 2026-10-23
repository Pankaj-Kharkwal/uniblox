from sqlalchemy import Column, Integer, String, Float, Boolean, ForeignKey
from database import Base

class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey('products.id'))
    order_id = Column(Integer, ForeignKey('orders.id'))
    quantity = Column(Integer)  # Represents the number of this product in an order or cart

class Cart(Base):
    __tablename__ = "carts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, unique=True)
    item_id = Column(Integer, ForeignKey('items.id'))
    quantity = Column(Integer)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String)
    total_price = Column(Float)
    discount_code = Column(String)

class DiscountCode(Base):
    __tablename__ = "discount_codes"
    code = Column(String, primary_key=True, index=True)
    is_used = Column(Boolean, default=False)
    
class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(String)
    price = Column(Float)
    stock = Column(Integer)  # Represents the number of items available for this product
    category = Column(String, index=True)

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True,index=True)
    full_name = Column(String, index=True)
