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

# ---- Videos ----
def get_pending_videos(limit=3):
    return _get("videos", {"status": "eq.pending", "select": "*",
                           "order": "created_at.asc", "limit": str(limit)})

def channel_has_open_video(cid):
    rows = _get("videos", {"channel_id": f"eq.{cid}",
                           "status": "in.(pending,scripting,voicing,sourcing,rendering,ready,publishing)",
                           "select": "id", "limit": "1"})
    return len(rows) > 0

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

def add_trend(channel_id, topic, source, category=None):
    try:
        return _post("trends", {"channel_id": channel_id, "topic": topic[:200],
                                "source": source, "category": category})
    except Exception as e:
        print("add_trend falló:", e)

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
