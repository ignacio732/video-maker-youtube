"""
Detección de tendencias / temas VIRALES por rubro — 100% gratis, sin API keys.
Fuentes: Google News (búsqueda por rubro), Google Trends (RSS de búsquedas del momento),
GDELT (noticias globales), Wikipedia Pageviews (lo más visto). stdlib + requests.

Filosofía: contenido evergreen y viral (salud, ciencia, tecnología, curiosidades,
mente, fitness, animales, historia, espacio) y NADA de política/economía de país.
Cada tema queda etiquetado con su 'category' (rubro).
"""
import os, requests, datetime, xml.etree.ElementTree as ET, re, html

UA = {"User-Agent": "Mozilla/5.0 (compatible; FabricaVideosYouTube/1.0)"}

# --- Filtro: descartar política y economía de país (no vira / polariza) ---
# Se matchea por LÍMITE DE PALABRA (prefijo) para no cazar falsos positivos
# (ej. "lula" dentro de "celulares"). Los stems ('elecc','econom') cubren plurales.
_POL_ECO = [
    # política (stems y nombres)
    "politic", "elecc", "comicios", "referend", "president", "ministr", "senad",
    "diputad", "gobern", "oficialismo", "oposici", "decreto", "fiscal", "sindicat",
    "votac", "voto", "parlament", "intendente", "piquete",
    "milei", "macri", "massa", "kirchner", "kicillof", "trump", "biden", "putin",
    "maduro", "petro", "boric", "sheinbaum", "lula", "bolsonaro", "congreso",
    # economía
    "inflac", "econom", "dólar", "dolar", "fmi", "riesgo país", "riesgo pais",
    "merval", "bonos", "cepo", "devaluac", "cotizac", "jubilac", "impuest", "subsid",
    "arancel", "déficit", "deficit", "superávit", "superavit", "salario",
    "banco central", "peso argentino", "tasa de interés",
    # conflicto geopolítico actual (historia general SÍ pasa: 'guerra' suelto no filtra)
    "hamas", "gaza", "guerra en", "misil",
    # violencia/tragedia/terrorismo real: nada de esto vira bien ni es apto para un
    # video motivacional/curioso, y algunos proveedores de LLM rechazan directamente
    # la generación si el prompt incluye estos temas como "tendencia a inspirar" (400).
    "atentad", "terroris", "11-s", "11s", "torres gemelas", "al qaeda", "isis",
    "yihad", "masacre", "tirote", "genocidio", "secuestr", "asesinat", "femicid",
    "homicid", "violacion", "violación", "suicid", "abuso sexual", "pedofil",
    "accidente aéreo", "accidente aereo", "tragedia", "explosion", "explosión",
]
_POL_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in _POL_ECO) + r")", re.IGNORECASE)
def _political(text):
    return bool(_POL_RE.search(text or ""))

# --- Rubros: cada uno con su consulta de Google News y de GDELT ---
CATEGORIES = {
    "salud":        {"gn": "salud OR bienestar OR hábitos saludables OR sueño OR longevidad",
                     "gdelt": "health wellbeing longevity", "kw": ["salud","bienestar","cuerpo","dormir","longev"]},
    "fitness":      {"gn": "crecimiento muscular OR rutina de ejercicios OR entrenamiento de fuerza OR salud física OR gimnasio en casa",
                     "gdelt": "muscle growth strength training", "kw": ["ejercicio","muscul","fuerza","entren","gimnasio"]},
    "ciencia":      {"gn": "estudio científico OR descubrimiento OR ciencia OR investigación",
                     "gdelt": "science discovery study", "kw": ["ciencia","estudio","descubr","investig","cuánt","cuant","físic","fisic","química","quimic"]},
    "espacio":      {"gn": "espacio OR astronomía OR NASA OR planeta OR telescopio OR eclipse",
                     "gdelt": "space astronomy NASA", "kw": ["espacio","astro","planeta","nasa","eclipse","luna"]},
    "tecnologia":   {"gn": "inteligencia artificial OR robot OR tecnología OR innovación OR gadget",
                     "gn_en": "artificial intelligence OR robot OR technology OR innovation OR gadget",
                     "gdelt": "artificial intelligence robot technology", "kw": ["tecno","robot","inteligencia artificial","ia ","gadget","app"]},
    "mente":        {"gn": "psicología OR cerebro OR productividad OR hábitos OR memoria",
                     "gdelt": "psychology brain productivity", "kw": ["psico","cerebro","mente","habito","hábito","memoria"]},
    "curiosidades": {"gn": "dato curioso OR sabías que OR fenómeno natural OR récord Guinness OR hecho insólito de la ciencia",
                     "gdelt": "amazing fact record guinness", "kw": ["curios","insólit","insolit","récord","record","sabías"]},
    "animales":     {"gn": "animales OR naturaleza OR especie OR océano OR vida salvaje",
                     "gdelt": "animals wildlife nature", "kw": ["animal","natura","especie","océano","oceano","salvaje"]},
    "historia":     {"gn": "historia OR arqueología OR civilización antigua OR descubrimiento histórico",
                     "gdelt": "history archaeology ancient", "kw": ["histor","arqueo","civiliz","antigu"]},
    "dinero":       {"gn": "finanzas personales OR ahorro OR hábitos de dinero OR productividad financiera",
                     "gdelt": "personal finance saving money habits", "kw": ["ahorr","finanzas","dinero","emprend","invertir"]},
    "fertilidad":   {"gn": "fertilidad OR ovulación OR reserva ovárica OR FIV OR reproducción asistida OR fertilidad masculina",
                     "gdelt": "fertility ovulation IVF reproductive health",
                     "kw": ["fertilidad","ovula","infertil","amh","fiv","icsi","reserva ovárica","reserva ovarica",
                            "seminograma","reproducci\u00f3n asistida","reproduccion asistida","endometriosis",
                            "inseminaci\u00f3n","inseminacion","congelar óvulos","congelar ovulos"]},
}

def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

# -------------------------------------------------------------------- FUENTES
def google_news(query, hl="es-419", gl="AR", maxn=10):
    """Titulares + URL del artículo real de Google News (antes se perdía la URL,
    y sin URL no se puede investigar de verdad una tendencia puntual)."""
    try:
        url = (f"https://news.google.com/rss/search?q={requests.utils.quote(query)}"
               f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")
        xml = requests.get(url, headers=UA, timeout=20, allow_redirects=True).text
        root = ET.fromstring(xml)
        out = []
        for it in root.findall(".//item")[:maxn]:
            title = re.sub(r"\s+-\s+[^-]+$", "", _clean(it.findtext("title")))
            link = _clean(it.findtext("link"))
            if title:
                out.append({"title": title, "url": link or None})
        return out
    except Exception:
        return []

def google_trends(geo="AR", maxn=20):
    """Búsquedas en tendencia ahora (RSS de Google Trends)."""
    try:
        url = f"https://trends.google.com/trending/rss?geo={geo}"
        xml = requests.get(url, headers=UA, timeout=20).text
        root = ET.fromstring(xml)
        out = []
        for it in root.findall(".//item")[:maxn]:
            t = _clean(it.findtext("title"))
            if t:
                out.append(t)
        return out
    except Exception:
        return []

def gdelt_news(query, timespan="3d", maxrecords=8):
    """Igual que antes pero también devuelve la URL real del artículo."""
    try:
        url = ("https://api.gdeltproject.org/api/v2/doc/doc?"
               f"query={requests.utils.quote(query)}&mode=ArtList&maxrecords={maxrecords}"
               f"&timespan={timespan}&format=json&sort=hybridrel")
        arts = requests.get(url, headers=UA, timeout=20).json().get("articles", [])
        return [{"title": _clean(a.get("title")), "url": a.get("url") or None}
                for a in arts if a.get("title")]
    except Exception:
        return []

def wikipedia_hot(lang="es", limit=25):
    try:
        d = datetime.date.today() - datetime.timedelta(days=1)
        url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
               f"{lang}.wikipedia/all-access/{d.year}/{d.month:02d}/{d.day:02d}")
        arts = requests.get(url, headers=UA, timeout=20).json()["items"][0]["articles"]
        stop = ("Wikipedia:", "Especial:", "Portada", "Anexo:", "Ayuda:", "Categoría:",
                "Plantilla:", "(desambiguación)", "Wikcionario")
        out = []
        for a in arts:
            t = a["article"].replace("_", " ")
            if any(s in t for s in stop):
                continue
            out.append(t)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []

# -------------------------------------------------------------------- HELPERS
def _dedupe(items):
    seen, out = set(), []
    for it in items:
        k = re.sub(r"[^a-z0-9]", "", it["topic"].lower())[:44]
        if k and k not in seen and len(it["topic"]) > 8:
            seen.add(k)
            out.append(it)
    return out

def _categorize_free(topic):
    """Asigna un rubro a un tema suelto (Google Trends/Wikipedia) por keywords."""
    t = topic.lower()
    for cat, cfg in CATEGORIES.items():
        if any(k in t for k in cfg["kw"]):
            return cat
    return None

def discover(categories=None, hl="es-419", gl="AR", per_cat=4, include_trends=True, lang="es"):
    """
    Descubre temas por rubro (evergreen/viral). Devuelve lista de
    {topic, url, source, category, metric}, ya filtrada de política/economía.
    Si el canal es en inglés y la categoría tiene 'gn_en', busca en inglés; si no,
    cae a la query en español (mejor eso que no buscar nada).
    """
    cats = categories or list(CATEGORIES.keys())
    items = []
    en = lang.startswith("en")
    for cat in cats:
        cfg = CATEGORIES.get(cat)
        if not cfg:
            continue
        query = (cfg.get("gn_en") if en else None) or cfg["gn"]
        for it in google_news(query, hl, gl, maxn=per_cat + 2):
            items.append({"topic": it["title"], "url": it.get("url"), "source": "google_news", "category": cat, "metric": 2})
        for it in gdelt_news(cfg["gdelt"], maxrecords=per_cat)[:per_cat]:
            items.append({"topic": it["title"], "url": it.get("url"), "source": "gdelt", "category": cat, "metric": 1})
    # Google Trends (lo que se busca ahora) — solo lo que cae en algún rubro
    if include_trends:
        for t in google_trends(gl):
            c = _categorize_free(t)
            if c and (not categories or c in cats):
                items.append({"topic": t, "url": None, "source": "google_trends", "category": c, "metric": 3})
    # Filtrar política/economía y deduplicar
    items = [x for x in items if not _political(x["topic"])]
    items = _dedupe(items)
    order = {"google_trends": 0, "google_news": 1, "gdelt": 2}
    items.sort(key=lambda x: (order.get(x["source"], 3), -x.get("metric", 0)))
    return items

# Categorías "amplias" (aplican a casi cualquier nicho por accidente: palabras como
# "estudio" o "investigación" aparecen en la descripción de canales que NO son de
# ciencia). Si el canal matchea alguna categoría ESPECÍFICA además de una amplia,
# se descartan las amplias — evita la "contaminación temática" (ej. Fertilidad Sin
# Mitos recibiendo trends de "ciencia" por la palabra "estudios" en su nicho).
_BROAD = {"ciencia", "mente", "tecnologia", "curiosidades", "salud"}

def _channel_categories(channel):
    """Elige los rubros relevantes al canal según su nicho/keywords."""
    blob = f"{channel.get('niche','')} {' '.join(channel.get('keywords') or [])}".lower()
    cats = [cat for cat, cfg in CATEGORIES.items() if any(k in blob for k in cfg["kw"])]
    specific = [c for c in cats if c not in _BROAD]
    if specific:
        return specific
    # si no matchea ninguno, usar un set evergreen amplio
    return cats or ["curiosidades", "ciencia", "salud", "tecnologia", "mente"]

# Países hispanohablantes: si una tendencia menciona uno explícitamente, es demasiado
# local para un canal de audiencia global (LATAM+España+EE.UU. hispano parejo). Se
# descarta salvo que el propio canal indique ese país como su mercado/audiencia.
_COUNTRY_GL = {
    "argentina": "AR", "méxico": "MX", "mexico": "MX", "españa": "ES", "colombia": "CO",
    "chile": "CL", "perú": "PE", "peru": "PE", "venezuela": "VE", "ecuador": "EC",
    "bolivia": "BO", "uruguay": "UY", "paraguay": "PY", "cuba": "CU",
    "república dominicana": "DO", "republica dominicana": "DO", "guatemala": "GT",
    "honduras": "HN", "el salvador": "SV", "nicaragua": "NI", "costa rica": "CR",
    "panamá": "PA", "panama": "PA",
}
_COUNTRIES = list(_COUNTRY_GL.keys())

def _mentions_country(text):
    t = (text or "").lower()
    return next((c for c in _COUNTRIES if c in t), None)

# Si la audiencia se describe con estos términos, es multi-región/global aunque
# nombre un país de pasada (ej. "Latinoamérica, España y EE.UU." no apunta a España).
_GLOBAL_SIGNALS = ("latinoamérica", "latinoamerica", "latam", "ee.uu", "eeuu",
                    "estados unidos", "global", "hispanohablante", "hispanoparlante")

def channel_target_countries(channel):
    """Países que el canal indica explícitamente como su mercado (audiencia/nicho).
    Si no menciona ninguno, si menciona 2+, o si hay señales de audiencia multi-región
    (ej. 'LATAM, España y EE.UU.'), se asume audiencia global hispanohablante pareja."""
    blob = f"{channel.get('target_audience','')} {channel.get('niche','')}".lower()
    if any(s in blob for s in _GLOBAL_SIGNALS):
        return []
    found = [c for c in _COUNTRIES if c in blob]
    return found if len(found) == 1 else []

def for_channel(channel, max_topics=8):
    """Temas candidatos para un canal (rubros afines, sin política/economía)."""
    cats = _channel_categories(channel)
    targets = channel_target_countries(channel)
    lang = channel.get("language", "es")
    en = lang.startswith("en")
    gl = _COUNTRY_GL[targets[0]] if targets else ("US" if en else "US")
    hl = "en" if en else "es-419"  # antes quedaba "es-419" fijo incluso para canales en inglés
    items = discover(cats, hl, gl, per_cat=4, lang=lang)
    # además, novedad general filtrada por rubro
    for t in wikipedia_hot(lang):
        c = _categorize_free(t)
        if c and c in cats and not _political(t):
            wiki_url = f"https://{lang}.wikipedia.org/wiki/{t.replace(' ', '_')}"
            items.append({"topic": t, "url": wiki_url, "source": "wikipedia", "category": c, "metric": 1})
    items = [x for x in items if not _political(x["topic"])]
    if not targets:
        # canal sin país propio declarado -> nunca sugerir tendencias atadas a un país puntual
        items = [x for x in items if not _mentions_country(x["topic"])]
    items = _dedupe(items)
    return items[:max_topics]

def discover_global(per_cat=3):
    """Para el dashboard: algunos temas frescos de CADA rubro."""
    return discover(list(CATEGORIES.keys()), per_cat=per_cat)

def fetch_article_text(url, max_chars=4000):
    """
    Investiga de verdad una tendencia puntual: baja la página del artículo y extrae
    el texto legible (párrafos), para que el LLM escriba el guion en base a hechos
    reales de la noticia, no solo al titular. Devuelve None si no se pudo (paywall,
    nota muy corta, etc.) — el llamador debe tener un fallback razonable, nunca
    inventar que "investigó" algo que en realidad no pudo leer.
    """
    if not url:
        return None
    try:
        # Google News redirige (news.google.com/rss/articles/...) al medio real
        r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
        if r.status_code >= 400 or not r.text:
            return None
        html_text = r.text
        html_text = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ",
                           html_text, flags=re.DOTALL | re.IGNORECASE)
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html_text, flags=re.DOTALL | re.IGNORECASE)
        texts = [html.unescape(re.sub(r"<[^>]+>", " ", p)).strip() for p in paragraphs]
        texts = [t for t in texts if len(t) > 40]  # descartar migas de pan, leyendas cortas, etc.
        full = re.sub(r"\s+", " ", "\n".join(texts)).strip()
        if len(full) < 200:  # muy poco texto real -> probablemente no se pudo leer bien
            return None
        return full[:max_chars]
    except Exception:
        return None
