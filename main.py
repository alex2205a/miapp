from fastapi import FastAPI, HTTPException, Depends, Request
from pydantic import BaseModel
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy.sql import text
from database import get_db
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware
import uuid
from typing import Optional
import os
from supabase import create_client, Client
from dotenv import load_dotenv
import ssl
import subprocess
import re
import platform
from datetime import datetime

ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
print(f"🔗 URL cargada: {SUPABASE_URL}")  # 👈 MUY IMPORTANTE
print(f"🔑 KEY cargada: {SUPABASE_KEY[:10]}...")  # 👈 MUY IMPORTANTE
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Sistema de Asistencia Escolar")
templates = Jinja2Templates(directory="templates")

# Habilitar CORS para desarrollo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- MODELOS ----
class LoginMaestroRequest(BaseModel):
    matricula: str

class LoginAlumnoRequest(BaseModel):
    matricula: str
    device_id: str

class IniciarSesionRequest(BaseModel):
    clase_id: int

class RegistrarAsistenciaRequest(BaseModel):
    alumno_id: str
    clase_id: int
    bssid_alumno: str

class ManualAsistenciaRequest(BaseModel):
    alumno_id: str
    sesion_id: int

class GrupoCreate(BaseModel):
    carrera_id: int
    cuatrimestre: int
    periodo: str

class ZonaWifiCreate(BaseModel):
    nombre_zona: str
    bssid_mac: str
    tipo_zona: str = "salon"  # salon, libre


def formatear_hora_12h(hora):
    """
    Convierte hora de 24h a 12h con AM/PM para MOSTRAR
    Ejemplo: 13:30:00 → "1:30 PM"
    """
    if hora is None:
        return None
    try:
        if isinstance(hora, str):
            hora_obj = datetime.strptime(hora, "%H:%M:%S").time()
        else:
            hora_obj = hora
        return hora_obj.strftime("%I:%M %p").lstrip('0')
    except:
        return str(hora)

# ============================================
# ========== ENDPOINTS PARA MAESTRO ==========
# ============================================

# ---- 1. LOGIN MAESTRO ----
@app.post("/api/maestro/login")
def login_maestro(request: LoginMaestroRequest, db: Session = Depends(get_db)):
    query = text("""
        SELECT id, nombre, matricula, rol 
        FROM public.perfiles_usuarios 
        WHERE matricula = :matricula AND rol IN ('maestro', 'administrador')
    """)
    usuario = db.execute(query, {"matricula": request.matricula.strip()}).fetchone()
    
    if not usuario:
        raise HTTPException(status_code=401, detail="Matrícula no encontrada o no es maestro/administrador")
    
    return {
        "status": "success",
        "id": str(usuario[0]),
        "nombre": usuario[1],
        "matricula": usuario[2],
        "rol": usuario[3]
    }

# ---- 2. CLASES DE HOY ----
@app.get("/api/maestro/clases-hoy/{maestro_id}")
def get_clases_hoy(maestro_id: str, db: Session = Depends(get_db)):
    dias_semana = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
    dia_actual = dias_semana[datetime.now().weekday()]
    hoy = date.today()
    hora_actual = datetime.now().time()
    
    query = text("""
        SELECT DISTINCT ON (c.id)
            c.id,
            c.nombre_materia,
            c.horario_inicio,
            c.horario_fin,
            c.dia_semana,
            s.id as sesion_id,
            s.activa as sesion_activa,
            s.fecha as sesion_fecha
        FROM public.clases c
        LEFT JOIN public.sesiones_clase s ON c.id = s.clase_id 
            AND s.fecha = :hoy
        WHERE c.maestro_id = :maestro_id 
          AND c.dia_semana = :dia_actual
        ORDER BY c.id, c.horario_inicio, s.hora_apertura DESC NULLS LAST
    """)
    
    clases = db.execute(query, {
        "maestro_id": maestro_id,
        "dia_actual": dia_actual,
        "hoy": hoy
    }).fetchall()
    
    resultado = []
    
    for c in clases:
        hora_inicio = c[2]
        hora_fin = c[3]
        sesion_id = c[5]
        sesion_activa = c[6] if c[6] is not None else False
        
        # 🔥 REGLA 1: Si tiene sesión activa
        if sesion_activa:
            estado = "activa"
            puede_abrir = False
            puede_ver_asistencia = True
            mensaje = "🟢 Clase en curso"
        
        # 🔥 REGLA 2: Si ya tuvo sesión (se abrió y cerró)
        elif sesion_id is not None and not sesion_activa:
            estado = "finalizada"
            puede_abrir = False
            puede_ver_asistencia = True
            mensaje = "✅ Clase finalizada"
        
        # 🔥 REGLA 3: Si NUNCA se abrió y la hora ya pasó
        elif sesion_id is None and hora_actual > hora_fin:
            estado = "nunca_abierta"
            puede_abrir = False
            puede_ver_asistencia = False
            mensaje = "❌ Clase nunca abierta"
        
        # 🔥 REGLA 4: Si está DENTRO del horario (se puede abrir)
        elif sesion_id is None and hora_inicio <= hora_actual <= hora_fin:
            estado = "proxima"
            puede_abrir = True
            puede_ver_asistencia = False
            mensaje = "⭐ Clase en horario - Disponible"
        
        # 🔥 REGLA 5: Si es FUTURA (aún no llega su hora)
        elif sesion_id is None and hora_actual < hora_inicio:
            estado = "futura"
            puede_abrir = False
            puede_ver_asistencia = False
            mensaje = f"⏳ Inicia a las {hora_inicio.strftime('%I:%M %p')}"
        
        # 🔥 REGLA 6: Cualquier otro caso (fallback)
        else:
            estado = "bloqueada"
            puede_abrir = False
            puede_ver_asistencia = False
            mensaje = "🔒 No disponible"
        
        resultado.append({
            "id": c[0],
            "materia": c[1],
            "hora_inicio": formatear_hora_12h(c[2]),
            "hora_fin": formatear_hora_12h(c[3]),
            "dia": c[4],
            "estado": estado,
            "mensaje": mensaje,
            "sesion_id": sesion_id,
            "sesion_abierta": sesion_activa,
            "puede_abrir": puede_abrir,
            "puede_ver_asistencia": puede_ver_asistencia,
            "es_proxima": estado == "proxima"
        })
    
    return resultado

# ---- 3. INICIAR SESIÓN DE CLASE ----
@app.post("/api/sesiones/abrir")
def abrir_sesion(request: IniciarSesionRequest, db: Session = Depends(get_db)):
    hoy = date.today()
    ahora = datetime.now().time()
    
    # 1. Verificar que la clase existe
    clase = db.execute(
        text("SELECT id, nombre_materia, horario_inicio, horario_fin FROM clases WHERE id = :id"),
        {"id": request.clase_id}
    ).fetchone()
    
    if not clase:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    
    # 2. Verificar si ya tiene sesión activa
    sesion_activa = db.execute(text("""
        SELECT id FROM sesiones_clase 
        WHERE clase_id = :c AND fecha = :h AND activa = true
    """), {"c": request.clase_id, "h": hoy}).fetchone()
    
    if sesion_activa:
        raise HTTPException(status_code=400, detail="⚠️ Esta clase ya tiene una sesión activa")
    
    # 🔥 3. VALIDACIÓN PRINCIPAL: ¿Está en horario?
    hora_inicio = clase[2]
    hora_fin = clase[3]
    
    # Verificar si la hora actual está dentro del rango
    if not (hora_inicio <= ahora <= hora_fin):
        hora_inicio_str = hora_inicio.strftime('%I:%M %p')
        hora_fin_str = hora_fin.strftime('%I:%M %p')
        ahora_str = ahora.strftime('%I:%M %p')
        
        raise HTTPException(
            status_code=400,
            detail=f"❌ No puedes abrir esta clase ahora. Son las {ahora_str}, el horario es de {hora_inicio_str} a {hora_fin_str}"
        )
    
    # 4. Verificar si hay otra clase activa (para evitar conflictos)
    otra_activa = db.execute(text("""
        SELECT s.id, c.nombre_materia
        FROM sesiones_clase s
        JOIN clases c ON s.clase_id = c.id
        WHERE s.fecha = :hoy 
          AND s.activa = true
          AND s.clase_id != :clase_id
    """), {
        "hoy": hoy,
        "clase_id": request.clase_id
    }).fetchone()
    
    if otra_activa:
        raise HTTPException(
            status_code=400,
            detail=f"⚠️ Ya hay una clase activa: {otra_activa[1]}. Ciérrala primero."
        )
    
    # 5. Proceder con la apertura
    result = db.execute(text("""
        INSERT INTO sesiones_clase (clase_id, fecha, activa, hora_apertura) 
        VALUES (:c, :h, true, NOW()) RETURNING id
    """), {"c": request.clase_id, "h": hoy})
    db.commit()
    
    return {
        "status": "success", 
        "message": f"✅ Sesión abierta para {clase[1]}",
        "sesion_id": result.fetchone()[0]
    }

# ---- 4. CERRAR SESIÓN DE CLASE ----
@app.post("/api/sesiones/cerrar/{sesion_id}")
def cerrar_sesion(sesion_id: int, db: Session = Depends(get_db)):
    sesion = db.execute(
        text("SELECT id, activa FROM public.sesiones_clase WHERE id = :id"),
        {"id": sesion_id}
    ).fetchone()
    
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    
    if not sesion[1]:
        raise HTTPException(status_code=400, detail="Sesión ya cerrada")
    
    db.execute(
        text("UPDATE public.sesiones_clase SET activa = false WHERE id = :id"),
        {"id": sesion_id}
    )
    db.commit()
    
    return {"status": "success", "message": "Sesión cerrada correctamente"}

# ---- 5. LISTA DE ALUMNOS CON ASISTENCIA ----
@app.get("/api/clase/asistencia/{clase_id}")
def get_asistencia_clase(clase_id: int, db: Session = Depends(get_db)):
    hoy = date.today()
    
    query = text("""
        SELECT 
            p.id as alumno_id,
            p.nombre,
            p.matricula,
            CASE 
                WHEN a.id IS NOT NULL THEN 'presente'
                ELSE 'ausente'
            END as estado,
            a.fecha_hora as hora_registro,
            s.id as sesion_id,
            s.activa as sesion_activa
        FROM public.inscripciones i
        JOIN public.perfiles_usuarios p ON i.alumno_id = p.id
        LEFT JOIN public.sesiones_clase s ON i.clase_id = s.clase_id 
            AND s.fecha = :hoy AND s.activa = true
        LEFT JOIN public.asistencias a ON i.id = a.inscripcion_id 
            AND a.sesion_clase_id = s.id
        WHERE i.clase_id = :clase_id
        ORDER BY p.nombre
    """)
    
    alumnos = db.execute(query, {
        "clase_id": clase_id,
        "hoy": hoy
    }).fetchall()
    
    return [{
        "alumno_id": str(r[0]),
        "nombre": r[1],
        "matricula": r[2],
        "estado": r[3],
        "hora_registro": formatear_hora_12h(r[4]) if r[4] else None,
        "sesion_id": r[5] if r[5] else None,
        "sesion_activa": r[6] if r[6] else False
    } for r in alumnos]

# ---- 6. DETALLE DE ASISTENCIA PARA UNA SESIÓN ESPECÍFICA ----
@app.get("/api/sesion/detalle/{sesion_id}")
def get_detalle_sesion(sesion_id: int, db: Session = Depends(get_db)):
    query = text("""
        SELECT 
            p.id as alumno_id,
            p.nombre,
            p.matricula,
            CASE 
                WHEN a.id IS NOT NULL THEN 'presente'
                ELSE 'ausente'
            END as estado,
            a.fecha_hora as hora_registro
        FROM public.sesiones_clase s
        JOIN public.clases c ON s.clase_id = c.id
        JOIN public.inscripciones i ON c.id = i.clase_id
        JOIN public.perfiles_usuarios p ON i.alumno_id = p.id
        LEFT JOIN public.asistencias a ON i.id = a.inscripcion_id AND a.sesion_clase_id = s.id
        WHERE s.id = :sesion_id
        ORDER BY p.nombre
    """)
    
    alumnos = db.execute(query, {"sesion_id": sesion_id}).fetchall()
    
    return [{
        "alumno_id": str(r[0]),
        "nombre": r[1],
        "matricula": r[2],
        "estado": r[3],
        "hora_registro": formatear_hora_12h(r[4]) if r[4] else None
    } for r in alumnos]

# ---- 7. ESTADÍSTICAS DEL MAESTRO ----
@app.get("/api/maestro/estadisticas/{maestro_id}")
def get_estadisticas_maestro(maestro_id: str, db: Session = Depends(get_db)):
    query = text("""
        SELECT 
            COUNT(DISTINCT c.id) as total_clases,
            COUNT(DISTINCT i.alumno_id) as total_alumnos,
            COUNT(a.id) as total_asistencias,
            COUNT(DISTINCT s.id) as total_sesiones
        FROM public.clases c
        JOIN public.inscripciones i ON c.id = i.clase_id
        LEFT JOIN public.sesiones_clase s ON c.id = s.clase_id
        LEFT JOIN public.asistencias a ON i.id = a.inscripcion_id AND a.sesion_clase_id = s.id
        WHERE c.maestro_id = :maestro_id
    """)
    
    stats = db.execute(query, {"maestro_id": maestro_id}).fetchone()
    
    total_clases = stats[0] or 0
    total_alumnos = stats[1] or 0
    total_asistencias = stats[2] or 0
    total_sesiones = stats[3] or 0
    
    return {
        "total_clases": total_clases,
        "total_alumnos": total_alumnos,
        "total_asistencias": total_asistencias,
        "total_sesiones": total_sesiones,
        "promedio_asistencia_por_sesion": round(total_asistencias / total_sesiones if total_sesiones > 0 else 0, 2),
        "porcentaje_global": round((total_asistencias / (total_alumnos * total_sesiones) * 100) if total_alumnos > 0 and total_sesiones > 0 else 0, 2)
    }

# ============================================
# ========== HISTORIAL CON FILTROS ==========
# ============================================

@app.get("/api/maestro/historial/{maestro_id}")
def get_historial_clases(
    maestro_id: str,
    filtro: str = "dia",
    fecha: Optional[str] = None,
    busqueda: Optional[str] = None,
    db: Session = Depends(get_db)
):
    hoy = date.today()
    
    # Determinar rango de fechas
    if filtro == "dia":
        fecha_inicio = hoy
        fecha_fin = hoy
    elif filtro == "semana":
        inicio_semana = hoy - timedelta(days=hoy.weekday())
        fecha_inicio = inicio_semana
        fecha_fin = inicio_semana + timedelta(days=6)
    elif filtro == "mes":
        fecha_inicio = hoy.replace(day=1)
        if hoy.month == 12:
            fecha_fin = hoy.replace(year=hoy.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            fecha_fin = hoy.replace(month=hoy.month + 1, day=1) - timedelta(days=1)
    else:
        fecha_inicio = hoy
        fecha_fin = hoy
    
    if fecha:
        try:
            fecha_obj = datetime.strptime(fecha, "%Y-%m-%d").date()
            fecha_inicio = fecha_obj
            fecha_fin = fecha_obj
        except:
            pass
    
    # 🔥 QUERY CORREGIDA: Cada sesión aparece con su fecha REAL
    query = text("""
        SELECT 
            c.id,
            c.nombre_materia,
            c.horario_inicio,
            c.horario_fin,
            s.fecha,
            s.id as sesion_id,
            s.activa,
            s.hora_apertura,
            (SELECT COUNT(DISTINCT i.alumno_id) 
             FROM public.inscripciones i 
             WHERE i.clase_id = c.id) as total_inscritos,
            (SELECT COUNT(a.id) 
             FROM public.asistencias a 
             WHERE a.sesion_clase_id = s.id) as total_presentes
        FROM public.clases c
        JOIN public.sesiones_clase s ON c.id = s.clase_id
        WHERE c.maestro_id = :maestro_id
          AND s.fecha BETWEEN :fecha_ini AND :fecha_fin
          AND s.fecha <= :hoy
        ORDER BY s.fecha DESC, s.hora_apertura DESC
    """)
    
    resultados = db.execute(query, {
        "maestro_id": maestro_id,
        "fecha_ini": fecha_inicio,
        "fecha_fin": fecha_fin,
        "hoy": hoy
    }).fetchall()
    
    historial = []
    for r in resultados:
        if busqueda and busqueda.lower() not in r[1].lower():
            continue
        
        historial.append({
            "id": r[0],
    "materia": r[1],
    "hora_inicio": formatear_hora_12h(r[2]),
    "hora_fin": formatear_hora_12h(r[3]),
    "fecha": str(r[4]),
    "sesion_id": r[5],
    "activa": r[6],
    "hora_apertura": formatear_hora_12h(r[7]) if r[7] else None,
    "total_inscritos": r[8] or 0,
    "total_presentes": r[9] or 0,
    "porcentaje": round((r[9] / r[8] * 100) if r[8] and r[8] > 0 else 0, 2)
        })
    
    return historial

# ============================================
# ========== ENDPOINTS PARA ALUMNO ==========
# ============================================

@app.post("/api/alumno/login")
def login_alumno(request: LoginAlumnoRequest, db: Session = Depends(get_db)):
    matricula_limpia = request.matricula.strip()
    query_alumno = text("""
        SELECT id, nombre, device_id 
        FROM public.perfiles_usuarios 
        WHERE TRIM(matricula) = :matricula AND rol = 'alumno'
    """)
    alumno = db.execute(query_alumno, {"matricula": matricula_limpia}).fetchone()
    
    if not alumno:
        raise HTTPException(status_code=404, detail="Matrícula no registrada.")
    
    alumno_id, nombre, device_guardado = alumno
    
    if device_guardado is None:
        db.execute(
            text("UPDATE public.perfiles_usuarios SET device_id = :d WHERE id = :id"),
            {"d": request.device_id, "id": alumno_id}
        )
        db.commit()
    elif device_guardado != request.device_id:
        raise HTTPException(status_code=403, detail="Dispositivo no autorizado.")
    
    return {"status": "success", "alumno_id": str(alumno_id), "nombre": nombre}

@app.post("/api/asistencia/registrar")
def registrar_asistencia(request: RegistrarAsistenciaRequest, db: Session = Depends(get_db)):
    try:
        hoy = date.today()
        
        sesion = db.execute(text("""
            SELECT id FROM public.sesiones_clase 
            WHERE clase_id = :c AND fecha = :h AND activa = true 
            ORDER BY id DESC LIMIT 1
        """), {"c": request.clase_id, "h": hoy}).fetchone()
        
        if not sesion:
            raise HTTPException(status_code=400, detail="Sesión no iniciada por el maestro.")
        sesion_id = sesion[0]

        query_val = text("""
            SELECT i.id, z.bssid_mac 
            FROM public.inscripciones i
            JOIN public.clases c ON i.clase_id = c.id
            JOIN public.zonas_wifi z ON c.zona_id = z.id
            WHERE i.alumno_id = :a AND c.id = :c
        """)
        datos = db.execute(query_val, {"a": request.alumno_id, "c": request.clase_id}).fetchone()
        
        if not datos:
            raise HTTPException(status_code=404, detail="Alumno no inscrito.")
        
        ins_id, bssid_bd = datos

        if bssid_bd and bssid_bd.strip() != "00:00:00:00:00:00":
            if request.bssid_alumno.upper() != bssid_bd.upper():
                raise HTTPException(status_code=403, detail="No estás conectado a la red Wi-Fi autorizada.")
        else:
            raise HTTPException(status_code=500, detail="La zona Wi-Fi del salón no está configurada correctamente.")

        duplicado = db.execute(text("""
            SELECT id FROM public.asistencias 
            WHERE inscripcion_id = :i AND sesion_clase_id = :s
        """), {"i": ins_id, "s": sesion_id}).fetchone()
        
        if duplicado:
            raise HTTPException(status_code=400, detail="Asistencia ya registrada.")

        db.execute(text("""
            INSERT INTO public.asistencias (inscripcion_id, sesion_clase_id, estatus) 
            VALUES (:i, :s, 'asistencia')
        """), {"i": ins_id, "s": sesion_id})
        db.commit()
        
        return {"status": "success", "message": "Asistencia registrada correctamente."}

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
    
@app.get("/api/sesion/info/{sesion_id}")
def get_sesion_info(sesion_id: int, db: Session = Depends(get_db)):
    query = text("""
        SELECT 
            s.id,
            s.fecha,
            s.hora_apertura,
            s.activa,
            c.nombre_materia
        FROM public.sesiones_clase s
        JOIN public.clases c ON s.clase_id = c.id
        WHERE s.id = :sesion_id
    """)
    resultado = db.execute(query, {"sesion_id": sesion_id}).fetchone()
    
    if not resultado:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    
    return {
        "id": resultado[0],
        "fecha": str(resultado[1]),
        "hora_apertura": formatear_hora_12h(resultado[2]) if resultado[2] else None,
        "activa": resultado[3],
        "materia": resultado[4]
    }
@app.get("/api/alumno/horario/{alumno_id}")
def get_horario_alumno(alumno_id: str, db: Session = Depends(get_db)):
    hoy = date.today()
    ahora = datetime.now().time()
    dias_semana = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
    dia_actual = dias_semana[datetime.now().weekday()]
    
    query = text("""
        SELECT 
            c.id,
            c.nombre_materia,
            c.horario_inicio,
            c.horario_fin,
            c.dia_semana,
            c.zona_id,
            s.id as sesion_id,
            s.activa as sesion_activa
        FROM public.clases c
        JOIN public.inscripciones i ON c.id = i.clase_id
        LEFT JOIN public.sesiones_clase s ON c.id = s.clase_id 
            AND s.fecha = :hoy 
            AND s.activa = true
        WHERE i.alumno_id = :alumno_id
          AND c.dia_semana = :dia_actual
        ORDER BY c.horario_inicio
    """)
    
    clases = db.execute(query, {
        "alumno_id": alumno_id,
        "dia_actual": dia_actual,
        "hoy": hoy
    }).fetchall()
    
    resultado = []
    for c in clases:
        hora_inicio = c[2]
        hora_fin = c[3]
        sesion_abierta = c[6] is not None
        
        # 🔥 DETERMINAR SI LA CLASE ESTÁ ACTIVA AHORA
        es_actual = False
        if sesion_abierta:
            es_actual = True
        elif hora_inicio <= ahora <= hora_fin:
            es_actual = True
        
        # Determinar estado para el alumno
        if es_actual:
            estado = "activa"
            puede_marcar = True
        elif ahora < hora_inicio:
            estado = "proxima"
            puede_marcar = False
        else:
            estado = "finalizada"
            puede_marcar = False
        
        # Solo mostrar clases activas o próximas
        if estado != "finalizada":
            resultado.append({
                "id": c[0],
                "materia": c[1],
                "hora_inicio": formatear_hora_12h(c[2]),
                "hora_fin": formatear_hora_12h(c[3]),
                "dia": c[4],
                "salon": "B-7",  # O el salón real si lo tienes
                "sesion_id": c[6] if c[6] else None,
                "sesion_abierta": sesion_abierta,
                "puede_marcar": puede_marcar,
                "es_actual": es_actual  # 🔥 ESTE ES EL CAMPO QUE FALTABA
            })
    
    return resultado

@app.post("/api/sesiones/cerrar-automaticas")
def cerrar_sesiones_automaticas(db: Session = Depends(get_db)):
    hoy = date.today()
    ahora = datetime.now().time()
    
    # Buscar sesiones activas cuya clase ya terminó
    query = text("""
        SELECT 
            s.id as sesion_id,
            s.clase_id,
            c.nombre_materia,
            c.horario_fin
        FROM public.sesiones_clase s
        JOIN public.clases c ON s.clase_id = c.id
        WHERE s.fecha = :hoy
          AND s.activa = true
          AND c.horario_fin < :hora_actual
    """)
    
    sesiones = db.execute(query, {
        "hoy": hoy,
        "hora_actual": ahora
    }).fetchall()
    
    cerradas = []
    for s in sesiones:
        # Cerrar la sesión
        db.execute(
            text("UPDATE public.sesiones_clase SET activa = false WHERE id = :id"),
            {"id": s[0]}
        )
        db.commit()
        cerradas.append({
            "sesion_id": s[0],
            "clase_id": s[1],
            "materia": s[2]
        })
    
    return {
        "status": "success",
        "sesiones_cerradas": cerradas,
        "total": len(cerradas)
    }

@app.post("/api/asistencia/manual")
def registrar_asistencia_manual(
    request: ManualAsistenciaRequest, 
    db: Session = Depends(get_db)
):
    try:
        hoy = date.today()
        
        sesion = db.execute(text("""
            SELECT id, activa, clase_id, fecha 
            FROM public.sesiones_clase 
            WHERE id = :sesion_id
        """), {"sesion_id": request.sesion_id}).fetchone()
        
        if not sesion:
            raise HTTPException(status_code=404, detail="Sesión no encontrada")
        
        # 🔥 PERMITIR modificar SOLO si es hoy O si es admin
        # Por ahora, solo permitimos hoy
        if sesion[3] != hoy:
            raise HTTPException(
                status_code=400, 
                detail=f"No puedes modificar asistencias de días pasados. Esta sesión es del {sesion[3].strftime('%d/%m/%Y')}"
            )
        
        # 2. Verificar que el alumno está inscrito
        inscripcion = db.execute(text("""
            SELECT id 
            FROM public.inscripciones 
            WHERE clase_id = :clase_id AND alumno_id = :alumno_id
        """), {
            "clase_id": sesion[2],
            "alumno_id": request.alumno_id
        }).fetchone()
        
        if not inscripcion:
            raise HTTPException(status_code=404, detail="Alumno no inscrito en esta clase")
        
        # 3. Verificar duplicado
        duplicado = db.execute(text("""
            SELECT id FROM public.asistencias 
            WHERE inscripcion_id = :inscripcion_id AND sesion_clase_id = :sesion_id
        """), {
            "inscripcion_id": inscripcion[0],
            "sesion_id": request.sesion_id
        }).fetchone()
        
        if duplicado:
            raise HTTPException(status_code=400, detail="Este alumno ya tiene asistencia registrada")
        
        # 4. Registrar asistencia manual
        db.execute(text("""
            INSERT INTO public.asistencias 
            (inscripcion_id, sesion_clase_id, estatus, device_verificado) 
            VALUES (:inscripcion_id, :sesion_id, 'asistencia', 'manual_maestro')
        """), {
            "inscripcion_id": inscripcion[0],
            "sesion_id": request.sesion_id
        })
        db.commit()
        
        return {
            "status": "success", 
            "message": "✅ Asistencia registrada manualmente",
            "alumno_id": request.alumno_id
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
    
@app.get("/api/admin/estadisticas")
def get_admin_estadisticas(db: Session = Depends(get_db)):
    # Totales
    total_usuarios = db.execute(text("SELECT COUNT(*) FROM perfiles_usuarios")).fetchone()[0]
    total_maestros = db.execute(text("SELECT COUNT(*) FROM perfiles_usuarios WHERE rol = 'maestro'")).fetchone()[0]
    total_alumnos = db.execute(text("SELECT COUNT(*) FROM perfiles_usuarios WHERE rol = 'alumno'")).fetchone()[0]
    total_clases = db.execute(text("SELECT COUNT(*) FROM clases")).fetchone()[0]
    total_inscripciones = db.execute(text("SELECT COUNT(*) FROM inscripciones")).fetchone()[0]
    sesiones_activas = db.execute(text("SELECT COUNT(*) FROM sesiones_clase WHERE activa = true")).fetchone()[0]
    
    # Asistencias de hoy
    hoy = date.today()
    asistencias_hoy = db.execute(
        text("SELECT COUNT(*) FROM asistencias WHERE DATE(fecha_hora) = :hoy"),
        {"hoy": hoy}
    ).fetchone()[0]
    
    # Asistencias por clase hoy
    asistencias_por_clase = db.execute(text("""
        SELECT 
            c.nombre_materia,
            COUNT(DISTINCT i.alumno_id) as total,
            COUNT(a.id) as presentes
        FROM clases c
        JOIN inscripciones i ON c.id = i.clase_id
        LEFT JOIN sesiones_clase s ON c.id = s.clase_id AND s.fecha = :hoy
        LEFT JOIN asistencias a ON i.id = a.inscripcion_id AND a.sesion_clase_id = s.id
        WHERE s.fecha = :hoy
        GROUP BY c.id, c.nombre_materia
        ORDER BY c.nombre_materia
    """), {"hoy": hoy}).fetchall()
    
    asistencias_por_clase_list = []
    for row in asistencias_por_clase:
        asistencias_por_clase_list.append({
            "nombre_materia": row[0],
            "total": row[1],
            "presentes": row[2] or 0
        })
    
    # Últimas actividades (asistencias recientes)
    ultimas = db.execute(text("""
        SELECT 
            p.nombre as alumno,
            c.nombre_materia,
            a.fecha_hora
        FROM asistencias a
        JOIN inscripciones i ON a.inscripcion_id = i.id
        JOIN perfiles_usuarios p ON i.alumno_id = p.id
        JOIN clases c ON i.clase_id = c.id
        ORDER BY a.fecha_hora DESC
        LIMIT 5
    """)).fetchall()
    
    ultimas_actividades = []
    for row in ultimas:
        ultimas_actividades.append({
            "descripcion": f"{row[0]} marcó asistencia en {row[1]}",
            "hora": formatear_hora_12h(row[2]) if row[2] else None
        })
    
    return {
        "total_usuarios": total_usuarios,
        "total_maestros": total_maestros,
        "total_alumnos": total_alumnos,
        "total_clases": total_clases,
        "total_inscripciones": total_inscripciones,
        "sesiones_activas": sesiones_activas,
        "asistencias_hoy": asistencias_hoy,
        "asistencias_por_clase": asistencias_por_clase_list,
        "ultimas_actividades": ultimas_actividades
    }

# ============================================
# ========== ENDPOINTS ADMIN ==========
# ============================================

# ---- 1. OBTENER TODOS LOS USUARIOS ----
@app.get("/api/admin/usuarios")
def get_admin_usuarios(
    rol: Optional[str] = None,
    busqueda: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = text("""
        SELECT 
            id,
            matricula,
            nombre,
            rol,
            device_id,
            creado_en,
            grupo_id
        FROM perfiles_usuarios
        WHERE 1=1
    """)
    
    params = {}
    
    if rol and rol != 'todos':
        query = text(str(query) + " AND rol = :rol")
        params["rol"] = rol
    
    if busqueda:
        query = text(str(query) + " AND (matricula ILIKE :busqueda OR nombre ILIKE :busqueda)")
        params["busqueda"] = f"%{busqueda}%"
    
    query = text(str(query) + " ORDER BY rol, nombre")
    
    resultados = db.execute(query, params).fetchall()
    
    return [{
        "id": str(r[0]),
        "matricula": r[1],
        "nombre": r[2],
        "rol": r[3],
        "device_id": r[4],
        "creado_en": str(r[5]) if r[5] else None,
         "grupo_id": r[6]

    } for r in resultados]

# ---- 2. CREAR USUARIO ----
class UsuarioCreate(BaseModel):
    matricula: str
    nombre: str
    rol: str
    email: str
    password: str
    grupo_id: Optional[int] = None  # ← NUEVO

@app.post("/api/admin/usuario")
def crear_admin_usuario(usuario: UsuarioCreate, db: Session = Depends(get_db)):
    # Validar rol
    if usuario.rol not in ['alumno', 'maestro', 'administrador']:
        raise HTTPException(status_code=400, detail="Rol inválido")
    
    # Verificar si ya existe la matrícula en perfiles_usuarios
    existe = db.execute(
        text("SELECT id FROM perfiles_usuarios WHERE matricula = :m"),
        {"m": usuario.matricula}
    ).fetchone()
    if existe:
        raise HTTPException(status_code=400, detail="La matrícula ya está registrada")
    
    # Asegurar que la matrícula exista en alumnos_autorizados (para todos los roles)
    existe_autorizado = db.execute(
        text("SELECT matricula FROM alumnos_autorizados WHERE matricula = :m"),
        {"m": usuario.matricula}
    ).fetchone()
    if not existe_autorizado:
        db.execute(
            text("INSERT INTO alumnos_autorizados (matricula, nombre_completo) VALUES (:m, :n)"),
            {"m": usuario.matricula, "n": usuario.nombre}
        )
        db.commit()
    
    # Crear usuario en auth.users con todos los metadatos (incluyendo grupo_id)
    try:
        response = supabase.auth.admin.create_user({
            "email": usuario.email,
            "password": usuario.password,
            "email_confirm": True,
            "user_metadata": {
                "matricula": usuario.matricula,
                "nombre": usuario.nombre,
                "rol": usuario.rol,
                "grupo_id": usuario.grupo_id if usuario.rol == 'alumno' else None
            }
        })
        user_id = response.user.id
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error al crear usuario en auth: {str(e)}")
    
    # ✅ EL TRIGGER SE ENCARGA DE INSERTAR EN perfiles_usuarios
    # No hacemos INSERT manual aquí

    return {
        "status": "success",
        "message": f"Usuario {usuario.nombre} creado correctamente",
        "id": user_id
    }

    

# ---- 3. ACTUALIZAR USUARIO ----
class UsuarioUpdate(BaseModel):
    nombre: Optional[str] = None
    matricula: Optional[str] = None
    rol: Optional[str] = None
    grupo_id: Optional[int] = None  # ← Asegurar que existe
    email: Optional[str] = None     # Opcional
    password: Optional[str] = None  # Opcional

@app.put("/api/admin/usuario/{usuario_id}")
def actualizar_admin_usuario(
    usuario_id: str,
    usuario: UsuarioUpdate,
    db: Session = Depends(get_db)
):
    # Verificar que el usuario existe
    existe = db.execute(
        text("SELECT id, rol FROM perfiles_usuarios WHERE id = :id"),
        {"id": usuario_id}
    ).fetchone()
    
    if not existe:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    updates = []
    params = {"id": usuario_id}
    
    if usuario.nombre is not None:
        updates.append("nombre = :nombre")
        params["nombre"] = usuario.nombre
    
    if usuario.matricula is not None:
        # 🔥 Verificar que la matrícula exista en alumnos_autorizados
        existe_autorizado = db.execute(
            text("SELECT matricula FROM alumnos_autorizados WHERE matricula = :m"),
            {"m": usuario.matricula}
        ).fetchone()
        if not existe_autorizado:
            # Si no existe, la creamos automáticamente
            db.execute(
                text("INSERT INTO alumnos_autorizados (matricula, nombre_completo) VALUES (:m, :n)"),
                {"m": usuario.matricula, "n": usuario.nombre or "Sin nombre"}
            )
            db.commit()
        updates.append("matricula = :matricula")
        params["matricula"] = usuario.matricula
    
    if usuario.rol is not None:
        if usuario.rol not in ['alumno', 'maestro', 'administrador']:
            raise HTTPException(status_code=400, detail="Rol inválido")
        updates.append("rol = :rol")
        params["rol"] = usuario.rol
    
    # 🔥 NUEVO: Manejar actualización de grupo_id
    if usuario.grupo_id is not None:
        # Si se envía grupo_id (puede ser un número o null), se actualiza
        updates.append("grupo_id = :grupo_id")
        params["grupo_id"] = usuario.grupo_id if usuario.grupo_id != '' else None
    
    if not updates:
        return {"status": "success", "message": "No se realizaron cambios"}
    
    query = text(f"UPDATE perfiles_usuarios SET {', '.join(updates)} WHERE id = :id")
    db.execute(query, params)
    db.commit()
    
    return {"status": "success", "message": "Usuario actualizado correctamente"}
# ---- 4. ELIMINAR USUARIO ----
@app.delete("/api/admin/usuario/{usuario_id}")
def eliminar_admin_usuario(usuario_id: str, db: Session = Depends(get_db)):
    # Verificar que el usuario existe
    existe = db.execute(
        text("SELECT id, rol FROM perfiles_usuarios WHERE id = :id"),
        {"id": usuario_id}
    ).fetchone()
    
    if not existe:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    # Verificar si es administrador (no se puede eliminar a sí mismo)
    # Podríamos añadir validación para no eliminar al admin actual
    
    # Verificar dependencias (si es maestro con clases asignadas)
    if existe[1] == 'maestro':
        clases = db.execute(
            text("SELECT id FROM clases WHERE maestro_id = :id"),
            {"id": usuario_id}
        ).fetchall()
        if clases:
            raise HTTPException(
                status_code=400, 
                detail="No se puede eliminar el maestro porque tiene clases asignadas"
            )
    
    # Si es alumno, verificar inscripciones
    if existe[1] == 'alumno':
        inscripciones = db.execute(
            text("SELECT id FROM inscripciones WHERE alumno_id = :id"),
            {"id": usuario_id}
        ).fetchall()
        if inscripciones:
            raise HTTPException(
                status_code=400, 
                detail="No se puede eliminar el alumno porque tiene inscripciones activas"
            )
    
    # Eliminar usuario
    db.execute(
        text("DELETE FROM perfiles_usuarios WHERE id = :id"),
        {"id": usuario_id}
    )
    db.commit()
    
    return {"status": "success", "message": "Usuario eliminado correctamente"}

# ---- 5. RESETEAR DEVICE ID ----
@app.post("/api/admin/usuario/{usuario_id}/reset-device")
def resetear_device_admin_usuario(usuario_id: str, db: Session = Depends(get_db)):
    existe = db.execute(
        text("SELECT id FROM perfiles_usuarios WHERE id = :id"),
        {"id": usuario_id}
    ).fetchone()
    
    if not existe:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    
    db.execute(
        text("UPDATE perfiles_usuarios SET device_id = NULL WHERE id = :id"),
        {"id": usuario_id}
    )
    db.commit()
    
    return {"status": "success", "message": "Device ID reseteado correctamente"}


# ============================================
# ========== ENDPOINTS ADMIN CLASES ==========
# ============================================

# ---- 1. OBTENER TODAS LAS CLASES ----
@app.get("/api/admin/clases")
def get_admin_clases(db: Session = Depends(get_db)):
    query = text("""
        SELECT 
            c.id,
            c.nombre_materia,
            c.dia_semana,
            c.horario_inicio,
            c.horario_fin,
            p.nombre as maestro_nombre,
            z.nombre_zona as zona_nombre,
            COUNT(i.id) as total_alumnos,
            c.grupo_id,
            c.maestro_id,
            c.zona_id 
        FROM clases c
        LEFT JOIN perfiles_usuarios p ON c.maestro_id = p.id
        LEFT JOIN zonas_wifi z ON c.zona_id = z.id
        LEFT JOIN inscripciones i ON c.id = i.clase_id
        GROUP BY c.id, p.nombre, z.nombre_zona, c.grupo_id
        ORDER BY c.dia_semana, c.horario_inicio
    """)
    resultados = db.execute(query).fetchall()

    return [{
        "id": r[0],
        "materia": r[1],
        "dia": r[2],
        "hora_inicio": formatear_hora_12h(r[3]),
        "hora_fin": formatear_hora_12h(r[4]),
        "maestro": r[5] or "Sin asignar",
        "zona": r[6] or "Sin zona",
        "total_alumnos": r[7] or 0,
        "grupo_id": r[8],
        "maestro_id": str(r[9]) if r[9] else None,  
        "zona_id": r[10] if r[10] else None
    } for r in resultados]
# ---- 2. CREAR/EDITAR CLASE ----
class ClaseCreate(BaseModel):
    nombre_materia: str
    maestro_id: str
    zona_id: int
    dia_semana: str
    horario_inicio: str
    horario_fin: str
    grupo_id: Optional[int] = None

@app.post("/api/admin/clase")
def crear_admin_clase(clase: ClaseCreate, db: Session = Depends(get_db)):
    # Validar día
    dias_validos = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
    if clase.dia_semana.lower() not in dias_validos:
        raise HTTPException(status_code=400, detail="Día inválido")
    
    # Validar que maestro existe
    maestro = db.execute(
        text("SELECT id FROM perfiles_usuarios WHERE id = :id AND rol = 'maestro'"),
        {"id": clase.maestro_id}
    ).fetchone()
    if not maestro:
        raise HTTPException(status_code=404, detail="Maestro no encontrado")
    
    # Validar que zona existe
    zona = db.execute(
        text("SELECT id FROM zonas_wifi WHERE id = :id"),
        {"id": clase.zona_id}
    ).fetchone()
    if not zona:
        raise HTTPException(status_code=404, detail="Zona WiFi no encontrada")
    
    # Insertar clase
    result = db.execute(text("""
        INSERT INTO clases (nombre_materia, maestro_id, zona_id, dia_semana, horario_inicio, horario_fin)
        VALUES (:materia, :maestro, :zona, :dia, :hora_ini, :hora_fin)
        RETURNING id
    """), {
        "materia": clase.nombre_materia,
        "maestro": clase.maestro_id,
        "zona": clase.zona_id,
        "dia": clase.dia_semana.lower(),
        "hora_ini": clase.horario_inicio,
        "hora_fin": clase.horario_fin,
        "grupo_id": clase.grupo_id
    })
    db.commit()
    clase_id = result.fetchone()[0]
    
    return {
        "status": "success",
        "message": f"Clase {clase.nombre_materia} creada correctamente",
        "id": clase_id
    }

@app.put("/api/admin/clase/{clase_id}")
def editar_admin_clase(clase_id: int, clase: ClaseCreate, db: Session = Depends(get_db)):
    # Verificar que la clase existe
    existe = db.execute(
        text("SELECT id FROM clases WHERE id = :id"),
        {"id": clase_id}
    ).fetchone()
    if not existe:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    
    # Actualizar
    db.execute(text("""
        UPDATE clases SET 
            nombre_materia = :materia,
            maestro_id = :maestro,
            zona_id = :zona,
            dia_semana = :dia,
            horario_inicio = :hora_ini,
            horario_fin = :hora_fin,
            grupo_id = :grupo_id
        
        WHERE id = :id
    """), {
        "id": clase_id,
        "materia": clase.nombre_materia,
        "maestro": clase.maestro_id,
        "zona": clase.zona_id,
        "dia": clase.dia_semana.lower(),
        "hora_ini": clase.horario_inicio,
        "hora_fin": clase.horario_fin,
         "grupo_id": clase.grupo_id
        
    })
    db.commit()
    
    return {"status": "success", "message": "Clase actualizada correctamente"}

# ---- 3. ELIMINAR CLASE ----
@app.delete("/api/admin/clase/{clase_id}")
def eliminar_admin_clase(clase_id: int, db: Session = Depends(get_db)):
    # Verificar si tiene inscripciones
    inscripciones = db.execute(
        text("SELECT COUNT(*) FROM inscripciones WHERE clase_id = :id"),
        {"id": clase_id}
    ).fetchone()[0]
    if inscripciones > 0:
        raise HTTPException(
            status_code=400,
            detail=f"No se puede eliminar la clase porque tiene {inscripciones} alumnos inscritos"
        )
    
    db.execute(
        text("DELETE FROM clases WHERE id = :id"),
        {"id": clase_id}
    )
    db.commit()
    
    return {"status": "success", "message": "Clase eliminada correctamente"}

# ---- 4. INSCRIBIR ALUMNO (INDIVIDUAL) ----
@app.post("/api/admin/clase/{clase_id}/inscribir")
def inscribir_alumno_clase(
    clase_id: int,
    alumno_id: str,
    db: Session = Depends(get_db)
):
    # Verificar clase
    clase = db.execute(
        text("SELECT id FROM clases WHERE id = :id"),
        {"id": clase_id}
    ).fetchone()
    if not clase:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    
    # Verificar alumno
    alumno = db.execute(
        text("SELECT id FROM perfiles_usuarios WHERE id = :id AND rol = 'alumno'"),
        {"id": alumno_id}
    ).fetchone()
    if not alumno:
        raise HTTPException(status_code=404, detail="Alumno no encontrado")
    
    # Verificar duplicado
    duplicado = db.execute(
        text("SELECT id FROM inscripciones WHERE clase_id = :clase AND alumno_id = :alumno"),
        {"clase": clase_id, "alumno": alumno_id}
    ).fetchone()
    if duplicado:
        raise HTTPException(status_code=400, detail="El alumno ya está inscrito en esta clase")
    
    # Inscribir
    db.execute(
        text("INSERT INTO inscripciones (clase_id, alumno_id) VALUES (:clase, :alumno)"),
        {"clase": clase_id, "alumno": alumno_id}
    )
    db.commit()
    
    return {"status": "success", "message": "Alumno inscrito correctamente"}

# ---- 5. INSCRIPCIÓN MASIVA ----
class InscripcionMasivaRequest(BaseModel):
    matricula: str  # Puede ser una lista separada por comas o saltos de línea

@app.post("/api/admin/clase/{clase_id}/inscribir-masiva")
def inscribir_alumnos_masiva(
    clase_id: int,
    request: InscripcionMasivaRequest,
    db: Session = Depends(get_db)
):
    # Verificar clase
    clase = db.execute(
        text("SELECT id FROM clases WHERE id = :id"),
        {"id": clase_id}
    ).fetchone()
    if not clase:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    
    # Dividir matrículas (por coma o salto de línea)
    matriculas = [m.strip() for m in request.matricula.replace('\n', ',').split(',') if m.strip()]
    
    if not matriculas:
        raise HTTPException(status_code=400, detail="No se ingresaron matrículas")
    
    inscritos = []
    errores = []
    
    for matricula in matriculas:
        # Buscar alumno por matrícula
        alumno = db.execute(
            text("SELECT id FROM perfiles_usuarios WHERE matricula = :m AND rol = 'alumno'"),
            {"m": matricula}
        ).fetchone()
        
        if not alumno:
            errores.append(f"Matrícula {matricula} no encontrada")
            continue
        
        # Verificar duplicado
        duplicado = db.execute(
            text("SELECT id FROM inscripciones WHERE clase_id = :clase AND alumno_id = :alumno"),
            {"clase": clase_id, "alumno": alumno[0]}
        ).fetchone()
        if duplicado:
            errores.append(f"Matrícula {matricula} ya inscrita")
            continue
        
        # Inscribir
        db.execute(
            text("INSERT INTO inscripciones (clase_id, alumno_id) VALUES (:clase, :alumno)"),
            {"clase": clase_id, "alumno": alumno[0]}
        )
        inscritos.append(matricula)
    
    db.commit()
    
    return {
        "status": "success",
        "message": f"{len(inscritos)} alumnos inscritos, {len(errores)} errores",
        "inscritos": inscritos,
        "errores": errores
    }

# ---- 6. LISTAR ALUMNOS DE UNA CLASE ----
@app.get("/api/admin/clase/{clase_id}/alumnos")
def get_alumnos_clase(clase_id: int, db: Session = Depends(get_db)):
    query = text("""
        SELECT 
            p.id,
            p.matricula,
            p.nombre
        FROM inscripciones i
        JOIN perfiles_usuarios p ON i.alumno_id = p.id
        WHERE i.clase_id = :clase_id
        ORDER BY p.nombre
    """)
    alumnos = db.execute(query, {"clase_id": clase_id}).fetchall()
    
    return [{
        "id": str(r[0]),
        "matricula": r[1],
        "nombre": r[2]
    } for r in alumnos]

# ---- 7. OBTENER MAESTROS Y ZONAS PARA FORMULARIOS ----
@app.get("/api/admin/maestros")
def get_maestros(db: Session = Depends(get_db)):
    query = text("SELECT id, nombre, matricula FROM perfiles_usuarios WHERE rol = 'maestro' ORDER BY nombre")
    return [{"id": str(r[0]), "nombre": r[1], "matricula": r[2]} for r in db.execute(query).fetchall()]

@app.get("/api/admin/zonas")
def get_zonas(db: Session = Depends(get_db)):
    query = text("SELECT id, nombre_zona FROM zonas_wifi ORDER BY nombre_zona")
    return [{"id": r[0], "nombre": r[1]} for r in db.execute(query).fetchall()]

# ---- CARRERAS Y GRUPOS ----
@app.get("/api/admin/carreras")
def get_carreras(db: Session = Depends(get_db)):
    query = text("SELECT id, nombre, creado_en FROM carreras ORDER BY nombre")
    resultados = db.execute(query).fetchall()
    return [{"id": r[0], "nombre": r[1], "creado_en": str(r[2])} for r in resultados]

@app.post("/api/admin/carrera")
def crear_carrera(nombre: str, db: Session = Depends(get_db)):
    existe = db.execute(text("SELECT id FROM carreras WHERE nombre = :n"), {"n": nombre}).fetchone()
    if existe:
        raise HTTPException(status_code=400, detail="Ya existe una carrera con ese nombre")
    
    result = db.execute(text("INSERT INTO carreras (nombre) VALUES (:n) RETURNING id"), {"n": nombre})
    db.commit()
    return {"status": "success", "id": result.fetchone()[0], "message": "Carrera creada"}

@app.get("/api/admin/grupos/{carrera_id}")
def get_grupos(carrera_id: int, db: Session = Depends(get_db)):
    # 1. Obtener el nombre de la carrera
    carrera = db.execute(
        text("SELECT nombre FROM carreras WHERE id = :id"),
        {"id": carrera_id}
    ).fetchone()
    if not carrera:
        raise HTTPException(status_code=404, detail="Carrera no encontrada")
    
    nombre_carrera = carrera[0]
    abreviatura = nombre_carrera[:3].upper()  # Ej: "Ingeniería en Sistemas" → "ING"

    # 2. Obtener los grupos
    query = text("""
        SELECT id, cuatrimestre, periodo
        FROM grupos_academicos 
        WHERE carrera_id = :carrera_id 
        ORDER BY cuatrimestre
    """)
    resultados = db.execute(query, {"carrera_id": carrera_id}).fetchall()

    # 3. Generar nombre_grupo en el backend
    return [{
        "id": r[0],
        "cuatrimestre": r[1],
        "periodo": r[2],
        "nombre_grupo": f"{abreviatura}-{r[1]}"  # Ej: "ING-5"
    } for r in resultados]
@app.post("/api/admin/grupo")
def crear_grupo(grupo: GrupoCreate, db: Session = Depends(get_db)):
    try:
        # Validar que la carrera exista
        carrera = db.execute(text("SELECT id, nombre FROM carreras WHERE id = :id"), {"id": grupo.carrera_id}).fetchone()
        if not carrera:
            raise HTTPException(status_code=404, detail="Carrera no encontrada")
        
        # Validar duplicado
        existe = db.execute(
            text("SELECT id FROM grupos_academicos WHERE carrera_id = :c AND cuatrimestre = :cuat AND periodo = :p"),
            {"c": grupo.carrera_id, "cuat": grupo.cuatrimestre, "p": grupo.periodo}
        ).fetchone()
        if existe:
            raise HTTPException(status_code=400, detail=f"El grupo para el cuatrimestre {grupo.cuatrimestre} y periodo {grupo.periodo} ya existe")
        
        # Insertar grupo
        result = db.execute(
            text("INSERT INTO grupos_academicos (carrera_id, cuatrimestre, periodo) VALUES (:c, :cuat, :p) RETURNING id"),
            {"c": grupo.carrera_id, "cuat": grupo.cuatrimestre, "p": grupo.periodo}
        )
        db.commit()
        grupo_id = result.fetchone()[0]
        
        return {"status": "success", "id": grupo_id, "message": f"Grupo creado correctamente para {carrera[1]} - Cuatrimestre {grupo.cuatrimestre}"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno: {str(e)}")
    
@app.get("/api/admin/grupos-todos")
def get_grupos_todos(db: Session = Depends(get_db)):
    query = text("""
        SELECT g.id, g.cuatrimestre, g.periodo, c.nombre as carrera_nombre
        FROM grupos_academicos g
        JOIN carreras c ON g.carrera_id = c.id
        ORDER BY c.nombre, g.cuatrimestre
    """)
    resultados = db.execute(query).fetchall()
    return [{
        "id": r[0],
        "nombre_grupo": f"{r[3]} - Cuatrimestre {r[1]}",
        "periodo": r[2],
        "cuatrimestre": r[1]
    } for r in resultados]

# ============================================
# ========== INSCRIPCIÓN POR GRUPO ==========
# ============================================

@app.get("/api/admin/clase/{clase_id}/alumnos-grupo")
def get_alumnos_grupo_clase(clase_id: int, db: Session = Depends(get_db)):
    # 1. Obtener el grupo de la clase
    clase = db.execute(
        text("SELECT grupo_id FROM clases WHERE id = :id"),
        {"id": clase_id}
    ).fetchone()
    if not clase:
        raise HTTPException(status_code=404, detail="Clase no encontrada")
    
    grupo_id = clase[0]
    print(f"🔍 grupo_id recuperado: {grupo_id}")  # Depuración

    if grupo_id is None or grupo_id == 0:
        return {
            "grupo_id": None,
            "grupo_nombre": "Sin grupo",
            "alumnos": []
        }

    # 2. Obtener nombre del grupo
    grupo_nombre = db.execute(
        text("""
            SELECT CONCAT(c.nombre, ' - Cuatrimestre ', g.cuatrimestre) 
            FROM grupos_academicos g
            JOIN carreras c ON g.carrera_id = c.id
            WHERE g.id = :id
        """), {"id": grupo_id}
    ).fetchone()
    grupo_nombre = grupo_nombre[0] if grupo_nombre else "Sin grupo"

    # 3. Obtener alumnos del grupo con su estado de inscripción en la clase
    alumnos = db.execute(text("""
        SELECT 
            u.id,
            u.matricula,
            u.nombre,
            CASE WHEN i.id IS NOT NULL THEN true ELSE false END as inscrito
        FROM perfiles_usuarios u
        LEFT JOIN inscripciones i ON i.alumno_id = u.id AND i.clase_id = :clase_id
        WHERE u.grupo_id = :grupo_id AND u.rol = 'alumno'
        ORDER BY u.nombre
    """), {"clase_id": clase_id, "grupo_id": grupo_id}).fetchall()

    return {
        "grupo_id": grupo_id,
        "grupo_nombre": grupo_nombre,
        "alumnos": [{
            "id": str(r[0]),
            "matricula": r[1],
            "nombre": r[2],
            "inscrito": r[3]
        } for r in alumnos]
    }


@app.post("/api/admin/clase/{clase_id}/inscribir-grupo")
def inscribir_grupo_clase(clase_id: int, db: Session = Depends(get_db)):
    clase = db.execute(
        text("SELECT grupo_id FROM clases WHERE id = :id"),
        {"id": clase_id}
    ).fetchone()
    if not clase or not clase[0]:
        raise HTTPException(status_code=400, detail="La clase no tiene grupo asignado")
    grupo_id = clase[0]

    result = db.execute(text("""
        INSERT INTO inscripciones (clase_id, alumno_id)
        SELECT :clase_id, u.id
        FROM perfiles_usuarios u
        WHERE u.grupo_id = :grupo_id 
          AND u.rol = 'alumno'
          AND NOT EXISTS (
              SELECT 1 FROM inscripciones i 
              WHERE i.clase_id = :clase_id AND i.alumno_id = u.id
          )
    """), {"clase_id": clase_id, "grupo_id": grupo_id})
    db.commit()
    return {
        "status": "success",
        "message": f"Alumnos inscritos correctamente",
        "insertados": result.rowcount
    }


@app.delete("/api/admin/clase/{clase_id}/desinscribir-grupo")
def desinscribir_grupo_clase(clase_id: int, db: Session = Depends(get_db)):
    clase = db.execute(
        text("SELECT grupo_id FROM clases WHERE id = :id"),
        {"id": clase_id}
    ).fetchone()
    if not clase or not clase[0]:
        raise HTTPException(status_code=400, detail="La clase no tiene grupo asignado")
    grupo_id = clase[0]

    result = db.execute(text("""
        DELETE FROM inscripciones
        WHERE clase_id = :clase_id
          AND alumno_id IN (
              SELECT u.id FROM perfiles_usuarios u
              WHERE u.grupo_id = :grupo_id AND u.rol = 'alumno'
          )
    """), {"clase_id": clase_id, "grupo_id": grupo_id})
    db.commit()
    return {
        "status": "success",
        "message": f"Alumnos desinscritos correctamente",
        "eliminados": result.rowcount
    }


@app.get("/api/admin/zonas-wifi")
def get_zonas_wifi(db: Session = Depends(get_db)):
    query = text("SELECT id, nombre_zona, bssid_mac, tipo_zona FROM zonas_wifi ORDER BY nombre_zona")
    resultados = db.execute(query).fetchall()
    return [{
        "id": r[0],
        "nombre": r[1],
        "bssid": r[2],
        "tipo": r[3]
    } for r in resultados]

@app.post("/api/admin/zona-wifi")
def crear_zona_wifi(zona: ZonaWifiCreate, db: Session = Depends(get_db)):
    existe = db.execute(
        text("SELECT id FROM zonas_wifi WHERE bssid_mac = :bssid"),
        {"bssid": zona.bssid_mac}
    ).fetchone()
    if existe:
        raise HTTPException(status_code=400, detail="El BSSID ya está registrado")
    
    result = db.execute(text("""
        INSERT INTO zonas_wifi (nombre_zona, bssid_mac, tipo_zona)
        VALUES (:nombre, :bssid, :tipo) RETURNING id
    """), {
        "nombre": zona.nombre_zona,
        "bssid": zona.bssid_mac,
        "tipo": zona.tipo_zona
    })
    db.commit()
    return {"status": "success", "id": result.fetchone()[0], "message": "Zona WiFi creada"}

@app.put("/api/admin/zona-wifi/{zona_id}")
def editar_zona_wifi(zona_id: int, zona: ZonaWifiCreate, db: Session = Depends(get_db)):
    existe = db.execute(
        text("SELECT id FROM zonas_wifi WHERE id = :id"),
        {"id": zona_id}
    ).fetchone()
    if not existe:
        raise HTTPException(status_code=404, detail="Zona no encontrada")
    
    duplicado = db.execute(
        text("SELECT id FROM zonas_wifi WHERE bssid_mac = :bssid AND id != :id"),
        {"bssid": zona.bssid_mac, "id": zona_id}
    ).fetchone()
    if duplicado:
        raise HTTPException(status_code=400, detail="El BSSID ya está registrado en otra zona")
    
    db.execute(text("""
        UPDATE zonas_wifi SET nombre_zona = :nombre, bssid_mac = :bssid, tipo_zona = :tipo
        WHERE id = :id
    """), {
        "id": zona_id,
        "nombre": zona.nombre_zona,
        "bssid": zona.bssid_mac,
        "tipo": zona.tipo_zona
    })
    db.commit()
    return {"status": "success", "message": "Zona WiFi actualizada"}

@app.delete("/api/admin/zona-wifi/{zona_id}")
def eliminar_zona_wifi(zona_id: int, db: Session = Depends(get_db)):
    en_uso = db.execute(
        text("SELECT id FROM clases WHERE zona_id = :id LIMIT 1"),
        {"id": zona_id}
    ).fetchone()
    if en_uso:
        raise HTTPException(status_code=400, detail="No se puede eliminar la zona porque está asignada a una clase")
    
    db.execute(
        text("DELETE FROM zonas_wifi WHERE id = :id"),
        {"id": zona_id}
    )
    db.commit()
    return {"status": "success", "message": "Zona WiFi eliminada"}

# ============================================
# FUNCIÓN PARA OBTENER BSSID (SISTEMA OPERATIVO)
# ============================================
def obtener_bssid_sistema():
    os_name = platform.system()
    try:
        if os_name == "Windows":
            # Windows: netsh wlan show interfaces
            output = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], 
                                             encoding="utf-8", 
                                             creationflags=subprocess.CREATE_NO_WINDOW)
            # Buscar "BSSID" o "Dirección física" (en español)
            match = re.search(r"BSSID\s*:\s*([0-9A-Fa-f:]{17})", output)
            if match:
                return match.group(1)
            # Fallback: Buscar "Dirección física" (puede ser en español)
            match = re.search(r"Direcci[óo]n física\s*:\s*([0-9A-Fa-f:]{17})", output)
            if match:
                return match.group(1)
                
        elif os_name == "Linux":
            # Linux: iwconfig
            output = subprocess.check_output(["iwconfig"], encoding="utf-8")
            match = re.search(r"Access Point:\s*([0-9A-Fa-f:]{17})", output)
            if match:
                return match.group(1)
                
        elif os_name == "Darwin":  # macOS
            # macOS: airport -I
            airport_path = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
            output = subprocess.check_output([airport_path, "-I"], encoding="utf-8")
            match = re.search(r"BSSID:\s*([0-9A-Fa-f:]{17})", output)
            if match:
                return match.group(1)
    except Exception as e:
        print(f"⚠️ Error obteniendo BSSID: {e}")
    return None

# ---- ENDPOINT PARA OBTENER BSSID ACTUAL ----
@app.get("/api/admin/bssid-actual")
def get_bssid_actual():
    bssid = obtener_bssid_sistema()
    if bssid:
        return {"bssid": bssid, "success": True}
    else:
        raise HTTPException(status_code=404, detail="No se pudo obtener el BSSID de la red actual. Asegúrate de estar conectado a WiFi.")

# ============================================
# ========== VISTAS HTML ==========
# ============================================

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return templates.TemplateResponse(request, "login.html")

@app.get("/login", response_class=HTMLResponse)
def ir_login(request: Request):
    return templates.TemplateResponse(request, "login.html")

@app.get("/panel-maestro", response_class=HTMLResponse)
def mostrar_panel(request: Request):
    return templates.TemplateResponse(request, "panel_maestro.html", {"nombre": "Maestro"})

@app.get("/clase-activa/{clase_id}", response_class=HTMLResponse)
def mostrar_clase_activa(request: Request, clase_id: int):
    return templates.TemplateResponse(request, "clase_activa.html", {"clase_id": clase_id})

@app.get("/clase-historial/{sesion_id}", response_class=HTMLResponse)
def mostrar_clase_historial(request: Request, sesion_id: int):
    return templates.TemplateResponse(request, "clase_historial.html", {"sesion_id": sesion_id})

# ============================================
# ========== VISTAS ADMIN ==========
# ============================================

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    return templates.TemplateResponse(request, "admin/dashboard.html")

@app.get("/admin/usuarios", response_class=HTMLResponse)
def admin_usuarios(request: Request):
    return templates.TemplateResponse(request, "admin/usuarios.html")


@app.get("/admin/clases", response_class=HTMLResponse)
def admin_clases(request: Request):
    return templates.TemplateResponse(request, "admin/clases.html")

@app.get("/admin/carreras", response_class=HTMLResponse)
def admin_carreras(request: Request):
    return templates.TemplateResponse( request, "admin/carreras.html")

# ---- VISTA PARA ZONAS WIFI ----
@app.get("/admin/zonas-wifi", response_class=HTMLResponse)
def admin_zonas_wifi(request: Request):
    return templates.TemplateResponse(request, "admin/zonas_wifi.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
