"""Turns the page snapshots and the user's description into a narrated, executable storyboard."""
import json

from . import llm

LANGUAGES = {
    "en": "English", "hi": "Hindi", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ar": "Arabic", "ru": "Russian",
    "ta": "Tamil", "te": "Telugu", "bn": "Bengali", "mr": "Marathi", "gu": "Gujarati",
}
ACTIONS = {"goto", "click", "fill", "select", "press", "hover", "scroll", "wait"}
WORDS_PER_SECOND = 2.4

SYSTEM = """You are a product-demo director. You plan a screen recording of a running web app together \
with the voice-over, one narration line per step. The recorder drives a real browser with Playwright.

Reply with ONLY a JSON object:
{
  "title": "video title",
  "tagline": "short subtitle for the intro card",
  "outro": "short closing line for the end card",
  "steps": [
    {"action": "goto|click|fill|select|press|hover|scroll|wait",
     "target": "selector copied EXACTLY from the snapshots (or a URL/path for goto)",
     "value": "text to type / option label / key name / pixels to scroll / seconds to wait",
     "narration": "what the narrator says while this step happens"}
  ]
}
Rules:
- Use ONLY selectors that appear in the snapshots. Never invent selectors.
- The first step is `goto` with the app's start URL. `goto` may later visit other URLs listed in the snapshots.
- `fill` needs realistic sample data (names, emails, titles) so the app looks lived-in, never "test" or "asdf".
- `press` uses key names like Enter or Tab (no target needed). `scroll` takes a number of pixels (positive = down).
- Every step needs a narration written as natural speech, 1-2 sentences, no selectors or technical jargon about the \
recording itself. Introduce value to the viewer, do not describe mouse movements.
- `fill` is ONLY for text inputs/textareas (tag input/textarea, not buttons). Use `click` for buttons and links.
- If a snapshot's page_height is larger than viewport_height, content continues below the fold: add `scroll` steps (about 400-600 px each) so the video actually reaches the sections the narration talks about.
- Narrate only what the snapshots show (headings, text, elements). Never claim features you cannot see there.
- Follow the user's description for focus, tone and length. End with a short wrap-up step (action `wait`, value 2).
- Keep to the requested number of steps and total word budget."""


def build_storyboard(cfg: dict, plan: dict, snapshots: list[dict], description: str, opts: dict) -> dict:
    length = int(opts.get("length") or 60)
    speed = float(opts.get("tts", {}).get("speed") or 1.0)
    words = int(length * WORDS_PER_SECOND * speed)
    steps = max(4, length // 7)
    language = LANGUAGES.get(opts.get("language", "en"), "English")
    user = (
        f"USER DESCRIPTION OF THE VIDEO:\n{description or '(none - make a clear overview demo)'}\n\n"
        f"APP: {plan.get('app_name')} - {plan.get('tagline')}\n{plan.get('summary')}\n"
        f"Features: {', '.join(plan.get('features', []))}\n\n"
        f"Start URL: {snapshots[0]['url'] if snapshots else plan.get('url')}\n"
        f"Language for narration, title, tagline and outro: {language}\n"
        f"Tone: {opts.get('tone', 'friendly')}\n"
        f"Target length: about {length} seconds, roughly {steps} steps, about {words} narration words in total.\n\n"
        f"PAGE SNAPSHOTS:\n{json.dumps(snapshots, ensure_ascii=False, separators=(',', ':'))}"
    )
    return clean_storyboard(llm.chat_json(cfg, SYSTEM, user), plan)


def clean_storyboard(raw: dict, plan: dict) -> dict:
    steps = []
    for s in raw.get("steps", []):
        if not isinstance(s, dict) or s.get("action") not in ACTIONS:
            continue
        steps.append({
            "action": s["action"],
            "target": str(s.get("target") or ""),
            "value": str(s.get("value") if s.get("value") is not None else ""),
            "narration": str(s.get("narration") or "").strip(),
        })
    if not steps:
        raise llm.LLMError("The model returned no usable steps. Try again or use a stronger model.")
    name = plan.get("app_name") or "Demo"
    return {
        "title": str(raw.get("title") or name),
        "tagline": str(raw.get("tagline") or plan.get("tagline") or ""),
        "outro": str(raw.get("outro") or ""),
        "steps": steps,
    }
