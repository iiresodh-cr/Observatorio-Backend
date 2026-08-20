import os
import json
import base64
import secrets
import string
import urllib.parse
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

# Importaciones para Firebase
import firebase_admin
from firebase_admin import credentials, firestore, storage, auth as admin_auth

app = FastAPI(title="Backend Observatorio Laboral CR")

# 1. CORS Seguro
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://observatoriolaboralcr.org",
        "https://www.observatoriolaboralcr.org",
        "https://observatorio-laboral-cr.web.app",
        "https://observatorio-laboral-cr.firebaseapp.com",
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "observatorio-laboral-cr")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

if not firebase_admin._apps:
    firebase_admin.initialize_app()
db_fs = firestore.client()

try:
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
except Exception as e:
    print(f"Error inicializando el cliente de Vertex AI: {e}")
    client = None

# ==============================================================================
# DEPENDENCIAS DE SEGURIDAD / AUTENTICACIÓN
# ==============================================================================
async def verificar_usuario_autenticado(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Encabezado de autorización ausente o inválido.")
    
    token = authorization.split("Bearer ")[1].strip()
    try:
        decoded_token = admin_auth.verify_id_token(token)
        return decoded_token
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token inválido o expirado: {str(e)}")

# ==============================================================================
# MODELOS PYDANTIC
# ==============================================================================
class DenunciaData(BaseModel):
    tipoDenuncia: str
    descripcion: str
    empresa: str

class EmailData(BaseModel):
    to_email: str
    subject: str
    body: str

class ReportData(BaseModel):
    total_denuncias: int
    pendientes: int
    completadas: int
    desglose_tipos: dict

class CreateUserData(BaseModel):
    email: str
    nombre: str
    rol: str
    addedBy: str

class StatusUpdatePayload(BaseModel):
    denuncia_id: str
    nuevo_estado: str = "completada"

class IncrementCompletadasData(BaseModel):
    tipoDenuncia: str

class PasswordResetPayload(BaseModel):
    email: str

# ==============================================================================
# ENDPOINTS PÚBLICOS
# ==============================================================================
@app.post("/analyze-denuncia")
async def analyze_denuncia(data: DenunciaData):
    if not client:
        raise HTTPException(status_code=500, detail="Cliente Vertex AI no inicializado.")
    
    prompt = f"""
    Eres PIDA, asistente de análisis e información del Observatorio de Derechos Laborales de Costa Rica.
    Redacta un borrador de orientación informativa, empática y objetiva sobre la siguiente consulta:
    Tipo de vulneración: {data.tipoDenuncia} | Empleador/Empresa: {data.empresa} | Hechos: {data.descripcion}.
    
    Instrucciones:
    1. Explica los derechos y principios normativos generales contemplados en el Código de Trabajo de Costa Rica aplicables al caso.
    2. Sugiere pasos prácticos y canales institucionales de atención (como la Inspección de Trabajo del MTSS o la Defensa Pública Laboral).
    3. Mantén un tono orientador. Devuelve ÚNICAMENTE el texto de orientación sin encabezados innecesarios.
    """
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return {"draft": response.text.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/incrementar-nuevas")
async def incrementar_nuevas():
    try:
        stats_ref = db_fs.collection("stats").document("global_counters")
        stats_ref.set({
            "total_denuncias": firestore.Increment(1),
            "pendientes": firestore.Increment(1),
            "lastUpdated": firestore.SERVER_TIMESTAMP
        }, merge=True)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/request-password-reset")
async def request_password_reset(payload: PasswordResetPayload):
    """Genera y envía enlace de recuperación con el SMTP corporativo."""
    try:
        email_clean = payload.email.strip().lower()
        user_record = admin_auth.get_user_by_email(email_clean)
        
        action_code_settings = admin_auth.ActionCodeSettings(
            url="https://observatoriolaboralcr.org/auth-action",
            handle_code_in_app=True,
        )

        reset_link = admin_auth.generate_password_reset_link(
            user_record.email, 
            action_code_settings=action_code_settings
        )

        subject = "Observatorio Laboral: Restablecer su contraseña"
        body = f"""
        <strong>Estimado/a usuario/a,</strong><br><br>
        Hemos recibido una solicitud para restablecer la contraseña de acceso al panel del Observatorio de Derechos Laborales.<br><br>
        Para definir una nueva contraseña, haga clic en el siguiente enlace de seguridad:<br>
        <a href="{reset_link}" style="display:inline-block; padding:10px 20px; background-color:#081A3D; color:white; text-decoration:none; border-radius:4px; font-weight:bold; margin: 15px 0;">Restablecer mi Contraseña</a><br><br>
        <small style="color:#666;">Si no solicitó este cambio, puede ignorar este mensaje de forma segura.</small>
        """

        _enviar_correo_interno(user_record.email, subject, body.strip(), template_name='emailTemplate')
        return {"message": "Si el correo existe, el enlace de restablecimiento ha sido enviado."}

    except admin_auth.UserNotFoundError:
        return {"message": "Si el correo existe, el enlace de restablecimiento ha sido enviado."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/documentos/{filename}")
async def servir_documento(filename: str):
    try:
        decoded_filename = urllib.parse.unquote(filename)
        bucket = storage.bucket(os.environ.get("STORAGE_BUCKET", "observatorio-laboral-cr.firebasestorage.app"))
        
        blob = bucket.blob(f"documentos/{decoded_filename}")
        if not blob.exists():
            blobs = bucket.list_blobs(prefix="documentos/")
            matched_blob = None
            for b in blobs:
                if b.name.endswith(f"_{decoded_filename}"):
                    matched_blob = b
                    break
            if matched_blob:
                blob = matched_blob
            else:
                raise HTTPException(status_code=404, detail="El documento no fue encontrado en el servidor.")

        contents = blob.download_as_bytes()
        clean_filename = blob.name.split('/')[-1].split('_', 1)[-1]

        return Response(
            content=contents,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename=\"{clean_filename}\"",
                "Cache-Control": "public, max-age=86400"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================
# ENDPOINTS ADMINISTRATIVOS PROTEGIDOS
# ==============================================================================
@app.post("/completar-denuncia")
async def completar_denuncia(payload: StatusUpdatePayload, user: dict = Depends(verificar_usuario_autenticado)):
    try:
        denuncia_ref = db_fs.collection("denuncias").document(payload.denuncia_id)
        denuncia_doc = denuncia_ref.get()

        if not denuncia_doc.exists:
            raise HTTPException(status_code=404, detail="La denuncia especificada no existe.")

        denuncia_data = denuncia_doc.to_dict()
        estado_actual = denuncia_data.get("estado", "pendiente")
        tipo_denuncia = denuncia_data.get("tipoDenuncia", "otros")

        if estado_actual == "completada":
            return {"message": "La denuncia ya se encontraba en estado completada."}

        denuncia_ref.update({
            "estado": "completada",
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "actualizadoPor": user.get("email")
        })

        stats_ref = db_fs.collection("stats").document("global_counters")
        update_data = {
            "completadas": firestore.Increment(1),
            f"desglose_tipos.{tipo_denuncia}": firestore.Increment(1),
            "lastUpdated": firestore.SERVER_TIMESTAMP
        }
        if estado_actual == "pendiente":
            update_data["pendientes"] = firestore.Increment(-1)

        stats_ref.set(update_data, merge=True)
        return {"message": "Denuncia completada exitosamente.", "denuncia_id": payload.denuncia_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/incrementar-completadas")
async def incrementar_completadas(data: IncrementCompletadasData, user: dict = Depends(verificar_usuario_autenticado)):
    try:
        stats_ref = db_fs.collection("stats").document("global_counters")
        update_data = {
            "completadas": firestore.Increment(1),
            "pendientes": firestore.Increment(-1),
            "desglose_tipos": { data.tipoDenuncia: firestore.Increment(1) },
            "lastUpdated": firestore.SERVER_TIMESTAMP
        }
        stats_ref.set(update_data, merge=True)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/recalcular-stats")
async def recalcular_stats(user: dict = Depends(verificar_usuario_autenticado)):
    try:
        denuncias_ref = db_fs.collection("denuncias").stream()
        total = 0
        completadas = 0
        pendientes = 0
        desglose_tipos = {}

        for doc in denuncias_ref:
            data = doc.to_dict()
            total += 1
            estado = data.get("estado", "pendiente")
            tipo = data.get("tipoDenuncia", "otros")

            if estado == "completada":
                completadas += 1
                desglose_tipos[tipo] = desglose_tipos.get(tipo, 0) + 1
            elif estado == "pendiente":
                pendientes += 1

        stats_ref = db_fs.collection("stats").document("global_counters")
        stats_ref.set({
            "total_denuncias": total,
            "completadas": completadas,
            "pendientes": pendientes,
            "desglose_tipos": desglose_tipos,
            "lastUpdated": firestore.SERVER_TIMESTAMP
        })
        return {"message": "Estadísticas recalculadas exitosamente."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/extract-metadata")
async def extract_metadata(file: UploadFile = File(...), user: dict = Depends(verificar_usuario_autenticado)):
    if not client:
        raise HTTPException(status_code=500, detail="Cliente Vertex AI no inicializado.")
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se permiten archivos PDF")
    try:
        content = await file.read()
        prompt = """
        Eres un asistente legal experto en la normativa de Costa Rica. 
        Analiza el documento PDF adjunto y extrae la siguiente información en formato JSON estricto:
        - 'titulo': El nombre oficial de la norma, ley o sentencia.
        - 'categoria': Clasifícalo strictly en una de estas: 'leyes', 'reglamentos', 'tratados', 'jurisprudencia', 'articulos'.
        - 'anio': El año de publicación o emisión (número entero).
        - 'descripcion': Un resumen o síntesis del documento que tenga entre dos y tres líneas.
        Reglas: 1. Si es una Ley Nacional, usa 'leyes'. 2. Si es un Reglamento, usa 'reglamentos'. 3. Devuelve SOLO el objeto JSON válido.
        """
        pdf_part = types.Part.from_bytes(data=content, mime_type="application/pdf")
        response = client.models.generate_content(model="gemini-2.5-flash", contents=[prompt, pdf_part])
        json_string = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(json_string)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate-report")
async def generate_report(data: ReportData, user: dict = Depends(verificar_usuario_autenticado)):
    if not client:
        raise HTTPException(status_code=500, detail="Cliente Vertex AI no inicializado.")
    
    ahora = datetime.now()
    fecha_formateada = ahora.strftime("%d de %m de %Y")
    anio_actual = ahora.year
    
    prompt = f"""
    Eres PIDA, la Inteligencia Artificial analítica del Observatorio de Derechos Laborales de Costa Rica.
    Hoy es {fecha_formateada}. Debes redactar un Informe Ejecutivo formal.
    
    Instrucciones de cabecera obligatorias:
    - En la 'Fecha de Emisión' usa: {fecha_formateada}.
    - En el código de Informe usa el año actual: ODL-PIDA-{anio_actual}-01.
    
    Datos matemáticos reales para el análisis:
    - Total de casos recibidos: {data.total_denuncias}
    - Casos pendientes de revisión: {data.pendientes}
    - Casos con asesoría completada: {data.completadas}
    - Desglose detallado por tipo de vulneración: {json.dumps(data.desglose_tipos, ensure_ascii=False)}
    
    El informe debe contener:
    1. Título formal.
    2. Resumen Ejecutivo.
    3. Análisis de Tendencias.
    4. Recomendaciones Estratégicas.
    
    Tono: Académico, institucional y objetivo. No inventes fechas, usa las proporcionadas arriba.
    """
    try:
        response = client.models.generate_content(model="gemini-2.5-pro", contents=prompt)
        return {"report": response.text.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _enviar_correo_interno(to_email: str, subject: str, body: str, template_name: str = 'emailTemplate'):
    smtp_user = os.environ.get("SMTP_USER", "no-responder@observatoriolaboralcr.org")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    
    if not smtp_pass:
        raise Exception("Falta la contraseña del servidor SMTP (SMTP_PASSWORD en las variables de entorno).")
        
    try:
        template_ref = db_fs.collection('config').document(template_name).get()
        if template_ref.exists:
            html_base = template_ref.to_dict().get('html')
        else:
            html_base = "<html><body>{{CONTENT}}</body></html>"
    except Exception:
        html_base = "<html><body>{{CONTENT}}</body></html>"

    disclaimer = """
    <br><hr style="border:0; border-top:1px solid #e0e0e0; margin:20px 0;">
    <p style="font-size:11px; color:#777; line-height:1.4;">
    <strong>Aviso Legal:</strong> La información suministrada tiene carácter estrictamente orientador e informativo conforme a la Ley N° 8968 de Costa Rica. No constituye patrocinio legal ni sustituye trámites ante el Ministerio de Trabajo y Seguridad Social (MTSS) o tribunales.
    </p>
    """

    formatted_body = body.replace("\n", "<br>") + disclaimer
    final_html = html_base.replace("{{CONTENT}}", formatted_body)

    message = EmailMessage()
    message.set_content(body)
    message.add_alternative(final_html, subtype='html')
    
    message['To'] = to_email
    message['From'] = f"Observatorio Laboral CR <{smtp_user}>"
    message['Subject'] = subject
    
    smtp_server = "mailout.easymail.ca"
    smtp_port = 465
    
    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(message)
    except Exception as e:
        print(f"Error SMTP: {e}")
        raise Exception("Ocurrió un error al intentar enviar el correo mediante SMTP.")

@app.post("/send-email")
async def send_email(data: EmailData, user: dict = Depends(verificar_usuario_autenticado)):
    try:
        _enviar_correo_interno(data.to_email, data.subject, data.body)
        return {"message": "Correo enviado con éxito."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/create-user")
async def create_user(data: CreateUserData, user: dict = Depends(verificar_usuario_autenticado)):
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    temp_password = ''.join(secrets.choice(alphabet) for i in range(12))
    
    try:
        try:
            user_record = admin_auth.create_user(
                email=data.email,
                email_verified=False,
                password=temp_password,
                display_name=data.nombre
            )
        except admin_auth.EmailAlreadyExistsError:
            user_record = admin_auth.get_user_by_email(data.email)
            admin_auth.update_user(user_record.uid, password=temp_password, email_verified=False)
            
        action_code_settings = admin_auth.ActionCodeSettings(
            url="https://observatoriolaboralcr.org/auth-action",
            handle_code_in_app=True,
        )

        verification_link = admin_auth.generate_email_verification_link(
            data.email,
            action_code_settings=action_code_settings
        )
        
        coleccion = "admins" if data.rol == "admin" else "autores"
        rol_legible = "Administrador del Sistema" if data.rol == "admin" else "Redactor del Blog"
        
        db_fs.collection(coleccion).document(data.email.lower()).set({
            "nombre": data.nombre,
            "email": data.email.lower(),
            "addedBy": data.addedBy,
            "date": firestore.SERVER_TIMESTAMP
        })
        
        subject = f"Invitación: Acceso como {rol_legible}"
        body = f"""
        <strong>Hola {data.nombre},</strong><br><br>
        Se te ha concedido acceso a la plataforma del Observatorio de Derechos Laborales con el rol de: <strong>{rol_legible}</strong>.<br><br>
        Tus credenciales de acceso temporal son:<br>
        Usuario: <strong>{data.email}</strong><br>
        Contraseña: <strong style="background-color:#f0f0f0; padding:3px 6px; border-radius:4px; letter-spacing:1px;">{temp_password}</strong><br><br>
        <strong style="color:#d32f2f;">MUY IMPORTANTE:</strong> Antes de poder iniciar sesión por primera vez, debes verificar tu cuenta haciendo clic en el siguiente botón de seguridad:<br>
        <a href="{verification_link}" style="display:inline-block; padding:12px 24px; background-color:#081A3D; color:white; text-decoration:none; border-radius:5px; margin-top:15px; margin-bottom:15px; font-weight:bold;">Verificar mi Cuenta</a><br><br>
        Una vez verificado el correo, podrás entrar al panel administrativo.
        """
        
        _enviar_correo_interno(data.email, subject, body.strip(), template_name='inviteTemplate')
        return {"message": "Usuario creado, registrado y correo de verificación enviado."}
        
    except Exception as e:
        print(f"Error creando usuario: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), reload=True)
