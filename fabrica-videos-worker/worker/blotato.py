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

# Plataformas verticales (reel/short). Un video horizontal (long) no va acá.
VERTICAL_PLATFORMS = ("tiktok", "instagram", "facebook")

def _target(platform, title, privacy, thumbnail_url, ai=True, page_id=None):
    """Arma el objeto 'target' según los campos que pide cada plataforma."""
    if platform == "youtube":
        t = {
            "targetType": "youtube",
            "title": (title or "")[:100],
            "privacyStatus": privacy if privacy in ("public", "unlisted", "private") else "public",
            "shouldNotifySubscribers": True,
            "isMadeForKids": False,
            "containsSyntheticMedia": bool(ai),  # declarar contenido IA (buena práctica)
        }
        if thumbnail_url:
            t["thumbnailUrl"] = thumbnail_url
        return t
    if platform == "tiktok":
        return {
            "targetType": "tiktok",
            "privacyLevel": "PUBLIC_TO_EVERYONE" if privacy == "public" else "SELF_ONLY",
            "disabledComments": False, "disabledDuet": False, "disabledStitch": False,
            "isBrandedContent": False, "isYourBrand": False, "isAiGenerated": bool(ai),
        }
    if platform == "instagram":
        return {"targetType": "instagram", "mediaType": "reel"}
    if platform == "facebook":
        return {"targetType": "facebook", "pageId": str(page_id or ""), "mediaType": "reel"}
    return {"targetType": platform}

def publish(api_key, platform, account_id, video_url, title, description,
            privacy="public", thumbnail_url=None, ai_generated=True):
    """
    Publica un video en una plataforma (youtube/tiktok/instagram/facebook) vía Blotato.
    Devuelve el JSON de respuesta. Lanza excepción si falla.

    YouTube exige el canal verificado por teléfono para aceptar thumbnailUrl por API:
    si Blotato rechaza por eso, se reintenta UNA vez sin miniatura personalizada en vez
    de perder la publicación entera (YouTube pone una miniatura automática).
    """
    def _post(thumb):
        target = _target(platform, title, privacy, thumb, ai_generated, page_id=account_id)
        body = {"post": {
            "accountId": str(account_id),
            "content": {"text": description or title or "", "mediaUrls": [video_url], "platform": platform},
            "target": target,
        }}
        return requests.post(f"{BASE}/posts", headers=_headers(api_key), json=body, timeout=120)

    r = _post(thumbnail_url)
    thumb_blocked = (platform == "youtube" and r.status_code >= 400
                     and "thumbnail" in r.text.lower()
                     and ("verified" in r.text.lower() or "verificad" in r.text.lower()))
    if thumb_blocked:
        r = _post(None)
    if r.status_code >= 400:
        raise RuntimeError(f"Blotato {platform} {r.status_code}: {r.text[:400]}")
    try:
        resp = r.json()
    except Exception:
        resp = {"raw": r.text}
    if thumb_blocked:
        resp["_thumbnail_fallback"] = True
    return resp

def publish_youtube(api_key, account_id, video_url, title, description,
                    privacy="public", thumbnail_url=None, ai_generated=True, **_):
    """Compat: publica en YouTube (usa publish())."""
    return publish(api_key, "youtube", account_id, video_url, title, description,
                   privacy, thumbnail_url, ai_generated)

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
