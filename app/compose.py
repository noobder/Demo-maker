"""FFmpeg assembly: voice track, music with ducking, zoom, subtitles, title/end cards."""
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
W, H, FPS, RATE = 1280, 720, 25, 44100
ZOOM_SCALE, ZOOM_RAMP = 1.35, 0.55


def ff(args: list[str], cwd: Path | None = None) -> None:
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-700:]}")


# --- audio ---------------------------------------------------------------------------------

def assemble_voice(clips: list[tuple[float, Path]], total: float, out: Path) -> None:
    """Places each narration wav at its start offset on a silent mono track."""
    buf = bytearray(int(total * RATE) * 2)
    for start, path in clips:
        with wave.open(str(path)) as w:
            data = w.readframes(w.getnframes())
        at = int(start * RATE) * 2
        data = data[: max(0, len(buf) - at)]
        buf[at : at + len(data)] = data
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(buf))


def make_ambient(duration: float, out: Path) -> None:
    """A soft synthesized pad (no copyrighted assets). Upload a track for something richer."""
    freqs = [110, 164.81, 220, 277.18, 329.63]
    inputs: list[str] = []
    for f in freqs:
        inputs += ["-f", "lavfi", "-i", f"sine=f={f}:r={RATE}:d={duration + 1}"]
    labels = "".join(f"[{i}]" for i in range(len(freqs)))
    graph = (f"{labels}amix=inputs={len(freqs)}:normalize=0,tremolo=f=0.12:d=0.6,lowpass=f=1100,"
             f"afade=t=in:d=3,afade=t=out:st={max(0, duration - 3)}:d=3,volume=0.5")
    ff([*inputs, "-filter_complex", graph, "-t", str(duration), "-ac", "1", str(out)])


def mix_audio(voice: Path, music: Path | None, duration: float, out: Path) -> None:
    if not music:
        ff(["-i", str(voice), "-t", str(duration), str(out)])
        return
    for amix in ("amix=inputs=2:duration=first:normalize=0", "amix=inputs=2:duration=first,volume=2"):
        graph = (f"[1:a]volume=0.55,aformat=sample_rates={RATE}:channel_layouts=mono[m];"
                 f"[0:a]asplit=2[v1][v2];[m][v1]sidechaincompress=threshold=0.015:ratio=9:attack=30:release=700[d];"
                 f"[d][v2]{amix}[o]")
        try:
            ff(["-i", str(voice), "-stream_loop", "-1", "-i", str(music), "-filter_complex", graph,
                "-map", "[o]", "-t", str(duration), str(out)])
            return
        except RuntimeError as e:
            err = e
    raise err


# --- subtitles -----------------------------------------------------------------------------

def _stamp(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def write_srt(entries: list[tuple[float, float, str]], out: Path, offset: float = 0.0) -> None:
    """Splits each narration into short lines whose timing follows word count."""
    lines, n = [], 1
    for start, end, text in entries:
        words = text.split()
        if not words:
            continue
        chunks = [" ".join(words[i : i + 8]) for i in range(0, len(words), 8)]
        total_chars = sum(len(c) for c in chunks)
        t = start
        for c in chunks:
            dur = (end - start) * len(c) / total_chars
            lines.append(f"{n}\n{_stamp(t + offset)} --> {_stamp(t + dur + offset)}\n{c}\n")
            n += 1
            t += dur
    out.write_text("\n".join(lines), encoding="utf-8")


# --- video ---------------------------------------------------------------------------------

def _zoom_state(t: float, zooms: list[dict]) -> tuple[float, float, float]:
    """Zoom factor and centre at time t; overlapping ramps blend by weight."""
    wsum = zsum = cx = cy = 0.0
    for z in zooms:
        a = min(max((t - z["t0"]) / ZOOM_RAMP, 0), 1, max((z["t1"] + ZOOM_RAMP - t) / ZOOM_RAMP, 0))
        w = a * a * (3 - 2 * a)
        if w > 0:
            wsum += w
            zsum += w * (ZOOM_SCALE - 1)
            cx += w * z["x"]
            cy += w * z["y"]
    if wsum == 0:
        return 1.0, W / 2, H / 2
    return 1 + zsum, cx / wsum, cy / wsum


def render_main(video: Path, audio: Path, srt: Path | None, zooms: list[dict], duration: float, out: Path) -> None:
    """Applies zoom frame by frame, burns subtitles and muxes the narration audio."""
    vf = ["-vf", f"subtitles={srt.name}:force_style='FontSize=13,Outline=1,Shadow=0,MarginV=22'"] if srt else []
    encode = ["-map", "0:v", "-map", "1:a", "-t", f"{duration:.3f}", "-r", str(FPS), "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
              "-ar", str(RATE), "-ac", "2", out.name]
    workdir = out.parent
    if not zooms:
        scale = f"scale={W}:{H}" + ("," + vf[1] if vf else "")
        ff(["-i", str(video), "-i", str(audio), "-vf", scale, *encode], cwd=workdir)
        return

    reader = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf", f"scale={W}:{H}", "-r", str(FPS),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    writer = subprocess.Popen(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio), *vf, *encode],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE, cwd=workdir)
    size, n, limit = W * H * 3, 0, int(duration * FPS)
    try:
        while n < limit and len(buf := reader.stdout.read(size)) == size:
            z, cx, cy = _zoom_state(n / FPS, zooms)
            if z > 1.001:
                bw, bh = W / z, H / z
                x = min(max(cx - bw / 2, 0), W - bw)
                y = min(max(cy - bh / 2, 0), H - bh)
                img = Image.frombuffer("RGB", (W, H), buf, "raw", "RGB", 0, 1)
                buf = img.resize((W, H), Image.BICUBIC, box=(x, y, x + bw, y + bh)).tobytes()
            writer.stdin.write(buf)
            n += 1
        writer.stdin.close()
        err = writer.stderr.read().decode(errors="replace")
        if writer.wait() != 0:
            raise RuntimeError(f"ffmpeg failed: {err[-700:]}")
    except BrokenPipeError:
        writer.wait(timeout=10)
        raise RuntimeError(f"ffmpeg stopped early: {writer.stderr.read().decode(errors='replace')[-700:]}") from None
    finally:
        reader.kill()
        if writer.poll() is None:
            writer.kill()


# --- cards ---------------------------------------------------------------------------------

def _font(size: int, text: str) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fonts = "C:/Windows/Fonts/"
    names = ["segoeuib.ttf", "arialbd.ttf"]
    if not text.isascii():
        names = ["Nirmala.ttc", "msyh.ttc", "malgun.ttf", "YuGothB.ttc", "seguisym.ttf"] + names
    for name in names:
        try:
            return ImageFont.truetype(fonts + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def make_card(title: str, subtitle: str, seconds: float, out: Path) -> None:
    ys = np.linspace(0, 1, H)[:, None, None]
    xs = np.linspace(0, 1, W)[None, :, None]
    top, bottom = np.array([15, 18, 38]), np.array([67, 56, 202])
    arr = (top * (1 - (ys * 0.7 + xs * 0.3)) + bottom * (ys * 0.7 + xs * 0.3)).astype(np.uint8)
    img = Image.fromarray(np.broadcast_to(arr, (H, W, 3)).copy())
    draw = ImageDraw.Draw(img)
    big, small = _font(72, title), _font(34, subtitle)
    t_lines = _wrap(draw, title, big, W - 240)
    s_lines = _wrap(draw, subtitle, small, W - 320)
    block = len(t_lines) * 88 + (30 + len(s_lines) * 46 if s_lines else 0)
    y = (H - block) / 2
    for line in t_lines:
        draw.text((W / 2, y), line, font=big, fill="white", anchor="mt")
        y += 88
    if s_lines:
        draw.rectangle((W / 2 - 40, y + 6, W / 2 + 40, y + 10), fill=(165, 180, 252))
        y += 30
        for line in s_lines:
            draw.text((W / 2, y), line, font=small, fill=(224, 231, 255), anchor="mt")
            y += 46
    png = out.with_suffix(".png")
    img.save(png)
    ff(["-loop", "1", "-framerate", str(FPS), "-i", str(png), "-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo",
        "-t", str(seconds), "-vf", f"fade=t=in:d=0.5,fade=t=out:st={seconds - 0.5}:d=0.5", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-shortest", str(out)])


def concat(parts: list[Path], out: Path) -> None:
    listing = out.parent / "concat.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    ff(["-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy", out.name], cwd=out.parent)
