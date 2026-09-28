"""
Avatares UGC hiperrealistas (Higgsfield) — librería de HOOKS de retención,
rotación de vestuario/escena con coherencia lógica, y armado del prompt final.

Cómo encaja en el pipeline:
- Los clips de avatar NO se generan por-video en el worker (Higgsfield no tiene
  una llamada directa sin GPU/infra propia integrable de forma confiable acá) —
  se generan en LOTES por un job periódico en la nube (sesión de Claude con
  Higgsfield conectado) que llama a build_avatar_clip_spec() de este módulo,
  genera el clip, lo sube a Supabase Storage y lo guarda en el pool
  (tabla ytfactory.avatar_clips) con db.add_avatar_clip().
- El worker (process_video en worker.py), video por video, solo ROTA clips ya
  listos del pool (db.get_ready_avatar_clip) y los pega adelante/atrás del
  video armado — rápido, síncrono, nunca bloquea el pipeline si el pool está
  vacío (en ese caso el video sale igual, sin avatar).

Retención (hooks): 8 patrones de apertura adaptados del playbook UGC de
Higgsfield (H1-H8) a contenido informativo/curiosidad/finanzas (no producto).
Cada uno define:
  - "label": nombre para mostrar en el dashboard (selector avatar_hook_style)
  - "staging": dirección de puesta en escena (gesto, mirada, energía) en inglés
    -- el prompt de generación de video funciona mejor en inglés; lo que el
    personaje DICE va en el idioma del canal.
  - "lines": {es,en,pt}: variantes de línea de apertura para ese patrón.

Vestuario/escena (coherencia creíble): en vez de mezclar piezas sueltas al
azar (lo que rompe la credibilidad — un buzo con luz de estudio, por ejemplo),
cada entrada de WARDROBE es una combinación YA coherente de outfit + locación +
luz (como si fuera un creador real grabándose en distintos momentos de su
rutina). Se agrupan en 3 "familias" con su propio tono (avatar_wardrobe_style):
home_casual, office_smart, outdoor_casual. La rotación evita repetir la
última entrada usada (channels.avatar_last_wardrobe) para que no salga
siempre la misma ropa/fondo, sin caer en combinaciones que no tengan sentido
entre sí (nunca se mezclan piezas de una entrada con la locación de otra).
"""
import random

REALISM_MODULE = """Shot as authentic, unscripted phone footage: filmed handheld on an iPhone in native
vertical video with subtle natural handheld micro-movement (not gimbal-smooth), OR
filmed on a compact DJI Osmo-style camera; consumer-grade image quality with true
sensor noise in shadows, realistic (not HDR-graded) dynamic range, no cinematic lens
flares, no shallow artificial bokeh, no teal-orange color grade, no film LUT.
Natural available light only: {lighting}, single consistent light source with
realistic soft shadow falloff, true-to-scene white balance, slightly imperfect
exposure like a real phone auto-exposure, no artificial three-point studio lighting,
no rim light, no beauty dish glow.
The setting and props must be plausible and internally consistent with who this
person is and what they're saying — a real, lived-in space (not a showroom),
matching the season, time of day implied by the light, and the topic — no random
or mismatched background objects.
Wearing {outfit}. {setting}.
{staging}
{voice_note}
{lang_instruction}: "{line}"
no on-screen text, no subtitles baked in."""

# ---- Patrones de hook (apertura) — pensados para RETENCIÓN en los primeros 2-3s ----
HOOK_PATTERNS = {
    "confesion": {
        "label": "Confesión a mitad de frase",
        "staging": "Caught mid-thought, as if the camera started recording a moment into an "
                   "ongoing personal realization — slightly lost in thought, not performing, "
                   "eyes drifting before landing on the lens.",
        "lines": {
            "es": ["...y ahí fue cuando me di cuenta de que todo lo que sabía sobre esto estaba mal.",
                   "...y por eso dejé de hacerlo como me habían enseñado."],
            "en": ["...and that's the moment I realized everything I knew about this was wrong.",
                   "...and that's when I stopped doing it the way everyone told me to."],
            "pt": ["...e foi aí que percebi que tudo que eu sabia sobre isso estava errado.",
                   "...e foi por isso que parei de fazer do jeito que me ensinaram."],
        },
    },
    "interrupcion": {
        "label": "Interrupción de patrón (frena el scroll)",
        "staging": "Stops abruptly mid-motion (putting down a cup, closing a laptop), snaps "
                   "attention straight to the lens with a sudden, deliberate stillness — a clear "
                   "visual 'stop scrolling' beat.",
        "lines": {
            "es": ["Pará. Antes de que sigas scrolleando, mirá esto.",
                   "Esperá un segundo — esto te importa más de lo que pensás."],
            "en": ["Wait. Before you keep scrolling, watch this.",
                   "Hold on a second — this matters more than you think."],
            "pt": ["Espera. Antes de continuar rolando, olha isso.",
                   "Pera aí — isso importa mais do que você imagina."],
        },
    },
    "reaccion_congelada": {
        "label": "Reacción congelada (shock)",
        "staging": "A beat of frozen, wide-eyed disbelief held for a second before speaking — "
                   "genuine, unscripted-looking shock, head tilted slightly, as if just seeing "
                   "something themselves.",
        "lines": {
            "es": ["No... no puedo creer que nadie me haya dicho esto antes.",
                   "Tuve que verlo dos veces para creerlo."],
            "en": ["I... I can't believe nobody told me this sooner.",
                   "I had to see it twice to believe it."],
            "pt": ["Eu... eu não acredito que ninguém me contou isso antes.",
                   "Tive que ver duas vezes para acreditar."],
        },
    },
    "confrontacion": {
        "label": "Confrontación directa",
        "staging": "Leaning slightly toward the lens, steady unbroken eye contact, calm but "
                   "serious tone — speaking directly and personally to the viewer, not performing "
                   "for an audience.",
        "lines": {
            "es": ["Necesito que me prestes atención un segundo.",
                   "Si estás viendo esto, es porque te está pasando a vos también."],
            "en": ["I need you to actually pay attention for a second.",
                   "If you're watching this, it's because this is happening to you too."],
            "pt": ["Preciso que você me preste atenção por um segundo.",
                   "Se você está vendo isso, é porque também está acontecendo com você."],
        },
    },
    "mecanismo": {
        "label": "Acción primero (mecanismo)",
        "staging": "Hands already in motion doing something concrete and specific (pointing at a "
                   "laptop screen, holding up a phone, tapping a notebook) before the first word "
                   "lands — action leads, speech follows.",
        "lines": {
            "es": ["(mostrando algo en la pantalla) Mirá esto un segundo.",
                   "(señalando una anotación) Esto es lo que nadie te explica."],
            "en": ["(pointing at the screen) Look at this for a second.",
                   "(tapping a notebook) This is the part nobody explains."],
            "pt": ["(apontando para a tela) Olha isso um segundo.",
                   "(apontando para uma anotação) Isso é o que ninguém te explica."],
        },
    },
    "pregunta_reto": {
        "label": "Pregunta desafío",
        "staging": "Slight head tilt, eyebrow raised, a knowing half-smile — asking the viewer "
                   "something directly, almost daring them to answer honestly.",
        "lines": {
            "es": ["¿Sabés cuánto estás perdiendo por no saber esto?",
                   "¿De verdad pensás que esto es casualidad?"],
            "en": ["Do you know how much you're losing by not knowing this?",
                   "Do you really think that's a coincidence?"],
            "pt": ["Você sabe quanto está perdendo por não saber isso?",
                   "Você realmente acha que isso é coincidência?"],
        },
    },
    "cifra_shock": {
        "label": "Cifra shock",
        "staging": "Says the number/fact almost as the very first sound out of their mouth, "
                   "matter-of-fact but pointed delivery, direct eye contact — no warm-up before "
                   "the hook lands.",
        "lines": {
            "es": ["El 90% de la gente comete este error. Yo también lo cometí.",
                   "Esto le pasa a 9 de cada 10 personas y casi nadie lo sabe."],
            "en": ["90% of people make this mistake. I did too.",
                   "This happens to 9 out of 10 people and almost nobody knows it."],
            "pt": ["90% das pessoas cometem esse erro. Eu também cometi.",
                   "Isso acontece com 9 em cada 10 pessoas e quase ninguém sabe."],
        },
    },
    "advertencia": {
        "label": "Advertencia urgente",
        "staging": "Tense, urgent energy — leaning in slightly, hand raised as if to stop the "
                   "viewer, tone shifts to a genuine 'listen carefully' warning register.",
        "lines": {
            "es": ["Pará antes de hacer esto — te puede salir muy caro.",
                   "Si estás por hacer esto, escuchame primero."],
            "en": ["Stop before you do this — it could cost you a lot.",
                   "If you're about to do this, hear me out first."],
            "pt": ["Para antes de fazer isso — pode custar muito caro.",
                   "Se você está prestes a fazer isso, me escuta primeiro."],
        },
    },
}

# ---- Cierre (outro) — no es un "hook", es la línea de salida/CTA con loop ----
OUTRO = {
    "label": "Cierre cálido con CTA",
    "staging": "Relaxed, warm, direct closing energy — a small genuine smile, nodding slightly, "
              "natural closing body language as if wrapping up a real conversation.",
    "lines": {
        "es": ["Si te sirvió esto, seguime que subo más como este.",
               "Contame en los comentarios si te pasó lo mismo."],
        "en": ["If this helped, follow along — I post more like this.",
               "Let me know in the comments if this happened to you too."],
        "pt": ["Se isso ajudou, me segue que eu posto mais assim.",
               "Me conta nos comentários se isso já aconteceu com você."],
    },
}

# ---- Arquetipos de voz/energía ----
VOICE_ARCHETYPES = {
    "natural": "Natural, conversational, unscripted delivery — like talking to a friend, varied "
              "pacing, small natural pauses, no performative enthusiasm, no salesy inflection.",
    "hyped": "High-energy, upbeat, fast-paced delivery — genuine excitement, slightly faster "
            "cadence, more emphatic hand gestures, engaging and animated but never fake or screamy.",
    "calm": "Calm, measured, low-key delivery — slower pace, quieter confident tone, minimal "
           "gestures, like sharing something thoughtfully rather than performing.",
}

# ---- Vestuario + escena + luz, agrupados en familias coherentes entre sí ----
WARDROBE = {
    "hc_sweater_living": {"outfit": "a plain grey crewneck sweater",
                          "setting": "a lived-in living room with an out-of-focus bookshelf and a plant in the background",
                          "light": "soft afternoon daylight through a side window"},
    "hc_hoodie_balcony": {"outfit": "a neutral-colored hoodie, hood down",
                          "setting": "a small home balcony or terrace with plants, city rooftops softly out of focus behind",
                          "light": "warm golden-hour daylight"},
    "hc_tshirt_kitchen": {"outfit": "a plain dark t-shirt",
                          "setting": "a home kitchen, leaning against the counter, everyday kitchen items softly out of focus",
                          "light": "bright natural midday light"},
    "hc_sweater_couch_night": {"outfit": "a cozy round-neck sweater",
                               "setting": "sitting on a couch with a throw blanket nearby, a warm lamp visible in the background",
                               "light": "warm indoor lamp light, evening"},
    "os_shirt_desk": {"outfit": "a light button-up shirt, sleeves rolled up",
                      "setting": "a home office desk with a laptop and notebooks, shelves softly out of focus",
                      "light": "clean morning daylight through a window"},
    "os_blazer_shelf": {"outfit": "a casual blazer over a plain t-shirt",
                        "setting": "standing near a bookshelf in a small home study",
                        "light": "soft diffused daylight"},
    "os_cardigan_desk_evening": {"outfit": "a neutral cardigan over a shirt",
                                 "setting": "the same home office desk, in a slightly dimmer late-day setting",
                                 "light": "warm late-afternoon light mixed with a desk lamp"},
    "oc_jacket_street": {"outfit": "a light casual jacket",
                         "setting": "walking slowly on a quiet urban street, buildings softly out of focus",
                         "light": "overcast, soft natural daylight"},
    "oc_hoodie_car": {"outfit": "a hoodie",
                      "setting": "sitting in the driver's seat of a parked car, phone propped on the dashboard",
                      "light": "natural daylight through the windshield"},
    "oc_sweater_park": {"outfit": "a casual sweater",
                        "setting": "sitting on a park bench, greenery softly out of focus behind",
                        "light": "natural daylight, partly cloudy"},
}
WARDROBE_FAMILIES = {
    "home_casual": ["hc_sweater_living", "hc_hoodie_balcony", "hc_tshirt_kitchen", "hc_sweater_couch_night"],
    "office_smart": ["os_shirt_desk", "os_blazer_shelf", "os_cardigan_desk_evening"],
    "outdoor_casual": ["oc_jacket_street", "oc_hoodie_car", "oc_sweater_park"],
}

# Opciones para selectores de configuración (dashboard)
HOOK_STYLE_CHOICES = [("auto_rotate", "Rotación automática (recomendado)")] + \
                     [(k, v["label"]) for k, v in HOOK_PATTERNS.items()]
WARDROBE_STYLE_CHOICES = [("mixed", "Mezclado — máxima variedad (recomendado)"),
                          ("home_casual", "Casual en casa"),
                          ("office_smart", "Smart casual / oficina"),
                          ("outdoor_casual", "Casual al aire libre")]
VOICE_ARCHETYPE_CHOICES = [("natural", "Natural (recomendado)"),
                           ("hyped", "Hyped / alta energía"),
                           ("calm", "Calma / pausada")]


def _lang_key(channel):
    lang = (channel.get("language") or "es").lower()
    if lang.startswith("en"):
        return "en"
    if lang.startswith("pt"):
        return "pt"
    return "es"

def _lang_instruction(lang):
    return {"es": "Habla en español diciendo exactamente",
            "en": "Speaking in English, saying exactly",
            "pt": "Falando em português, dizendo exatamente"}[lang]

def _wardrobe_pool_for(channel):
    style = (channel.get("avatar_wardrobe_style") or "mixed").lower()
    if style in WARDROBE_FAMILIES:
        return {k: WARDROBE[k] for k in WARDROBE_FAMILIES[style]}
    return dict(WARDROBE)  # 'mixed' o valor no reconocido: pool completo

def pick_wardrobe(channel):
    """Elige una combinación de vestuario+escena+luz YA coherente entre sí (nunca
    mezcla piezas de dos entradas distintas), evitando repetir la última usada
    por este canal (channels.avatar_last_wardrobe)."""
    pool = _wardrobe_pool_for(channel)
    keys = list(pool.keys())
    last = channel.get("avatar_last_wardrobe")
    candidates = [k for k in keys if k != last] or keys
    key = random.choice(candidates)
    return key, pool[key]

def pick_hook_pattern(channel):
    """Si avatar_hook_style está fijado a un patrón puntual, siempre usa ese.
    Si es 'auto_rotate' (default), rota evitando repetir el último patrón usado
    (channels.avatar_last_hook_pattern)."""
    style = (channel.get("avatar_hook_style") or "auto_rotate").lower()
    if style in HOOK_PATTERNS:
        return style
    last = channel.get("avatar_last_hook_pattern")
    candidates = [k for k in HOOK_PATTERNS if k != last]
    return random.choice(candidates or list(HOOK_PATTERNS.keys()))

def build_avatar_clip_spec(channel, mode="intro", hook_pattern_key=None,
                           wardrobe_key=None, voice_archetype=None):
    """Arma la especificación completa de un clip de avatar para este canal:
    qué patrón de hook (o de cierre, si mode='outro'), qué vestuario/escena,
    qué arquetipo de voz y qué línea va a decir — con rotación automática salvo
    que se fuerce un valor puntual. Devuelve el prompt final listo para
    Higgsfield (generate_video, junto con el character_media_id del canal) y
    los metadatos para guardar el clip en el pool una vez generado.
    """
    lang = _lang_key(channel)
    if mode == "outro":
        hp_key, hp = "cierre", OUTRO
    else:
        hp_key = hook_pattern_key or pick_hook_pattern(channel)
        hp = HOOK_PATTERNS[hp_key]
    wd_key, wd = (wardrobe_key, WARDROBE[wardrobe_key]) if wardrobe_key else pick_wardrobe(channel)
    voice = (voice_archetype or channel.get("avatar_voice_archetype") or "natural").lower()
    if voice not in VOICE_ARCHETYPES:
        voice = "natural"
    line = random.choice(hp["lines"][lang])
    scene_hint = (channel.get("avatar_scene_hint") or "").strip()
    setting = wd["setting"] + (f" — {scene_hint}" if scene_hint else "")
    prompt = REALISM_MODULE.format(
        lighting=wd["light"], outfit=wd["outfit"], setting=setting,
        staging=hp["staging"], voice_note=VOICE_ARCHETYPES[voice],
        lang_instruction=_lang_instruction(lang), line=line,
    )
    return {
        "mode": mode, "hook_pattern": hp_key, "wardrobe": wd_key,
        "voice_archetype": voice, "language": lang, "line": line, "prompt": prompt,
    }
