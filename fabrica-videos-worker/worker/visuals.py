"""Búsqueda y descarga de video stock gratis (Pexels). Fallback a Pixabay."""
import os, requests, tempfile

PEXELS_KEY = os.environ.get("PEXELS_API_KEY")
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY")

def _pexels(keyword, orientation):
    if not PEXELS_KEY:
        return None
    r = requests.get("https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_KEY},
        params={"query": keyword, "orientation": orientation,
                "per_page": 5, "size": "medium"}, timeout=30)
    if r.status_code != 200:
        return None
    vids = r.json().get("videos", [])
    for v in vids:
        files = sorted(v.get("video_files", []),
                       key=lambda f: (f.get("height") or 0))
        # elegir un archivo de resolución media (hd si existe)
        pick = None
        for f in files:
            if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= 720:
                pick = f; break
        pick = pick or (files[-1] if files else None)
        if pick:
            return pick["link"]
    return None

def _pixabay(keyword, orientation):
    if not PIXABAY_KEY:
        return None
    r = requests.get("https://pixabay.com/api/videos/",
        params={"key": PIXABAY_KEY, "q": keyword, "per_page": 5}, timeout=30)
    if r.status_code != 200:
        return None
    hits = r.json().get("hits", [])
    if not hits:
        return None
    v = hits[0]["videos"]
    return (v.get("medium") or v.get("large") or v.get("small") or {}).get("url")

def _download(url, outdir, idx):
    r = requests.get(url, stream=True, timeout=90)
    r.raise_for_status()
    path = os.path.join(outdir, f"clip_{idx}.mp4")
    with open(path, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    return path

def fetch_clips(segments, outdir, vtype="short"):
    """
    Descarga un clip por segmento (matcheando keywords).
    Devuelve lista de rutas de clips. Vacía si no hay API keys/resultados.
    """
    orientation = "portrait" if vtype == "short" else "landscape"
    clips = []
    for i, seg in enumerate(segments):
        kws = seg.get("keywords") or []
        link = None
        for kw in kws:
            link = _pexels(kw, orientation) or _pixabay(kw, orientation)
            if link:
                break
        if link:
            try:
                clips.append(_download(link, outdir, i))
            except Exception:
                pass
    return clips
