"""
Publicación en YouTube (y otras redes) vía Blotato API.
Blotato es una app verificada por Google → puede publicar PÚBLICO sin que el usuario
tenga que pasar la verificación de la API de YouTube. Nuestros videos ya son URLs
públicas en Supabase Storage, así que se pasan directo (sin subir nada extra).

Docs: https://help.blotato.com/api  — Base: https://backend.blotato.com/v2
Auth: header 'blotato-api-key'.
"""
import requests

BASE = "https://backend.blotato.com/v2"

def _headers(api_key):
    return {"blotato-api-key": api_key, "Content-Type": "application/json"}

def list_accounts(api_key):
    """Devuelve las cuentas sociales conectadas en Blotato (para asignarlas a cada canal)."""
    r = requests.get(f"{BASE}/users/me/accounts", headers=_headers(api_key), timeout=30)
    r.raise_for_status()
    data = r.json()
    # La API puede devolver una lista o un objeto {items:[...]}/{accounts:[...]}
    if isinstance(data, dict):
        for k in ("items", "accounts", "data"):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return data if isinstance(data, list) else []

def publish_youtube(api_key, account_id, video_url, title, description,
                    privacy="public", thumbnail_url=None, made_for_kids=False,
                    notify_subscribers=True, ai_generated=True):
    """
    Publica un video en YouTube a través de Blotato.
    Devuelve el JSON de respuesta (que suele incluir id/url del post).
    Lanza excepción si falla (para que el worker lo deje 'ready' y reintente).
    """
    target = {
        "targetType": "youtube",
        "title": (title or "")[:100],
        "privacyStatus": privacy if privacy in ("public", "unlisted", "private") else "public",
        "shouldNotifySubscribers": bool(notify_subscribers),
        "isMadeForKids": bool(made_for_kids),
        "containsSyntheticMedia": bool(ai_generated),  # buena práctica: declarar contenido IA
    }
    if thumbnail_url:
        target["thumbnailUrl"] = thumbnail_url
    body = {"post": {
        "accountId": str(account_id),
        "content": {
            "text": description or title or "",
            "mediaUrls": [video_url],
            "platform": "youtube",
        },
        "target": target,
    }}
    r = requests.post(f"{BASE}/posts", headers=_headers(api_key), json=body, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"Blotato {r.status_code}: {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

def extract_url(resp):
    """Intenta extraer una URL/id publicada de la respuesta de Blotato."""
    if not isinstance(resp, dict):
        return None, None
    # buscar url/id en distintas formas comunes
    for path in (("url",), ("postUrl",), ("data", "url"), ("post", "url")):
        cur = resp
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, str):
            url = cur
            break
    else:
        url = None
    pid = resp.get("id") or resp.get("postId") or (resp.get("data") or {}).get("id") if isinstance(resp, dict) else None
    return url, pid
