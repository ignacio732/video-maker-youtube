"""
Generación de idea + guion con LLM gratis.
Soporta Groq (OpenAI-compatible) y Google Gemini. Elegí con LLM_PROVIDER.
- Groq:   LLM_PROVIDER=groq   GROQ_API_KEY=...   (https://console.groq.com  gratis)
- Gemini: LLM_PROVIDER=gemini GEMINI_API_KEY=... (https://aistudio.google.com gratis)
"""
import os, json, re, requests

PROVIDER = os.environ.get("LLM_PROVIDER", "groq").lower()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

SYSTEM = (
    "Sos un guionista experto en videos virales de YouTube en español. "
    "Escribís guiones con gancho fuerte en los primeros 3 segundos, ritmo dinámico, "
    "lenguaje claro y llamada a la acción al final. Respondés SIEMPRE en JSON válido."
)

def _prompt(channel, vtype, seed_title):
    dur = "45-58 segundos (short vertical)" if vtype == "short" else "4 a 7 minutos (video largo)"
    seg_hint = "6 a 9" if vtype == "short" else "12 a 20"
    seed = f'El tema pedido es: "{seed_title}". ' if seed_title else "Elegí vos el mejor tema del nicho. "
    return f"""Canal: {channel['name']}
Nicho: {channel['niche']}
Descripción: {channel.get('description') or ''}
Tono: {channel.get('tone') or 'informativo'}
Audiencia: {channel.get('target_audience') or 'general'}
Idioma: español ({channel.get('language','es')})
Formato: {vtype} — duración objetivo {dur}.

{seed}Generá un video optimizado para retención y posicionamiento.

Devolvé SOLO un JSON con esta forma exacta:
{{
  "title": "título atractivo para YouTube (<=70 caracteres)",
  "description": "descripción de 2-3 frases con hashtags al final",
  "tags": ["10 a 15 tags relevantes"],
  "hook": "la primera frase del guion, un gancho potente",
  "segments": [
    {{"text": "frase o par de frases narradas", "keywords": ["2-3 términos EN INGLÉS para buscar video stock que ilustre esta parte"]}}
  ]
}}
Generá {seg_hint} segmentos. El 'text' de todos los segmentos unidos es el guion completo narrado. Las keywords deben ser visuales y concretas (ej: "galaxy stars", "ocean deep")."""

def _extract_json(s):
    s = s.strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        s = m.group(0)
    return json.loads(s)

def _groq(messages):
    key = os.environ["GROQ_API_KEY"]
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.9,
              "response_format": {"type": "json_object"}}, timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def _gemini(messages):
    key = os.environ["GEMINI_API_KEY"]
    prompt = messages[0]["content"] + "\n\n" + messages[1]["content"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    r = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"}},
        timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def generate(channel, vtype="short", seed_title=None):
    """Devuelve dict con title, description, tags, hook, segments."""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(channel, vtype, seed_title)}]
    raw = _gemini(messages) if PROVIDER == "gemini" else _groq(messages)
    data = _extract_json(raw)
    data.setdefault("tags", [])
    data["full_text"] = " ".join(s["text"].strip() for s in data.get("segments", []))
    return data
