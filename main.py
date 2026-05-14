import os
import json
import base64
import secrets
import string
from datetime import datetime
from email.message import EmailMessage
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

# Importaciones para OAuth y Gmail API
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Importaciones para Firebase
import firebase_admin
from firebase_admin import credentials, firestore, auth as admin_auth

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
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

# NUEVO: Modelo para creación de usuarios autorizados
class CreateUserData(BaseModel):
    email: str
    nombre: str
    rol: str # 'admin' o 'autor'
    addedBy: str

@app.post("/extract-metadata")
async def extract_metadata(file: UploadFile = File(...)):
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
        - 'categoria': Clasifícalo estrictamente en una de estas: 'leyes', 'tratados', 'jurisprudencia', 'articulos', 'reglamentos'.
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

@app.post("/analyze-denuncia")
async def analyze_denuncia(data: DenunciaData):
    if not client:
        raise HTTPException(status_code=500, detail="Cliente Vertex AI no inicializado.")
    prompt = f"""
    Eres PIDA, una abogada experta en derecho laboral de Costa Rica. Redacta un borrador de respuesta empática y profesional para este caso:
    Tipo: {data.tipoDenuncia} | Empresa: {data.empresa} | Hechos: {data.descripcion}.
    Brinda opinión legal inicial basada en el Código de Trabajo y pasos a seguir. Devuelve SOLO el texto de asesoría.
    """
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return {"draft": response.text.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate-report")
async def generate_report(data: ReportData):
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

def _enviar_correo_interno(to_email: str, subject: str, body: str):
    """Función auxiliar para enviar correos usando la lógica existente"""
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    
    if not client_id or not client_secret or not refresh_token:
        raise Exception("Faltan credenciales de OAuth.")
        
    try:
        template_ref = db_fs.collection('config').document('emailTemplate').get()
        if template_ref.exists:
            html_base = template_ref.to_dict().get('html')
        else:
            html_base = "<html><body>{{CONTENT}}</body></html>"
    except Exception:
        html_base = "<html><body>{{CONTENT}}</body></html>"

    formatted_body = body.replace("\n", "<br>")
    final_html = html_base.replace("{{CONTENT}}", formatted_body)

    creds = Credentials(token=None, refresh_token=refresh_token, token_uri="https://oauth2.googleapis.com/token", client_id=client_id, client_secret=client_secret)
    service = build('gmail', 'v1', credentials=creds)
    
    message = EmailMessage()
    message.set_content(body) 
    message.add_alternative(final_html, subtype='html') 
    
    message['To'] = to_email
    message['From'] = 'webmaster@iiresodh.org'
    message['Subject'] = subject
    
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={'raw': encoded_message}).execute()

@app.post("/send-email")
async def send_email(data: EmailData):
    try:
        _enviar_correo_interno(data.to_email, data.subject, data.body)
        return {"message": "Correo enviado con éxito."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# NUEVO: Endpoint para crear usuario, guardar en Firestore y enviar invitación
@app.post("/create-user")
async def create_user(data: CreateUserData):
    # 1. Generar contraseña temporal segura
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    temp_password = ''.join(secrets.choice(alphabet) for i in range(12))
    
    try:
        # 2. Crear el usuario en Firebase Authentication
        try:
            user_record = admin_auth.create_user(
                email=data.email,
                email_verified=False,
                password=temp_password,
                display_name=data.nombre
            )
        except admin_auth.EmailAlreadyExistsError:
            # Si el usuario ya existe en Auth, actualizamos su contraseña para asegurarnos de que la tenga
            user_record = admin_auth.get_user_by_email(data.email)
            admin_auth.update_user(user_record.uid, password=temp_password, email_verified=False)
            
        # 3. Generar enlace oficial de verificación de correo de Firebase
        verification_link = admin_auth.generate_email_verification_link(data.email)
        
        # 4. Registrar en Firestore según el rol
        coleccion = "admins" if data.rol == "admin" else "autores"
        rol_legible = "Administrador del Sistema" if data.rol == "admin" else "Redactor del Blog"
        
        db_fs.collection(coleccion).document(data.email.lower()).set({
            "nombre": data.nombre,
            "email": data.email.lower(),
            "addedBy": data.addedBy,
            "date": firestore.SERVER_TIMESTAMP
        })
        
        # 5. Enviar el correo de bienvenida y verificación
        subject = f"Invitación: Acceso como {rol_legible}"
        body = f"""
        Hola {data.nombre},
        
        Se le ha concedido acceso a la plataforma del Observatorio de Derechos Laborales con el rol de: {rol_legible}.
        
        Tus credenciales de acceso temporal son:
        Usuario: {data.email}
        Contraseña: {temp_password}
        
        MUY IMPORTANTE: Antes de poder iniciar sesión por primera vez, debes verificar tu cuenta haciendo clic en el siguiente enlace de seguridad:
        
        {verification_link}
        
        Una vez verificado el correo, podrás entrar al panel administrativo.
        """
        
        _enviar_correo_interno(data.email, subject, body.strip())
        
        return {"message": "Usuario creado, registrado y correo de verificación enviado."}
        
    except Exception as e:
        print(f"Error creando usuario: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), reload=True)
