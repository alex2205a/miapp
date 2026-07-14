from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool
import os
from dotenv import load_dotenv
import socket

load_dotenv()

# 🔥 Obtener la IP de Supabase (IPv4)
SUPABASE_HOST = "db.krtwieirxyfyarqklleo.supabase.co"
try:
    SUPABASE_IP = socket.gethostbyname(SUPABASE_HOST)
    print(f"✅ IP de Supabase: {SUPABASE_IP}")
except:
    SUPABASE_IP = SUPABASE_HOST

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL no está configurada")

# 🔥 Usar hostaddr para forzar IPv4
DATABASE_URL = DATABASE_URL.replace(
    SUPABASE_HOST, 
    SUPABASE_IP
)

engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    connect_args={
        "sslmode": "require",
        "connect_timeout": 10,
        "hostaddr": SUPABASE_IP  # Forzar IPv4
    }
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
