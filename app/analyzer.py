"""Reads a project folder and produces a run plan (how to install, start and reach it)."""
import json
from pathlib import Path

from . import llm

IGNORE = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build", ".next",
    "target", ".idea", ".vscode", "coverage", ".cache", "workspace",
}
KEY_FILES = [
    "README.md", "README", "readme.md", "package.json", "requirements.txt", "pyproject.toml",
    "Pipfile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Makefile",
    ".env.example", "manage.py", "app.py", "main.py", "server.py", "index.js", "server.js",
    "app.js", "index.html", "vite.config.js", "vite.config.ts", "next.config.js",
    "pom.xml", "go.mod", "Cargo.toml", "composer.json", "Gemfile",
]
BUDGET = 22000

PLAN_SYSTEM = """You analyze software projects so an automated tool can run them locally on Windows \
(commands run through cmd.exe, so `&&` works and `set X=1` is used for env vars; prefer the `env` field) \
and then record a product demo video of the running web app.

Reply with ONLY a JSON object:
{
  "app_name": "short product name",
  "tagline": "one-line description of what it does",
  "summary": "2-4 sentences: what the project is and who it is for",
  "features": ["main user-facing features worth showing in a demo"],
  "kind": "web" | "cli" | "other",
  "install": ["commands to install dependencies, e.g. npm install"],
  "seed": ["optional commands that load demo/sample data, else []"],
  "start": "ONE command that starts the app and stays running (dev server). Never interactive.",
  "port": 3000,
  "url": "http://localhost:3000",
  "env": {"KEY": "value"},
  "ready_timeout": 90,
  "notes": "anything the user must provide (API keys, database) or empty string"
}
Rules: choose an explicit port and make the start command bind to it (e.g. `--port 5173`). For a plain \
static site (only HTML/CSS/JS) use `python -m http.server 8080`. Do not invent files that do not exist. \
If the app needs services you cannot start, still give the best plan and explain in notes."""


def file_tree(root: Path, max_depth: int = 3, max_entries: int = 150) -> str:
    lines: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth or len(lines) >= max_entries:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return
        for p in entries:
            if p.name in IGNORE or (p.name.startswith(".") and p.name != ".env.example"):
                continue
            lines.append("  " * depth + p.name + ("/" if p.is_dir() else ""))
            if len(lines) >= max_entries:
                return
            if p.is_dir():
                walk(p, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def read_key_files(root: Path) -> dict[str, str]:
    found: list[Path] = []
    for depth in range(3):
        pattern = "/".join(["*"] * depth + ["*"]) if depth else "*"
        for p in sorted(root.glob(pattern)):
            if p.is_file() and p.name in KEY_FILES and not (set(p.relative_to(root).parts) & IGNORE):
                found.append(p)
    found.sort(key=lambda p: (len(p.relative_to(root).parts), KEY_FILES.index(p.name)))
    out: dict[str, str] = {}
    used = 0
    for p in found[:14]:
        limit = 4000 if p.name.lower().startswith("readme") else 2500
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            continue
        if used + len(text) > BUDGET:
            break
        used += len(text)
        out[p.relative_to(root).as_posix()] = text
    return out


def gather(root: Path) -> dict:
    return {"tree": file_tree(root), "files": read_key_files(root)}


def _read(root: Path, name: str) -> str:
    p = root / name
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def heuristic_plan(root: Path) -> dict:
    """Best-effort plan from well-known files, used when no LLM answer is usable."""
    plan = {"app_name": root.name, "tagline": "", "summary": "", "features": [], "kind": "web",
            "install": [], "seed": [], "start": "", "port": 0, "env": {}, "ready_timeout": 90, "notes": ""}
    pkg = _read(root, "package.json")
    reqs = (_read(root, "requirements.txt") + _read(root, "pyproject.toml")).lower()
    if pkg:
        try:
            data = json.loads(pkg)
        except ValueError:
            data = {}
        scripts = data.get("scripts", {})
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        plan["install"] = ["npm install"]
        script = "dev" if "dev" in scripts else "start" if "start" in scripts else ""
        if script:
            plan["start"] = f"npm run {script}"
        plan["port"] = 5173 if "vite" in deps else 3000
        if "vite" in deps and script:
            plan["start"] += f" -- --port {plan['port']}"
    elif (root / "manage.py").is_file():
        plan.update(install=["pip install -r requirements.txt"] if reqs else [], port=8000,
                    start="python manage.py runserver 8000")
    elif reqs and (root / "app.py").is_file():
        plan.update(install=["pip install -r requirements.txt"], start="python app.py", port=5000)
        if "streamlit" in reqs:
            plan.update(start="streamlit run app.py --server.port 8501 --server.headless true", port=8501)
    elif reqs and (root / "main.py").is_file() and "fastapi" in reqs:
        plan.update(install=["pip install -r requirements.txt"], start="python -m uvicorn main:app --port 8000", port=8000)
    elif (root / "index.html").is_file():
        plan.update(start="python -m http.server 8080", port=8080)
    return plan


def normalize_plan(plan: dict, root: Path) -> dict:
    base = heuristic_plan(root)
    out = {**base, **{k: v for k, v in plan.items() if v not in (None, "")}}
    for key in ("install", "seed", "features"):
        value = out.get(key) or []
        out[key] = [str(x) for x in value] if isinstance(value, list) else [str(value)]
    out["env"] = {str(k): str(v) for k, v in (out.get("env") or {}).items()} if isinstance(out.get("env"), dict) else {}
    try:
        out["port"] = int(out.get("port") or 0)
    except (TypeError, ValueError):
        out["port"] = 0
    if not out.get("url") and out["port"]:
        out["url"] = f"http://localhost:{out['port']}"
    try:
        out["ready_timeout"] = int(out.get("ready_timeout") or 90)
    except (TypeError, ValueError):
        out["ready_timeout"] = 90
    for key in ("app_name", "tagline", "summary", "start", "url", "notes", "kind"):
        out[key] = str(out.get(key) or "")
    return out


def _context(root: Path) -> str:
    info = gather(root)
    parts = ["FILE TREE:\n" + info["tree"]]
    parts += [f"--- {name} ---\n{text}" for name, text in info["files"].items()]
    return "\n\n".join(parts)


def make_plan(root: Path, cfg: dict, description: str, log) -> dict:
    user = f"The user wants a demo video described as: {description or '(no description)'}\n\n{_context(root)}"
    try:
        raw = llm.chat_json(cfg, PLAN_SYSTEM, user)
    except llm.LLMError as e:
        log(f"LLM analysis failed ({e}); falling back to file-based detection.")
        raw = {}
    return normalize_plan(raw, root)


def repair_plan(root: Path, cfg: dict, plan: dict, error_tail: str) -> dict:
    user = (
        "This run plan failed on Windows. Fix it and return the full corrected JSON plan.\n\n"
        f"PLAN:\n{json.dumps(plan, indent=1)}\n\nERROR OUTPUT (last lines):\n{error_tail}\n\n{_context(root)}"
    )
    return normalize_plan({**plan, **llm.chat_json(cfg, PLAN_SYSTEM, user)}, root)
