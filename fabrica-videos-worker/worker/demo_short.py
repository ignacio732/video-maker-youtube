import os
os.environ.setdefault("EDGE_TTS_PROXY", "http://127.0.0.1:38013")  # sandbox
import render

OUT = "/home/claude/output"
os.makedirs(OUT, exist_ok=True)

# --- Lo que en producción genera el LLM (idea + guion) ---
title = "3 datos del pulpo\nque parecen mentira"
voice = "es-AR-ElenaNeural"
theme = "generic"
narration = (
    "¿Sabías que el pulpo tiene tres corazones? "
    "Dos bombean sangre a las branquias, y el tercero al resto del cuerpo. "
    "Pero hay más. Su sangre es azul, porque usa cobre en vez de hierro "
    "para transportar oxígeno. "
    "Y lo más increíble: tiene nueve cerebros. "
    "Uno central, y ocho más, uno en cada brazo, que pueden actuar por su cuenta. "
    "La próxima vez que veas un pulpo, acordate: "
    "estás mirando a uno de los animales más raros del planeta. "
    "Seguime para más datos increíbles."
)

print("1/3 Generando voz + timings (edge-tts)...")
mp3 = os.path.join(OUT, "voz.mp3")
words = render.synth_voice(narration, voice, mp3)
print(f"   voz ok: {len(words)} palabras, {render.audio_duration(mp3):.1f}s")

print("2/3 Construyendo subtítulos ASS sincronizados...")
ass = render.build_ass(words, os.path.join(OUT, "subs.ass"))

print("3/3 Renderizando short 1080x1920 con ffmpeg...")
mp4 = os.path.join(OUT, "short_datos_pulpo.mp4")
render.compose_from_gradient(theme, mp3, ass, mp4, title=title)
print("LISTO:", mp4, f"{os.path.getsize(mp4)/1e6:.1f} MB")
