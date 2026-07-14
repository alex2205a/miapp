import os
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

print(f"URL: {SUPABASE_URL}")
print(f"KEY: {SUPABASE_KEY[:10]}...")

try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    response = supabase.auth.admin.create_user({
        "email": "admin@escuela.com",
        "password": "Admin123!",
        "email_confirm": True,
        "user_metadata": {
            "matricula": "ADMIN1",
            "nombre": "Admin Principal",
            "rol": "administrador"
        }
    })
    print("✅ Usuario creado con éxito.")
    print(f"🆔 UUID: {response.user.id}")
    print("➡️ Ahora ve a http://localhost:8000/login y usa matrícula: ADMIN1")
except Exception as e:
    print(f"❌ Error: {e}")