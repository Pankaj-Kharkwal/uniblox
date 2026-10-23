from database import Base, engine
from models import User, Product, Order, Cart, DiscountCode, Item

def init_database():
    Base.metadata.create_all(bind=engine)
    print("Database initialized with tables.")

if __name__ == "__main__":
    init_database()
