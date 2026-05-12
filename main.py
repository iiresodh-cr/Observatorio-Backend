import os
import json
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

app = FastAPI()

# Configuración de CORS para permitir peticiones desde tu frontend en React
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # IMPORTANTE: En producción, cambia "*" por la URL de tu frontend
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuración del cliente GenAI para Vertex AI.
# En Cloud Run, las Application Default Credentials (ADC) se detectan automáticamente.
# Debes asegurarte de que tu proyecto en GCP tiene habilitada la API de Vertex AI.
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "observatorio-laboral-cr")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1") # O la región de tu preferencia

try:
    # Inicializa el cliente forzando el uso de Vertex AI
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
except Exception as e:
    print(f"Error inicializando el cliente de Vertex AI: {e}")
    client = None

@app.post("/extract-metadata")
async def extract_metadata(file: UploadFile = File(...)):
    if not client:
        raise HTTPException(
            status_code=500, 
            detail="El cliente de Vertex AI no está inicializado. Verifica los permisos de Cloud Run y el proyecto."
        )

    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se permiten archivos PDF")

    try:
        content = await file.read()
        
        # Prompt estructurado para Gemini
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
        3. Devuelve SOLO el objeto JSON válido, sin usar bloques de código de markdown (como ```json) y sin texto extra.
        """

        # Preparamos el archivo PDF como una parte compatible con el SDK de GenAI
        pdf_part = types.Part.from_bytes(
            data=content,
            mime_type="application/pdf"
        )

        # Invocamos a Gemini 2.5 Flash mediante Vertex AI
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, pdf_part]
        )

        # Limpiar la respuesta por si el LLM devuelve formato Markdown y convertir a diccionario
        json_string = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(json_string)

    except json.JSONDecodeError as je:
        print(f"Error de parseo JSON: {je} - Respuesta del modelo: {response.text}")
        raise HTTPException(status_code=500, detail="El modelo no devolvió un JSON válido.")
    except Exception as e:
        print(f"Error procesando el documento: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
