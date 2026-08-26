# 🎬 Fábrica de Videos YouTube — Worker Autónomo

Sistema **100% gratis** que genera videos (shorts + largos) en español de forma autónoma
para múltiples canales, y los publica solo en YouTube.

```
pending → scripting → voicing → sourcing → rendering → ready → publishing → published
  (LLM)     (guion)    (edge-tts)  (Pexels)   (ffmpeg)          (YouTube API)
```

Ya están listos y funcionando:
- ✅ Base de datos en Supabase (esquema `ytfactory`)
- ✅ Dashboard de control: https://fabrica-videos-youtube.netlify.app
- ✅ Este worker (guion → voz → visuales → render → publicación)

Falta solo **conectar tus API keys gratis** y subir esto a un repo de GitHub. 15 minutos.

---

## 🧩 Componentes

| Archivo | Qué hace |
|---|---|
| `worker.py` | Orquesta todo. Autopiloto + procesa la cola. |
| `llm.py` | Genera idea + guion (Groq o Gemini, gratis). |
| `render.py` | Voz (edge-tts) + subtítulos + render ffmpeg. |
| `visuals.py` | Descarga video stock (Pexels/Pixabay). |
| `youtube_upload.py` | Publica en YouTube (Data API v3). |
| `db.py` | Lee/escribe en Supabase. |
| `.github/workflows/factory.yml` | Corre el worker cada 6h (cron). |

---

## 🚀 Setup en 6 pasos (todo gratis)

### 1. Repo de GitHub
Creá un repo (puede ser **privado**) y subí esta carpeta.
> Tip: si lo hacés **público**, los minutos de GitHub Actions son **ilimitados**.

### 2. API key de LLM (elegí una)
- **Groq** (recomendado, rápido): https://console.groq.com → API Keys. Gratis.
- **Gemini**: https://aistudio.google.com → Get API key. Gratis.

### 3. API key de video stock
- **Pexels**: https://www.pexels.com/api → gratis, miles de requests/mes.
- (opcional) **Pixabay**: https://pixabay.com/api/docs
> Sin estas keys igual funciona, pero usa fondos de color en vez de video real.

### 4. Service key de Supabase
Supabase → tu proyecto → **Project Settings → API → `service_role`** (es secreta).

### 5. YouTube (opcional, para publicar solo)
1. Google Cloud Console → nuevo proyecto → habilitá **YouTube Data API v3**.
2. Credenciales → OAuth client (tipo **Desktop**) → guardá `client_secret.json`.
3. Por cada canal: `python scripts/get_youtube_token.py client_secret.json`
   y autorizá con la cuenta de ese canal.
4. Guardá el `refresh_token` en Supabase, tabla `settings`:
   ```sql
   insert into ytfactory.settings (key, value)
   values ('yt_tokens', '{"misterios-universo":"TU_REFRESH_TOKEN"}'::jsonb)
   on conflict (key) do update set value = excluded.value;
   ```
> Sin esto, los videos se generan y quedan en estado **`ready`** para que los subas a mano.

### 6. Cargar los secrets en GitHub
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Valor |
|---|---|
| `SUPABASE_URL` | `https://ecbwzrkxsetrwdvzkwkm.supabase.co` |
| `SUPABASE_SERVICE_KEY` | (service_role del paso 4) |
| `LLM_PROVIDER` | `groq` (o `gemini`) |
| `GROQ_API_KEY` | (paso 2) |
| `PEXELS_API_KEY` | (paso 3) |
| `YT_CLIENT_ID` / `YT_CLIENT_SECRET` | (paso 5, opcional) |

Listo. El cron produce videos cada 6 horas. También podés dispararlo a mano en
**Actions → Fábrica de Videos → Run workflow**.

---

## ▶️ Probar local
```bash
pip install -r requirements.txt
cp .env.example .env      # completá tus keys
export $(grep -v '^#' .env | xargs)
python worker.py          # autopiloto + procesa la cola
python worker.py --no-auto  # solo procesa lo encolado desde el dashboard
```

## 🎛️ Cómo se controla
- **Dashboard** (Netlify): crear canales, encolar videos, ver el pipeline, ideas y logs.
- **Autopiloto**: cada corrida encola 1 video por canal activo que no tenga trabajo en curso.
- **Manual**: en el dashboard, botón "+ Encolar video" en cada canal.

## 📈 Escalar (seguir siendo gratis)
- Repo público → Actions ilimitado.
- Varias API keys rotadas (Groq/Pexels) para más volumen.
- Subir la cadencia del cron (ej. `0 */3 * * *`).
- Cuota YouTube: ~6 uploads/día por proyecto de Google Cloud; creá más proyectos si necesitás más.
