"""
Obtené el refresh_token de un canal de YouTube (una sola vez por canal).
Requiere: pip install google-auth-oauthlib
Uso:
  1) Descargá el client_secret.json de Google Cloud (credenciales OAuth Desktop).
  2) python scripts/get_youtube_token.py client_secret.json
  3) Autorizá en el navegador con la cuenta del canal.
  4) Copiá el refresh_token que imprime y guardalo en Supabase:
     settings.yt_tokens = {"<slug-del-canal>": "<refresh_token>"}
"""
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def main():
    if len(sys.argv) < 2:
        print("Uso: python scripts/get_youtube_token.py client_secret.json"); return
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0)
    print("\n=== GUARDÁ ESTO ===")
    print("refresh_token:", creds.refresh_token)
    print("client_id:", creds.client_id)
    print("client_secret:", creds.client_secret)

if __name__ == "__main__":
    main()
