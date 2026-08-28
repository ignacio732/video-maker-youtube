"""
Motor de render de la Fábrica de Videos — 100% gratis (edge-tts + ffmpeg + Pillow).
Reutilizable por el worker autónomo y por el generador de demo.
"""
import os, re, asyncio, subprocess, tempfile, math, textwrap
import edge_tts
from PIL import Image

PROXY = os.environ.get("EDGE_TTS_PROXY")  # ej http://127.0.0.1:38013 en sandbox; vacío en prod
FONT = os.environ.get("SUB_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

# ---------------------------------------------------------------- VOZ + TIMINGS
def _split_sentence(sent, start, dur):
    """Reparte la duración de una frase entre sus palabras (proporcional a longitud)."""
    toks = [t for t in sent.split() if t.strip()]
    if not toks:
        return []
    weights = [len(t) + 1 for t in toks]
    total = sum(weights)
    out, t = [], start
    for tok, wgt in zip(toks, weights):
        d = dur * (wgt / total)
        out.append({"text": tok, "start": t, "end": t + d})
        t += d
    return out

async def _synth(text, voice, out_mp3):
    words, sentences = [], []
    comm = edge_tts.Communicate(text, voice, proxy=PROXY)
    with open(out_mp3, "wb") as f:
        async for ch in comm.stream():
            t = ch.get("type")
            if t == "audio":
                f.write(ch["data"])
            elif t == "WordBoundary":
                words.append({"text": ch["text"],
                              "start": ch["offset"] / 1e7,
                              "end": (ch["offset"] + ch["duration"]) / 1e7})
            elif t == "SentenceBoundary":
                sentences.append({"text": ch["text"],
                                  "start": ch["offset"] / 1e7,
                                  "dur": ch["duration"] / 1e7})
    if words:
        return words
    # Fallback: expandir frases a palabras con timing proporcional
    for s in sentences:
        words.extend(_split_sentence(s["text"], s["start"], s["dur"]))
    return words

def synth_voice(text, voice, out_mp3):
    """Genera mp3 y devuelve lista de palabras con start/end en segundos."""
    return asyncio.run(_synth(text, voice, out_mp3))

# ------------------------------------------------------------------- SUBTÍTULOS
def _group_words(words, max_chars=16):
    """Agrupa palabras en líneas cortas de subtítulo tipo short viral."""
    lines, cur, start = [], [], None
    for w in words:
        if start is None:
            start = w["start"]
        tentative = (" ".join([x["text"] for x in cur] + [w["text"]])).strip()
        if cur and len(tentative) > max_chars:
            lines.append({"text": " ".join(x["text"] for x in cur),
                          "start": start, "end": cur[-1]["end"]})
            cur, start = [w], w["start"]
        else:
            cur.append(w)
    if cur:
        lines.append({"text": " ".join(x["text"] for x in cur),
                      "start": start, "end": cur[-1]["end"]})
    return lines

def _ass_time(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"

def build_ass(words, out_ass, w=1080, h=1920, fontsize=None, margin_v=None):
    """
    Subtítulos VIRALES estilo Hormozi/TikTok: palabra por palabra, MAYÚSCULAS,
    fuente grande y gruesa, contorno negro, 'pop' de escala y palabra clave en amarillo.
    Aprovecha los timings por palabra derivados del TTS.
    """
    WHITE = "&H00FFFFFF"
    YELLOW = "&H0000FFFF"      # énfasis (BGR)
    fontsize = fontsize or int(w * (0.095 if w < h else 0.06))
    margin_v = margin_v or int(h * 0.34)
    outline = max(6, int(fontsize * 0.11))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Main,DejaVu Sans,{fontsize},{WHITE},&H00000000,&H90000000,-1,{outline},3,2,40,40,{margin_v}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    body = []
    n = len(words)
    for i, wd in enumerate(words):
        start = wd["start"]
        end = words[i + 1]["start"] if i + 1 < n else wd["end"]
        if end <= start:
            end = start + 0.25
        clean = re.sub(r"[^0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", "", wd["text"])
        color = YELLOW if len(clean) >= 6 else WHITE
        txt = wd["text"].upper()
        # pop de escala + fade rápido; palabra larga => amarillo (énfasis)
        tag = (f"{{\\an2\\c{color}\\fad(30,20)"
               f"\\t(0,80,\\fscx116\\fscy116)\\t(80,150,\\fscx100\\fscy100)}}")
        body.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Main,,0,0,0,,{tag}{txt}")
    with open(out_ass, "w") as f:
        f.write(header + "\n".join(body) + "\n")
    return out_ass

def _hex_rgb(s, default=(255, 61, 87)):
    try:
        s = (s or "").lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return default

def make_thumbnail(text, theme, out_png, w=1280, h=720, accent=None, bg_image=None):
    """
    Miniatura 1280x720 COHERENTE con el canal: fondo con una imagen del propio video
    (misma estética) o gradiente temático, + barra y palabra clave en el color de acento
    del canal + texto grande con contorno.
    """
    from PIL import ImageDraw, ImageFont, ImageFilter
    acc = _hex_rgb(accent)
    if bg_image and os.path.exists(bg_image):
        # Usar una imagen del video como fondo (recorte 16:9 + oscurecido para legibilidad)
        try:
            im = Image.open(bg_image).convert("RGB")
            sw, sh = im.size
            scale = max(w / sw, h / sh)
            im = im.resize((int(sw * scale), int(sh * scale)))
            left = (im.size[0] - w) // 2; top = (im.size[1] - h) // 2
            im = im.crop((left, top, left + w, top + h))
            # scrim oscuro (degradado desde abajo) para que el texto resalte
            dark = Image.new("RGB", (w, h), (0, 0, 0))
            mask = Image.new("L", (w, h), 0)
            md = ImageDraw.Draw(mask)
            for y in range(h):
                md.line([(0, y), (w, y)], fill=int(60 + 150 * (y / h)))
            img = Image.composite(dark, im, mask)
            bg = None
        except Exception:
            img = Image.open(make_gradient_bg(theme, out_png + ".bg.png", w, h)).convert("RGB"); bg = out_png + ".bg.png"
    else:
        bg = make_gradient_bg(theme, out_png + ".bg.png", w, h)
        img = Image.open(bg).convert("RGB")
    d = ImageDraw.Draw(img)
    # barra de acento (color del canal)
    d.rectangle([0, 0, int(w * 0.02), h], fill=acc)
    words = (text or "").upper().split()
    max_w = w * 0.90  # nunca ocupar todo el ancho: margen real a los costados

    def _wrap(font):
        """Arma líneas midiendo el ANCHO REAL en píxeles (no cantidad de caracteres),
        para que el texto nunca se corte fuera del cuadro (bug real que se vio en
        producción: 'NO ES SOLO EL DÍA 14' se cortaba a la derecha)."""
        lines, cur = [], ""
        for wd in words:
            trial = (cur + " " + wd).strip()
            if cur and d.textbbox((0, 0), trial, font=font)[2] > max_w:
                lines.append(cur); cur = wd
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    # Elegir el tamaño de fuente más grande que, tras el ajuste por ancho real,
    # entre en 3 líneas o menos Y en el alto disponible.
    fs = int(h * 0.20)
    try:
        font = ImageFont.truetype(FONT, fs)
    except Exception:
        font = ImageFont.load_default()
    lines = _wrap(font)
    while (len(lines) > 3 or len(lines) * fs * 1.15 > h * 0.85) and fs > int(h * 0.08):
        fs = int(fs * 0.9)
        try:
            font = ImageFont.truetype(FONT, fs)
        except Exception:
            break
        lines = _wrap(font)
    lines = lines[:3]
    total_h = len(lines) * fs * 1.15
    y = (h - total_h) / 2
    for ln in lines:
        bb = d.textbbox((0, 0), ln, font=font)
        tw = bb[2] - bb[0]
        x = (w - tw) / 2
        # contorno
        for dx in (-4, 0, 4):
            for dy in (-4, 0, 4):
                d.text((x + dx, y + dy), ln, font=font, fill=(0, 0, 0))
        d.text((x, y), ln, font=font, fill=(255, 255, 255))
        y += fs * 1.15
    img.save(out_png, "PNG")
    try:
        os.remove(bg)
    except Exception:
        pass
    return out_png

# ------------------------------------------------------------------- FONDOS
_THEMES = {
    "space":   [(8, 12, 40), (40, 15, 70)],
    "history": [(30, 20, 10), (70, 45, 20)],
    "money":   [(6, 30, 20), (15, 70, 45)],
    "generic": [(15, 18, 30), (40, 30, 60)],
    "dark":    [(10, 12, 18), (28, 30, 46)],
}
def make_gradient_bg(theme, out_png, w=1080, h=1920):
    top, bot = _THEMES.get(theme, _THEMES["generic"])
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        r = int(top[0] + (bot[0]-top[0])*t)
        g = int(top[1] + (bot[1]-top[1])*t)
        b = int(top[2] + (bot[2]-top[2])*t)
        for x in range(w):
            px[x, y] = (r, g, b)
    img.save(out_png)
    return out_png

def _run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.returncode != 0:
        raise RuntimeError(cmd[0] + " falló:\n" + p.stdout.decode()[-2000:])
    return p

def audio_duration(path):
    out = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                          "-of","default=nk=1:nw=1", path],
                         stdout=subprocess.PIPE).stdout.decode().strip()
    return float(out)

# ------------------------------------------------------------------- COMPOSICIÓN
def compose_from_gradient(theme, audio_mp3, ass_path, out_mp4,
                          w=1080, h=1920, title=None, music=None):
    """Render con fondo gradiente + Ken Burns + subtítulos quemados + voz."""
    dur = audio_duration(audio_mp3) + 0.6
    with tempfile.TemporaryDirectory() as td:
        bg = make_gradient_bg(theme, os.path.join(td, "bg.png"), w, h)
        ass = ass_path.replace(":", "\\:")
        # zoompan lento (Ken Burns) sobre el gradiente
        vf = (f"zoompan=z='min(zoom+0.0006,1.12)':d={int(dur*30)}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=30,"
              f"ass={ass}")
        title_draw = ""
        if title:
            safe = title.replace("'", "").replace(":", " ")
            title_draw = (f",drawtext=fontfile={FONT}:text='{safe}':"
                          f"fontcolor=white:fontsize={int(w*0.06)}:borderw=6:bordercolor=black@0.8:"
                          f"x=(w-text_w)/2:y={int(h*0.12)}:line_spacing=8")
        vf += title_draw
        cmd = ["ffmpeg","-y","-loop","1","-i",bg]
        # audio: voz (+ música opcional mezclada)
        if music and os.path.exists(music):
            cmd += ["-i", audio_mp3, "-stream_loop","-1","-i", music,
                    "-filter_complex",
                    f"[1:a]volume=1.0[v];[2:a]volume=0.12[m];[v][m]amix=inputs=2:duration=first[a]",
                    "-map","0:v","-map","[a]"]
        else:
            cmd += ["-i", audio_mp3, "-map","0:v","-map","1:a"]
        cmd += ["-vf", vf, "-t", f"{dur:.2f}",
                "-c:v","libx264","-preset","medium","-crf","23","-pix_fmt","yuv420p",
                "-c:a","aac","-b:a","192k","-r","30", out_mp4]
        _run(cmd)
    return out_mp4

def compose_from_clips(clips, audio_mp3, ass_path, out_mp4, w=1080, h=1920, music=None):
    """Render con clips de stock (Pexels) escalados/recortados al formato, + voz + subs."""
    dur = audio_duration(audio_mp3) + 0.4
    n = len(clips)
    per = dur / max(1, n)
    with tempfile.TemporaryDirectory() as td:
        inputs, filters = [], []
        for idx, clip in enumerate(clips):
            inputs += ["-stream_loop","-1","-i", clip]
            filters.append(
                f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},trim=0:{per:.2f},setpts=PTS-STARTPTS[v{idx}]")
        concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]"
        ass = ass_path.replace(":", "\\:")
        fc = ";".join(filters) + ";" + concat + f";[vc]ass={ass}[vout]"
        cmd = ["ffmpeg","-y"] + inputs + ["-i", audio_mp3]
        ai = n  # audio input index
        if music and os.path.exists(music):
            cmd += ["-stream_loop","-1","-i", music]
            fc += f";[{ai}:a]volume=1.0[vv];[{ai+1}:a]volume=0.12[mm];[vv][mm]amix=inputs=2:duration=first[aout]"
            amap = "[aout]"
        else:
            amap = f"{ai}:a"
        cmd += ["-filter_complex", fc, "-map","[vout]","-map",amap,
                "-t", f"{dur:.2f}","-c:v","libx264","-preset","medium","-crf","23",
                "-pix_fmt","yuv420p","-c:a","aac","-b:a","192k","-r","30", out_mp4]
        _run(cmd)
    return out_mp4

def compose_timeline(visuals, durations, audio_mp3, ass_path, out_mp4,
                     w=1080, h=1920, theme="generic", music=None):
    """
    Render SINCRONIZADO: una lista de visuales (1 por segmento) donde cada uno dura
    exactamente el tiempo que se narra su frase. Mezcla tipos:
      - 'video'    : clip de stock (looped) recortado a su duración
      - 'image'    : foto de stock con Ken Burns
      - 'gradient' : tarjeta temática con Ken Burns (cuando no hubo match relevante)
    Así lo que se ve coincide con lo que se dice → mucha más credibilidad.
    """
    voice = audio_duration(audio_mp3)
    n = len(visuals)
    if n == 0:
        return compose_from_gradient(theme, audio_mp3, ass_path, out_mp4, w=w, h=h, music=music)
    # Normaliza duraciones para que sumen la voz (+ pequeña cola en el último)
    durs = [max(0.6, float(d)) for d in durations[:n]]
    while len(durs) < n:
        durs.append(2.0)
    scale = voice / sum(durs) if sum(durs) > 0 else 1.0
    durs = [d * scale for d in durs]
    durs[-1] += 0.5  # cola para que no corte antes del final del audio
    total = sum(durs)

    with tempfile.TemporaryDirectory() as td:
        inputs, filters = [], []
        for idx, vis in enumerate(visuals):
            dur = durs[idx]
            frames = max(1, int(dur * 30))
            vtype = vis.get("type")
            path = vis.get("path")
            if vtype == "video" and path and os.path.exists(path):
                inputs += ["-stream_loop", "-1", "-i", path]
                filters.append(
                    f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h},trim=0:{dur:.2f},setpts=PTS-STARTPTS,fps=30,setsar=1[v{idx}]")
            else:
                # imagen o gradiente → foto/tarjeta con Ken Burns
                if vtype == "image" and path and os.path.exists(path):
                    src = path
                else:
                    src = make_gradient_bg(theme, os.path.join(td, f"grad_{idx}.png"), w, h)
                inputs += ["-loop", "1", "-t", f"{dur:.2f}", "-i", src]
                filters.append(
                    f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                    f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=30,"
                    f"trim=0:{dur:.2f},setsar=1,setpts=PTS-STARTPTS[v{idx}]")
        concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]"
        ass = ass_path.replace(":", "\\:")
        fc = ";".join(filters) + ";" + concat + f";[vc]ass={ass}[vout]"
        cmd = ["ffmpeg", "-y"] + inputs + ["-i", audio_mp3]
        ai = n
        if music and os.path.exists(music):
            cmd += ["-stream_loop", "-1", "-i", music]
            fc += f";[{ai}:a]volume=1.0[vv];[{ai+1}:a]volume=0.12[mm];[vv][mm]amix=inputs=2:duration=first[aout]"
            amap = "[aout]"
        else:
            amap = f"{ai}:a"
        cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", amap,
                "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-r", "30", out_mp4]
        _run(cmd)
    return out_mp4

def compose_from_images(images, audio_mp3, ass_path, out_mp4, w=1080, h=1920, music=None):
    """Render con IMÁGENES propias del usuario: Ken Burns (zoom/paneo) secuenciado + voz + subs."""
    dur = audio_duration(audio_mp3) + 0.4
    n = len(images)
    per = dur / max(1, n)
    inputs, filters = [], []
    for idx, img in enumerate(images):
        inputs += ["-loop", "1", "-t", f"{per:.2f}", "-i", img]
        filters.append(
            f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"zoompan=z='min(zoom+0.0012,1.15)':d={int(per*30)}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps=30,"
            f"trim=0:{per:.2f},setsar=1,setpts=PTS-STARTPTS[v{idx}]")
    concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]"
    ass = ass_path.replace(":", "\\:")
    fc = ";".join(filters) + ";" + concat + f";[vc]ass={ass}[vout]"
    cmd = ["ffmpeg", "-y"] + inputs + ["-i", audio_mp3]
    ai = n
    if music and os.path.exists(music):
        cmd += ["-stream_loop", "-1", "-i", music]
        fc += f";[{ai}:a]volume=1.0[vv];[{ai+1}:a]volume=0.12[mm];[vv][mm]amix=inputs=2:duration=first[aout]"
        amap = "[aout]"
    else:
        amap = f"{ai}:a"
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", amap,
            "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-r", "30", out_mp4]
    _run(cmd)
    return out_mp4
