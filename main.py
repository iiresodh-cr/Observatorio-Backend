import os
import json
import base64
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

# Importaciones para Firebase (leer templates)
import firebase_admin
from firebase_admin import credentials, firestore

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "observatorio-laboral-cr")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

# Inicializar Firebase Admin
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
    Eres un abogado experto en derecho laboral de Costa Rica. Redacta un borrador de respuesta empática y profesional para este caso:
    Tipo: {data.tipoDenuncia} | Empresa: {data.empresa} | Hechos: {data.descripcion}.
    Brinda opinión legal inicial basada en el Código de Trabajo y pasos a seguir. Devuelve SOLO el texto de asesoría.
    """
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return {"draft": response.text.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-email")
async def send_email(data: EmailData):
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    
    if not client_id or not client_secret or not refresh_token:
        raise HTTPException(status_code=500, detail="Faltan credenciales de OAuth.")
        
    try:
        # Intentar obtener el template HTML desde Firestore (config/emailTemplate)
        try:
            template_ref = db_fs.collection('config').document('emailTemplate').get()
            if template_ref.exists:
                html_base = template_ref.to_dict().get('html')
            else:
                # Template por defecto si no existe en Firestore
                html_base = """
                <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: auto; border: 1px solid #ddd; border-radius: 8px; overflow: hidden;">
                        <div style="background-color: #003399; color: white; padding: 20px; text-align: center;">
                            <h2>Observatorio de Derechos Laborales</h2>
                        </div>
                        <div style="padding: 30px;">
                            <p>Estimado(a) ciudadano(a),</p>
                            <div style="background-color: #f9f9f9; padding: 20px; border-radius: 5px; border-left: 4px solid #FFCC00;">
                                {{CONTENT}}
                            </div>
                            <p>Esperamos que esta orientación sea de utilidad para la defensa de sus derechos.</p>
                        </div>
                        <div style="background-color: #f1f1f1; padding: 15px; text-align: center; font-size: 12px; color: #777;">
                            Este es un mensaje automático. Por favor no responda a este correo.
                        </div>
                    </div>
                </body>
                </html>
                """
        except Exception:
            html_base = "<html><body>{{CONTENT}}</body></html>"

        # Reemplazar el marcador por el cuerpo de la asesoría
        # Convertimos los saltos de línea en <br> para el HTML
        formatted_body = data.body.replace("\n", "<br>")
        final_html = html_base.replace("{{CONTENT}}", formatted_body)

        creds = Credentials(token=None, refresh_token=refresh_token, token_uri="https://oauth2.googleapis.com/token", client_id=client_id, client_secret=client_secret)
        service = build('gmail', 'v1', credentials=creds)
        
        message = EmailMessage()
        message.set_content(data.body) # Versión texto plano
        message.add_alternative(final_html, subtype='html') # Versión HTML "bonita"
        
        message['To'] = data.to_email
        message['From'] = 'webmaster@iiresodh.org'
        message['Subject'] = data.subject
        
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={'raw': encoded_message}).execute()
        
        return {"message": "Correo HTML enviado con éxito."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), reload=True)
