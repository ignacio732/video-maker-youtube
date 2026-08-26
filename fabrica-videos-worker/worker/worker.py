"""
WORKER AUTÓNOMO — Fábrica de Videos YouTube.
Corre en GitHub Actions (cron). Flujo por video:
  pending -> scripting -> voicing -> sourcing -> rendering -> ready -> (publishing) -> published

Uso:
  python worker.py            # procesa la cola + autopiloto de canales
  python worker.py --no-auto  # solo procesa lo que ya está en cola
"""
import os, sys, tempfile, traceback
import db, render

MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS_PER_RUN", "3"))
PRIVACY = os.environ.get("YT_PRIVACY", "public")

THEME_MAP = [
    (("espacio","universo","astronomia","cosmos","planeta"), "space"),
    (("historia","civilizacion","antiguo","imperio"),        "history"),
    (("dinero","finanzas","exito","emprend","motiv"),         "money"),
]
def pick_theme(channel):
    blob = f"{channel.get('niche','')} {' '.join(channel.get('keywords') or [])}".lower()
    for keys, theme in THEME_MAP:
        if any(k in blob for k in keys):
            return theme
    return "generic"

def process_video(v):
    vid = v["id"]
    ch = db.get_channel(v["channel_id"])
    if not ch:
        db.set_status(vid, "failed", "canal inexistente"); return
    vtype = v.get("type") or "short"
    db.log("start", f"Procesando {vtype} de '{ch['name']}'", vid=vid, cid=ch["id"])

    with tempfile.TemporaryDirectory() as td:
        # 1) GUION (LLM)
        db.set_status(vid, "scripting")
        import llm
        data = llm.generate(ch, vtype, seed_title=v.get("title"))
        db.add_script(vid, data["full_text"], data["segments"])
        db.add_idea(ch["id"], data["title"], data.get("hook"), None, vtype, "used")
        db.update_video(vid, title=data["title"],
                        description=data.get("description"), tags=data.get("tags", []))
        db.log("script", f"Guion listo: {data['title']}", vid=vid, cid=ch["id"])

        # 2) VOZ + timings
        db.set_status(vid, "voicing")
        mp3 = os.path.join(td, "voz.mp3")
        words = render.synth_voice(data["full_text"], ch.get("voice") or "es-AR-TomasNeural", mp3)
        dur = render.audio_duration(mp3)
        db.log("voice", f"Voz {dur:.0f}s, {len(words)} palabras", vid=vid, cid=ch["id"])

        # 3) SUBTÍTULOS
        w, h = (1080, 1920) if vtype == "short" else (1920, 1080)
        ass = render.build_ass(words, os.path.join(td, "subs.ass"), w=w, h=h)

        # 4) VISUALES (Pexels/Pixabay) con fallback a gradiente temático
        db.set_status(vid, "sourcing")
        import visuals
        clips = visuals.fetch_clips(data["segments"], td, vtype)
        db.log("visuals", f"{len(clips)} clips de stock", vid=vid, cid=ch["id"])

        # 5) RENDER
        db.set_status(vid, "rendering")
        out = os.path.join(td, "final.mp4")
        music = os.environ.get("MUSIC_PATH")
        if len(clips) >= 2:
            render.compose_from_clips(clips, mp3, ass, out, w=w, h=h, music=music)
        else:
            render.compose_from_gradient(pick_theme(ch), mp3, ass, out,
                                         w=w, h=h, title=None, music=music)
        size = os.path.getsize(out) / 1e6
        db.log("render", f"Render OK {size:.1f}MB", vid=vid, cid=ch["id"])
        db.update_video(vid, duration_seconds=round(dur, 1))

        # 6) PUBLICAR (opcional)
        yt_tokens = db.get_setting("yt_tokens", {}) or {}
        token = yt_tokens.get(ch.get("slug"))
        if token and os.environ.get("YT_CLIENT_ID"):
            db.set_status(vid, "publishing")
            try:
                import youtube_upload
                yid, url = youtube_upload.upload(out, data["title"],
                    data.get("description"), data.get("tags"), token, privacy=PRIVACY)
                db.update_video(vid, status="published", youtube_video_id=yid,
                                youtube_url=url, published_at="now()")
                db.add_asset(vid, "final", url=url)
                db.log("publish", f"Publicado: {url}", vid=vid, cid=ch["id"])
            except Exception as e:
                db.set_status(vid, "ready", f"upload falló: {e}")
                db.log("publish", f"Upload falló, queda 'ready': {e}", "warn", vid, ch["id"])
        else:
            # Sin credenciales de YouTube -> queda listo para subir manual
            db.set_status(vid, "ready")
            db.log("ready", "Video listo (sin credenciales YouTube configuradas)",
                   "info", vid, ch["id"])

def autopilot():
    """Encola un video por cada canal activo que no tenga trabajo en curso."""
    for ch in db.get_active_channels():
        if db.channel_has_open_video(ch["id"]):
            continue
        fmt = ch.get("format") or "both"
        vtype = "long" if fmt == "long" else "short"  # 'both' arranca por short
        db.enqueue_video(ch["id"], vtype)
        db.log("autopilot", f"Encolado {vtype} para '{ch['name']}'", cid=ch["id"])

def main():
    if "--no-auto" not in sys.argv:
        try:
            autopilot()
        except Exception as e:
            db.log("autopilot", f"Error: {e}", "error")

    pend = db.get_pending_videos(MAX_VIDEOS)
    db.log("run", f"{len(pend)} videos en cola para procesar")
    for v in pend:
        try:
            process_video(v)
        except Exception as e:
            db.set_status(v["id"], "failed", str(e))
            db.log("error", traceback.format_exc()[-800:], "error", v["id"])

if __name__ == "__main__":
    main()
