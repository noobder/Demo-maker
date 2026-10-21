"""Drives the app in a recorded browser, pacing every step to its narration length."""
import math
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

WIDTH, HEIGHT = 1280, 720
LEAD_IN = 0.25    # narration starts this long after the step begins
TAIL = 0.45       # pause after the narration ends

CURSOR_JS = r"""
(() => {
  if (window.__demoCursor) return;
  window.__demoCursor = true;
  const mount = () => {
    const c = document.createElement('div');
    c.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;left:0;top:0;width:24px;height:24px;';
    c.innerHTML = '<svg viewBox="0 0 24 24" width="24" height="24"><path d="M4 2l0 17 4.5-4 3 7 3-1.3-3-6.8 6.3 0z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    let pos = {x: -60, y: -60};
    try { pos = JSON.parse(sessionStorage.getItem('__dmPos') || 'null') || pos; } catch (e) {}
    const place = () => { c.style.transform = `translate(${pos.x}px,${pos.y}px)`; };
    place();
    document.documentElement.appendChild(c);
    addEventListener('mousemove', e => {
      pos = {x: e.clientX, y: e.clientY}; place();
      try { sessionStorage.setItem('__dmPos', JSON.stringify(pos)); } catch (_) {}
    }, true);
    addEventListener('mousedown', e => {
      const r = document.createElement('div');
      r.style.cssText = `position:fixed;z-index:2147483646;pointer-events:none;left:${e.clientX - 22}px;top:${e.clientY - 22}px;width:44px;height:44px;border-radius:50%;background:rgba(99,102,241,.35);border:2px solid rgba(99,102,241,.9);`;
      document.documentElement.appendChild(r);
      r.animate([{transform: 'scale(.3)', opacity: 1}, {transform: 'scale(1.6)', opacity: 0}], {duration: 600, easing: 'ease-out'}).onfinish = () => r.remove();
    }, true);
  };
  if (document.documentElement) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
"""


class Cancelled(Exception):
    pass


def record(base_url: str, steps: list[dict], durations: list[float], workdir: Path, opts: dict, log, stop) -> dict:
    """Returns {video, timeline: [{start, end}], zooms: [{t0, t1, x, y}], total}."""
    polish = opts.get("polish", {})
    cursor_on, zoom_on = bool(polish.get("cursor", True)), bool(polish.get("zoom", True))
    rec_dir = workdir / "rec"
    rec_dir.mkdir(exist_ok=True)
    for old in rec_dir.glob("*.webm"):
        old.unlink()

    timeline: list[dict] = []
    zooms: list[dict] = []
    state = {"x": WIDTH / 2, "y": HEIGHT / 2}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            record_video_dir=str(rec_dir), record_video_size={"width": WIDTH, "height": HEIGHT},
        )
        if cursor_on:
            context.add_init_script(CURSOR_JS)
        page = context.new_page()
        page.set_default_timeout(6000)
        t0 = time.monotonic()
        now = lambda: time.monotonic() - t0

        def move(x: float, y: float) -> None:
            sx, sy = state["x"], state["y"]
            # Each CDP move is slow while recording, so pace by the clock rather than by step count.
            span = min(0.9, max(0.35, math.hypot(x - sx, y - sy) / 1400)) if cursor_on else 0
            begin = time.monotonic()
            while span and (u := (time.monotonic() - begin) / span) < 1:
                e = u * u * (3 - 2 * u)
                page.mouse.move(sx + (x - sx) * e, sy + (y - sy) * e)
            page.mouse.move(x, y)
            state["x"], state["y"] = x, y

        def locate(target: str) -> tuple[float, float]:
            loc = page.locator(target).first
            loc.wait_for(state="visible", timeout=5000)
            loc.scroll_into_view_if_needed(timeout=3000)
            time.sleep(0.3)
            box = loc.bounding_box()
            if not box:
                raise RuntimeError("element has no box")
            return box["x"] + min(box["width"] / 2, 220), box["y"] + box["height"] / 2

        def settle() -> None:
            try:
                page.wait_for_load_state("load", timeout=4000)
            except Exception:
                pass

        def run_step(step: dict) -> tuple[float, float, float] | None:
            """Runs one step; returns (arrive_time, x, y) when a zoom target exists."""
            action, target, value = step["action"], step["target"], step["value"]
            if action == "goto":
                page.goto(urljoin(base_url, target or "/"), wait_until="load", timeout=20000)
                time.sleep(0.8)
                return None
            if action == "wait":
                time.sleep(float(value or 1))
                return None
            if action == "press":
                page.keyboard.press(value or "Enter")
                settle()
                return None
            if action == "scroll":
                pixels = int(float(value or 400))
                for _ in range(12):
                    page.mouse.wheel(0, pixels / 12)
                    time.sleep(0.04)
                return None

            x, y = locate(target)
            move(x, y)
            arrived = now()
            if action == "click":
                page.mouse.click(x, y)
                time.sleep(0.35)
                settle()
            elif action == "fill":
                editable = page.locator(target).first.evaluate(
                    "el => el.isContentEditable || (el.matches('input,textarea') && !['button','submit','checkbox','radio','file'].includes(el.type))")
                page.mouse.click(x, y)
                if editable:
                    page.keyboard.press("Control+A")
                    page.keyboard.type(value, delay=55)
                else:
                    log(f"  '{target}' is not a text field; clicked it instead of typing.")
            elif action == "select":
                loc = page.locator(target).first
                try:
                    loc.select_option(label=value)
                except Exception:
                    loc.select_option(value=value)
            return arrived, x, y

        try:
            for i, step in enumerate(steps):
                if stop.is_set():
                    raise Cancelled()
                start = now()
                min_end = start + LEAD_IN + durations[i] + TAIL
                try:
                    hit = run_step(step)
                    if hit and zoom_on and step["action"] in ("click", "fill", "select"):
                        zooms.append({"t0": hit[0], "t1": now() + 0.7, "x": hit[1], "y": hit[2]})
                except Exception as e:
                    log(f"Step {i + 1} ({step['action']} {step['target']}) skipped: {str(e).splitlines()[0][:160]}")
                    try:
                        page.screenshot(path=str(workdir / f"step{i + 1}_failed.png"))
                    except Exception:
                        pass
                time.sleep(max(0.4, min_end - now()))
                timeline.append({"start": start, "end": now(), "lead": LEAD_IN})
            time.sleep(0.8)
            total = now()
        finally:
            video = page.video
            context.close()
            video_path = Path(video.path()) if video else None
            browser.close()

    if not video_path or not video_path.exists():
        raise RuntimeError("Playwright did not produce a video file.")
    return {"video": video_path, "timeline": timeline, "zooms": zooms, "total": total}
