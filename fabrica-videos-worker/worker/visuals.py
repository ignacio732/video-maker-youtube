"""
Selección de visuales para cada segmento — 100% gratis (Pexels vídeos + fotos, fallback Pixabay).

Objetivo: que lo que se ve tenga que ver con lo que se narra (credibilidad).
Estrategia por segmento:
  1) Buscar VÍDEO en Pexels con consultas ancladas al tema (keywords del segmento + visual_subject).
  2) Puntuar cada candidato por relevancia real (coincidencia de tokens con el slug/tema),
     duración y orientación. Descartar los que NO comparten ningún token del tema (evita relleno).
  3) Si no hay vídeo relevante, buscar FOTO (Pexels tiene muchísimas más) y puntuar por su 'alt'
     descriptivo → se anima con Ken Burns. Cubre temas específicos sin caer en clips off-topic.
  4) Si nada matchea, se marca 'gradient' (tarjeta temática) — mejor eso que un clip fuera de tema.
Dedupe global por id para no repetir el mismo material.
"""
import os, re, requests

PEXELS_KEY = os.environ.get("PEXELS_API_KEY")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY")
UA = {"User-Agent": "FabricaVideosYouTube/1.0"}

_STOP = {
    "the","a","an","of","and","or","to","in","on","at","for","with","from","by","as","is",
    "are","be","this","that","these","those","your","you","it","its","into","over","under",
    "video","stock","footage","clip","background","scene","shot","view","con","de","la","el",
    "los","las","un","una","para","por","que","del","una","the","was","has","had","not","but",
    "all","new","how","why","she","him","her","who","did","yes","per","via","top","use","can",
    "out","one","two","big","set","and","day","way",
}

# Palabras EN INGLÉS cortas pero muy polisémicas: si son el ÚNICO match, generan falsos
# positivos clásicos de banco de stock (ej. "spring" → trae temporada primavera/flores,
# no un resorte metálico; "tube" → trae tubos de ensayo de laboratorio). Se excluyen del
# conteo de relevancia salvo que aparezcan como parte de una FRASE completa (ver _phrase_hit).
_AMBIGUOUS = {
    "spring","tube","bank","bat","mouse","seal","crane","bar","palm","mine","plant",
    "present","tank","fair","novel","current","match","pool","pitcher","iron","board",
    "pipe","drive","band","dish","letter","case",
}

def _tokens(s, drop_ambiguous=False):
    toks = [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 3 and t not in _STOP]
    if drop_ambiguous:
        toks = [t for t in toks if t not in _AMBIGUOUS]
    return toks

def _phrase_hit(keywords, text):
    """True si alguna de las keywords ORIGINALES (frase completa, no tokenizada) aparece
    literalmente en el texto del candidato (slug de Pexels / alt de la foto). Es una señal
    de relevancia mucho más fuerte y precisa que el overlap de tokens sueltos."""
    t = (text or "").lower()
    for kw in keywords:
        kw = (kw or "").strip().lower()
        if len(kw) >= 5 and kw in t:
            return True
    return False

def _download(url, outdir, name):
    r = requests.get(url, stream=True, timeout=90, headers=UA)
    r.raise_for_status()
    path = os.path.join(outdir, name)
    with open(path, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    return path

# --------------------------------------------------------------------- PEXELS
def _pexels_videos(query, orientation, per_page=15):
    if not PEXELS_KEY:
        return []
    try:
        r = requests.get("https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": query, "orientation": orientation,
                    "per_page": per_page, "size": "medium"}, timeout=30)
        if r.status_code != 200:
            return []
        return r.json().get("videos", [])
    except Exception:
        return []

def _pexels_photos(query, orientation, per_page=15):
    if not PEXELS_KEY:
        return []
    try:
        r = requests.get("https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": query, "orientation": orientation, "per_page": per_page}, timeout=30)
        if r.status_code != 200:
            return []
        return r.json().get("photos", [])
    except Exception:
        return []

def _video_file(v, w, h):
    """Elige el mp4 de mejor resolución para el formato (>=720p, cercano al alto objetivo)."""
    files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"]
    if not files:
        return None
    hd = [f for f in files if (f.get("height") or 0) >= 720]
    pool = hd or files
    pool.sort(key=lambda f: abs((f.get("height") or 0) - h))
    return (pool[0] or {}).get("link")

def _orient_ok(width, height, orientation):
    if not width or not height:
        return True
    return (orientation == "portrait" and height >= width) or \
           (orientation == "landscape" and width >= height)

# Términos que NUNCA deben aparecer en un video, sin importar cuánto "matcheen" por
# token: son categorías sensibles/fuera de marca que un banco de stock puede devolver
# por coincidencia semántica accidental (ej. "tubo"+"presión" trae casquillos de bala,
# porque un cartucho también es un "tubo" que usa "presión"). Se descartan de plano.
_BANNED = {
    "gun","guns","firearm","firearms","rifle","pistol","bullet","bullets","ammo",
    "ammunition","cartridge","cartridges","shell","shells","shotgun","weapon","weapons",
    "grenade","explosive","explosives","knife","knives","blood","gore","corpse","nude",
    "naked","nsfw","sex","sexual","drug","drugs","cocaine","syringe","needle",
}

def _is_banned(*texts):
    blob = " ".join(re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower()) for t in texts)
    words = set(blob.split())
    return bool(words & _BANNED)

def _score_video(v, want, keywords, min_dur, orientation):
    """
    Puntúa un candidato. Acepta SOLO si hay evidencia real de relevancia:
      - una FRASE completa de las keywords aparece literalmente en el slug (fuerte), o
      - al menos 2 tokens distintos (ya sin las palabras ambiguas tipo 'spring'/'tube')
        coinciden con el tema.
    Un único token ambiguo NUNCA alcanza para aceptar un candidato: es la causa más común
    de clips sin relación con el video (ej. 'spring' trayendo flores de primavera).
    Además, cualquier candidato con contenido de la lista _BANNED se descarta directamente,
    incluso si matchea por token (ej. munición matcheando por 'tubo'/'presión').
    """
    slug = v.get("url", "")
    if _is_banned(slug):
        return 0, 0
    slug_tokens = set(_tokens(slug, drop_ambiguous=True))
    overlap = len(slug_tokens & set(want))
    phrase = _phrase_hit(keywords, slug)
    if not phrase and overlap < 2:
        return 0, 0
    dur = v.get("duration") or 0
    ok_dur = dur >= max(3, min_dur * 0.5)
    ori = _orient_ok(v.get("width"), v.get("height"), orientation)
    score = (20 if phrase else 0) + overlap * 6 + (2 if ok_dur else 0) + (2 if ori else 0)
    return score, max(overlap, 1 if phrase else 0)

def _score_photo(p, want, keywords):
    alt, url = p.get("alt", ""), p.get("url", "")
    if _is_banned(alt, url):
        return 0
    txt = set(_tokens(alt, drop_ambiguous=True) + _tokens(url, drop_ambiguous=True))
    overlap = len(txt & set(want))
    phrase = _phrase_hit(keywords, alt) or _phrase_hit(keywords, url)
    if not phrase and overlap < 2:
        return 0
    return (20 if phrase else 0) + overlap * 6

def _queries(kws, subject):
    """Consultas ordenadas de más específica (tema+keyword) a más general.
    Las keywords ambiguas en solitario (spring, tube, etc.) NUNCA se buscan solas:
    siempre van acompañadas del tema para desambiguar la búsqueda en el banco de stock."""
    qs = []
    subj = (subject or "").strip()
    kws = [k for k in kws[:3] if (k or "").strip()]
    if subj and kws:
        qs.append(f"{' '.join(k.strip() for k in kws)} {subj}")
    for kw in kws:
        kw = kw.strip()
        ambiguous_alone = bool(set(_tokens(kw)) & _AMBIGUOUS) and len(_tokens(kw)) == 1
        if subj and subj.lower() not in kw.lower():
            qs.append(f"{kw} {subj}")
        if not ambiguous_alone:
            qs.append(kw)
    if subj:
        qs.append(subj)
    # dedupe conservando orden
    return list(dict.fromkeys([q for q in qs if q]))

def choose_visual(seg, subject, outdir, idx, w, h, orientation, min_dur, used_ids):
    """Devuelve dict {type:'video'|'image'|'gradient', path?} para un segmento."""
    kws = seg.get("keywords") or []
    want = []
    for kw in kws:
        want += _tokens(kw, drop_ambiguous=True)
    want += _tokens(subject, drop_ambiguous=True)
    want = list(dict.fromkeys(want))
    all_kw_and_subject = kws + ([subject] if subject else [])
    queries = _queries(kws, subject)

    # 1) VÍDEO relevante (exige frase completa o >=2 tokens de tema en común;
    #    un solo token ambiguo tipo 'spring'/'tube' nunca alcanza — ver _score_video)
    best, best_score = None, 0
    for q in queries:
        for v in _pexels_videos(q, orientation):
            if v.get("id") in used_ids:
                continue
            sc, overlap = _score_video(v, want, all_kw_and_subject, min_dur, orientation)
            if sc == 0:
                continue
            if sc > best_score:
                best, best_score = v, sc
        if best_score >= 26:      # match muy fuerte (frase completa + buena duración/orientación): cortamos
            break
    if best:
        link = _video_file(best, w, h)
        if link:
            try:
                path = _download(link, outdir, f"clip_{idx}.mp4")
                used_ids.add(best.get("id"))
                return {"type": "video", "path": path}
            except Exception:
                pass

    # 2) FOTO relevante (Ken Burns) — puntuada por el 'alt' descriptivo de Pexels
    best_p, best_ps = None, 0
    for q in queries:
        for p in _pexels_photos(q, orientation):
            if p.get("id") in used_ids:
                continue
            ov = _score_photo(p, want, all_kw_and_subject)
            if ov == 0:
                continue
            if ov > best_ps:
                best_p, best_ps = p, ov
        if best_ps >= 20:
            break
    if best_p:
        src = best_p.get("src", {})
        url = src.get("large2x") or src.get("large") or src.get("original")
        if url:
            try:
                path = _download(url, outdir, f"img_{idx}.jpg")
                used_ids.add(best_p.get("id"))
                return {"type": "image", "path": path}
            except Exception:
                pass

    # 3) Nada relevante → tarjeta de gradiente (mejor que un clip fuera de tema)
    return {"type": "gradient"}

def fetch_visuals(segments, seg_durations, subject, outdir, vtype="short", w=1080, h=1920):
    """
    Devuelve una lista de visuales (1 por segmento, alineada al orden de 'segments'),
    cada uno {type:'video'|'image'|'gradient', path?}. Dedupe global por id.
    """
    orientation = "portrait" if vtype == "short" else "landscape"
    used_ids = set()
    out = []
    for i, seg in enumerate(segments):
        dur = seg_durations[i] if i < len(seg_durations) else 3.0
        out.append(choose_visual(seg, subject, outdir, i, w, h, orientation, dur, used_ids))
    return out

def fill_gaps(visual_list, segments, seg_durations, subject, outdir, vtype="short", w=1080, h=1920):
    """Completa SOLO los segmentos marcados 'gradient' con stock relevante (eficiente)."""
    orientation = "portrait" if vtype == "short" else "landscape"
    used_ids = set()
    for i, vis in enumerate(visual_list):
        if vis.get("type") == "gradient":
            dur = seg_durations[i] if i < len(seg_durations) else 3.0
            got = choose_visual(segments[i], subject, outdir, i, w, h, orientation, dur, used_ids)
            if got.get("type") in ("video", "image"):
                visual_list[i] = got
    return visual_list

# ----------------------------------------------------- COMPAT (API anterior)
def fetch_clips(segments, outdir, vtype="short"):
    """Compatibilidad: devuelve solo rutas de vídeo (sin fotos/gradiente)."""
    vis = fetch_visuals(segments, [4.0] * len(segments), "", outdir, vtype)
    return [v["path"] for v in vis if v.get("type") == "video" and v.get("path")]
