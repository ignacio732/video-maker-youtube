"""
Motor de render de la Fábrica de Videos — 100% gratis (edge-tts + ffmpeg + Pillow).
Reutilizable por el worker autónomo y por el generador de demo.
"""
import os, asyncio, subprocess, tempfile, math, textwrap
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

def build_ass(words, out_ass, w=1080, h=1920, primary="&H00FFFFFF",
              highlight="&H0057FF3D", fontsize=None, margin_v=None):
    """Crea subtítulos ASS grandes, centrados en el tercio inferior."""
    fontsize = fontsize or int(w * 0.062)
    margin_v = margin_v or int(h * 0.30)
    lines = _group_words(words)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Main,DejaVu Sans,{fontsize},{primary},&H00000000,&H00000000,-1,{max(4,int(fontsize*0.08))},2,2,60,60,{margin_v}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    body = []
    for ln in lines:
        txt = ln["text"].upper().replace("\n", " ")
        body.append(f"Dialogue: 0,{_ass_time(ln['start'])},{_ass_time(ln['end'])},Main,,0,0,0,,{{\\fad(120,80)}}{txt}")
    with open(out_ass, "w") as f:
        f.write(header + "\n".join(body) + "\n")
    return out_ass

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
