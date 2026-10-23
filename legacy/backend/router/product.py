import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models import Product
from schema import ProductCreate

router = APIRouter(tags=['Product'])

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@router.post("/products/")
def create_product(product: ProductCreate, db: Session = Depends(get_db)):
    existing_product = db.query(Product).filter(Product.name == product.name).first()
    if existing_product:
        logger.error(f"Product with name {product.name} already exists.")
        raise HTTPException(status_code=400, detail="Product with this name already exists.")
    db_product = Product(
        name=product.name,
        description=product.description,
        price=product.price,
        stock=product.stock,
        category=product.category
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    logger.info(f"Product {product.name} created successfully.")
    return db_product

@router.get("/products/")
def list_products(
    db: Session = Depends(get_db),
    name: str = None,
    min_price: float = None,
    max_price: float = None,
    category: str = None
):
    query = db.query(Product)

    if name:
        query = query.filter(Product.name.contains(name))
    if min_price is not None:
        query = query.filter(Product.price >= min_price)
    if max_price is not None:
        query = query.filter(Product.price <= max_price)
    if category:
        query = query.filter(Product.category == category)

    products = query.all()
    
    if not products:
        logger.error("No products found for given criteria.")
        raise HTTPException(status_code=404, detail="No products found for given criteria.")

    logger.info(f"Found {len(products)} products for given criteria.")
    return products

@router.get("/products/{product_id}")
def get_product_by_id(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        logger.error(f"Product with ID {product_id} not found.")
        raise HTTPException(status_code=404, detail="Product not found.")
    
    logger.info(f"Fetched product with ID {product_id}.")
    return product