"""
Busca videos de YouTube (shorts/largos, filtrando por vistas mínimas) y extrae su
guion real (subtítulos) — 100% gratis, sin API key, vía yt-dlp. Sirve de base para
"remixar" un video viral: mismo tema y mismo ritmo de hook, guion nuevo (ver
llm.remix_script).
"""
import re
import requests

UA = {"User-Agent": "Mozilla/5.0"}


def _ydl_opts(**extra):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "nocheckcertificate": True,
            # El cliente "web" (default) es al que más le exige YouTube el chequeo
            # "confirm you're not a bot" en IPs de datacenter (GitHub Actions incluido).
            # android/tv lo evitan la mayoría de las veces, sin necesitar cookies.
            "extractor_args": {"youtube": {"player_client": ["android", "tv", "web"]}}}
    opts.update(extra)
    return opts


def extract_video_id(url_or_id):
    if re.fullmatch(r"[\w-]{11}", url_or_id or ""):
        return url_or_id
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", url_or_id or "")
    return m.group(1) if m else None


def search_videos(query, kind="short", min_views=1_000_000, max_results=8, pool=40):
    """Busca en YouTube (sin API key) y devuelve los candidatos que cumplen el
    filtro de vistas y de duración (short <=180s, long >180s), por vistas
    descendente. `kind='any'` no filtra por duración."""
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts(extract_flat=True)) as ydl:
        info = ydl.extract_info(f"ytsearch{pool}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    out = []
    for e in entries:
        dur = e.get("duration") or 0
        views = e.get("view_count") or 0
        is_short = 0 < dur <= 180
        if views < min_views:
            continue
        if kind == "short" and not is_short:
            continue
        if kind == "long" and is_short:
            continue
        vid = e.get("id")
        thumbs = e.get("thumbnails") or []
        out.append({
            "id": vid, "title": e.get("title"),
            "channel": e.get("channel") or e.get("uploader"),
            "view_count": views, "duration": dur,
            "url": e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
            "thumbnail": thumbs[-1]["url"] if thumbs else None,
        })
    out.sort(key=lambda x: x["view_count"], reverse=True)
    return out[:max_results]


def get_video_info(url_or_id):
    """Metadata de un video puntual (modo 'pegar un link directo')."""
    import yt_dlp
    vid = extract_video_id(url_or_id)
    if not vid:
        return None
    url = f"https://www.youtube.com/watch?v={vid}"
    with yt_dlp.YoutubeDL(_ydl_opts(extract_flat=True)) as ydl:
        info = ydl.extract_info(url, download=False)
    dur = info.get("duration") or 0
    return {
        "id": vid, "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "view_count": info.get("view_count") or 0, "duration": dur,
        "url": info.get("webpage_url") or url,
    }


_TS_RE = re.compile(r"(\d\d:\d\d:\d\d\.\d\d\d) --> (\d\d:\d\d:\d\d\.\d\d\d)")


def _ts_to_sec(ts):
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _parse_vtt(text):
    """Parsea un .vtt a segmentos {start, duration, text}, descartando el ruido
    típico de auto-captions (línea repetida en cues superpuestos)."""
    blocks = re.split(r"\n\n+", text.replace("\r", ""))
    segs, seen = [], None
    for b in blocks:
        m = _TS_RE.search(b)
        if not m:
            continue
        start, end = _ts_to_sec(m.group(1)), _ts_to_sec(m.group(2))
        lines = [l for l in b.splitlines() if l and not _TS_RE.search(l) and "-->" not in l]
        txt = re.sub(r"<[^>]+>", "", " ".join(lines)).strip()
        if not txt or txt == seen:
            continue
        seen = txt
        segs.append({"start": round(start, 2), "duration": round(max(0.1, end - start), 2),
                    "text": txt})
    return segs


def _fetch_vtt(url):
    """Baja el subtítulo desde la URL que da yt-dlp. A veces esa URL es en
    realidad una playlist HLS (#EXTM3U) que apunta a la URL real del .vtt — hay
    que seguirla, si no se devuelve basura."""
    r = requests.get(url, headers=UA, timeout=20)
    if r.status_code != 200:
        return None
    text = r.text
    if text.lstrip().startswith("#EXTM3U"):
        inner = re.search(r"(https://\S+fmt=vtt\S*)", text)
        if not inner:
            return None
        r = requests.get(inner.group(1), headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        text = r.text
    return text if "-->" in text else None


def extract_transcript(url_or_id, langs=("es", "es-419", "es-ES", "en")):
    """Baja los subtítulos (manuales o automáticos) de un video, en el primer
    idioma disponible de `langs`, y los devuelve como segmentos. None si el video
    no tiene subtítulos en ninguno de esos idiomas."""
    import yt_dlp
    vid = extract_video_id(url_or_id)
    if not vid:
        return None
    url = f"https://www.youtube.com/watch?v={vid}"
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = {**(info.get("subtitles") or {}), **(info.get("automatic_captions") or {})}
    for lang in langs:
        for key in tracks:
            if key != lang and not key.startswith(lang + "-"):
                continue
            fmts = [f for f in tracks[key] if f.get("ext") == "vtt"]
            if not fmts:
                continue
            try:
                text = _fetch_vtt(fmts[0]["url"])
                if text:
                    segs = _parse_vtt(text)
                    if segs:
                        return segs
            except Exception:
                continue
    return None
