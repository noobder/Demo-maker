import atexit
import shutil
import zipfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyzer, auth, jobs, llm
from .auth import require_user
from .jobs import JOBS, WORKSPACE, Busy, Job

STATIC = Path(__file__).resolve().parent.parent / "static"
WORKSPACE.mkdir(exist_ok=True)

auth.init()
app = FastAPI(title="Demo Maker")
app.mount("/files", StaticFiles(directory=WORKSPACE), name="files")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def protect_files(request: Request, call_next):
    """Only a job's owner may fetch its finished video or captions; nothing else in the workspace is served."""
    if request.url.path.startswith("/files/"):
        user = auth.user_from_request(request)
        if not user:
            return JSONResponse({"detail": "Please sign in to continue."}, status_code=401)
        parts = request.url.path.split("/")  # ['', 'files', job, 'render', name]
        job = JOBS.get(parts[2]) if len(parts) > 2 else None
        if not job or job.owner != user["id"] or len(parts) != 5 or parts[3] != "render"                 or not parts[4].lower().endswith((".mp4", ".srt")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
    return await call_next(request)


class SignUp(BaseModel):
    name: str
    email: str
    password: str


class SignIn(BaseModel):
    email: str
    password: str


def _start_session(response: Response, user: dict) -> dict:
    response.set_cookie(auth.COOKIE, auth.create_session(user["id"]), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="lax", path="/")
    return {"user": user}


@app.post("/api/auth/register")
def sign_up(body: SignUp, response: Response):
    return _start_session(response, auth.register(body.name, body.email, body.password))


@app.post("/api/auth/login")
def sign_in(body: SignIn, request: Request, response: Response):
    client = request.client.host if request.client else "?"
    return _start_session(response, auth.login(body.email, body.password, client))


@app.post("/api/auth/logout")
def sign_out(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    return {"user": auth.user_from_request(request)}


@atexit.register
def _cleanup() -> None:
    for job in JOBS.values():
        job.stop_app()


def get_job(job_id: str, user: dict) -> Job:
    job = JOBS.get(job_id)
    if not job or job.owner != user["id"]:  # same answer for "missing" and "not yours"
        raise HTTPException(404, "Unknown job")
    return job


def _extract_zip(upload: UploadFile, dest: Path) -> Path:
    dest.mkdir(parents=True)
    tmp = dest.parent / "upload.zip"
    with tmp.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    with zipfile.ZipFile(tmp) as z:
        for member in z.namelist():
            if not (dest / member).resolve().is_relative_to(dest.resolve()):
                raise HTTPException(400, "Unsafe path in zip file")
        z.extractall(dest)
    tmp.unlink()
    entries = [p for p in dest.iterdir()]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else dest


@app.post("/api/jobs")
def create_job(project_path: str = Form(""), description: str = Form(""), project_zip: UploadFile | None = File(None),
               user: dict = Depends(require_user)):
    if project_zip and project_zip.filename:
        job = Job(Path("."), description, user["id"])
        job.project_dir = _extract_zip(project_zip, job.dir / "project")
    else:
        path = Path(project_path.strip().strip('"')).expanduser()
        if not path.is_dir():
            raise HTTPException(400, f"Folder not found: {path}")
        job = Job(path.resolve(), description, user["id"])
    JOBS[job.id] = job
    return {"id": job.id, "project_dir": str(job.project_dir)}


class StageBody(BaseModel):
    settings: dict = {}
    description: str | None = None
    plan: dict | None = None
    storyboard: dict | None = None


def _start(job: Job, name: str, fn, body: StageBody):
    try:
        job.start_stage(name, fn, body.settings)
    except Busy as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/analyze")
def analyze(job_id: str, body: StageBody, user: dict = Depends(require_user)):
    job = get_job(job_id, user)
    if body.description is not None:
        job.description = body.description
    return _start(job, "analyze", jobs.stage_analyze, body)


@app.post("/api/jobs/{job_id}/storyboard")
def draft_storyboard(job_id: str, body: StageBody, user: dict = Depends(require_user)):
    job = get_job(job_id, user)
    if body.plan:
        job.plan = analyzer.normalize_plan(body.plan, job.project_dir)
        job.stop_app()
        job.installed = False
    if body.description is not None:
        job.description = body.description
    if not job.plan:
        raise HTTPException(400, "Analyze the project first.")
    return _start(job, "storyboard", jobs.stage_storyboard, body)


@app.post("/api/jobs/{job_id}/render")
def render(job_id: str, body: StageBody, user: dict = Depends(require_user)):
    job = get_job(job_id, user)
    if not job.plan:
        raise HTTPException(400, "Analyze the project first.")
    if body.storyboard:
        try:
            job.storyboard = storyboard_clean(body.storyboard, job)
        except llm.LLMError:
            raise HTTPException(400, "The storyboard has no valid steps. Add at least one step.")
    if not job.storyboard:
        raise HTTPException(400, "Draft the storyboard first.")
    return _start(job, "render", jobs.stage_render, body)


def storyboard_clean(raw: dict, job: Job) -> dict:
    from . import storyboard

    return storyboard.clean_storyboard(raw, job.plan or {})


@app.post("/api/jobs/{job_id}/stop")
def stop(job_id: str, user: dict = Depends(require_user)):
    job = get_job(job_id, user)
    job.stop_event.set()
    job.stop_app()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/music")
def upload_music(job_id: str, music: UploadFile = File(...), user: dict = Depends(require_user)):
    job = get_job(job_id, user)
    suffix = Path(music.filename or "music.mp3").suffix or ".mp3"
    job.music_path = job.dir / f"music{suffix}"
    with job.music_path.open("wb") as f:
        shutil.copyfileobj(music.file, f)
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def status(job_id: str, since: int = 0, user: dict = Depends(require_user)):
    job = get_job(job_id, user)
    return {
        "status": job.status, "stage": job.stage, "error": job.error,
        "logs": job.logs[since:], "log_n": len(job.logs),
        "plan": job.plan, "plan_v": job.plan_v,
        "storyboard": job.storyboard, "sb_v": job.sb_v,
        "output": job.output, "out_v": job.out_v,
    }


class ModelsBody(BaseModel):
    llm: dict


@app.post("/api/models")
def models(body: ModelsBody, user: dict = Depends(require_user)):
    try:
        return {"models": llm.list_models(body.llm)}
    except llm.LLMError as e:
        raise HTTPException(400, str(e))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
