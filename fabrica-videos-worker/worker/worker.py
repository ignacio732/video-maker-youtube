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

def visual_config(ch):
    """Deriva (modo_motor, estilo_IA) desde el tipo de contenido del canal.
    Tipos amigables: video_real | anime | mix | hibrido. Migrables cuando se quiera."""
    ct = (ch.get("content_type") or "").lower()
    style = ch.get("visual_style") or "realista"
    if ct == "video_real":
        return "stock", "realista"
    if ct == "anime":
        # 100% IA con la estética del canal (si quedó 'realista', usar 'anime')
        return "ai", (style if style != "realista" else "anime")
    if ct == "mix":
        return "hybrid", style
    if ct == "hibrido":
        return "hybrid", "realista"
    # Sin content_type definido: respetar el modo/estilo existentes
    return (ch.get("visual_mode") or "hybrid").lower(), style

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

        # Modo visual del canal: 'hybrid' (stock + IA rellena huecos), 'ai' (todo IA),
        # 'stock' (solo stock). Por defecto híbrido → cobertura visual del 100%.
        subject = data.get("visual_subject") or ch.get("niche") or ""
        mode, ai_style = visual_config(ch)

        visual_list = []
        if not imgs:
            # 1) Stock relevante (salvo modo IA puro)
            if mode in ("stock", "hybrid"):
                try:
                    visual_list = visuals.fetch_visuals(segs, seg_durations, subject, td, vtype, w, h)
                except Exception as e:
                    db.log("visuals", f"stock falló: {e}", "warn", vid, ch["id"])
            if not visual_list:
                visual_list = [{"type": "gradient"} for _ in segs]

            # 2) Imágenes IA (gratis, sin API key): en 'ai' generan todo;
            #    en 'hybrid' rellenan los segmentos que quedaron sin stock (gradiente).
            if mode in ("ai", "hybrid"):
                need = (list(range(len(segs))) if mode == "ai"
                        else [i for i, x in enumerate(visual_list) if x.get("type") == "gradient"])
                if need:
                    try:
                        import aiimg
                        prompts = [(segs[i].get("image_prompt") or subject) for i in need]
                        gen = aiimg.generate_batch(prompts, td, w=w, h=h,
                                                   style=ai_style,
                                                   seed=ch.get("ai_seed"), idx_offset=1000)
                        ok = 0
                        for k, i in enumerate(need):
                            if gen[k]:
                                visual_list[i] = {"type": "image", "path": gen[k]}; ok += 1
                        db.log("ai", f"{ok}/{len(need)} imágenes IA generadas "
                                     f"(modo {mode}, estilo {ai_style})",
                               vid=vid, cid=ch["id"])
                    except Exception as e:
                        db.log("ai", f"IA de imágenes falló: {e}", "warn", vid, ch["id"])

            # Respaldo en modo IA: SOLO si el estilo es fotorrealista se completa con video real.
            # En estilos estilizados (anime, cómic, 3d...) meter footage real rompería la estética,
            # así que se deja gradiente temático (más coherente con el look del canal).
            if mode == "ai" and ai_style in ("realista", "documental") \
               and any(x.get("type") == "gradient" for x in visual_list):
                try:
                    visuals.fill_gaps(visual_list, segs, seg_durations, subject, td, vtype, w, h)
                except Exception as e:
                    db.log("visuals", f"stock de respaldo falló: {e}", "warn", vid, ch["id"])

        n_vid = sum(1 for x in visual_list if x.get("type") == "video")
        n_img = sum(1 for x in visual_list if x.get("type") == "image")
        n_grad = sum(1 for x in visual_list if x.get("type") == "gradient")
        db.log("visuals",
               f"{len(imgs)} propias | {n_vid} vídeos stock, {n_img} imágenes, {n_grad} gradiente (modo {mode})",
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
        video_url = None
        thumb_url = None
        try:
            dest = f"{ch.get('slug','canal')}/{vid}.mp4"
            video_url = db.upload_video(out, dest)
            db.update_video(vid, video_url=video_url)
            db.add_asset(vid, "final", url=video_url)
            db.log("upload", "Video subido a Storage (visible en dashboard)", vid=vid, cid=ch["id"])
        except Exception as e:
            db.log("upload", f"No se pudo subir a Storage: {e}", "warn", vid, ch["id"])

        # 5b) MINIATURA (portada) coherente con la identidad del canal
        try:
            thumb = os.path.join(td, "thumb.png")
            # Fondo: una imagen del propio video (misma estética) o un frame del render
            bg_img = next((x.get("path") for x in visual_list
                           if x.get("type") == "image" and x.get("path")), None)
            if not bg_img:
                frame = os.path.join(td, "thumbframe.jpg")
                try:
                    subprocess.run(["ffmpeg", "-y", "-ss", "1.5", "-i", out,
                                    "-frames:v", "1", frame],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    bg_img = frame if (os.path.exists(frame) and os.path.getsize(frame) > 0) else None
                except Exception:
                    bg_img = None
            render.make_thumbnail(data.get("thumbnail_text") or data["title"], theme, thumb,
                                  accent=ch.get("accent_color"), bg_image=bg_img)
            thumb_url = db.upload_media(thumb, f"{ch.get('slug','canal')}/{vid}.png", "image/png")
            db.update_video(vid, thumbnail_url=thumb_url)
            db.add_asset(vid, "thumbnail", url=thumb_url)
        except Exception as e:
            db.log("thumb", f"No se pudo generar miniatura: {e}", "warn", vid, ch["id"])

        # 6) PUBLICAR — vía Blotato, SOLO al accountId exacto asignado al canal.
        #    Sin cuenta asignada NO se publica (nunca cae a otra cuenta) → evita el
        #    problema de subir al canal equivocado.
        db.set_status(vid, "ready")
        publish_video(vid, ch, data, video_url, thumb_url, vtype=vtype)

def _channel_accounts(ch):
    """Lista de cuentas destino del canal: [{platform, accountId}]. Compat con el campo viejo."""
    accts = ch.get("publish_accounts") or []
    if not accts and ch.get("blotato_account_id"):
        accts = [{"platform": "youtube", "accountId": ch["blotato_account_id"]}]
    # normalizar y descartar entradas incompletas
    out = []
    for a in accts:
        p = (a.get("platform") or "").lower()
        i = str(a.get("accountId") or "").strip()
        if p and i:
            out.append({"platform": p, "accountId": i})
    return out

def publish_video(vid, ch, data, video_url, thumb_url, manual=False, vtype="short"):
    """Publica en TODAS las cuentas asignadas al canal (YouTube/TikTok/IG/FB) vía Blotato."""
    if not manual and not ch.get("publish_enabled"):
        return False  # auto-publicación desactivada en el canal
    accts = _channel_accounts(ch)
    if not accts:
        db.log("publish", "Canal sin cuentas asignadas → no se publica (queda listo)",
               "warn", vid, ch["id"])
        return False
    if not video_url:
        db.log("publish", "Sin video_url en Storage → no se puede publicar", "warn", vid, ch["id"])
        return False
    api_key = (db.get_secret("blotato_api_key") or {}).get("key")
    if not api_key:
        db.log("publish", "Falta la API key de Blotato en secrets", "warn", vid, ch["id"])
        return False

    import blotato
    privacy = ch.get("yt_privacy") or "public"
    db.set_status(vid, "publishing")
    ok_any, yt_url = False, None
    for a in accts:
        plat, acc = a["platform"], a["accountId"]
        # Un video horizontal (long) no va a plataformas verticales (reels/shorts)
        if vtype == "long" and plat in blotato.VERTICAL_PLATFORMS:
            db.log("publish", f"{plat}: se omite (video horizontal, esa plataforma es vertical)",
                   "info", vid, ch["id"])
            continue
        try:
            resp = blotato.publish(api_key, plat, acc, video_url,
                                   title=data.get("title"), description=data.get("description"),
                                   privacy=privacy, thumbnail_url=thumb_url, ai_generated=True)
            url, _ = blotato.extract_url(resp)
            if plat == "youtube":
                yt_url = url
            ok_any = True
            db.log("publish", f"Publicado en {plat} (cuenta {acc})" + (f": {url}" if url else " ✓"),
                   vid=vid, cid=ch["id"])
        except Exception as e:
            db.log("publish", f"{plat} (cuenta {acc}) falló: {e}", "warn", vid, ch["id"])
    if ok_any:
        db.update_video(vid, status="published", youtube_url=yt_url, published_at="now()")
        return True
    db.set_status(vid, "ready", "publicación falló en todas las cuentas")
    return False

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
