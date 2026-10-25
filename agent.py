"""ReelRadar: Instagram Reels trend and verdict agent (Agentverse hosted, Chat Protocol).

Secrets (Agent Secrets panel):
  ASI1_API_KEY    injected by Agentverse (ASI:One LLM, text reasoning)
  TAVILY_API_KEY  live web research for trends   (optional, strongly recommended)
  GEMINI_API_KEY  watches video / looks at images (optional)
  GEMINI_MODEL    optional override
"""
import base64
import json
import os
import re
from datetime import datetime, timezone
from uuid import uuid4

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    ResourceContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

try:  # needed only to receive uploaded video/images
    from uagents_core.storage import ExternalStorage
except Exception:  # pragma: no cover
    ExternalStorage = None

agent = Agent(
    name="reels_radar",
    port=8000,
    mailbox=True,
)
chat_proto = Protocol(spec=chat_protocol_spec)

HOURLY_LIMIT = 12
MAX_INLINE_BYTES = 14 * 1024 * 1024  # base64 inflates ~33%; Gemini inline cap is ~20 MB
STORAGE_URL = "https://agentverse.ai/v1/storage"

HELP = (
    "**ReelRadar** judges whether an Instagram Reel idea will work, using current web research.\n\n"
    "Try:\n"
    "- *Will a 'day in my life as a nurse' reel work right now?*\n"
    "- *What Reels formats are trending for home workouts?*\n"
    "- Send a **video or screenshot** (upload it), or a **public YouTube/Shorts link**, "
    "and I will watch it and give feedback.\n\n"
    "I cannot open Instagram links (Instagram blocks it), so upload the file instead. "
    "My score is an estimate from public research, not Instagram analytics."
)

INTENT_SYS = (
    "You help an Instagram Reels analyst. Return ONLY JSON: "
    '{"intent": "verdict"|"trends"|"help", "niche": "<1-4 words or empty>", '
    '"idea": "<the reel idea being judged, one sentence, or empty>", '
    '"queries": ["<3 short web-search queries for CURRENT Instagram Reels trends for this niche/format>"]}. '
    "verdict = they ask if a reel/idea/video will work or perform; trends = they ask what is trending; "
    "help = greeting or capability question. Include the month and year from TODAY in the queries. TODAY={today}"
)

VERDICT_SYS = (
    "You are ReelRadar, a careful Instagram Reels analyst. Judge whether the idea or video is likely to "
    "perform using ONLY (a) the numbered web snippets and (b) the video analysis you are given.\n"
    "Rules: every claim about what is trending must cite a snippet like [2]; if the snippets do not support "
    "a trend claim, do not make it. If no snippets are given, say research was unavailable and set "
    'confidence to "low". Never promise views or virality; the score (0-100) is a judgment, not a reach '
    "prediction. If no video analysis is given, do not invent anything about the video.\n"
    "Return ONLY JSON: "
    '{"score": 0-100, "verdict": "Likely to work"|"Could work with changes"|"Unlikely to work", '
    '"confidence": "low"|"medium"|"high", "why": ["3-5 short reasons"], '
    '"trend_evidence": [{"claim": "...", "source": <snippet number>}], '
    '"video_feedback": ["only if a video analysis was given"], '
    '"improvements": ["3-5 concrete changes"], "hook_options": ["3 hooks for the first 3 seconds"]}'
)

TRENDS_SYS = (
    "You are ReelRadar. Write a short markdown briefing of what is trending on Instagram Reels for the "
    "user's niche using ONLY the numbered web snippets. 3-5 trends; for each: what it is, why it works, "
    "how to use it, and a citation like [2]. If no snippets are given, say live research is unavailable and "
    "keep it generic and clearly labelled. End with one line: Confidence: low/medium/high."
)

WATCH_PROMPT = (
    "You are analysing an Instagram Reel candidate. Describe ONLY what you can actually see and hear; "
    "write 'not visible' when unsure. Cover: hook in the first 3 seconds, format/genre, pacing and cuts, "
    "on-screen text and captions, audio (voiceover, music, trending sound?), length, call to action, "
    "visual and audio quality, and the biggest viewer-retention risks. Be concise, as bullet points."
)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------- model calls
def asi_chat(system: str, user: str, max_tokens: int = 1500, timeout: int = 45) -> str:
    key = _env("ASI1_API_KEY")
    if not key:
        raise RuntimeError("ASI1_API_KEY is not set")
    base = _env("ASI1_BASE_URL", "https://api.asi1.ai/v1").rstrip("/")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": "asi1",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_json(text: str):
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def web_search(query: str, time_range: str = "month", n: int = 5):
    """Returns a list of results, or None when no search key is configured."""
    key = _env("TAVILY_API_KEY")
    if not key:
        return None
    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "topic": "general", "search_depth": "basic",
              "max_results": n, "time_range": time_range},
        timeout=15,
    )
    resp.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "content": (r.get("content") or "")[:400]}
        for r in resp.json().get("results", [])
    ]


def gemini_watch(media: dict) -> str:
    """media = {'file_uri': ...} for a public YouTube URL, or {'mime': ..., 'data': b64} for a file."""
    key = _env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    if "file_uri" in media:
        part = {"file_data": {"file_uri": media["file_uri"]}}
    else:
        part = {"inline_data": {"mime_type": media["mime"], "data": media["data"]}}
    body = {"contents": [{"parts": [part, {"text": WATCH_PROMPT}]}]}
    models = []
    for m in (_env("GEMINI_MODEL", "gemini-3.8-flash"), "gemini-2.5-flash"):
        if m not in models:
            models.append(m)
    for model in models:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body,
            timeout=120,
        )
        if resp.status_code == 404:  # model name not available, try the next one
            continue
        resp.raise_for_status()
        parts = resp.json()["candidates"][0]["content"]["parts"]
        return "\n".join(p.get("text", "") for p in parts).strip()
    raise RuntimeError("no Gemini model available; set GEMINI_MODEL")


# ------------------------------------------------------------------- pipeline
YOUTUBE_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-?=&%.]+", re.I)
INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/\S+", re.I)


def fallback_intent(text: str) -> dict:
    low = text.lower()
    if re.search(r"\b(will|would|work|perform|viral|flop|good idea|feedback|rate)\b", low):
        intent = "verdict"
    elif re.search(r"\b(trend|trending|popular|hot|viral formats?)\b", low):
        intent = "trends"
    elif len(low.split()) <= 4:
        intent = "help"
    else:
        intent = "verdict"
    return {"intent": intent, "niche": "", "idea": text[:200], "queries": []}


def understand(text: str) -> dict:
    today = now_utc().strftime("%B %Y")
    try:
        data = parse_json(asi_chat(INTENT_SYS.format(today=today), text, max_tokens=300, timeout=25))
        if data and data.get("intent") in ("verdict", "trends", "help"):
            return data
    except Exception:
        pass
    return fallback_intent(text)


def research(intent: dict):
    """Returns (results, note). results is [] when unavailable."""
    today = now_utc()
    niche = (intent.get("niche") or "").strip() or "general"
    queries = [q for q in (intent.get("queries") or []) if isinstance(q, str) and q.strip()][:3]
    if not queries:
        queries = [
            f"trending Instagram Reels {niche} {today.strftime('%B %Y')}",
            f"Instagram Reels viral formats and hooks {niche} {today.year}",
            f"Instagram Reels algorithm ranking signals {today.year}",
        ]
    if not _env("TAVILY_API_KEY"):
        return [], "Live web research is not configured, so this answer is not based on current data."
    seen, results, failures = set(), [], 0
    for query in queries:
        try:
            found = web_search(query) or web_search(query, time_range="year") or []
        except Exception:
            failures += 1
            continue
        for item in found:
            if item["url"] and item["url"] not in seen:
                seen.add(item["url"])
                results.append(item)
    results = results[:8]
    if not results:
        return [], "Web research returned nothing usable, so confidence is low."
    return results, ("Some searches failed; coverage may be thin." if failures else "")


def snippets_block(results) -> str:
    if not results:
        return "(no web snippets available)"
    return "\n".join(f"[{i}] {r['title']}: {r['content']} ({r['url']})" for i, r in enumerate(results, 1))


def sources_block(results, cited) -> str:
    chosen = [i for i in sorted(cited) if 1 <= i <= len(results)] or list(range(1, min(5, len(results)) + 1))
    return "\n".join(f"[{i}] [{results[i - 1]['title'] or results[i - 1]['url']}]({results[i - 1]['url']})" for i in chosen)


def render_verdict(data: dict, results) -> str:
    cited = set()
    for ev in data.get("trend_evidence") or []:
        try:
            cited.add(int(ev.get("source")))
        except (TypeError, ValueError):
            pass
    out = [f"## {data.get('verdict', 'Verdict')}  ·  {data.get('score', '?')}/100  ·  confidence: {data.get('confidence', 'low')}"]
    for title, key, ordered in (("Why", "why", False), ("Feedback on your video", "video_feedback", False),
                                ("How to improve", "improvements", False), ("Hook options for the first 3 seconds", "hook_options", True)):
        items = [str(x) for x in (data.get(key) or []) if x]
        if items:
            lines = [f"{n}. {x}" if ordered else f"- {x}" for n, x in enumerate(items, 1)]
            out.append(f"**{title}**\n" + "\n".join(lines))
    evidence = [e for e in (data.get("trend_evidence") or []) if isinstance(e, dict) and e.get("claim")]
    if evidence:
        out.append("**What the research says**\n" + "\n".join(f"- {e['claim']} [{e.get('source', '?')}]" for e in evidence))
    if results:
        out.append("**Sources**\n" + sources_block(results, cited))
    return "\n\n".join(out)


def analyze(text: str, media: dict | None = None, media_error: str = "") -> str:
    """Pure function: question (+ optional media) in, markdown answer out."""
    intent = understand(text) if text else {"intent": "verdict", "niche": "", "idea": "", "queries": []}
    if intent["intent"] == "help" and not media:
        return HELP
    notes = []
    if media_error:
        notes.append(media_error)
    if INSTAGRAM_RE.search(text or "") and not media:
        notes.append("I cannot open Instagram links. Upload the video file or share a public YouTube link and I will watch it.")

    video_analysis = ""
    if media:
        try:
            video_analysis = gemini_watch(media)
        except Exception as exc:
            notes.append(f"I could not analyse the media ({type(exc).__name__}: {str(exc)[:120]}), so this answer ignores it.")

    results, note = research(intent)
    if note:
        notes.append(note)

    if intent["intent"] == "trends":
        try:
            body = asi_chat(TRENDS_SYS, f"Niche: {intent.get('niche') or text}\n\nSnippets:\n{snippets_block(results)}", max_tokens=900)
        except Exception as exc:
            return f"I could not reach the reasoning model ({type(exc).__name__}). Please try again in a minute."
        cited = {int(n) for n in re.findall(r"\[(\d+)\]", body)}
        extra = f"\n\n**Sources**\n{sources_block(results, cited)}" if results else ""
        return body.strip() + extra + _notes(notes)

    user = (f"User question: {text}\nIdea: {intent.get('idea') or text}\nNiche: {intent.get('niche') or 'unspecified'}\n\n"
            f"Web snippets:\n{snippets_block(results)}\n\nVideo analysis:\n{video_analysis or '(no video provided)'}")
    try:
        raw = asi_chat(VERDICT_SYS, user, max_tokens=1400, timeout=60)
    except Exception as exc:
        return f"I could not reach the reasoning model ({type(exc).__name__}). Please try again in a minute."
    data = parse_json(raw)
    body = render_verdict(data, results) if data else raw.strip()
    footer = "\n\n_This is an estimate from public web research and your input, not Instagram analytics. No result is guaranteed._"
    return body + _notes(notes) + footer


def _notes(notes) -> str:
    return ("\n\n> " + "\n> ".join(notes)) if notes else ""


# -------------------------------------------------------------------- chat I/O
def load_media(ctx: Context, item: ResourceContent):
    """Returns (media_dict | None, error_message)."""
    if ExternalStorage is None:
        return None, "This runtime cannot receive uploads. Send a public YouTube link instead."
    try:
        res = item.resource[0] if isinstance(item.resource, list) else item.resource
        mime = (res.metadata or {}).get("mime_type", "")
        asset_id = res.uri.rstrip("/").split("/")[-1]
        store = ExternalStorage(identity=ctx.agent.identity, storage_url=STORAGE_URL)
        asset = store.download(asset_id)
        b64 = asset.get("contents") or asset.get("content") or ""
        mime = mime or asset.get("mime_type", "")
        if not (mime.startswith("video/") or mime.startswith("image/")):
            return None, f"I can only analyse video or images (got '{mime or 'unknown'}')."
        if len(b64) * 3 // 4 > MAX_INLINE_BYTES:
            return None, "That file is too large for me (limit about 14 MB). Trim or compress it, or share a public YouTube link."
        return {"mime": mime, "data": b64}, ""
    except Exception as exc:
        return None, f"I could not read the upload ({type(exc).__name__})."


def rate_limited(ctx: Context, sender: str) -> bool:
    bucket = now_utc().strftime("%Y%m%d%H")
    try:
        state = ctx.storage.get(sender) or {}
        if state.get("h") != bucket:
            state = {"h": bucket, "n": 0}
        state["n"] += 1
        ctx.storage.set(sender, state)
        return state["n"] > HOURLY_LIMIT
    except Exception:
        return False


async def reply(ctx: Context, sender: str, text: str):
    await ctx.send(sender, ChatMessage(timestamp=now_utc(), msg_id=uuid4(), content=[TextContent(type="text", text=text)]))


@chat_proto.on_message(ChatMessage)
async def on_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=now_utc(), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    resources = [c for c in msg.content if isinstance(c, ResourceContent)]
    started = any(isinstance(c, StartSessionContent) for c in msg.content)
    if not text and not resources:
        if started:
            await reply(ctx, sender, HELP)
        return
    if rate_limited(ctx, sender):
        await reply(ctx, sender, "You have reached the hourly limit for this free agent. Please try again later.")
        return

    media, media_error = None, ""
    if resources:
        media, media_error = load_media(ctx, resources[0])
    if media is None:
        yt = YOUTUBE_RE.search(text)
        if yt:
            media = {"file_uri": yt.group(0)}
    await reply(ctx, sender, "Researching current Reels trends" + (" and watching your video" if media else "") + "... this takes about a minute.")
    try:
        answer = analyze(text or "Will this reel work?", media, media_error)
    except Exception as exc:  # never leave the user without an answer
        ctx.logger.error(f"analyze failed: {type(exc).__name__}")
        answer = "Something went wrong on my side. Please try again in a minute."
    await reply(ctx, sender, answer)


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(chat_proto, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
