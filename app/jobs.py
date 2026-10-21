"""Job state and the three long-running stages: analyze, draft storyboard, render."""
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import analyzer, compose, discover, llm, recorder, storyboard, tts
from .runner import AppRunner, RunError, port_in_use

WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
JOBS: dict[str, "Job"] = {}


class Busy(Exception):
    pass


class Job:
    def __init__(self, project_dir: Path, description: str, owner: int | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.dir = WORKSPACE / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.owner = owner  # id of the user who created the job; only they can see it
        self.project_dir = project_dir
        self.description = description
        self.settings: dict = {}
        self.logs: list[str] = []
        self.status = "idle"        # idle | busy | error
        self.stage = ""
        self.error = ""
        self.plan: dict | None = None
        self.plan_v = 0
        self.storyboard: dict | None = None
        self.sb_v = 0
        self.output: dict | None = None
        self.out_v = 0
        self.runner: AppRunner | None = None
        self.installed = False
        self.music_path: Path | None = None
        self.stop_event = threading.Event()
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        self.logs.append(msg)
        print(f"[{self.id}] {msg}", flush=True)

    def stop_app(self) -> None:
        if self.runner:
            self.runner.stop()
            self.runner = None

    def start_stage(self, name: str, fn, settings: dict) -> None:
        with self._lock:
            if self.status == "busy":
                raise Busy("Another step is still running.")
            self.status, self.stage, self.error = "busy", name, ""
            self.settings = settings or self.settings
            self.stop_event.clear()
        threading.Thread(target=self._run, args=(name, fn), daemon=True).start()

    def _run(self, name: str, fn) -> None:
        try:
            fn(self)
            self.status = "idle"
        except recorder.Cancelled:
            self.log("Stopped.")
            self.status = "idle"
        except Exception as e:
            self.log(f"ERROR: {e}")
            if not isinstance(e, (llm.LLMError, RunError, tts.TTSError)):
                self.log(traceback.format_exc()[-1200:])
            self.error, self.status = str(e), "error"
        finally:
            if name == "render":
                self.stop_app()


def llm_cfg(job: Job) -> dict:
    return job.settings.get("llm", {})


# --- stage 1 -------------------------------------------------------------------------------

def stage_analyze(job: Job) -> None:
    job.log(f"Analyzing {job.project_dir} ...")
    plan = analyzer.make_plan(job.project_dir, llm_cfg(job), job.description, job.log)
    job.plan, job.plan_v = plan, job.plan_v + 1
    job.log(f"Plan ready: {plan['app_name']} -> `{plan['start'] or '(no start command)'}` at {plan['url'] or '?'}")
    if plan["notes"]:
        job.log(f"Note: {plan['notes']}")
    if plan["kind"] != "web":
        raise RunError(f"This project looks like a '{plan['kind']}' app; only web apps can be recorded for now.")


# --- stage 2 -------------------------------------------------------------------------------

PYTHON_HINT = re.compile(r"(pip3?|python3?|uvicorn|flask|streamlit|gunicorn|poetry)|manage\.py")


def ensure_venv(job: Job) -> Path | None:
    """Python projects get a throwaway virtualenv in the job folder, never the user's own environment."""
    plan = job.plan
    if not any(PYTHON_HINT.search(c) for c in [*plan["install"], *plan["seed"], plan["start"]]):
        return None
    venv = job.dir / "venv"
    if not venv.exists():
        job.log("Creating an isolated Python environment for this project ...")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    return venv


def ensure_running(job: Job) -> None:
    if job.runner and job.runner.alive():
        return
    for other in JOBS.values():  # idle jobs keep their app up for a later render; free the port for this one
        if other is not job and other.status != "busy":
            other.stop_app()
    for attempt in range(3):
        plan = job.plan
        runner = job.runner = AppRunner(job.project_dir, job.log, ensure_venv(job))
        try:
            if not job.installed:
                runner.run_cmds(plan["install"], plan["env"])
                job.installed = True
            runner.run_cmds(plan["seed"], plan["env"], fatal=False)
            if port_in_use(plan["url"]):
                raise RunError(
                    f"Something is already listening at {plan['url']}. Stop it or choose another port in the run plan.", fixable=False)
            runner.start(plan["start"], plan["env"])
            runner.wait_ready(plan["url"], plan["ready_timeout"])
            return
        except RunError as e:
            runner.stop()
            job.log(f"Run failed: {e}")
            if attempt == 2 or not e.fixable or not llm_cfg(job).get("model"):
                raise
            job.log("Asking the model to repair the run plan ...")
            job.installed = False
            job.plan = analyzer.repair_plan(job.project_dir, llm_cfg(job), plan, e.tail or str(e))
            job.plan_v += 1


def stage_storyboard(job: Job) -> None:
    ensure_running(job)
    job.log("Inspecting the running app ...")
    snapshots = discover.snapshot_app(job.plan["url"], job.log)
    job.log("Writing the storyboard and narration ...")
    job.storyboard = storyboard.build_storyboard(llm_cfg(job), job.plan, snapshots, job.description, job.settings)
    job.sb_v += 1
    job.log(f"Storyboard ready: {len(job.storyboard['steps'])} steps. Review it, then render.")


# --- stage 3 -------------------------------------------------------------------------------

def stage_render(job: Job) -> None:
    s, sb = job.settings, job.storyboard
    steps = [st for st in sb["steps"] if st.get("narration") or st["action"] != "wait"]
    out_dir = job.dir / "render"
    out_dir.mkdir(exist_ok=True)

    job.log("Generating voice-over ...")
    clips = []
    for i, st in enumerate(steps, 1):
        job.log(f"  voice {i}/{len(steps)}")
        if job.stop_event.is_set():
            raise recorder.Cancelled()
        clips.append(tts.synthesize(st["narration"], job.dir, s, llm_cfg(job)) if st["narration"].strip() else (None, 0.0))

    ensure_running(job)
    job.log("Recording the app ...")
    rec = recorder.record(job.plan["url"], steps, [d for _, d in clips], job.dir, s, job.log, job.stop_event)
    job.stop_app()
    total = rec["total"]
    job.log(f"Recorded {total:.1f}s. Mixing audio ...")

    voice = out_dir / "voice.wav"
    compose.assemble_voice(
        [(t["start"] + t["lead"], wav) for t, (wav, _) in zip(rec["timeline"], clips) if wav], total, voice)
    music = None
    if s.get("music") == "ambient":
        music = out_dir / "ambient.wav"
        compose.make_ambient(total, music)
    elif s.get("music") == "upload" and job.music_path:
        music = job.music_path
    mixed = out_dir / "mix.wav"
    compose.mix_audio(voice, music, total, mixed)

    srt = None
    entries = [(t["start"] + t["lead"], t["start"] + t["lead"] + d, st["narration"])
               for t, st, (_, d) in zip(rec["timeline"], steps, clips) if st["narration"].strip()]
    compose.write_srt(entries, out_dir / "captions.srt")
    if s.get("subtitles", True):
        srt = out_dir / "captions.srt"

    job.log("Rendering video (zoom + captions) ...")
    main = out_dir / "main.mp4"
    compose.render_main(rec["video"], mixed, srt, rec["zooms"], total, main)

    parts = [main]
    offset = 0.0
    if s.get("cards", True):
        job.log("Adding title and end cards ...")
        intro, outro = out_dir / "intro.mp4", out_dir / "outro.mp4"
        compose.make_card(sb["title"], sb["tagline"], 3.0, intro)
        compose.make_card(sb["outro"] or "Thanks for watching", sb["title"], 3.0, outro)
        parts, offset = [intro, main, outro], 3.0
    final = out_dir / "demo.mp4"
    compose.concat(parts, final)
    compose.write_srt(entries, out_dir / "demo.srt", offset=offset)

    job.output = {"video": f"/files/{job.id}/render/demo.mp4", "srt": f"/files/{job.id}/render/demo.srt"}
    job.out_v += 1
    job.log(f"Done: {final}")
