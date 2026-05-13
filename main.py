import os
import json
import smtplib
from email.mime.text import MIMEText
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types

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
    sender_email = os.environ.get("SMTP_EMAIL", "simulado")
    sender_password = os.environ.get("SMTP_PASSWORD", "simulado")
    
    if sender_email == "simulado":
        # Simulación segura si no hay credenciales configuradas
        print(f"[CORREO SIMULADO] Para: {data.to_email} | Asunto: {data.subject}\nCuerpo:\n{data.body}")
        return {"message": "Correo procesado (Modo simulación, falta configurar SMTP)"}
        
    try:
        msg = MIMEText(data.body)
        msg['Subject'] = data.subject
        msg['From'] = sender_email
        msg['To'] = data.to_email

        # Esto funciona para Gmail y Google Workspace (Requiere Contraseña de Aplicación)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, sender_password)
            server.send_message(msg)
            
        return {"message": "Correo enviado con éxito"}
    except Exception as e:
        print(f"Error enviando correo: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
