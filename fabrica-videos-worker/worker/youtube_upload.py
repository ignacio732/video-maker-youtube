"""
Subida autónoma a YouTube (Data API v3). Opcional: si no hay credenciales,
el worker deja el video en estado 'ready' para subir manualmente.

Setup (una vez, gratis):
1. Google Cloud Console -> nuevo proyecto -> habilitar 'YouTube Data API v3'.
2. Crear credenciales OAuth (tipo Desktop). Guardar client_id y client_secret.
3. Autorizar cada canal una vez y guardar el refresh_token.
   (Ver scripts/get_youtube_token.py en el repo.)

Variables de entorno:
  YT_CLIENT_ID, YT_CLIENT_SECRET
Refresh token por canal: en Supabase settings, key 'yt_tokens' = {"<slug>": "<refresh_token>"}
"""
import os

def upload(video_path, title, description, tags, refresh_token,
           privacy="public", category_id="27"):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {"title": title[:100],
                    "description": (description or "")[:4900],
                    "tags": (tags or [])[:20],
                    "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/*")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    return vid, f"https://youtube.com/watch?v={vid}"
