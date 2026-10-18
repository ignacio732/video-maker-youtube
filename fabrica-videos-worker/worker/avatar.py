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

# Variante para conductores de noticiero / streaming: cámara profesional de estudio,
# luz de estudio real (pero natural, no publicitaria). Se usa cuando el canal tiene
# avatar_persona = 'news_anchor' (ver PERSONAS).
STUDIO_MODULE = """Shot as a real live news / financial streaming broadcast: locked-off professional
broadcast camera on a tripod in native vertical framing, medium shot from the waist up,
clean natural image with true skin texture (no beauty filter, no plastic skin), realistic
dynamic range, no cinematic lens flares, no teal-orange grade, no film LUT.
Lighting: {lighting} — soft, even, credible studio light, single consistent key direction
with gentle fill, natural skin tones, no dramatic rim light, no glamour glow.
The set must be plausible and consistent for a serious Argentine economy news program —
real furniture and screens, nothing random or mismatched, no readable on-screen text,
no channel logos, no lower-thirds.
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

# ---- Cutaways — beats CORTOS para intercalar A MITAD del video (no abren ni
# cierran: son una intervención breve del avatar entre dos frases de la
# narración, sin sacar nada de lo narrado — el video queda un poco más largo).
# Energía de "aparte" en una conversación, nunca de apertura/cierre.
CUTAWAYS = {
    "enfasis": {
        "label": "Énfasis rápido",
        "staging": "A brief, natural aside — quick emphatic point toward the lens or a short "
                  "decisive nod, mid-conversation energy (not an opener), then a small glance "
                  "away as if returning to a train of thought.",
        "lines": {
            "es": ["Anotá esto.", "Esto es lo importante.", "Prestá atención acá."],
            "en": ["Write this down.", "This is the important part.", "Pay attention here."],
            "pt": ["Anota isso.", "Essa é a parte importante.", "Presta atenção aqui."],
        },
    },
    "aclaracion": {
        "label": "Aclaración",
        "staging": "A small correcting beat — slight head tilt or shake, brief 'let me clarify' "
                  "energy, like interjecting mid-thought to sharpen a point, not to open or close.",
        "lines": {
            "es": ["Ojo, no es tan simple.", "Aclaro esto antes de seguir.", "Pará, no es lo que pensás."],
            "en": ["Careful, it's not that simple.", "Let me clarify before moving on.", "Wait, it's not what you think."],
            "pt": ["Cuidado, não é tão simples assim.", "Deixa eu esclarecer isso.", "Pera, não é o que você pensa."],
        },
    },
    "complicidad": {
        "label": "Complicidad",
        "staging": "A knowing half-smile and a brief direct glance to the lens, like sharing an "
                  "aside with someone who already gets it — casual, mid-conversation, not a hook.",
        "lines": {
            "es": ["Vos ya lo viviste, ¿no?", "Sabés bien de qué hablo.", "A vos te pasó también, seguro."],
            "en": ["You've been there, right?", "You know exactly what I mean.", "This has happened to you too, I bet."],
            "pt": ["Você já passou por isso, né?", "Você sabe bem do que eu falo.", "Isso já aconteceu com você também, aposto."],
        },
    },
    "pausa_shock": {
        "label": "Pausa shock",
        "staging": "A brief understated pause — raised eyebrows, a short breath, quiet emphasis "
                  "rather than a big reaction — like letting something serious sink in for a beat.",
        "lines": {
            "es": ["Posta.", "En serio.", "Como lo escuchás."],
            "en": ["Seriously.", "I mean it.", "No exaggeration."],
            "pt": ["De verdade.", "Falo sério.", "Sem exagero."],
        },
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
    "anchor": "Professional news-anchor delivery with a natural Rioplatense (Buenos Aires, "
              "Argentina) Spanish accent and voseo: clear diction, confident and credible, "
              "semi-formal and warm like a modern financial streaming host — steady pace, "
              "small natural head movements and restrained hand gestures, never salesy, "
              "never shouting, never a neutral or Spain Spanish accent.",
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
WARDROBE.update({
    "na_navy_blazer_desk": {"outfit": "a tailored navy blazer over a white silk blouse, small gold stud earrings",
                            "setting": "seated at a modern news desk, a large screen behind her showing abstract green and red market chart lines, Buenos Aires skyline softly out of focus",
                            "light": "soft frontal studio key light with warm practical lights in the background"},
    "na_camel_blazer_standing": {"outfit": "a camel blazer over a black fitted top, thin gold necklace",
                                 "setting": "standing beside a tall video wall with abstract currency and market graphics, modern studio set softly out of focus",
                                 "light": "even soft studio light, neutral white balance"},
    "na_white_shirt_stream": {"outfit": "a crisp white button-up shirt, sleeves slightly rolled, a smartwatch",
                              "setting": "a semi-formal streaming studio desk with a broadcast microphone on an arm, a laptop and a mug, warm shelves and plants softly out of focus",
                              "light": "warm soft studio light, cozy evening streaming ambience"},
    "na_black_blazer_night": {"outfit": "a black blazer over an ivory blouse",
                              "setting": "seated at the news desk at night, large window behind with Buenos Aires city lights and the Obelisco area softly out of focus",
                              "light": "soft frontal key light with cool night ambience from the window"},
    "na_burgundy_knit_stream": {"outfit": "a fitted burgundy knit top with a delicate necklace",
                                "setting": "a streaming studio set with a broadcast microphone, a monitor showing abstract price lines, neon-free warm lamp in the background",
                                "light": "soft key light mixed with a warm desk lamp"},
    "na_grey_suit_newsroom": {"outfit": "a light grey suit jacket over a pale blue blouse",
                              "setting": "standing in a working newsroom, desks and monitors softly out of focus behind her",
                              "light": "clean bright office studio light"},
})

WARDROBE_FAMILIES = {
    "home_casual": ["hc_sweater_living", "hc_hoodie_balcony", "hc_tshirt_kitchen", "hc_sweater_couch_night"],
    "office_smart": ["os_shirt_desk", "os_blazer_shelf", "os_cardigan_desk_evening"],
    "outdoor_casual": ["oc_jacket_street", "oc_hoodie_car", "oc_sweater_park"],
    "news_studio": ["na_navy_blazer_desk", "na_camel_blazer_standing", "na_white_shirt_stream",
                    "na_black_blazer_night", "na_burgundy_knit_stream", "na_grey_suit_newsroom"],
}

# ---- Personas: cambian el REGISTRO de todo el avatar (ganchos, cierre, set, cámara).
# 'ugc' = creador casual grabándose con el celular (default, lo de siempre).
# 'news_anchor' = conductora/or de noticiero o streaming económico semi-formal: ganchos
# genéricos (sirven para cualquier noticia del día, el clip se reutiliza en muchos
# videos), sin cifras inventadas, voseo rioplatense.
NEWS_HOOKS = {
    "apertura_noticiero": {
        "label": "Apertura de noticiero",
        "staging": "Opens like the top of a live news segment — a small composed nod, steady "
                   "eye contact with the lens, hands resting on the desk, then starts speaking.",
        "lines": {"es": ["Buenas, ¿cómo están? Arrancamos con lo que tenés que saber del dólar hoy.",
                         "Hola, bienvenidos. Vamos con lo más importante que se movió hoy en la economía."],
                  "en": ["Hi everyone, welcome. Let's start with what you need to know today."],
                  "pt": ["Olá, bem-vindos. Vamos ao que você precisa saber hoje."]},
    },
    "ultimo_momento": {
        "label": "Último momento",
        "staging": "Slight lean toward the camera with focused urgency, glancing down at notes for "
                   "a split second, then back to the lens — breaking-news energy, composed.",
        "lines": {"es": ["Atención, porque esto acaba de pasar y te impacta directo en el bolsillo.",
                         "Último momento: hay un movimiento en el mercado que tenés que conocer."],
                  "en": ["Breaking: there's a move in the market you need to know about."],
                  "pt": ["Atenção, isso acabou de acontecer e mexe direto no seu bolso."]},
    },
    "pregunta_conductora": {
        "label": "Pregunta al espectador",
        "staging": "A slight head tilt and a knowing, professional half-smile, asking the viewer "
                   "directly as a host would at the start of a segment.",
        "lines": {"es": ["¿Te preguntaste por qué el dólar se mueve así? Te lo explico en un minuto.",
                         "¿Qué conviene hacer hoy con tus pesos? Vamos a verlo."],
                  "en": ["Ever wondered why the market moves like this? I'll explain in a minute."],
                  "pt": ["Você já se perguntou por que o mercado se mexe assim? Eu explico."]},
    },
    "dato_clave": {
        "label": "Dato clave",
        "staging": "Points briefly toward the screen behind or beside her, then turns back to the "
                   "lens — presenting a key fact like a host introducing a graphic.",
        "lines": {"es": ["Hay un dato de hoy que casi nadie está mirando, y es clave.",
                         "Mirá este dato, porque explica todo lo que está pasando."],
                  "en": ["There's one data point today almost nobody is watching, and it's key."],
                  "pt": ["Tem um dado de hoje que quase ninguém está olhando, e é chave."]},
    },
    "advertencia_formal": {
        "label": "Advertencia (formal)",
        "staging": "Serious, calm expression, one hand slightly raised in a measured 'hold on' "
                   "gesture — a credible, responsible warning, not alarmist.",
        "lines": {"es": ["Antes de comprar o vender dólares hoy, escuchá esto.",
                         "Ojo con lo que viene, porque puede cambiar tus planes de esta semana."],
                  "en": ["Before you buy or sell today, listen to this."],
                  "pt": ["Antes de comprar ou vender hoje, escuta isso."]},
    },
    "resumen_rapido": {
        "label": "Resumen en 30 segundos",
        "staging": "Brisk, confident host energy, a quick smile and a small 'let's go' hand "
                   "gesture, like kicking off a fast recap segment.",
        "lines": {"es": ["Te resumo en treinta segundos qué pasó hoy con el dólar.",
                         "Vamos rápido: esto es lo que tenés que saber de la economía hoy."],
                  "en": ["Here's today's recap in thirty seconds."],
                  "pt": ["Resumo em trinta segundos o que aconteceu hoje."]},
    },
}
NEWS_OUTRO = {
    "label": "Cierre de conductora",
    "staging": "Warm, composed sign-off like the end of a news segment — a small genuine smile, "
               "a slight nod, hands together on the desk or a brief open-hand gesture.",
    "lines": {"es": ["Esto fue todo por hoy. Seguinos para no perderte el próximo resumen.",
                     "Si te sirvió, compartilo con alguien que lo necesite. Nos vemos mañana.",
                     "Contanos en los comentarios qué vas a hacer con tus ahorros. Hasta la próxima.",
                     "Te dejo esto para pensar. Seguinos y mañana te cuento cómo sigue."],
              "en": ["That's all for today. Follow so you don't miss the next update."],
              "pt": ["Isso é tudo por hoje. Siga para não perder o próximo resumo."]},
}
PERSONAS = {
    "ugc": {"hooks": HOOK_PATTERNS, "outro": OUTRO, "module": REALISM_MODULE,
            "voice": None, "wardrobe_family": None},
    "news_anchor": {"hooks": NEWS_HOOKS, "outro": NEWS_OUTRO, "module": STUDIO_MODULE,
                    "voice": "anchor", "wardrobe_family": "news_studio"},
}
def _persona(channel):
    return PERSONAS.get((channel.get("avatar_persona") or "ugc").lower(), PERSONAS["ugc"])

# Opciones para selectores de configuración (dashboard)
HOOK_STYLE_CHOICES = [("auto_rotate", "Rotación automática (recomendado)")] + \
                     [(k, v["label"]) for k, v in HOOK_PATTERNS.items()]
WARDROBE_STYLE_CHOICES = [("mixed", "Mezclado — máxima variedad (recomendado)"),
                          ("home_casual", "Casual en casa"),
                          ("office_smart", "Smart casual / oficina"),
                          ("outdoor_casual", "Casual al aire libre"),
                          ("news_studio", "Estudio de noticias / streaming")]
VOICE_ARCHETYPE_CHOICES = [("natural", "Natural (recomendado)"),
                           ("hyped", "Hyped / alta energía"),
                           ("calm", "Calma / pausada"),
                           ("anchor", "Conductora/or de noticiero (rioplatense)")]
PERSONA_CHOICES = [("ugc", "Creador UGC casual"), ("news_anchor", "Conductora/or de noticiero / streaming")]


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
    fam = _persona(channel)["wardrobe_family"]
    if fam and style not in WARDROBE_FAMILIES:
        style = fam  # una persona de noticiero nunca cae en el living/parque del UGC
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
    hooks = _persona(channel)["hooks"]
    style = (channel.get("avatar_hook_style") or "auto_rotate").lower()
    if style in hooks:
        return style
    last = channel.get("avatar_last_hook_pattern")
    candidates = [k for k in hooks if k != last]
    return random.choice(candidates or list(hooks.keys()))

def pick_cutaway_beat(channel):
    """Rota entre los 4 tipos de cutaway evitando repetir el último patrón usado
    por el canal (comparte la misma memoria que los hooks — el objetivo es
    variedad general, no una cola separada por tipo de clip)."""
    last = channel.get("avatar_last_hook_pattern")
    candidates = [k for k in CUTAWAYS if k != last]
    return random.choice(candidates or list(CUTAWAYS.keys()))

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
    persona = _persona(channel)
    if mode == "outro":
        hp_key, hp = "cierre", persona["outro"]
    elif mode == "cutaway":
        hp_key = hook_pattern_key or pick_cutaway_beat(channel)
        hp = CUTAWAYS[hp_key]
    else:
        hp_key = hook_pattern_key or pick_hook_pattern(channel)
        hp = persona["hooks"].get(hp_key) or HOOK_PATTERNS[hp_key]
    wd_key, wd = (wardrobe_key, WARDROBE[wardrobe_key]) if wardrobe_key else pick_wardrobe(channel)
    voice = (voice_archetype or persona["voice"] or channel.get("avatar_voice_archetype") or "natural").lower()
    if voice not in VOICE_ARCHETYPES:
        voice = "natural"
    line = random.choice(hp["lines"][lang])
    scene_hint = (channel.get("avatar_scene_hint") or "").strip()
    setting = wd["setting"] + (f" — {scene_hint}" if scene_hint else "")
    prompt = persona["module"].format(
        lighting=wd["light"], outfit=wd["outfit"], setting=setting,
        staging=hp["staging"], voice_note=VOICE_ARCHETYPES[voice],
        lang_instruction=(_lang_instruction(lang) if not (persona is PERSONAS["news_anchor"] and lang == "es")
                          else "Habla en español rioplatense de Buenos Aires (acento argentino, con voseo) diciendo exactamente"),
        line=line,
    )
    return {
        "mode": mode, "hook_pattern": hp_key, "wardrobe": wd_key,
        "voice_archetype": voice, "language": lang, "line": line, "prompt": prompt,
    }
