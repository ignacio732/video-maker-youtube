"""
Generación de idea + guion VIRAL con LLM gratis (Groq / Gemini).
Incorpora el playbook de virality 2026: hook en 2s, estructura de retención,
títulos-pregunta <=60 chars, ~160 wpm, CTA con loop, SEO/GEO y formatos ganadores.
"""
import os, json, re, requests

PROVIDER = os.environ.get("LLM_PROVIDER", "groq").lower()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

SYSTEM = (
    "Sos el mejor guionista de YouTube Shorts virales en español, experto en retención "
    "y en el algoritmo 2026. Sabés que el 50-60% del abandono ocurre en los primeros 3 "
    "segundos, que el gancho debe aparecer como TEXTO y VOZ a la vez, que hay que abrir un "
    "'open loop' y pagarlo recién al final, y que el último renglón debe encadenar con el "
    "primero (loop). Escribís frases cortas, ritmo rápido (~160 palabras/min), sin relleno. "
    "Respondés SIEMPRE en JSON válido y en español rioplatense neutro."
)

HOOKS = (
    '1) Afirmación audaz/contraintuitiva: "Todo lo que sabés de X es mentira." '
    '2) Curiosity gap: "Hay algo sobre X que nadie te cuenta." '
    '3) Advertencia: "Nunca hagas X sin saber esto." '
    '4) Dato shock: "El 90% no sabe que X." '
    '5) Pregunta directa: "¿Sabías por qué X?" '
    '6) Ranking teaser: "El número 3 te va a sorprender." '
    '7) Secreto: "Solo unos pocos conocen esto." '
    '8) La verdadera razón: "Esta es la razón real por la que X."'
)

def _prompt(channel, vtype, seed_title, trends):
    if vtype == "short":
        dur = "18 a 28 segundos"
        seg_hint = ("6 a 8 segmentos. Cada 'text' = UNA frase corta (8-12 palabras). "
                    "Segmento 1 = el GANCHO (aparece como texto y voz). "
                    "Último segmento = UN solo CTA breve que además encadene con el gancho (loop).")
    else:
        dur = "4 a 7 minutos"
        seg_hint = ("14 a 20 segmentos. Mantené un giro o dato nuevo cada 5-7 segundos. "
                    "Incluí capítulos (chapters) con timestamps aproximados empezando en 0:00.")
    seed = f'TEMA PEDIDO (respetalo): "{seed_title}".\n' if seed_title else ""
    trend_block = ""
    if trends:
        lst = "\n".join(f"- {t}" for t in trends[:8])
        trend_block = ("TEMAS DEL MOMENTO (usá uno SOLO si encaja perfecto con el nicho; "
                       "si ninguno encaja, ignoralos y elegí el mejor tema del nicho):\n" + lst + "\n")
    return f"""Canal: {channel['name']}
Nicho: {channel['niche']}
Tono: {channel.get('tone') or 'informativo'}
Audiencia: {channel.get('target_audience') or 'general'}
Formato del video: {vtype} — duración objetivo {dur}.

{seed}{trend_block}
Plantillas de gancho probadas (elegí/adaptá la mejor): {HOOKS}

Reglas de retención: gancho en los primeros 2 segundos; abrí un open loop y pagalo al final;
frases cortas y ritmo rápido; sin introducciones ni relleno; un cambio/idea nueva cada 5-7s;
el último renglón debe encadenar con el primero para generar re-visualización (loop).

SEO/GEO: el título es una PREGUNTA o lleva un número, <=60 caracteres, con la palabra clave
al principio. La descripción arranca respondiendo la pregunta en 1 frase (para IA/buscadores),
luego 2-3 frases con contexto y datos, y termina con 2-3 hashtags relevantes.

Devolvé SOLO un JSON con esta forma EXACTA:
{{
  "title": "titulo-pregunta o con numero, <=60 caracteres",
  "hook": "la primera frase, el gancho potente",
  "description": "1 frase que responde + contexto + #hashtag1 #hashtag2 #hashtag3",
  "tags": ["10 a 15 tags/keywords relevantes"],
  "hashtags": ["3 hashtags sin #"],
  "thumbnail_text": "2 a 4 PALABRAS en mayusculas para la miniatura",
  "format": "uno de: datos_curiosos | ranking | historia | motivacion | quiz | explicacion",
  "segments": [
    {{"text": "frase narrada corta", "keywords": ["2-3 terminos EN INGLES para buscar video stock que ilustre esta frase"]}}
  ]
}}
{seg_hint}
Las keywords deben ser visuales y concretas en inglés (ej: "galaxy stars", "ancient ruins", "stock market")."""

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

def generate(channel, vtype="short", seed_title=None, trends=None):
    """Devuelve dict con title, hook, description, tags, hashtags, thumbnail_text, format, segments."""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(channel, vtype, seed_title, trends)}]
    raw = _gemini(messages) if PROVIDER == "gemini" else _groq(messages)
    data = _extract_json(raw)
    data.setdefault("tags", [])
    data.setdefault("hashtags", [])
    data.setdefault("thumbnail_text", (data.get("title") or "")[:24])
    data.setdefault("format", "datos_curiosos")
    data["full_text"] = " ".join(s["text"].strip() for s in data.get("segments", []))
    return data
