"""
WORKER AUTÓNOMO — Fábrica de Videos YouTube.
Corre en GitHub Actions (cron). Flujo por video:
  pending -> scripting -> voicing -> sourcing -> rendering -> ready -> (publishing) -> published

Uso:
  python worker.py            # procesa la cola + autopiloto de canales
  python worker.py --no-auto  # solo procesa lo que ya está en cola
"""
import os, sys, tempfile, traceback, subprocess
import db, render, trends, requests, re as _re

def _download(url, dest):
    r = requests.get(url, stream=True, timeout=90)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    return dest

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
        # 1) GUION — propio (si lo subiste) o generado por IA con tendencias
        db.set_status(vid, "scripting")
        import llm
        us = (v.get("user_script") or "").strip()
        if us:
            title = v.get("title") or us.split("\n")[0][:70]
            sents = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', us) if s.strip()]
            data = {"title": title, "hook": sents[0] if sents else title,
                    "description": "", "tags": [], "hashtags": [],
                    "thumbnail_text": title[:24], "format": "propio",
                    "segments": [{"text": s, "keywords": []} for s in sents] or [{"text": us, "keywords": []}],
                    "full_text": us}
            db.log("script", "Usando guion propio del usuario", vid=vid, cid=ch["id"])
        else:
            trend_topics = None
            if not v.get("title"):
                try:
                    found = trends.for_channel(ch, 8)
                    trend_topics = [t["topic"] for t in found]
                    for t in found[:5]:
                        db.add_trend(ch["id"], t["topic"], t["source"])
                    db.log("trends", f"{len(trend_topics)} tendencias detectadas", vid=vid, cid=ch["id"])
                except Exception as e:
                    db.log("trends", f"sin tendencias: {e}", "warn", vid, ch["id"])
            data = llm.generate(ch, vtype, seed_title=v.get("title"), trends=trend_topics)
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

        # 4) VISUALES: imágenes propias > stock relevante por segmento (vídeo/foto) > gradiente
        db.set_status(vid, "sourcing")
        import visuals
        imgs = []
        for i, url in enumerate(v.get("image_urls") or []):
            try:
                ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
                imgs.append(_download(url, os.path.join(td, f"img_{i}{ext}")))
            except Exception:
                pass

        # Duración estimada por segmento (proporcional al texto), para alinear cada visual
        segs = data["segments"]
        weights = [max(1, len(s.get("text", ""))) for s in segs]
        tot_w = sum(weights) or 1
        seg_durations = [dur * (wgt / tot_w) for wgt in weights]

        # Selección de stock relevante por segmento (salvo que el usuario haya subido imágenes)
        visual_list = []
        if not imgs:
            subject = data.get("visual_subject") or ch.get("niche") or ""
            try:
                visual_list = visuals.fetch_visuals(segs, seg_durations, subject, td, vtype, w, h)
            except Exception as e:
                db.log("visuals", f"stock falló: {e}", "warn", vid, ch["id"])
        n_vid = sum(1 for x in visual_list if x.get("type") == "video")
        n_img = sum(1 for x in visual_list if x.get("type") == "image")
        n_grad = sum(1 for x in visual_list if x.get("type") == "gradient")
        db.log("visuals",
               f"{len(imgs)} imágenes propias | stock relevante: {n_vid} vídeos, {n_img} fotos, {n_grad} gradiente",
               vid=vid, cid=ch["id"])

        # Música: propia del video > default global > MUSIC_PATH
        music = None
        murl = v.get("music_url") or (db.get_setting("music_default", {}) or {}).get("url")
        if murl:
            try:
                music = _download(murl, os.path.join(td, "music.mp3"))
            except Exception:
                music = None
        if not music:
            music = os.environ.get("MUSIC_PATH")

        # 5) RENDER
        db.set_status(vid, "rendering")
        out = os.path.join(td, "final.mp4")
        theme = pick_theme(ch)
        if imgs:
            render.compose_from_images(imgs, mp3, ass, out, w=w, h=h, music=music)
        elif visual_list and any(x.get("type") in ("video", "image") for x in visual_list):
            # Timeline sincronizado: cada visual relevante dura lo que su frase narrada
            render.compose_timeline(visual_list, seg_durations, mp3, ass, out,
                                    w=w, h=h, theme=theme, music=music)
        else:
            render.compose_from_gradient(theme, mp3, ass, out,
                                         w=w, h=h, title=None, music=music)
        size = os.path.getsize(out) / 1e6
        db.log("render", f"Render OK {size:.1f}MB", vid=vid, cid=ch["id"])
        db.update_video(vid, duration_seconds=round(dur, 1))

        # Comprimir si supera el límite de Storage (50MB) y subir a Supabase
        if os.path.getsize(out) > 45 * 1024 * 1024:
            comp = os.path.join(td, "final_c.mp4")
            subprocess.run(["ffmpeg", "-y", "-i", out, "-c:v", "libx264",
                            "-crf", "30", "-preset", "veryfast",
                            "-maxrate", "2500k", "-bufsize", "5000k",
                            "-c:a", "aac", "-b:a", "128k", comp],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(comp) and os.path.getsize(comp) > 0:
                out = comp
                db.log("render", f"Comprimido a {os.path.getsize(out)/1e6:.1f}MB", vid=vid, cid=ch["id"])
        try:
            dest = f"{ch.get('slug','canal')}/{vid}.mp4"
            video_url = db.upload_video(out, dest)
            db.update_video(vid, video_url=video_url)
            db.add_asset(vid, "final", url=video_url)
            db.log("upload", "Video subido a Storage (visible en dashboard)", vid=vid, cid=ch["id"])
        except Exception as e:
            db.log("upload", f"No se pudo subir a Storage: {e}", "warn", vid, ch["id"])

        # 5b) MINIATURA (thumbnail viral)
        try:
            thumb = os.path.join(td, "thumb.png")
            render.make_thumbnail(data.get("thumbnail_text") or data["title"], pick_theme(ch), thumb)
            thumb_url = db.upload_media(thumb, f"{ch.get('slug','canal')}/{vid}.png", "image/png")
            db.update_video(vid, thumbnail_url=thumb_url)
            db.add_asset(vid, "thumbnail", url=thumb_url)
        except Exception as e:
            db.log("thumb", f"No se pudo generar miniatura: {e}", "warn", vid, ch["id"])

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
