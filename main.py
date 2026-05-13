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

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "observatorio-laboral-cr")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

try:
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
except Exception as e:
    print(f"Error inicializando el cliente de Vertex AI: {e}")
    client = None

# Modelos de datos esperados en las peticiones
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

        Reglas:
        1. Si es una Ley Nacional, usa 'leyes'.
        2. Si es un Reglamento, usa 'reglamentos'.
        3. Devuelve SOLO el objeto JSON válido, sin usar bloques de código de markdown.
        """

        pdf_part = types.Part.from_bytes(data=content, mime_type="application/pdf")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, pdf_part]
        )

        json_string = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(json_string)

    except json.JSONDecodeError as je:
        raise HTTPException(status_code=500, detail="El modelo no devolvió un JSON válido.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/analyze-denuncia")
async def analyze_denuncia(data: DenunciaData):
    if not client:
        raise HTTPException(status_code=500, detail="Cliente Vertex AI no inicializado.")
        
    prompt = f"""
    Eres un abogado experto en derecho laboral de Costa Rica, trabajando para el Observatorio de Derechos Laborales.
    Un ciudadano ha reportado la siguiente situación:
    
    Tipo de caso: {data.tipoDenuncia}
    Empresa/Patrono: {data.empresa}
    Descripción de los hechos: {data.descripcion}
    
    Redacta un borrador de respuesta empática, profesional y orientadora dirigida al ciudadano.
    El objetivo es brindarle una primera opinión legal sobre sus derechos según el Código de Trabajo de Costa Rica y los pasos que debería seguir (por ejemplo, acudir al Ministerio de Trabajo, plazos de prescripción, etc.).
    La respuesta debe ser clara, sin lenguaje excesivamente técnico, y en un tono de apoyo. No incluyas marcadores como [Nombre del ciudadano], ve directo al texto de asesoría.
    Devuelve ÚNICAMENTE el texto del mensaje, listo para ser enviado por correo.
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return {"draft": response.text.strip()}
    except Exception as e:
        print(f"Error en Gemini: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-email")
async def send_email(data: EmailData):
    # Obtiene las variables estrictamente (devuelve None si no existen)
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    
    # 1. Falla ruidosamente si no se configuraron las variables en Cloud Run
    if not client_id or not client_secret or not refresh_token:
        print("Error crítico: Faltan credenciales de OAuth en Cloud Run.")
        raise HTTPException(
            status_code=500, 
            detail="Error del servidor: Faltan las credenciales de correo electrónico. Contacte a soporte."
        )
        
    try:
        # 2. Autenticar usando el Refresh Token
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )
        
        # 3. Inicializar la API de Gmail
        service = build('gmail', 'v1', credentials=creds)
        
        # 4. Construir el mensaje de correo
        message = EmailMessage()
        message.set_content(data.body)
        message['To'] = data.to_email
        message['From'] = 'webmaster@iiresodh.org'
        message['Subject'] = data.subject
        
        # 5. Codificar a Base64 seguro para URL (Requerido por Gmail API)
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': encoded_message}
        
        # 6. Enviar usando la API de Google
        send_message = (service.users().messages().send(userId="me", body=create_message).execute())
        
        return {"message": f"Correo enviado con éxito. ID: {send_message['id']}"}
        
    except HttpError as error:
        print(f"Error de la API de Gmail: {error}")
        raise HTTPException(status_code=500, detail=f"Google rechazó el envío (Verifique el token): {error}")
    except Exception as e:
        print(f"Error general enviando correo: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
