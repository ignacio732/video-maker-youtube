"""Cliente Supabase (REST) para el esquema ytfactory. Usa service_role en el worker."""
import os, requests

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]          # service_role (bypassa RLS)
BASE = f"{URL}/rest/v1"
H = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Accept-Profile": "ytfactory",
    "Content-Profile": "ytfactory",
    "Content-Type": "application/json",
}

def _get(path, params):
    r = requests.get(f"{BASE}/{path}", headers=H, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def _patch(path, params, body):
    h = dict(H); h["Prefer"] = "return=representation"
    r = requests.patch(f"{BASE}/{path}", headers=h, params=params, json=body, timeout=30)
    r.raise_for_status()
    return r.json()

def _post(path, body):
    h = dict(H); h["Prefer"] = "return=representation"
    r = requests.post(f"{BASE}/{path}", headers=h, json=body, timeout=30)
    r.raise_for_status()
    return r.json()

# ---- Canales ----
def get_active_channels():
    return _get("channels", {"active": "eq.true", "select": "*"})

def get_channel(cid):
    rows = _get("channels", {"id": f"eq.{cid}", "select": "*"})
    return rows[0] if rows else None

def update_channel(cid, **fields):
    return _patch("channels", {"id": f"eq.{cid}"}, fields)

# ---- Videos ----
def get_pending_videos(limit=3):
    return _get("videos", {"status": "eq.pending", "select": "*",
                           "order": "created_at.asc", "limit": str(limit)})

def channel_has_open_video(cid):
    rows = _get("videos", {"channel_id": f"eq.{cid}",
                           "status": "in.(pending,scripting,voicing,sourcing,rendering,ready,publishing)",
                           "select": "id", "limit": "1"})
    return len(rows) > 0

def count_open_videos(cid):
    rows = _get("videos", {"channel_id": f"eq.{cid}",
                           "status": "in.(pending,scripting,voicing,sourcing,rendering,ready,publishing)",
                           "select": "id"})
    return len(rows)

def get_ready_videos(cid, limit=1):
    return _get("videos", {"channel_id": f"eq.{cid}", "status": "eq.ready",
                           "select": "*", "order": "created_at.asc", "limit": str(limit)})

def get_recent_titles(cid, limit=40):
    """Memoria de contenido: títulos ya usados en el canal (cualquier estado), para
    que el LLM no repita tema. No incluye el guion completo, solo el título."""
    rows = _get("videos", {"channel_id": f"eq.{cid}", "select": "title",
                           "order": "created_at.desc", "limit": str(limit)})
    return [r["title"] for r in rows if r.get("title")]

def get_top_performers(cid, limit=3, min_views=1):
    """Los videos con mejor desempeño del canal hasta ahora (por vistas más recientes
    registradas en video_metrics), para que el próximo guion aprenda del formato/ángulo
    que mejor funcionó. Devuelve [] si todavía no hay datos suficientes."""
    rows = _get("video_metrics", {
        "select": "video_id,views,likes,comments,fetched_at,videos(title,type,thumbnail_style)",
        "order": "fetched_at.desc", "limit": "500"})
    latest = {}
    for r in rows:
        vid = r["video_id"]
        if vid not in latest:
            latest[vid] = r
    ranked = sorted(latest.values(), key=lambda r: r.get("views") or 0, reverse=True)
    out = []
    for r in ranked:
        v = r.get("videos") or {}
        if not v or (r.get("views") or 0) < min_views:
            continue
        out.append({"title": v.get("title"), "type": v.get("type"),
                    "thumbnail_style": v.get("thumbnail_style"),
                    "views": r.get("views"), "likes": r.get("likes"), "comments": r.get("comments")})
    return out[:limit]

def enqueue_video(cid, vtype, title=None):
    return _post("videos", {"channel_id": cid, "type": vtype,
                            "status": "pending", "title": title})[0]

def update_video(vid, **fields):
    return _patch("videos", {"id": f"eq.{vid}"}, fields)

def set_status(vid, status, error=None):
    body = {"status": status}
    if error is not None:
        body["error"] = str(error)[:900]
    return update_video(vid, **body)

# ---- Ideas / Scripts / Assets ----
def add_idea(cid, title, hook=None, angle=None, fmt="short", status="used"):
    return _post("ideas", {"channel_id": cid, "title": title, "hook": hook,
                           "angle": angle, "format": fmt, "status": status})[0]

def add_script(vid, full_text, segments):
    return _post("scripts", {"video_id": vid, "full_text": full_text,
                             "segments": segments})[0]

def add_asset(vid, kind, url=None, storage_path=None, meta=None):
    return _post("assets", {"video_id": vid, "kind": kind, "url": url,
                            "storage_path": storage_path, "meta": meta or {}})[0]

# ---- Settings / Secrets / Logs ----
def get_setting(key, default=None):
    rows = _get("settings", {"key": f"eq.{key}", "select": "value"})
    return rows[0]["value"] if rows else default

def get_secret(key, default=None):
    """Lee un secreto de la tabla protegida ytfactory.secrets (solo service_role)."""
    try:
        rows = _get("secrets", {"key": f"eq.{key}", "select": "value"})
        return rows[0]["value"] if rows else default
    except Exception:
        return default

def upload_media(local_path, dest_name, content_type="video/mp4"):
    """Sube un archivo al bucket público 'videos' y devuelve la URL pública."""
    with open(local_path, "rb") as f:
        data = f.read()
    up = f"{URL}/storage/v1/object/videos/{dest_name}"
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}",
         "Content-Type": content_type, "x-upsert": "true"}
    r = requests.post(up, headers=h, data=data, timeout=180)
    r.raise_for_status()
    return f"{URL}/storage/v1/object/public/videos/{dest_name}"

def upload_video(local_path, dest_name):
    return upload_media(local_path, dest_name, "video/mp4")

def add_trend(channel_id, topic, source, category=None, url=None):
    try:
        return _post("trends", {"channel_id": channel_id, "topic": topic[:200],
                                "source": source, "category": category, "url": url})
    except Exception as e:
        print("add_trend falló:", e)

def get_trend(trend_id):
    rows = _get("trends", {"id": f"eq.{trend_id}", "select": "*"})
    return rows[0] if rows else None

def clear_global_trends():
    """Borra las tendencias globales (channel_id null) para refrescar la vista del dashboard."""
    try:
        h = dict(H)
        requests.delete(f"{BASE}/trends?channel_id=is.null", headers=h, timeout=30)
    except Exception as e:
        print("clear_global_trends falló:", e)

def log(step, message, level="info", vid=None, cid=None):
    try:
        _post("logs", {"step": step, "message": str(message)[:900],
                       "level": level, "video_id": vid, "channel_id": cid})
    except Exception as e:
        print("log falló:", e)
    print(f"[{level}] {step}: {message}")

# ---- Análisis de selección de material visual (para aprender qué tipo de visual
#      convierte mejor, no solo qué tema/formato) ----
_HUMAN_HINTS = ("woman","man","person","people","hand","hands","couple","girl","boy",
                "face","child","baby","doctor","patient","holding","sitting","talking","smiling")

def add_segment_visuals(vid, segs, visual_list):
    """Registra QUÉ se eligió mostrar en cada segmento (no solo qué se buscó), para
    poder correlacionar después con el desempeño real del video (views/likes/comments).
    Ej: confirmar si un gancho con escena humana concreta rinde mejor que un diagrama."""
    rows = []
    for i, seg in enumerate(segs):
        vis = visual_list[i] if visual_list and i < len(visual_list) else {}
        kws = seg.get("keywords") or []
        text = " ".join(kws).lower()
        rows.append({
            "video_id": vid, "idx": i, "segment_text": seg.get("text"),
            "keywords": kws, "chosen_type": vis.get("type"),
            "chosen_source": vis.get("source"), "chosen_ref": vis.get("ref"),
            "is_human_scene": any(h in text for h in _HUMAN_HINTS),
        })
    if rows:
        try:
            _post("segment_visuals", rows)
        except Exception as e:
            print("add_segment_visuals falló:", e)

def get_visual_learnings(cid, min_sample=3):
    """¿El gancho (segmento 0) con escena humana concreta rinde mejor que uno abstracto/
    diagrama en este canal? Compara el promedio de vistas de ambos grupos. Devuelve None
    si no hay muestra suficiente todavía para decir algo con sentido."""
    videos = _get("videos", {"channel_id": f"eq.{cid}", "select": "id"})
    ids = {v["id"] for v in videos}
    if not ids:
        return None
    hooks = _get("segment_visuals", {"idx": "eq.0", "select": "video_id,is_human_scene"})
    hooks = {h["video_id"]: h["is_human_scene"] for h in hooks if h["video_id"] in ids}
    metrics = _get("video_metrics", {"select": "video_id,views,fetched_at", "order": "fetched_at.desc"})
    latest = {}
    for m in metrics:
        if m["video_id"] not in latest:
            latest[m["video_id"]] = m["views"] or 0
    human_views = [latest[vid] for vid, is_human in hooks.items() if is_human and vid in latest]
    other_views = [latest[vid] for vid, is_human in hooks.items() if not is_human and vid in latest]
    if len(human_views) < min_sample or len(other_views) < min_sample:
        return None  # todavía no hay muestra suficiente para sacar una conclusión seria
    return {
        "human_scene_avg_views": sum(human_views) / len(human_views), "human_scene_n": len(human_views),
        "other_avg_views": sum(other_views) / len(other_views), "other_n": len(other_views),
    }


