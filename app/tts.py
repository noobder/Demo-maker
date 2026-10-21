"""Text-to-speech: Edge (free, online), OpenAI, or the operating system's offline voices."""
import asyncio
import hashlib
import wave
from pathlib import Path

import httpx

from .compose import ff

EDGE_VOICES = {
    "en": "en-US-AriaNeural", "hi": "hi-IN-SwaraNeural", "es": "es-ES-ElviraNeural", "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural", "pt": "pt-BR-FranciscaNeural", "it": "it-IT-ElsaNeural", "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural", "zh": "zh-CN-XiaoxiaoNeural", "ar": "ar-SA-ZariyahNeural", "ru": "ru-RU-SvetlanaNeural",
    "ta": "ta-IN-PallaviNeural", "te": "te-IN-ShrutiNeural", "bn": "bn-IN-TanishaaNeural",
    "mr": "mr-IN-AarohiNeural", "gu": "gu-IN-DhwaniNeural",
}


class TTSError(Exception):
    pass


def _edge(text: str, voice: str, speed: float, out: Path) -> None:
    import edge_tts

    rate = f"{round((speed - 1) * 100):+d}%"
    asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out)))


def _openai(text: str, voice: str, speed: float, out: Path, llm_cfg: dict) -> None:
    key = llm_cfg.get("api_key")
    if llm_cfg.get("provider") != "openai" or not key:
        raise TTSError("OpenAI voice needs the OpenAI provider with an API key selected in AI model.")
    base = (llm_cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    r = httpx.post(
        base + "/audio/speech", headers={"Authorization": f"Bearer {key}"}, timeout=120,
        json={"model": "gpt-4o-mini-tts", "voice": voice or "alloy", "input": text, "speed": speed, "response_format": "mp3"},
    )
    if r.status_code != 200:
        raise TTSError(f"OpenAI TTS failed ({r.status_code}): {r.text[:200]}")
    out.write_bytes(r.content)


def _system(text: str, voice: str, speed: float, out: Path) -> None:
    import pyttsx3

    engine = pyttsx3.init()
    engine.setProperty("rate", int(engine.getProperty("rate") * speed))
    if voice:
        for v in engine.getProperty("voices"):
            if voice.lower() in v.name.lower():
                engine.setProperty("voice", v.id)
                break
    engine.save_to_file(text, str(out))
    engine.runAndWait()


def synthesize(text: str, workdir: Path, opts: dict, llm_cfg: dict) -> tuple[Path, float]:
    """Returns (44.1 kHz mono wav path, duration in seconds). Results are cached by content."""
    tts = opts.get("tts", {})
    engine = tts.get("engine", "edge")
    speed = float(tts.get("speed") or 1.0)
    voice = tts.get("voice") or (EDGE_VOICES.get(opts.get("language", "en"), "en-US-AriaNeural") if engine == "edge" else "")
    key = hashlib.sha1(f"{engine}|{voice}|{speed}|{text}".encode()).hexdigest()[:16]
    wav = workdir / f"voice_{key}.wav"
    if not wav.exists():
        raw = workdir / f"voice_{key}.raw"
        try:
            if engine == "edge":
                _edge(text, voice, speed, raw)
            elif engine == "openai":
                _openai(text, voice, speed, raw, llm_cfg)
            elif engine == "system":
                _system(text, voice, speed, raw)
            else:
                raise TTSError(f"Unknown voice engine: {engine}")
            ff(["-i", str(raw), "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        except TTSError:
            raise
        except Exception as e:
            raise TTSError(f"{engine} voice failed: {e}") from e
        finally:
            raw.unlink(missing_ok=True)
    with wave.open(str(wav)) as w:
        return wav, w.getnframes() / w.getframerate()
