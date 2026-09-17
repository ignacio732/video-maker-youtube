"""
WORKER AUTÓNOMO — Fábrica de Videos YouTube.
Corre en GitHub Actions (cron). Flujo por video:
  pending -> scripting -> voicing -> sourcing -> rendering -> ready -> (publishing) -> published

Uso:
  python worker.py            # procesa la cola + autopiloto de canales
  python worker.py --no-auto  # solo procesa lo que ya está en cola
"""
import os, sys, tempfile, traceback, subprocess, math, uuid, random
from datetime import datetime, timezone
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

# Voz por defecto si el canal no tiene una elegida explícitamente, según su idioma.
_DEFAULT_VOICE_BY_LANG = {
    "es": "es-AR-TomasNeural",
    "en": "en-US-AndrewNeural",
    "pt": "pt-BR-AntonioNeural",
}
def _default_voice(channel):
    lang = (channel.get("language") or "es").lower()
    for prefix, voice in _DEFAULT_VOICE_BY_LANG.items():
        if lang.startswith(prefix):
            return voice
    return _DEFAULT_VOICE_BY_LANG["es"]

# Etiquetas reconocidas en un guion propio "con formato" (hook / guion de voz /
# plano y edición / prompt visual completo / cta / control factual). Si el usuario
# pega un guion con estas secciones, sólo se narra lo que corresponde narrar
# (hook + guion de voz + cta); el resto (plano y edición, prompt visual, control
# factual) NUNCA se lee en voz alta — se usan como notas de dirección para buscar
# visuales en vez de inflar el audio con texto de producción.
_SECTION_PATTERNS = [
    ("hook", r"hook\b[^:\n]*"),
    ("guion_voz", r"gui[oó]n\s+de\s+voz|gui[oó]n\s+para\s+la\s+ia"),
    ("plano_edicion", r"plano\s+y\s+edici[oó]n"),
    ("prompt_visual", r"prompt\s+visual[^:\n]*"),
    ("cta", r"cta\b"),
    ("control_factual", r"control\s+factual[^:\n]*|fuentes[^:\n]*"),
    ("miniatura", r"(?:texto\s+de\s+)?miniatura(?:\s*\/\s*portada)?"),
    ("descripcion", r"descripci[oó]n(?:\s+para\s+youtube)?"),
]
_SECTION_RE = _re.compile(
    r"(?im)^\s*(" + "|".join(p for _, p in _SECTION_PATTERNS) + r")\s*:\s*"
)

def parse_user_script(us):
    """
    Si el guion propio sigue el formato con etiquetas, separa:
      - narracion: lo que hay que narrar de verdad (hook + guion de voz + cta)
      - shot_list: la lista de planos ("plano y edición") para anclar los visuales
      - thumb_text: el texto de miniatura/portada, si el usuario lo definió
        explícitamente (si no, el llamador decide un fallback)
      - descripcion: si el usuario ya escribió una descripción de YouTube completa
        (con hashtags y todo), se usa tal cual y no se le pide una nueva al LLM
    Si no hay etiquetas reconocidas, devuelve (us, None, None, None): se narra todo
    tal cual (guion simple, sin este formato).
    """
    matches = list(_SECTION_RE.finditer(us))
    if not matches:
        return us.strip(), None, None, None
    sections = {}
    for i, m in enumerate(matches):
        label = m.group(1).strip().lower()
        key = next((k for k, p in _SECTION_PATTERNS if _re.match(p, label, _re.I)), None)
        start, end = m.end(), (matches[i + 1].start() if i + 1 < len(matches) else len(us))
        content = us[start:end].strip()
        if key and content:
            sections[key] = (sections.get(key, "") + " " + content).strip()
    narracion = " ".join(sections[k] for k in ("hook", "guion_voz", "cta") if sections.get(k)).strip()
    if not narracion:
        narracion = us.strip()
    return narracion, sections.get("plano_edicion"), sections.get("miniatura"), sections.get("descripcion")

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
        us_raw = (v.get("user_script") or "").strip()
        reference_url = v.get("source_url")  # heredada si es un hermano de A/B (ver add_ab_sibling)
        if us_raw:
            narracion, shot_list, thumb_text, own_desc = parse_user_script(us_raw)
            title = v.get("title") or narracion.split("\n")[0][:70]
            sents = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', narracion) if s.strip()]
            segments = [{"text": s, "keywords": []} for s in sents] or [{"text": narracion, "keywords": []}]
            data = {"title": title, "hook": sents[0] if sents else title,
                    "description": own_desc or "", "tags": [], "hashtags": [],
                    "thumbnail_text": (thumb_text or title)[:28], "format": "propio",
                    "segments": segments, "full_text": narracion}
            # Un guion propio no trae keywords en inglés como el generado por IA: sin esto,
            # el motor de stock (fetch_visuals) no tiene con qué anclar la búsqueda y todo
            # el video termina en gradiente. Generamos keywords + visual_subject con el LLM,
            # priorizando la lista de planos ("plano y edición") si el usuario la incluyó.
            try:
                if shot_list:
                    shots = [s.strip() for s in _re.split(r"[;\n]+", shot_list) if s.strip()]
                    vis_kw = llm.visuals_for_own_script(shots, ch.get("niche"))
                    if vis_kw.get("keywords"):
                        for i, seg in enumerate(segments):
                            seg["keywords"] = vis_kw["keywords"][i % len(vis_kw["keywords"])]
                else:
                    vis_kw = llm.visuals_for_own_script([s["text"] for s in segments], ch.get("niche"))
                    for seg, kws in zip(segments, vis_kw.get("keywords") or []):
                        seg["keywords"] = kws
                if vis_kw.get("visual_subject"):
                    data["visual_subject"] = vis_kw["visual_subject"]
            except Exception as e:
                db.log("script", f"No se pudieron generar keywords para el guion propio: {e}",
                       "warn", vid, ch["id"])
            # Un guion propio tampoco trae descripción/tags/hashtags de YouTube — antes
            # quedaba la descripción vacía (sin SEO/GEO, sin hashtag de marca). Se genera
            # con el LLM a partir de la narración real, sin tocar el guion en sí — SALVO
            # que el usuario ya haya escrito su propia descripción completa (own_desc),
            # en cuyo caso se respeta tal cual y no se le pide una nueva al LLM.
            if not own_desc:
                try:
                    meta = llm.metadata_for_own_script(narracion, ch, title=v.get("title"), thumb_text=thumb_text)
                    data["title"] = meta.get("title") or data["title"]
                    data["description"] = meta.get("description") or data["description"]
                    data["tags"] = meta.get("tags") or data["tags"]
                    data["hashtags"] = meta.get("hashtags") or data["hashtags"]
                except Exception as e:
                    db.log("script", f"No se pudo generar descripción/SEO para el guion propio: {e}",
                           "warn", vid, ch["id"])
            db.log("script", "Usando guion propio del usuario"
                   + (" (con plano y edición separado)" if shot_list else ""),
                   vid=vid, cid=ch["id"])
        elif v.get("remix_of") or v.get("remix_query"):
            # "Remix" de un video viral de referencia: mismo tema y mismo ritmo de
            # hook que el original, pero un guion nuevo (ver llm.remix_script). Si
            # algo falla acá (sin subtítulos, sin resultados de búsqueda), se cae al
            # camino normal de generación por IA en vez de perder el video.
            import transcript
            data = None
            try:
                candidates = []
                if v.get("remix_of"):
                    candidates = [{"url": v["remix_of"], "title": None, "view_count": None}]
                else:
                    kind = v.get("remix_kind") or vtype
                    min_views = v.get("remix_min_views") or 1_000_000
                    candidates = transcript.search_videos(v["remix_query"], kind=kind,
                                                          min_views=min_views, max_results=5)
                    if not candidates:
                        db.log("remix", f"Sin resultados con >= {min_views:,} vistas para "
                                       f"\"{v['remix_query']}\"; se genera normal", "warn", vid, ch["id"])
                # Los videos MUY populares suelen tener más control anti-bot de YouTube y
                # a veces bloquean la extracción del guion — si el más visto falla, se
                # prueba con el siguiente candidato en vez de perder el remix entero.
                lang = (ch.get("language") or "es")
                langs = (lang, "es", "es-419", "en") if lang != "en" else ("en", "es")
                for cand in candidates:
                    try:
                        info = transcript.get_video_info(cand["url"])
                        segs = transcript.extract_transcript(cand["url"], langs=langs)
                        if not segs:
                            # yt-dlp no pudo (sin subtítulos o bloqueo de YouTube) —
                            # último intento con Supadata (100 créditos gratis/mes,
                            # no depende de la IP) antes de descartar este candidato.
                            supadata_key = (db.get_secret("supadata_api_key") or {}).get("key")
                            if supadata_key:
                                segs = transcript.supadata_transcript(cand["url"], supadata_key,
                                                                      lang=lang if lang != "es" else None)
                                if segs:
                                    db.log("remix", "yt-dlp no pudo; se usó Supadata como red de contención",
                                          vid=vid, cid=ch["id"])
                        if not segs:
                            db.log("remix", f"Sin subtítulos disponibles: \"{cand.get('title') or cand['url']}\"",
                                  "warn", vid, ch["id"])
                            continue
                        if v.get("remix_save_transcript"):
                            db.update_video(vid, original_transcript=" ".join(s["text"] for s in segs))
                        data = llm.remix_script(ch, vtype, info["title"] if info else "",
                                               info["view_count"] if info else 0, segs)
                        # Nota: a diferencia de una noticia (donde la foto de portada es
                        # material editorial neutro), la miniatura de un video de YouTube
                        # es la imagen de marca de otro creador — no la usamos como hook
                        # para evitar problemas de autoría/spam. El remix se ilustra con
                        # el stock/IA normal, como cualquier otro video.
                        db.log("remix", f"Guion remixado sobre \"{(info or {}).get('title') or cand['url']}\"",
                              vid=vid, cid=ch["id"])
                        break
                    except Exception as e:
                        db.log("remix", f"Falló con \"{cand.get('title') or cand['url']}\" ({e}); "
                                       "probando el siguiente candidato" if cand is not candidates[-1]
                                       else f"Falló con \"{cand.get('title') or cand['url']}\" ({e})",
                              "warn", vid, ch["id"])
                        continue
            except Exception as e:
                db.log("remix", f"Falló el remix ({e}); se genera normal", "warn", vid, ch["id"])
            if data is None:
                # Fallback: generación normal, usando el título original como semilla si lo tenemos.
                recent_titles = db.get_recent_titles(ch["id"], 40)
                top_performers = db.get_top_performers(ch["id"], 3)
                visual_learning = db.get_visual_learnings(ch["id"])
                data = llm.generate(ch, vtype, seed_title=v.get("title"), recent_titles=recent_titles,
                                    top_performers=top_performers, visual_learning=visual_learning)
        else:
            trend_topics = None
            reference_url = None
            if not v.get("title"):
                try:
                    found = trends.for_channel(ch, 8)
                    trend_topics = [t["topic"] for t in found]
                    for t in found[:5]:
                        db.add_trend(ch["id"], t["topic"], t["source"], t.get("category"), t.get("url"))
                    cats = ", ".join(sorted(set(t.get("category") or "" for t in found[:5])))
                    db.log("trends", f"{len(trend_topics)} tendencias ({cats})", vid=vid, cid=ch["id"])
                    # Canal de sitios de referencia (noticias reales): guardamos la URL de la
                    # nota más relevante para, más abajo, sacarle una captura real de pantalla
                    # y mezclarla con el resto del material del video (autenticidad).
                    if ch.get("reference_sites") and found and found[0].get("url"):
                        reference_url = found[0]["url"]
                except Exception as e:
                    db.log("trends", f"sin tendencias: {e}", "warn", vid, ch["id"])
            data = None
            recent_titles = db.get_recent_titles(ch["id"], 40)
            top_performers = db.get_top_performers(ch["id"], 3)
            visual_learning = db.get_visual_learnings(ch["id"])
            research_context = None
            if v.get("seed_trend_id"):
                # Video pedido desde una tendencia puntual (dashboard → "🎬 Generar"):
                # investigar de verdad la noticia (bajar el artículo real), no solo
                # narrar el titular a ciegas.
                trend_row = db.get_trend(v["seed_trend_id"])
                if trend_row and trend_row.get("url"):
                    research_context = trends.fetch_article_text(trend_row["url"])
                    reference_url = trend_row["url"]
                    db.log("trends",
                           "Investigación real del artículo OK" if research_context
                           else "No se pudo leer el artículo original (paywall o formato no soportado); "
                                "se sigue solo con el titular",
                           "info" if research_context else "warn", vid, ch["id"])
            try:
                data = llm.generate(ch, vtype, seed_title=v.get("title"), trends=trend_topics,
                                    recent_titles=recent_titles, top_performers=top_performers,
                                    visual_learning=visual_learning, research_context=research_context)
            except Exception as e:
                if trend_topics:
                    # Si falló con tendencias (ej. una tendencia sensible que el LLM rechaza,
                    # o cualquier otro problema puntual del prompt), reintentar sin tendencias
                    # en vez de perder el video entero.
                    db.log("script", f"Guion con tendencias falló ({e}); reintentando sin tendencias",
                           "warn", vid, ch["id"])
                    data = llm.generate(ch, vtype, seed_title=v.get("title"), trends=None,
                                        recent_titles=recent_titles, top_performers=top_performers,
                                        visual_learning=visual_learning, research_context=research_context)
                else:
                    raise
        db.add_script(vid, data["full_text"], data["segments"])
        db.add_idea(ch["id"], data["title"], data.get("hook"), None, vtype, "used")
        db.update_video(vid, title=data["title"],
                        description=data.get("description"), tags=data.get("tags", []))
        db.log("script", f"Guion listo: {data['title']}", vid=vid, cid=ch["id"])

        # A/B de hooks: si el canal lo tiene activado, genera un video "hermano" con
        # el mismo cuerpo pero un hook distinto, y fuerza a ambos a salir como reel
        # de prueba (además del cupo normal de trial_reels_per_day) para comparar cuál
        # engancha más. Solo para guiones generados por IA (no un guion propio subido a
        # mano) y solo una vez por video (no encadenar hermanos de hermanos).
        if (ch.get("ab_hook_testing_enabled") and vtype == "short"
                and not us_raw and not v.get("ab_group_id")):
            try:
                alt = llm.alt_hook(data, ch)
                if alt:
                    group_id = str(uuid.uuid4())
                    db.update_video(vid, ab_group_id=group_id, force_trial=True)
                    body = " ".join(s.get("text", "") for s in data["segments"][1:]) or data["full_text"]
                    sibling_script = (f"Hook: {alt}\nGuion de voz: {body}\n"
                                     f"Miniatura: {(data.get('thumbnail_text') or data['title'])[:28]}")
                    db.add_ab_sibling(ch["id"], vtype, sibling_script, group_id, source_url=reference_url)
                    db.log("ab_test", f"Hook alternativo generado, hermano encolado: \"{alt[:60]}\"",
                          vid=vid, cid=ch["id"])
            except Exception as e:
                db.log("ab_test", f"No se pudo generar el hermano A/B: {e}", "warn", vid, ch["id"])

        # 2) VOZ + timings
        db.set_status(vid, "voicing")
        mp3 = os.path.join(td, "voz.mp3")
        words = render.synth_voice(data["full_text"], ch.get("voice") or _default_voice(ch), mp3)
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
            # ids de stock usados en videos recientes de este canal, para no repetir
            # la misma foto/video entre un video y otro (nichos con poco stock, ej. eSIM).
            avoid_ids = db.get_recent_visual_ids(ch["id"])
            # 1) Stock relevante (salvo modo IA puro)
            if mode in ("stock", "hybrid"):
                try:
                    visual_list = visuals.fetch_visuals(segs, seg_durations, subject, td, vtype, w, h,
                                                        avoid_ids=avoid_ids)
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
                                visual_list[i] = {"type": "image", "path": gen[k],
                                                  "source": "pollinations", "ref": prompts[k][:80]}
                                ok += 1
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
                    visuals.fill_gaps(visual_list, segs, seg_durations, subject, td, vtype, w, h,
                                      avoid_ids=avoid_ids)
                except Exception as e:
                    db.log("visuals", f"stock de respaldo falló: {e}", "warn", vid, ch["id"])

            db.record_visual_ids(ch["id"], visual_list)

            # 3) Captura real de la fuente (autenticidad): si el video sale de una
            # noticia puntual (reference_sites o tendencia elegida a mano), sacamos
            # UNA captura de pantalla real de esa página y la ponemos en el hook —
            # mezclada con el resto del material (stock/IA), no en reemplazo de todo.
            if reference_url and visual_list:
                try:
                    import screenshot
                    shot_path = os.path.join(td, "shot_0.jpg")
                    got = screenshot.get_main_image(reference_url, shot_path)
                    if got:
                        visual_list[0] = {"type": "image", "path": got, "source": "main_image",
                                          "ref": reference_url[:120]}
                        db.update_video(vid, source_url=reference_url)
                        db.log("visuals", f"Imagen principal real de la fuente OK: {reference_url}",
                               vid=vid, cid=ch["id"])
                    else:
                        db.log("visuals", f"No se encontró imagen principal de la fuente ({reference_url}); "
                                          "se sigue con el resto del material normal",
                               "warn", vid, ch["id"])
                except Exception as e:
                    db.log("visuals", f"Imagen principal de la fuente falló: {e}", "warn", vid, ch["id"])

        n_vid = sum(1 for x in visual_list if x.get("type") == "video")
        n_img = sum(1 for x in visual_list if x.get("type") == "image")
        n_grad = sum(1 for x in visual_list if x.get("type") == "gradient")
        db.log("visuals",
               f"{len(imgs)} propias | {n_vid} vídeos stock, {n_img} imágenes, {n_grad} gradiente (modo {mode})",
               vid=vid, cid=ch["id"])
        if not imgs and visual_list:
            db.add_segment_visuals(vid, segs, visual_list)

        # Música: propia del video > biblioteca por estilo del canal > default global > MUSIC_PATH
        music = None
        music_credit = None
        murl = v.get("music_url")
        if not murl:
            mood = ch.get("music_mood") or "curioso"
            tracks = (db.get_setting("music_library", {}) or {}).get(mood) or []
            if tracks:
                import random
                pick = random.choice(tracks)
                murl = pick["url"]
                if pick.get("license") != "CC0":  # CC BY exige crédito (no hablado, en la descripción)
                    music_credit = f'Music: "{pick["name"]}" by {pick["artist"]} ({pick["license"]}) - {pick["credit_url"]}'
        if not murl:
            murl = (db.get_setting("music_default", {}) or {}).get("url")
        if murl:
            try:
                music = _download(murl, os.path.join(td, "music.mp3"))
            except Exception:
                music = None
        if not music:
            music = os.environ.get("MUSIC_PATH")
        if music and music_credit:
            data["description"] = (data.get("description") or "").rstrip() + f"\n\n{music_credit}"
            db.update_video(vid, description=data["description"])

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
                                  accent=ch.get("accent_color"), bg_image=bg_img, vtype=vtype)
            thumb_url = db.upload_media(thumb, f"{ch.get('slug','canal')}/{vid}.png", "image/png")
            style = "ai_image" if (bg_img and "img_" in os.path.basename(bg_img)) else ("frame" if bg_img else "gradient")
            db.update_video(vid, thumbnail_url=thumb_url, thumbnail_style=style)
            db.add_asset(vid, "thumbnail", url=thumb_url)
        except Exception as e:
            db.log("thumb", f"No se pudo generar miniatura: {e}", "warn", vid, ch["id"])

        # 6) PUBLICAR — vía Blotato, SOLO al accountId exacto asignado al canal.
        #    Sin cuenta asignada NO se publica (nunca cae a otra cuenta) → evita el
        #    problema de subir al canal equivocado.
        #    Canales AUTÓNOMOS no publican acá: los publica run_scheduled_publishing()
        #    respetando el ritmo configurado (si no, se publicarían todos juntos apenas
        #    terminan de renderizarse, sin ninguna cadencia real).
        db.set_status(vid, "ready")
        if not ch.get("autonomous"):
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

    # Reels de prueba (Instagram): el canal define a mano cuántos por día quiere
    # (trial_reels_per_day, sin tope fijo del sistema) y con qué estrategia de
    # graduación. Si todavía no se llegó al tope de hoy, este video sale como trial.
    # Un video de un test A/B de hooks (force_trial) SIEMPRE sale como trial, sin
    # contar contra ese cupo diario — es una prueba deliberada, no el cupo normal.
    use_trial = False
    if vtype == "short":
        vrow = db.get_video(vid) or {}
        if vrow.get("force_trial"):
            use_trial = True
        elif ch.get("trial_reels_enabled"):
            per_day = int(ch.get("trial_reels_per_day") or 0)
            if per_day > 0 and db.count_trial_reels_today(ch["id"]) < per_day:
                use_trial = True

    for a in accts:
        plat, acc = a["platform"], a["accountId"]
        # Un video horizontal (long) no va a plataformas verticales (reels/shorts)
        if vtype == "long" and plat in blotato.VERTICAL_PLATFORMS:
            db.log("publish", f"{plat}: se omite (video horizontal, esa plataforma es vertical)",
                   "info", vid, ch["id"])
            continue
        trial = None
        if plat == "instagram" and use_trial:
            trial = {"graduationStrategy": ch.get("trial_graduation_strategy") or "MANUAL"}
        try:
            resp = blotato.publish(api_key, plat, acc, video_url,
                                   title=data.get("title"), description=data.get("description"),
                                   privacy=privacy, thumbnail_url=thumb_url, ai_generated=True,
                                   trial=trial)
            url, _ = blotato.extract_url(resp)
            if plat == "youtube":
                yt_url = url
            ok_any = True
            fb = " [sin miniatura personalizada: cuenta no verificada por teléfono]" if resp.get("_thumbnail_fallback") else ""
            trial_note = f" — REEL DE PRUEBA ({trial['graduationStrategy']})" if trial else ""
            db.log("publish", f"Publicado en {plat} (cuenta {acc})" + (f": {url}" if url else " ✓") + fb + trial_note,
                   vid=vid, cid=ch["id"])
            if trial:
                db.update_video(vid, published_as_trial=True)
            db.add_publication(vid, ch["id"], plat, acc, is_trial=bool(trial), status="ok", post_url=url)
        except Exception as e:
            db.log("publish", f"{plat} (cuenta {acc}) falló: {e}", "warn", vid, ch["id"])
            db.add_publication(vid, ch["id"], plat, acc, is_trial=bool(trial), status="failed", error=str(e))
    if ok_any:
        db.update_video(vid, status="published", youtube_url=yt_url, published_at="now()")
        return True
    db.set_status(vid, "ready", "publicación falló en todas las cuentas")
    return False

def _autopilot_remix(ch):
    """Remix automático: si el canal lo tiene activado, busca un video viral del
    nicho del canal y encola un remix al ritmo configurado (ej. 2 por semana o 1
    por día) — sin que nadie tenga que abrir el modal a mano cada vez."""
    if not ch.get("auto_remix_enabled"):
        return
    count = max(0, ch.get("auto_remix_count") or 0)
    days = max(1, ch.get("auto_remix_period_days") or 7)
    if count <= 0:
        return
    interval_h = 24.0 * days / count
    last = ch.get("last_remix_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            elapsed_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            if elapsed_h < interval_h:
                return
        except Exception:
            pass
    kws = ch.get("keywords") or []
    query = random.choice(kws) if kws else (ch.get("niche") or "").split(",")[0].strip()
    if not query:
        db.log("autopilot", "Remix automático: canal sin keywords ni nicho para buscar", "warn", cid=ch["id"])
        return
    fmt = ch.get("format") or "both"
    vtype = "long" if fmt == "long" else "short" if fmt == "shorts" else random.choice(["short", "long"])
    min_views = ch.get("auto_remix_min_views") or 1_000_000
    db.enqueue_remix(ch["id"], vtype, remix_query=query, remix_kind=vtype, remix_min_views=min_views)
    db.update_channel(ch["id"], last_remix_at=datetime.now(timezone.utc).isoformat())
    db.log("autopilot", f"Remix automático encolado: \"{query}\" ({vtype}, mín. {min_views:,} vistas)",
          cid=ch["id"])

def autopilot():
    """Encola videos según el modo del canal:
       - Modo simple (autonomous=false, default): 1 video en curso por vez, como antes.
       - Modo AUTÓNOMO (autonomous=true): sostiene una cola suficiente para cumplir el
         ritmo configurado (videos_per_period cada period_days) de forma autoregulada,
         sin depender de que un humano dispare nada."""
    for ch in db.get_active_channels():
        try:
            _autopilot_remix(ch)
        except Exception as e:
            db.log("autopilot", f"Remix automático falló: {e}", "warn", cid=ch["id"])
        if ch.get("autonomous"):
            _autopilot_paced(ch)
            continue
        if db.channel_has_open_video(ch["id"]):
            continue
        fmt = ch.get("format") or "both"
        vtype = "long" if fmt == "long" else "short"  # 'both' arranca por short
        db.enqueue_video(ch["id"], vtype)
        db.log("autopilot", f"Encolado {vtype} para '{ch['name']}'", cid=ch["id"])

def _autopilot_paced(ch):
    """Canal autónomo: calcula cuántos SHORTS y cuántos LARGOS hacen falta en la cola
    por separado (antes era un solo contador con una regla fija de 1 largo cada 3 —
    ahora cada tipo tiene su propia cuota configurable, ej. '2 shorts + 1 largo por día').
    Colchón de unos días — nunca genera de más ni se queda corto."""
    buffer_days = 3
    days = max(1, ch.get("period_days") or 7)
    shorts_target = max(0, ch.get("shorts_per_period") if ch.get("shorts_per_period") is not None
                        else (ch.get("videos_per_period") or 3))
    longs_target = max(0, ch.get("longs_per_period") or 0)
    open_short = db.count_open_videos(ch["id"], vtype="short")
    open_long = db.count_open_videos(ch["id"], vtype="long")
    made = 0
    max_per_run = 5  # tope de seguridad: nunca encolar de una un montón por un cálculo raro
    for vtype, target, open_n in (("short", shorts_target, open_short), ("long", longs_target, open_long)):
        if target <= 0:
            continue
        rate_per_day = target / days
        target_queue = max(1, math.ceil(rate_per_day * buffer_days))
        n = 0
        while open_n + n < target_queue and made < max_per_run:
            db.enqueue_video(ch["id"], vtype)
            n += 1; made += 1
    if made:
        db.log("autopilot",
               f"Canal autónomo '{ch['name']}': encolados {made} video(s) — objetivo "
               f"{shorts_target} short(s) + {longs_target} largo(s) cada {days}d", cid=ch["id"])

def _publish_next_ready(ch, vtype, per, days, last_field):
    """Publica el próximo video 'listo' de un tipo (short/long) si ya corresponde según
    su propia cuota — cada tipo tiene su propio contador y su propio último-publicado,
    para poder sostener mezclas como '2 shorts + 1 largo por día' de verdad."""
    if per <= 0:
        return
    interval_h = 24.0 * days / per
    last = ch.get(last_field)
    due = True
    last_dt = None
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            elapsed_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            due = elapsed_h >= interval_h
        except Exception:
            due = True
            elapsed_h = None
    if not due:
        return
    # Sesgo suave por "mejor horario": si ya hay suficiente historial de vistas por
    # hora de publicación, espera a esa ventana (±2h) en vez de publicar apenas se
    # cumple el intervalo — salvo que ya se pase 1.5x el intervalo (para no trabarse
    # esperando el horario ideal si algo lo corrió). Sin datos suficientes, publica
    # como siempre (por elapsed time nomás).
    best_h = db.best_hour_utc(ch["id"])
    if best_h is not None and last_dt is not None and elapsed_h is not None \
       and elapsed_h < interval_h * 1.5:
        now_h = datetime.now(timezone.utc).hour
        gap = min((now_h - best_h) % 24, (best_h - now_h) % 24)
        if gap > 2:
            if datetime.now(timezone.utc).minute < 15:  # solo una vez por hora, no cada 15 min
                db.log("publish", f"{vtype}: esperando la mejor hora para publicar "
                                  f"(~{best_h}:00 UTC, ahora son las {now_h}:00 UTC)", cid=ch["id"])
            return  # todavía no es la hora que mejor rinde para este canal — esperar
    # Varios candidatos (no solo 1): si el más viejo está roto (sin video_url, ej. una
    # fila que quedó mal por algún corte a mitad de proceso), antes se reintentaba
    # SIEMPRE el mismo roto cada 15 min y nunca se avanzaba, bloqueando el canal entero.
    candidates = db.get_ready_videos(ch["id"], vtype=vtype, limit=5)
    for cand in candidates:
        if not cand.get("video_url"):
            db.set_status(cand["id"], "failed",
                          error="video_url nulo detectado en publicación programada; se salta")
            db.log("publish", f"'{cand.get('title')}' sin video_url — se marca fallido y se sigue",
                   "warn", cand["id"], ch["id"])
            continue
        data = {"title": cand.get("title"), "description": cand.get("description") or ""}
        ok = publish_video(cand["id"], ch, data, cand.get("video_url"), cand.get("thumbnail_url"),
                           manual=True, vtype=vtype)
        if ok:
            db.update_channel(ch["id"], **{last_field: datetime.now(timezone.utc).isoformat()})
            db.log("publish", f"Publicación programada ({vtype}, ritmo {per}/{days}d, cada ~{interval_h:.1f}h)",
                   vid=cand["id"], cid=ch["id"])
        return  # un solo intento de este tipo por corrida, roto o no

def run_scheduled_publishing():
    """Para canales autónomos con auto-publicar activo: en vez de publicar apenas
    termina de renderizarse (lo cual tira todo junto si se generaron varios seguidos),
    publica el próximo 'listo' de cada tipo cuando corresponde según SU propia cuota
    (shorts y largos se sostienen por separado, ej. '2 shorts + 1 largo por día')."""
    for ch in db.get_active_channels():
        if not ch.get("autonomous") or not ch.get("publish_enabled"):
            continue
        shorts_target = ch.get("shorts_per_period") if ch.get("shorts_per_period") is not None \
            else (ch.get("videos_per_period") or 3)
        longs_target = ch.get("longs_per_period") or 0
        days = max(1, ch.get("period_days") or 7)
        _publish_next_ready(ch, "short", max(0, shorts_target), days, "last_short_published_at")
        _publish_next_ready(ch, "long", max(0, longs_target), days, "last_long_published_at")

def refresh_global_trends():
    """Refresca las tendencias globales por rubro (para la vista del dashboard)."""
    try:
        found = trends.discover_global(per_cat=3)
        if not found:
            return
        db.clear_global_trends()
        for t in found[:60]:
            db.add_trend(None, t["topic"], t["source"], t.get("category"), t.get("url"))
        db.log("trends", f"{len(found[:60])} tendencias globales refrescadas por rubro")
    except Exception as e:
        db.log("trends", f"refresh global falló: {e}", "warn")

def sync_analytics():
    """Resuelve el id de analytics de Blotato para publicaciones recientes (matcheando
    por post_url) y guarda un snapshot de vistas/likes/comentarios en video_metrics —
    la tabla que get_top_performers() y get_visual_learnings() ya leían pero que hasta
    ahora nadie alimentaba. Corre una vez por corrida del worker, es liviano (1-2 GETs)."""
    api_key = (db.get_secret("blotato_api_key") or {}).get("key")
    if not api_key:
        return
    import blotato
    pending = db.get_unresolved_publications(limit=200)
    if not pending:
        return
    by_url = {p["post_url"]: p for p in pending if p.get("post_url")}
    if not by_url:
        return
    since = min(p["published_at"] for p in pending)
    try:
        items = blotato.list_top_performing(api_key, since=since, limit=100)
    except Exception as e:
        db.log("analytics", f"Error consultando Blotato analytics: {e}", "warn")
        return
    matched = 0
    for it in items:
        url = it.get("postUrl")
        pub = by_url.get(url)
        if not pub:
            continue
        db.resolve_publication(pub["id"], it.get("id"))
        m = (it.get("latestMetrics") or {}).get("metrics") or {}
        def _num(key):
            v = m.get(key)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        db.add_video_metric(pub["video_id"], views=_num("viewsCount"),
                            likes=_num("likesCount"), comments=_num("commentsCount"),
                            source="blotato")
        matched += 1
    if matched:
        db.log("analytics", f"Sincronizadas métricas de {matched} publicación(es)")

def _repurpose_overperformers():
    """Si un video superó por mucho el promedio de vistas de su canal (2.5x, con
    muestra mínima), encola una variante nueva sobre el mismo ángulo — para
    aprovechar lo que ya demostró que funciona en vez de solo notarlo."""
    for ch in db.get_active_channels():
        rows = db.get_channel_metrics(ch["id"])
        if len(rows) < 4:
            continue
        avg = sum(r["views"] for r in rows) / len(rows)
        if avg <= 0:
            continue
        for r in rows:
            if r.get("repurposed") or (r["views"] or 0) < avg * 2.5:
                continue
            seed = f"Otro ángulo sobre este tema, que ya funcionó muy bien antes: {r['title']}"
            db.enqueue_video(ch["id"], r.get("type") or "short", title=seed)
            db.update_video(r["video_id"], repurposed=True)
            db.log("boost",
                   f"'{r['title']}' hizo {r['views']} vistas ({r['views']/avg:.1f}x el promedio "
                   f"del canal) — se encoló una variante del mismo ángulo",
                   cid=ch["id"])

def main():
    if "--no-auto" not in sys.argv:
        try:
            autopilot()
        except Exception as e:
            db.log("autopilot", f"Error: {e}", "error")
        try:
            run_scheduled_publishing()
        except Exception as e:
            db.log("publish", f"Error en publicación programada: {e}", "error")
        try:
            sync_analytics()
        except Exception as e:
            db.log("analytics", f"Error: {e}", "error")
        try:
            _repurpose_overperformers()
        except Exception as e:
            db.log("boost", f"Error: {e}", "error")
        refresh_global_trends()

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
