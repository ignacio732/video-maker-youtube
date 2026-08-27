"""
Generación de imágenes con IA — 100% gratis y SIN API key (Pollinations.ai).
Da cobertura visual del 100% aunque no exista stock del tema, y permite estética
fija por canal (realista, anime, 3D, documental...).

Uso principal:
  paths = generate_batch(prompts, outdir, w, h, style, seed, model)
  -> lista de rutas (o None por posición si falló esa imagen)

Cada imagen se pide a:
  https://image.pollinations.ai/prompt/<PROMPT>?width=&height=&seed=&nologo=true&model=flux
Tolerante a fallos: si una imagen no sale, devuelve None en esa posición
(el worker cae a stock/gradiente para ese segmento).
"""
import os, time, requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

BASE = "https://image.pollinations.ai/prompt/"
MODEL = os.environ.get("AIIMG_MODEL", "flux")
UA = {"User-Agent": "FabricaVideosYouTube/1.0"}
# Concurrencia baja = más confiable con el endpoint gratis (evita throttling/timeouts).
MAX_WORKERS = int(os.environ.get("AIIMG_WORKERS", "2"))

# Presets de estilo por canal (se elige con channel.visual_style; texto libre también sirve)
STYLE_PRESETS = {
    "realista":   "cinematic realistic photo, dramatic lighting, ultra detailed, sharp focus, 8k",
    "documental": "documentary photography, natural lighting, realistic, photojournalism",
    "anime":      "anime illustration, studio ghibli inspired, vibrant colors, clean lines, detailed background",
    "comic":      "comic book illustration, bold ink lines, cel shading, dynamic",
    "3d":         "3d render, octane render, pixar style, soft global illumination, highly detailed",
    "acuarela":   "watercolor painting, soft brush strokes, artistic, textured paper",
    "neon":       "synthwave, neon lights, cyberpunk aesthetic, glowing, cinematic",
}

def resolve_style(style):
    """Acepta un preset por nombre o un texto de estilo libre."""
    if not style:
        return STYLE_PRESETS["realista"]
    return STYLE_PRESETS.get(str(style).strip().lower(), str(style))

def _try_fetch(url, params, out_path, timeout):
    try:
        r = requests.get(url, params=params, headers=UA, timeout=timeout)
        if r.status_code == 200 and r.content and len(r.content) > 3000:
            ct = r.headers.get("Content-Type", "")
            if "image" in ct or r.content[:3] == b"\xff\xd8\xff":
                with open(out_path, "wb") as f:
                    f.write(r.content)
                return True
    except Exception:
        pass
    return False

def _generate_one(prompt, out_path, w, h, seed, style_text, model, retries=3):
    """Reintenta con backoff; si el modelo principal falla, prueba 'turbo' (más liviano)."""
    full = f"{prompt.strip()}, {style_text}" if style_text else prompt.strip()
    url = BASE + quote(full[:600], safe="")
    base_params = {"width": w, "height": h, "nologo": "true"}
    if seed is not None:
        base_params["seed"] = int(seed)
    models = [model] + (["turbo"] if model != "turbo" else [])
    for attempt in range(retries):
        m = models[min(attempt, len(models) - 1)]  # últimos intentos usan turbo
        params = dict(base_params, model=m)
        if _try_fetch(url, params, out_path, timeout=150):
            return out_path
        time.sleep(3 * (attempt + 1))
    return None

def generate_batch(prompts, outdir, w=1080, h=1920, style=None, seed=None,
                   model=None, idx_offset=0):
    """
    Genera una imagen por cada prompt (en paralelo). Devuelve lista alineada
    a 'prompts' con la ruta de cada imagen (o None si falló).
    - seed: si se pasa un entero base, cada imagen usa seed+posición (variedad
      controlada con estética consistente). Si es None, Pollinations elige.
    """
    model = model or MODEL
    style_text = resolve_style(style)
    results = [None] * len(prompts)

    def task(i, p):
        s = (int(seed) + i) if seed is not None else None
        out = os.path.join(outdir, f"ai_{idx_offset + i}.jpg")
        return i, _generate_one(p, out, w, h, s, style_text, model)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(task, i, p) for i, p in enumerate(prompts)]
        for fut in as_completed(futs):
            try:
                i, path = fut.result()
                results[i] = path
            except Exception:
                pass
    # 2ª pasada SECUENCIAL para los que fallaron (más gentil con el endpoint gratis → sube el %)
    missing = [i for i, r in enumerate(results) if not r]
    for i in missing:
        s = (int(seed) + i) if seed is not None else None
        out = os.path.join(outdir, f"ai_{idx_offset + i}.jpg")
        results[i] = _generate_one(prompts[i], out, w, h, s, style_text, model)
        time.sleep(1)
    return results

def generate_one(prompt, out_path, w=1080, h=1920, style=None, seed=None, model=None):
    """Atajo para una sola imagen."""
    return _generate_one(prompt, out_path, w, h, seed, resolve_style(style), model or MODEL)
