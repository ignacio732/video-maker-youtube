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
    "los","las","un","una","para","por","que","del","una","the",
}

def _tokens(s):
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 4 and t not in _STOP]

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

def _score_video(v, want, min_dur, orientation):
    slug_tokens = set(_tokens(v.get("url", "")))
    overlap = len(slug_tokens & set(want))
    dur = v.get("duration") or 0
    ok_dur = dur >= max(3, min_dur * 0.5)
    ori = _orient_ok(v.get("width"), v.get("height"), orientation)
    score = overlap * 10 + (2 if ok_dur else 0) + (2 if ori else 0)
    return score, overlap

def _score_photo(p, want):
    txt = set(_tokens(p.get("alt", "")) + _tokens(p.get("url", "")))
    return len(txt & set(want))

def _queries(kws, subject):
    """Consultas ordenadas de más específica (tema+keyword) a más general."""
    qs = []
    subj = (subject or "").strip()
    for kw in kws[:3]:
        kw = (kw or "").strip()
        if not kw:
            continue
        if subj and subj.lower() not in kw.lower():
            qs.append(f"{kw} {subj}")
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
        want += _tokens(kw)
    want += _tokens(subject)
    want = list(dict.fromkeys(want))
    queries = _queries(kws, subject)

    # 1) VÍDEO relevante (exige al menos 1 token del tema en común)
    best, best_score = None, 0
    for q in queries:
        for v in _pexels_videos(q, orientation):
            if v.get("id") in used_ids:
                continue
            sc, overlap = _score_video(v, want, min_dur, orientation)
            if overlap == 0:      # sin relación con el tema → descartar
                continue
            if sc > best_score:
                best, best_score = v, sc
        if best_score >= 12:      # match fuerte: cortamos temprano
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
            ov = _score_photo(p, want)
            if ov == 0:
                continue
            if ov > best_ps:
                best_p, best_ps = p, ov
        if best_ps >= 2:
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
