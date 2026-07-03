import base64
import glob
import io
import os
import random
import re
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
from elevenlabs.client import ElevenLabs
from openai import OpenAI
try:
    from google import genai as genai_sdk
    GEMINI_AVAILABLE = True
except ImportError:
    try:
        import google.generativeai as genai_sdk
        GEMINI_AVAILABLE = True
    except ImportError:
        GEMINI_AVAILABLE = False

try:
    import fal_client
    FAL_AVAILABLE = True
except ImportError:
    FAL_AVAILABLE = False
from PIL import Image

st.set_page_config(page_title="Shorts Maker", page_icon="🎬", layout="wide")

# ---------------------------------------------------------------------------
# Password gate — set APP_PASSWORD in Streamlit secrets to enable.
# If the secret is not set, the app runs open (for local dev).
# ---------------------------------------------------------------------------
def check_password() -> bool:
    """Returns True if the user has entered the correct password."""
    app_password = st.secrets.get("APP_PASSWORD", "")
    if not app_password:
        return True   # No password configured → open access (local dev)

    if st.session_state.get("authenticated"):
        return True

    # Centre the login card
    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        st.markdown("## 🎬 Shorts Maker")
        st.markdown("Enter the password to continue.")
        pwd = st.text_input("Password", type="password", key="pwd_input")
        if st.button("Login", type="primary", use_container_width=True):
            if pwd == app_password:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("❌ Incorrect password. Try again.")
    return False

if not check_password():
    st.stop()   # Everything below this line is hidden until login succeeds

MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")

# Git doesn't track empty folders, so on a fresh deploy (e.g. Streamlit Cloud)
# these may not exist even if they were created locally. Recreate them at
# startup so pick_music_track() never has to deal with a missing directory.
_MOOD_FOLDERS = [
    "upbeat", "dramatic", "calm", "inspirational", "suspense",
    "sad", "romantic", "epic", "energetic", "nostalgic",
]
for _mood in _MOOD_FOLDERS:
    os.makedirs(os.path.join(MUSIC_DIR, _mood), exist_ok=True)

SFX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sfx")

# ---------------------------------------------------------------------------
# SFX library — split into two kinds because they need different mixing:
#
#   ONE-SHOT: a single short hit that plays once at the start of a scene
#   (a bell, a ding, a camera click). Trimmed if longer than the scene.
#
#   AMBIENT/SUSTAINED: a continuous bed that should loop/fill the whole
#   scene (ocean waves, a drone, distant thunder, a heartbeat). These are
#   the layers a script means when it lists several things happening "in
#   the background" at once (e.g. "deep ocean waves / low cinematic drone /
#   distant thunder / heartbeat beginning in the background").
#
# Each key = a folder under sfx/. Drop .mp3/.wav/.m4a files into it. Both
# lists are general-purpose, not tied to one script's topic.
# ---------------------------------------------------------------------------
_SFX_ONESHOT_FOLDERS = [
    "whoosh", "dramatic_sting", "notification_ding", "coins", "applause",
    "camera_shutter", "tick_tock", "page_turn", "door_creak",
    "footsteps", "siren_alert", "magic_sparkle", "temple_bell",
    "crowd_gasp", "typing", "phone_ring", "explosion_soft",
    "success_chime", "sonar_ping", "thunder_crack",
]
_SFX_AMBIENT_FOLDERS = [
    "ocean_waves", "cinematic_drone", "thunder_distant", "heartbeat_loop",
    "wind_ambience", "rain_ambience", "fire_crackle", "city_ambience",
    "forest_ambience", "crowd_ambience", "engine_hum", "clock_ambience",
]
_SFX_FOLDERS = _SFX_ONESHOT_FOLDERS + _SFX_AMBIENT_FOLDERS
for _sfx in _SFX_FOLDERS:
    os.makedirs(os.path.join(SFX_DIR, _sfx), exist_ok=True)


# ---------------------------------------------------------------------------
# Shared session state
# ---------------------------------------------------------------------------
if "generated_images" not in st.session_state:
    st.session_state.generated_images = {}   # scene_id -> (filename, bytes)
if "generated_images_log" not in st.session_state:
    st.session_state.generated_images_log = None
if "step1_script_df" not in st.session_state:
    st.session_state.step1_script_df = None
if "step1_scene_prompts" not in st.session_state:
    st.session_state.step1_scene_prompts = {}
if "animated_clips" not in st.session_state:
    st.session_state.animated_clips = {}

# ---------------------------------------------------------------------------
# Mood classifier (Step 2 background music)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Music moods — 10 categories. Create a folder under music/ for each key
# and drop .mp3/.wav/.m4a files in. The classifier picks the best match.
# ---------------------------------------------------------------------------
MOOD_KEYWORDS = {
    "upbeat": [
        "win", "won", "victory", "celebrat", "happy", "joy", "exciting",
        "amazing", "success", "triumph", "champion", "festival", "fun",
        "bright", "smile", "achieve", "proud", "cheer", "party", "dance",
    ],
    "dramatic": [
        "war", "battle", "fight", "danger", "crisis", "death", "fear",
        "tension", "struggle", "fierce", "darkness", "betray", "shock",
        "underdog", "pressure", "risk", "storm", "collapse", "desperate",
        "villain", "enemy", "confront", "survive",
    ],
    "calm": [
        "calm", "quiet", "peace", "gentle", "slow", "reflect", "history",
        "memory", "story", "remember", "morning", "soft", "ordinary",
        "simple", "everyday", "breathe", "still", "serene",
    ],
    "inspirational": [
        "hope", "dream", "rise", "believe", "inspire", "courage", "overcome",
        "strong", "persevere", "never give up", "faith", "possible", "will",
        "determination", "motivat", "aspire", "goal", "future", "change",
    ],
    "suspense": [
        "secret", "hidden", "mystery", "unknown", "shadow", "watch", "lurk",
        "follow", "hunt", "trace", "clue", "reveal", "uncover", "suspect",
        "silence", "wait", "dark", "tension", "crawl", "creep",
    ],
    "sad": [
        "sad", "loss", "grief", "mourn", "alone", "lonely", "cry", "tears",
        "heartbreak", "miss", "gone", "farewell", "goodbye", "regret",
        "pain", "suffer", "sorrow", "tragedy", "lost", "broke",
    ],
    "romantic": [
        "love", "heart", "together", "forever", "romance", "kiss", "hold",
        "care", "affection", "partner", "couple", "wed", "marriage",
        "tender", "warmth", "embrace", "cherish", "devoted",
    ],
    "epic": [
        "legend", "hero", "quest", "journey", "great", "mighty", "power",
        "glory", "conquer", "army", "throne", "king", "empire", "ancient",
        "warrior", "sword", "shield", "myth", "destiny", "chosen",
    ],
    "energetic": [
        "run", "race", "sprint", "fast", "speed", "rush", "action",
        "adrenaline", "pump", "explosive", "intense", "burst", "fire",
        "charge", "push", "go", "now", "fast", "hustle", "grind",
    ],
    "nostalgic": [
        "nostalg", "childhood", "remember", "those days", "used to", "back then",
        "classic", "vintage", "old", "young", "grew up", "past", "decade",
        "era", "generation", "tradition", "heritage", "roots", "origin",
    ],
}


def classify_mood(full_text: str) -> str:
    text = full_text.lower()
    scores = {mood: 0 for mood in MOOD_KEYWORDS}
    for mood, keywords in MOOD_KEYWORDS.items():
        for kw in keywords:
            scores[mood] += len(re.findall(kw, text))
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "calm"


def pick_music_track(mood: str, music_root: str):
    """Pick a random track for the given mood. Falls back to any available
    track from other mood folders if the requested mood folder is empty.
    Returns None (never crashes) if no music directory/files exist at all —
    background music is optional, not required for the video to succeed."""
    def tracks_in(folder):
        result = []
        if not os.path.isdir(folder):
            return result
        for ext in ("*.mp3", "*.wav", "*.m4a"):
            result.extend(glob.glob(os.path.join(folder, ext)))
        return result

    if not os.path.isdir(music_root):
        return None

    # Try exact mood first
    candidates = tracks_in(os.path.join(music_root, mood))
    if candidates:
        return random.choice(candidates)

    # Fallback: scan all mood folders and pick from whatever has files
    all_tracks = []
    for entry in os.scandir(music_root):
        if entry.is_dir():
            all_tracks.extend(tracks_in(entry.path))
    return random.choice(all_tracks) if all_tracks else None


def pick_sfx_clip(keyword: str, sfx_root: str):
    """Resolve a free-text SFX cue (e.g. 'coins falling', 'deep ocean waves') to
    a (file_path, folder_name) pair in the sfx/ library. Tries an exact
    folder-name match, then substring match, then a small hint table for
    common phrasings, then fuzzy match. Returns None (never raises) if
    nothing matches — a scene with no match just plays with no SFX for
    that cue."""
    if not keyword or not str(keyword).strip():
        return None
    if not os.path.isdir(sfx_root):
        return None

    def tracks_in(folder):
        result = []
        for ext in ("*.mp3", "*.wav", "*.m4a"):
            result.extend(glob.glob(os.path.join(folder, ext)))
        return result

    key = re.sub(r"[^a-z0-9 ]", "", str(keyword).strip().lower())
    key_slug = key.replace(" ", "_")
    folders = [e.name for e in os.scandir(sfx_root) if e.is_dir()]

    def resolve(folder_name):
        candidates = tracks_in(os.path.join(sfx_root, folder_name))
        return (random.choice(candidates), folder_name) if candidates else None

    # 1) exact folder name match
    if key_slug in folders:
        hit = resolve(key_slug)
        if hit:
            return hit

    # 2) substring match either direction (e.g. "coins falling" ~ "coins")
    for folder in folders:
        if folder in key_slug or key_slug in folder:
            hit = resolve(folder)
            if hit:
                return hit

    # 3) hint table for common phrasings that don't literally contain the
    # folder name (e.g. "deep ocean waves" -> ocean_waves folder)
    SFX_HINTS = {
        "ocean_waves": ["ocean", "sea wave", "waves crash", "surf"],
        "cinematic_drone": ["drone", "low hum", "tension pad", "ambient tone"],
        "thunder_distant": ["distant thunder", "thunder rumble", "storm rumble"],
        "thunder_crack": ["thunder crack", "lightning strike", "thunder clap"],
        "sonar_ping": ["sonar", "submarine ping", "radar ping"],
        "heartbeat_loop": ["heartbeat", "heart beat", "pulse beat"],
        "wind_ambience": ["wind blowing", "howling wind", "breeze"],
        "rain_ambience": ["rain falling", "rainfall", "raindrops"],
        "fire_crackle": ["fire crackle", "campfire", "burning"],
        "city_ambience": ["city noise", "traffic ambience", "street ambience"],
        "forest_ambience": ["forest sounds", "birds chirping", "jungle ambience"],
        "crowd_ambience": ["crowd murmur", "crowd noise", "chatter"],
        "engine_hum": ["engine sound", "machine hum", "motor hum"],
    }
    for folder, hints in SFX_HINTS.items():
        if folder in folders and any(h in key for h in hints):
            hit = resolve(folder)
            if hit:
                return hit

    # 4) fuzzy match on the folder-name level
    import difflib
    close = difflib.get_close_matches(key_slug, folders, n=1, cutoff=0.5)
    if close:
        hit = resolve(close[0])
        if hit:
            return hit

    return None


def parse_sfx_cues(cell_text: str):
    """Split a possibly multi-line/bulleted SFX cell into individual cues,
    e.g.:
        'Deep ocean waves\n* Low cinematic drone\n* Distant thunder\n
         * Submarine sonar ping (very faint)\n* Slow heartbeat beginning
         in the background'
    becomes a list of cue dicts, one per layer, each carrying a parsed
    volume (from '(faint)'/'(loud)' hints) and whether it should fade in
    (from 'beginning'/'building'/'fading in' phrasing). A plain single-line
    cell like 'coins falling' becomes a list with exactly one cue, so the
    same code path handles both simple and layered SFX."""
    if not cell_text or not str(cell_text).strip():
        return []

    raw = str(cell_text)
    # Split on newlines/semicolons, and on bullet markers (*, -, •) at the
    # start of a line.
    parts = re.split(r"[\n;]+", raw)
    parts = [re.sub(r"^\s*[\*\-•]\s*", "", p).strip() for p in parts]
    parts = [p for p in parts if p]

    # Single line with no bullets but multiple comma-separated cues
    # (rare, but handle e.g. "thunder, sonar ping")
    if len(parts) == 1 and "," in parts[0]:
        comma_parts = [p.strip() for p in parts[0].split(",") if p.strip()]
        if len(comma_parts) > 1:
            parts = comma_parts

    cues = []
    for p in parts[:6]:   # cap layers per scene to keep mixes sane
        text = p
        volume = 1.0
        low = text.lower()

        m_faint = re.search(r"\((very faint|faint|quiet|subtle|low)\)", low)
        if m_faint:
            volume = 0.3 if "very" in m_faint.group(1) else 0.5
            text = re.sub(r"\([^)]*\)", "", text).strip()

        m_loud = re.search(r"\((loud|prominent|strong)\)", low)
        if m_loud:
            volume = 1.2
            text = re.sub(r"\([^)]*\)", "", text).strip()

        fade_in = any(
            k in low for k in
            ("beginning", "fading in", "fade in", "building", "growing", "starts low")
        )

        if text.strip():
            cues.append({"text": text.strip(), "volume": volume, "fade_in": fade_in})

    return cues


def safe_name(s: str) -> str:
    keep = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(c for c in str(s).strip() if c in keep).strip().replace(" ", "_") or "scene"


def ensure_9x16(img: Image.Image, width: int, height: int) -> Image.Image:
    """Centre-crop then resize any image to exact w×h (9:16)."""
    if img.size == (width, height):
        return img
    target_ratio = width / height
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    if abs(src_ratio - target_ratio) > 0.01:
        if src_ratio > target_ratio:
            new_w = int(src_h * target_ratio)
            left = (src_w - new_w) // 2
            img = img.crop((left, 0, left + new_w, src_h))
        else:
            new_h = int(src_w / target_ratio)
            top = (src_h - new_h) // 2
            img = img.crop((0, top, src_w, top + new_h))
    return img.resize((width, height), Image.LANCZOS)


# ===========================================================================
# STEP 1 — OpenAI Image Generation (gpt-image-1)
# ===========================================================================
def render_step1():
    st.header("🖼️ Step 1: Generate Scene Images")
    st.caption(
        "Upload an Excel with **id**, **script_text**, **image_prompt** → "
        "OpenAI gpt-image-1 → true 9:16 PNG images ready for Step 2"
    )

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.subheader("Step 1 — Image settings")

        image_provider = st.radio(
            "Image provider",
            ["OpenAI (gpt-image-1)", "Gemini (imagen-3.0)"],
            index=0,
            key="step1_provider",
            help=(
                "OpenAI gpt-image-1 — reliable, good quality, pay-per-image.\n\n"
                "Gemini imagen-3.0 — Google's image model, often cheaper. "
                "Requires GEMINI_API_KEY in secrets."
            ),
        )
        use_gemini = image_provider == "Gemini (imagen-3.0)"

        quality = st.selectbox(
            "Image quality",
            ["medium", "low", "high"] if not use_gemini else ["standard", "hd"],
            index=0,
            help=(
                "OpenAI: low ~$0.011 · medium ~$0.042 · high ~$0.167/image\n"
                "Gemini: standard · hd"
            ),
            key="step1_quality",
        )

        style_suffix = st.text_input(
            "Style suffix (appended to every prompt)",
            value="cinematic, highly detailed, dramatic lighting",
            help="Keeps a consistent visual style across all scenes.",
            key="step1_style",
        )

        pace_delay = st.slider(
            "Delay between images (seconds)", 0, 5, 1,
            help="Small pause to avoid rate-limit bursts on large batches.",
            key="step1_pace_delay",
        )
        max_retries = st.slider(
            "Retries per image on failure", 0, 5, 2,
            key="step1_retries",
        )
        project_id_step1 = st.text_input(
            "Project ID",
            value=datetime.now().strftime("scenes_%Y%m%d_%H%M%S"),
            help="Used as the output ZIP filename.",
            key="step1_project_id",
        )

    # ── API client init (after sidebar so we know which provider is selected) ─
    use_gemini = st.session_state.get("step1_provider", "OpenAI (gpt-image-1)") == "Gemini (imagen-3.0)"

    if use_gemini:
        if "GEMINI_API_KEY" not in st.secrets:
            st.error(
                "Gemini selected but GEMINI_API_KEY is missing from secrets. "
                "Get one at https://aistudio.google.com/app/apikey and add it to your secrets."
            )
            return
        if not GEMINI_AVAILABLE:
            st.error("google-generativeai package not installed. Add 'google-generativeai' to requirements.txt.")
            return
        gemini_api_key = st.secrets["GEMINI_API_KEY"]
        openai_client = None
        gemini_client = None   # not used — we call the API directly in call_gemini_image
    else:
        if "OPENAI_API_KEY" not in st.secrets:
            st.error(
                "Missing secret: OPENAI_API_KEY. "
                "Add it under App Settings → Secrets or .streamlit/secrets.toml."
            )
            return
        openai_client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
        gemini_client = None

    # ── Cost info banner ──────────────────────────────────────────────────────
    cost_map = {"low": 0.011, "medium": 0.042, "high": 0.167, "standard": 0.02, "hd": 0.08}
    st.info(
        f"💰 **Estimated cost:** ~${cost_map[quality]:.3f} per image at **{quality}** quality "
        f"(1024×1792 px, 9:16). "
        "No daily cap — pay only for what you generate."
    )

    # ── File upload ───────────────────────────────────────────────────────────
    script_file = st.file_uploader("Scene Excel (.xlsx)", type=["xlsx", "xls"], key="step1_upload")
    st.caption(
        "Excel must have columns: **id** · **script_text** · **image_prompt**. "
        "Image filenames will be `<id>.png` — these must match the `id` column in Step 2's Excel."
    )

    if not script_file:
        st.info("Upload your scene Excel to get started.")
        if st.button("Download sample template", key="step1_sample_btn"):
            sample = pd.DataFrame({
                "id": ["scene_01", "scene_02", "scene_03"],
                "script_text": [
                    "In 1983, nobody expected India to win the World Cup.",
                    "The team walked onto the field at Lord's, underdogs once again.",
                    "Kapil Dev led from the front, calm under pressure.",
                ],
                "image_prompt": [
                    "A packed cricket stadium in 1983, vintage photograph style",
                    "Indian cricket team walking onto the Lord's field, dramatic wide shot",
                    "Kapil Dev standing confidently on the pitch, determined expression",
                ],
            })
            buf = io.BytesIO()
            sample.to_excel(buf, index=False)
            st.download_button(
                "⬇️ scene_template.xlsx",
                data=buf.getvalue(),
                file_name="scene_template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="step1_sample_dl",
            )
        return

    try:
        df = pd.read_excel(script_file)
    except Exception as e:
        st.error(f"Could not read Excel: {e}")
        return

    df.columns = [str(c).strip().lower() for c in df.columns]
    missing_cols = {"id", "script_text", "image_prompt"} - set(df.columns)
    if missing_cols:
        st.error(f"Excel is missing column(s): {', '.join(sorted(missing_cols))}")
        return

    # Save to session state so Step 2 can reuse it without a re-upload
    st.session_state.step1_script_df = df.copy()

    df = df[df["image_prompt"].notna()].reset_index(drop=True)
    n = len(df)
    est_cost = n * cost_map[quality]
    st.success(f"Loaded **{n} scene(s)**. Estimated cost: **${est_cost:.3f}**")
    st.dataframe(df[["id", "script_text", "image_prompt"]], use_container_width=True)

    # ── Generation helpers ───────────────────────────────────────────────────

    def is_policy_error(err: str) -> bool:
        """Return True if the error is an OpenAI content policy rejection."""
        err_lower = err.lower()
        policy_signals = [
            "content_policy", "safety system", "content policy",
            "violates", "rejected", "inappropriate", "unsafe",
            "content management policy", "policy violation",
            "your request was rejected", "moderation",
        ]
        return any(signal in err_lower for signal in policy_signals)

    def rewrite_prompt_safe(original_prompt: str, scene_context: str) -> str:
        """Ask GPT-4o-mini to rewrite the prompt to be policy-safe
        while preserving the visual intent as closely as possible."""
        system = (
            "You are an expert at rewriting image generation prompts to comply with "
            "OpenAI content policies while preserving the original visual intent. "
            "Rules: Remove or replace anything depicting violence, blood, weapons, "
            "real people, sensitive political content, or anything that could be flagged. "
            "Replace with cinematic equivalents — e.g. 'aftermath of battle' instead of "
            "'soldiers dying', 'determined crowd' instead of 'angry mob with weapons'. "
            "Keep the mood, setting, and cinematic style. "
            "Return ONLY the rewritten prompt — no explanation, no quotes, no preamble."
        )
        user = (
            f"Scene context (narration): {scene_context}\n\n"
            f"Original image prompt that was rejected: {original_prompt}\n\n"
            "Rewrite this prompt to be fully policy-safe while keeping the same "
            "visual mood and setting for a YouTube Shorts scene."
        )
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=200,
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()

    SAFE_FALLBACK_PROMPTS = [
        "cinematic aerial view of a vast landscape at golden hour, dramatic clouds, 9:16 vertical",
        "dramatic close-up of hands holding something meaningful, cinematic lighting, 9:16 vertical",
        "silhouette of a person standing at the edge of a cliff at sunset, epic, 9:16 vertical",
        "ancient stone architecture with dramatic shadows and warm light, 9:16 vertical",
        "vast crowd of people in a stadium, aerial view, dramatic atmosphere, 9:16 vertical",
        "cinematic shot of a city skyline at dusk, fog and golden light, 9:16 vertical",
    ]

    def call_openai_image(prompt: str) -> bytes:
        """Raw OpenAI image API call — returns PNG bytes."""
        response = openai_client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            n=1,
            size="1024x1536",
            quality=quality,
        )
        b64 = response.data[0].b64_json
        img_bytes = base64.b64decode(b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img = ensure_9x16(img, 1080, 1920)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def call_gemini_image(prompt: str) -> bytes:
        """Gemini image call using gemini-2.5-flash-image (Nano Banana) — returns PNG bytes.
        Uses generate_content with IMAGE modality — the correct method as of mid-2026.
        Imagen 3 is deprecated; the new recommended model is gemini-2.5-flash-image."""
        from google import genai as _genai
        from google.genai.types import GenerateContentConfig, Modality
        client = _genai.Client(api_key=gemini_api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash-image",
            contents=f"Generate a photorealistic 9:16 vertical image: {prompt}",
            config=GenerateContentConfig(
                response_modalities=[Modality.IMAGE],
            ),
        )
        # Find the image part in the response
        for part in response.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data:
                img_bytes = part.inline_data.data
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                img = ensure_9x16(img, 1080, 1920)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
        raise RuntimeError(
            "Gemini returned no image. Check your GEMINI_API_KEY and that billing is enabled."
        )

    def call_image_api(prompt: str) -> bytes:
        """Route to the selected provider."""
        if use_gemini:
            return call_gemini_image(prompt)
        return call_openai_image(prompt)

    def generate_image_openai(prompt: str, scene_id: str, script_text: str):
        """
        Robust image generation with 3-layer fallback:
          1. Try original prompt (with retries for transient errors)
          2. On policy error → GPT rewrites prompt → retry
          3. If rewrite also fails → use a safe cinematic fallback prompt
        Returns (png_bytes, final_prompt_used, note)
        """
        note = ""

        # ── Layer 1: Try original prompt ──────────────────────────────────────
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                return call_image_api(prompt), prompt, note
            except Exception as e:
                last_err = str(e)
                if is_policy_error(last_err):
                    break   # No point retrying same prompt on policy block
                if attempt < max_retries:
                    time.sleep(5)

        # ── Layer 2: Policy error → auto-rewrite prompt ───────────────────────
        if is_policy_error(last_err or ""):
            note = "⚠️ Original prompt blocked — auto-rewritten for policy compliance"
            try:
                safe_prompt = rewrite_prompt_safe(prompt, script_text)
                safe_prompt_full = f"{safe_prompt}, {style_suffix}".strip(", ")
                for attempt in range(max_retries + 1):
                    try:
                        return call_image_api(safe_prompt_full), safe_prompt_full, note
                    except Exception as e2:
                        rewrite_err = str(e2)
                        if is_policy_error(rewrite_err):
                            break
                        if attempt < max_retries:
                            time.sleep(5)
            except Exception:
                pass   # Rewrite API call itself failed — fall through

        # ── Layer 3: Safe cinematic fallback ──────────────────────────────────
        note = "⚠️ Both original and rewritten prompts blocked — used safe cinematic fallback"
        fallback_prompt = random.choice(SAFE_FALLBACK_PROMPTS)
        for attempt in range(3):
            try:
                return call_image_api(fallback_prompt), fallback_prompt, note
            except Exception:
                if attempt < 2:
                    time.sleep(5)

        raise RuntimeError(
            f"All 3 layers failed for scene '{scene_id}'. Last error: {last_err}"
        )

    if st.button("🖼️ Generate all scene images", type="primary", key="step1_generate_btn"):
        progress = st.progress(0.0)
        status = st.empty()
        live_results_box = st.empty()
        live_image_box = st.empty()
        results = []
        image_bufs = {}

        for i, row in df.iterrows():
            scene_id = str(row["id"])
            script_text = str(row.get("script_text", "")).strip()
            prompt = f"{str(row['image_prompt']).strip()}, {style_suffix}".strip(", ")
            status.write(f"⏳ Generating {i+1}/{n}: `{scene_id}`...")
            try:
                img_bytes, used_prompt, note = generate_image_openai(
                    prompt, scene_id, script_text
                )
                fname = f"{safe_name(scene_id)}.png"
                image_bufs[scene_id] = (fname, img_bytes)
                status_label = f"✅ OK{' — ' + note if note else ''}"
                results.append({
                    "id": scene_id,
                    "status": status_label,
                    "original_prompt": prompt,
                    "used_prompt": used_prompt,
                })
                status.write(f"✅ {i+1}/{n} done: `{scene_id}`")
                with live_image_box.container():
                    st.image(img_bytes, caption=f"{scene_id} — just generated", width=200)
            except Exception as e:
                results.append({
                    "id": scene_id,
                    "status": f"❌ {e}",
                    "original_prompt": prompt,
                    "used_prompt": "",
                })
                status.write(f"❌ {i+1}/{n} FAILED: `{scene_id}` — {e}")
                live_image_box.empty()

            # Update the running results table after every single scene
            with live_results_box.container():
                st.dataframe(pd.DataFrame(results), use_container_width=True)

            progress.progress((i + 1) / n)
            if pace_delay > 0 and i < n - 1:
                time.sleep(pace_delay)

        live_results_box.empty()
        live_image_box.empty()
        status.write("✅ Batch complete.")

        results_df = pd.DataFrame(results)
        st.subheader("Final Results")

        # Highlight rewritten/fallback rows so user can review what changed
        rewrites = results_df[results_df["status"].str.contains("rewritten|fallback", case=False, na=False)]
        if not rewrites.empty:
            st.warning(
                f"⚠️ {len(rewrites)} scene(s) had their prompt auto-rewritten due to policy blocks. "
                "Review the 'used_prompt' column below to see what was actually generated."
            )

        st.dataframe(results_df, use_container_width=True)

        n_ok = results_df["status"].str.startswith("✅").sum()
        st.success(f"{n_ok}/{n} images generated.")

        if n_ok == 0:
            st.error("No images succeeded. Check the errors above and your API key/billing.")
            return

        # Store in session state → Step 2 picks them up automatically
        st.session_state.generated_images = image_bufs
        st.session_state.generated_images_log = results_df
        # Also remember the prompts used, so "regenerate single image" has context
        st.session_state.step1_scene_prompts = {
            str(row["id"]): {
                "image_prompt": str(row["image_prompt"]).strip(),
                "script_text": str(row.get("script_text", "")).strip(),
            }
            for _, row in df.iterrows()
        }

        st.success(
            "✅ Images ready — switch to **② Assemble Video** tab. "
            "They are already loaded; no need to re-upload."
        )

    # ── Review & regenerate individual images ──────────────────────────────────
    # This runs every time the page renders (not just after clicking Generate),
    # so you can come back, review, and fix specific scenes any time.
    if st.session_state.generated_images:
        st.divider()
        st.subheader("🔍 Review & fix individual images")
        st.caption(
            "Not happy with a specific scene? Edit its prompt below and regenerate "
            "just that one — no need to redo the whole batch."
        )

        scene_prompts = st.session_state.get("step1_scene_prompts", {})

        for sid, (fname, ibytes) in list(st.session_state.generated_images.items()):
            with st.expander(f"🖼️ {sid}", expanded=False):
                col_img, col_controls = st.columns([1, 2])
                with col_img:
                    st.image(ibytes, use_container_width=True)
                with col_controls:
                    existing = scene_prompts.get(sid, {})
                    current_prompt = existing.get("image_prompt", "")
                    current_script = existing.get("script_text", "")

                    new_prompt = st.text_area(
                        "Image prompt",
                        value=current_prompt,
                        key=f"regen_prompt_{sid}",
                        height=100,
                    )
                    regen_key = f"regen_btn_{sid}"
                    if st.button(f"🔄 Regenerate '{sid}'", key=regen_key):
                        with st.spinner(f"Regenerating {sid}..."):
                            full_prompt = f"{new_prompt.strip()}, {style_suffix}".strip(", ")
                            try:
                                img_bytes, used_prompt, note = generate_image_openai(
                                    full_prompt, sid, current_script
                                )
                                new_fname = f"{safe_name(sid)}.png"
                                st.session_state.generated_images[sid] = (new_fname, img_bytes)
                                # Keep the edited prompt as the new baseline for next time
                                if sid not in st.session_state.step1_scene_prompts:
                                    st.session_state.step1_scene_prompts[sid] = {}
                                st.session_state.step1_scene_prompts[sid]["image_prompt"] = new_prompt.strip()
                                if note:
                                    st.warning(note)
                                st.success(f"✅ '{sid}' regenerated.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Regeneration failed: {e}")

        # Re-export ZIP button reflecting any regenerations made above
        st.divider()
        zip_buf2 = io.BytesIO()
        with zipfile.ZipFile(zip_buf2, "w", zipfile.ZIP_DEFLATED) as zf:
            for sid, (fname, ibytes) in st.session_state.generated_images.items():
                zf.writestr(fname, ibytes)
        zip_buf2.seek(0)
        st.download_button(
            "⬇️ Download updated images (.zip)",
            data=zip_buf2.getvalue(),
            file_name=f"{project_id_step1}_updated.zip",
            mime="application/zip",
            key="step1_zip_dl_updated",
        )




# ===========================================================================
# STEP 1.5 — Animate images → video clips via fal.ai Wan 2.1
# ===========================================================================
def render_step15():
    st.header("🎞️ Step 1.5: Animate Images (fal.ai Wan 2.1)")
    st.caption(
        "Turn your static scene images into true animated video clips using Wan 2.1 — "
        "people move, cameras orbit, fire and water flow. Then use these clips in Step 2."
    )

    if not FAL_AVAILABLE:
        st.error(
            "fal-client package not installed. Add `fal-client` to requirements.txt and redeploy."
        )
        return

    if "FAL_KEY" not in st.secrets:
        st.error(
            "Missing secret: FAL_KEY. "
            "Get a free API key at https://fal.ai → Dashboard → API Keys and add it to secrets."
        )
        return

    import os
    os.environ["FAL_KEY"] = st.secrets["FAL_KEY"]

    images = st.session_state.generated_images
    script_df = st.session_state.step1_script_df

    if not images:
        st.info("No images loaded yet — run Step 1 first to generate images.")
        return

    st.subheader("📋 Scenes to animate")

    # Build table of scenes with their video_prompt
    rows = []
    for sid, (fname, ibytes) in images.items():
        vp = ""
        if script_df is not None and "video_prompt" in script_df.columns:
            match = script_df[script_df["id"].astype(str) == str(sid)]
            if not match.empty:
                vp = str(match.iloc[0].get("video_prompt", "")).strip()
        rows.append({"id": sid, "video_prompt": vp or "(auto — no prompt in Excel)"})

    scenes_df = pd.DataFrame(rows)
    st.dataframe(scenes_df, use_container_width=True)

    with st.sidebar:
        st.subheader("Step 1.5 — Animation settings")
        clip_duration = st.selectbox(
            "Clip duration (seconds)", [5, 10], index=0,
            help="5 sec costs ~$0.025/clip · 10 sec costs ~$0.05/clip on fal.ai free trial.",
            key="step15_duration",
        )
        default_motion = st.text_input(
            "Default motion prompt (used if video_prompt column is empty)",
            value="cinematic slow camera push forward, smooth motion, high quality",
            key="step15_default_motion",
        )

    st.info(
        f"💰 Estimated cost: ~${len(images) * 0.025 * (clip_duration // 5):.2f} "
        f"for {len(images)} clips at {clip_duration}s each. "
        "fal.ai gives $5 free trial credit — enough for ~200 clips."
    )

    if st.button("🎞️ Animate all scenes", type="primary", key="step15_animate_btn"):
        progress = st.progress(0.0)
        status = st.empty()
        animated_clips = {}
        results = []
        total = len(images)

        for i, (sid, (fname, ibytes)) in enumerate(images.items()):
            status.write(f"⏳ Animating {i+1}/{total}: `{sid}`...")

            # Get video_prompt from Excel if available
            vp = default_motion
            if script_df is not None and "video_prompt" in script_df.columns:
                match = script_df[script_df["id"].astype(str) == str(sid)]
                if not match.empty:
                    col_val = str(match.iloc[0].get("video_prompt", "")).strip()
                    if col_val and col_val.lower() not in ("nan", "none", ""):
                        vp = col_val

            try:
                # Upload image to fal
                import tempfile, base64 as b64mod
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(ibytes)
                    tmp_path = tmp.name

                img_url = fal_client.upload_file(tmp_path)
                os.unlink(tmp_path)

                # Call Wan 2.1 image-to-video
                result = fal_client.subscribe(
                    "fal-ai/wan/v2.1/image-to-video",
                    arguments={
                        "image_url": img_url,
                        "prompt": vp,
                        "duration": str(clip_duration),
                        "resolution": "480p",
                    },
                    with_logs=False,
                )
                video_url = result["video"]["url"]

                # Download the clip
                import requests as req
                clip_bytes = req.get(video_url, timeout=120).content
                animated_clips[sid] = (f"{sid}.mp4", clip_bytes)
                results.append({"id": sid, "status": "✅ OK", "prompt_used": vp})
                status.write(f"✅ {i+1}/{total} done: `{sid}`")

            except Exception as e:
                results.append({"id": sid, "status": f"❌ {e}", "prompt_used": vp})
                status.write(f"❌ {i+1}/{total} FAILED: `{sid}`")

            progress.progress((i + 1) / total)

        st.session_state.animated_clips = animated_clips
        results_df = pd.DataFrame(results)
        st.subheader("Results")
        st.dataframe(results_df, use_container_width=True)

        n_ok = results_df["status"].str.startswith("✅").sum()
        st.success(f"{n_ok}/{total} clips animated.")

        if animated_clips:
            # Download ZIP
            import io as _io, zipfile as _zf
            zip_buf = _io.BytesIO()
            with _zf.ZipFile(zip_buf, "w", _zf.ZIP_DEFLATED) as zf:
                for sid, (fname, cbytes) in animated_clips.items():
                    zf.writestr(fname, cbytes)
            zip_buf.seek(0)
            st.download_button(
                "⬇️ Download animated clips (.zip)",
                data=zip_buf.getvalue(),
                file_name="animated_clips.zip",
                mime="application/zip",
                key="step15_zip_dl",
            )
            st.success(
                "✅ Clips ready — go to **② Assemble Video** tab and choose "
                "'Upload a ZIP of pre-made video clips' then upload this ZIP."
            )

# ===========================================================================
# STEP 2 — Assemble Video (ElevenLabs voice + pan/zoom + music)
# ===========================================================================
def render_step2():
    st.header("🎬 Step 2: Assemble Voiced Video")
    st.caption(
        "Images → ElevenLabs voiceover → pan/zoom clips → background music → final Short"
    )

    if "ELEVENLABS_API_KEY" not in st.secrets:
        st.error("Missing secret: ELEVENLABS_API_KEY. Add it to your secrets.")
        return

    el_client = ElevenLabs(api_key=st.secrets["ELEVENLABS_API_KEY"])

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.subheader("Step 2 — Video settings")

        # Try to load voices from ElevenLabs. If the API key is missing the
        # voices_read permission (401), fall back to a manual voice ID input
        # so the rest of the app still works.
        @st.cache_data(ttl=3600)
        def get_voices_safe(api_key: str):
            try:
                client_tmp = ElevenLabs(api_key=api_key)
                resp = client_tmp.voices.get_all()
                return {v.name: v.voice_id for v in resp.voices}, None
            except Exception as e:
                return {}, str(e)

        voice_options, voices_err = get_voices_safe(st.secrets["ELEVENLABS_API_KEY"])

        if voices_err:
            st.warning(
                f"⚠️ Could not load voice list: voices_read permission missing on your API key.\n\n"
                "**Fix:** Go to [elevenlabs.io](https://elevenlabs.io) → Profile → API Keys → "
                "edit your key → enable **voices_read**. Then refresh this page.\n\n"
                "For now, paste a Voice ID manually below."
            )
            voice_id = st.text_input(
                "Voice ID (paste from elevenlabs.io → Voices → click a voice → ID)",
                value="",
                key="step2_voice_id_manual",
            ).strip()
            if not voice_id:
                st.info("Enter a Voice ID above to continue.")
        else:
            voice_name = st.selectbox(
                "Narrator voice", list(voice_options.keys()), key="step2_voice"
            )
            voice_id = voice_options[voice_name]

        tts_model = st.selectbox(
            "ElevenLabs model",
            ["eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_turbo_v2_5"],
            index=0,
            help="Flash v2.5 = cheapest, English. For Hindi/other languages use multilingual_v2.",
            key="step2_tts_model",
        )

        st.divider()
        st.subheader("Voice controls")
        voice_stability = st.slider(
            "Stability", 0.0, 1.0, 0.5, 0.05,
            help="Low = expressive/varied. High = consistent/monotone. 0.5 is a good default.",
            key="step2_stability",
        )
        voice_similarity = st.slider(
            "Similarity boost", 0.0, 1.0, 0.75, 0.05,
            help="How closely output matches the original voice. Keep above 0.7.",
            key="step2_similarity",
        )
        voice_style = st.slider(
            "Style exaggeration", 0.0, 1.0, 0.0, 0.05,
            help="0 = off (best for narration). Raise for more dramatic delivery.",
            key="step2_style",
        )
        voice_speed = st.slider(
            "Speed", 0.7, 1.3, 1.0, 0.05,
            help="0.7 = slow/deliberate. 1.0 = normal. 1.3 = fast paced.",
            key="step2_speed",
        )

        # ── Voice preview (after controls so it uses the latest slider values) ─
        st.divider()
        preview_text = st.text_input(
            "Preview text",
            value="This is a quick preview of how this voice will sound in your video.",
            key="step2_preview_text",
            help="Edit this to test a line closer to your actual script if you like.",
        )
        if st.button("🔊 Preview this voice", key="step2_preview_btn"):
            if not voice_id:
                st.warning("Select or enter a Voice ID first.")
            else:
                with st.spinner("Generating preview..."):
                    try:
                        preview_audio = el_client.text_to_speech.convert(
                            voice_id=voice_id,
                            text=preview_text,
                            model_id=tts_model,
                            output_format="mp3_44100_128",
                            voice_settings={
                                "stability": voice_stability,
                                "similarity_boost": voice_similarity,
                                "style": voice_style,
                                "speed": voice_speed,
                            },
                        )
                        preview_bytes = b"".join(preview_audio)
                        st.audio(preview_bytes, format="audio/mp3")
                    except Exception as e:
                        st.error(f"Preview failed: {e}")


        st.divider()
        st.subheader("Motion effect")
        effect_mode = st.radio(
            "Effect selection",
            ["🤖 Auto — AI picks per scene", "✋ Manual — same for all scenes"],
            index=0,
            key="step2_effect_mode",
            help=(
                "Auto: reads each scene's script_text and picks the most fitting "
                "motion effect automatically.\n\n"
                "Manual: you pick one effect applied to every scene."
            ),
        )
        EFFECTS = [
            "Slow zoom in",
            "Slow zoom out",
            "Pan left → right",
            "Pan right → left",
            "Pan top → bottom",
            "Ken Burns (zoom + diagonal pan)",
            "Fade in / Fade out",
            "Cross dissolve",
            "Handheld shake",
        ]
        manual_effect = st.selectbox(
            "Effect (manual mode)",
            EFFECTS,
            index=0,
            disabled=(effect_mode == "🤖 Auto — AI picks per scene"),
            key="step2_manual_effect",
        )

        st.divider()
        st.subheader("Background music")
        enable_music = st.checkbox("Add background music", value=True, key="step2_music_enable")
        music_target_lufs = st.slider(
            "Music level (LUFS)", -40, -20, -30,
            help="-30 = clearly under voice. Higher = more present music.",
            disabled=not enable_music,
            key="step2_music_lufs",
        )
        music_override = st.selectbox(
            "Mood override",
            [
                "Auto-detect from script",
                "Force: upbeat",
                "Force: dramatic",
                "Force: calm",
                "Force: inspirational",
                "Force: suspense",
                "Force: sad",
                "Force: romantic",
                "Force: epic",
                "Force: energetic",
                "Force: nostalgic",
            ],
            index=0,
            disabled=not enable_music,
            help=(
                "Auto: scores your script against keyword lists and picks the best mood.\n\n"
                "Each mood maps to a folder under music/ — drop .mp3/.wav files there. "
                "If a folder is empty, it falls back to the next best match."
            ),
            key="step2_music_mood",
        )

        # ── Music preview ─────────────────────────────────────────────────────
        if enable_music:
            preview_mood = (
                music_override.replace("Force: ", "")
                if music_override != "Auto-detect from script"
                else "dramatic"   # default preview mood when auto is selected
            )
            if st.button("🎵 Preview music track", key="step2_music_preview_btn",
                         help="Plays a random track from the selected mood folder."):
                track = pick_music_track(preview_mood, MUSIC_DIR)
                if track is None:
                    st.warning(
                        f"No music files found in music/{preview_mood}/ — "
                        "add .mp3 files to that folder and redeploy."
                    )
                else:
                    with open(track, "rb") as f:
                        track_bytes = f.read()
                    ext = os.path.splitext(track)[1].lower().strip(".")
                    fmt = "audio/wav" if ext == "wav" else "audio/mp3"
                    st.audio(track_bytes, format=fmt)
                    st.caption(f"🎵 {os.path.basename(track)} ({preview_mood})")

        st.divider()
        st.subheader("Scene-level SFX & music (optional, from Excel)")
        st.caption(
            "If your script Excel has **SFX** and/or **Background Music** columns filled in, "
            "the app will use them automatically per scene. Leave a cell blank to skip it for "
            "that scene. If the whole column is empty, this is ignored and the settings above "
            "apply instead."
        )
        use_excel_sfx_music = st.checkbox(
            "Use per-scene SFX / Background Music from Excel when present",
            value=True,
            key="step2_excel_sfx_music_enable",
            help=(
                "SFX: one simple cue ('coins falling') plays once at the start of the scene. "
                "A multi-line/bulleted cell layers several sounds together, e.g.:\n"
                "  Deep ocean waves\n"
                "  * Low cinematic drone\n"
                "  * Distant thunder\n"
                "  * Heartbeat beginning in the background\n"
                "Each line is matched to its own sfx/ folder. Ambient-type cues (waves, drone, "
                "thunder, heartbeat) loop to fill the whole scene; '(faint)'/'(loud)' in a line "
                "sets that layer's volume; 'beginning'/'building' fades that layer in.\n\n"
                "Background Music: a mood per scene (e.g. 'dramatic', 'calm'). When the mood "
                "changes between scenes, the music crossfades to the new track instead of "
                "playing one fixed track for the whole video."
            ),
        )

        st.divider()
        st.subheader("Outro")
        add_outro = st.checkbox(
            "Add 'Subscribe' outro at the end", value=True, key="step2_add_outro"
        )
        outro_text = st.text_area(
            "Outro narration + on-screen text",
            value="If you liked this content, please subscribe and follow my channel for new videos every week!",
            disabled=not add_outro,
            key="step2_outro_text",
            help="This will be narrated in the same voice and shown as text overlay on the last frame.",
        )
        outro_duration_extra = st.slider(
            "Extra hold time after narration ends (sec)",
            0.0, 3.0, 1.0, 0.5,
            disabled=not add_outro,
            key="step2_outro_hold",
            help="Keeps the outro visible a bit longer after the voice finishes speaking.",
        )

        st.divider()
        st.subheader("Script settings")
        MAX_WORDS = st.slider(
            "Auto-split threshold (words per scene)",
            min_value=10, max_value=40, value=22, step=1,
            help=(
                "Scenes longer than this word count are automatically split into "
                "shorter chunks to prevent audio overlap. "
                "22 words ≈ 8 seconds — safe for most voices and speeds. "
                "Lower = more splits. Higher = fewer splits but more overlap risk."
            ),
            key="step2_max_words",
        )

        project_id = st.text_input(
            "Project ID",
            value=datetime.now().strftime("short_%Y%m%d_%H%M%S"),
            key="step2_project_id",
        )

    # ── Media source: 3-way picker ────────────────────────────────────────────
    st.subheader("📁 Media source")

    images_from_step1 = st.session_state.generated_images

    ALL_SOURCES = [
        "Use images from Step 1 (already loaded)",
        "Upload a ZIP of pre-made images",
        "Upload a ZIP of pre-made video clips (skip pan/zoom)",
    ]
    if not images_from_step1:
        source_choice = st.radio(
            "Where is your media coming from?",
            ALL_SOURCES[1:],   # hide Step 1 option if nothing generated yet
            index=0,
            key="step2_source_radio",
        )
    else:
        source_choice = st.radio(
            "Where is your media coming from?",
            ALL_SOURCES,
            index=0,
            key="step2_source_radio",
            help=(
                "**Step 1 images** — generated this session, loaded automatically.\n\n"
                "**Image ZIP** — pre-made images named `<id>.png` to match your Excel.\n\n"
                "**Video clip ZIP** — pre-made 5-sec clips named `<id>.mp4`. The app "
                "will skip pan/zoom and use your clips directly, adding only voiceover + music."
            ),
        )

    images_zip = None
    clips_zip = None
    using_prebuilt_clips = source_choice == "Upload a ZIP of pre-made video clips (skip pan/zoom)"

    if source_choice == "Upload a ZIP of pre-made images":
        st.markdown(
            "**Naming rule:** `scene_01.png` → Excel row `id = scene_01`. "
            "Row order in Excel controls scene sequence."
        )
        images_zip = st.file_uploader("Scene images (.zip)", type=["zip"], key="step2_zip_upload")
        if not images_zip:
            st.info("Upload your image ZIP to continue.")
            return

    elif using_prebuilt_clips:
        st.markdown(
            "**Naming rule:** `scene_01.mp4` → Excel row `id = scene_01`. "
            "Each clip can be any length — voiceover duration controls the final clip timing. "
            "Row order in Excel controls scene sequence."
        )
        clips_zip = st.file_uploader(
            "Pre-made video clips (.zip of .mp4/.mov files)",
            type=["zip"],
            key="step2_clips_zip_upload",
        )
        if not clips_zip:
            st.info("Upload your video clips ZIP to continue.")
            return

    # ── Narration script ──────────────────────────────────────────────────────
    st.subheader("📝 Narration script")

    script_from_step1 = st.session_state.step1_script_df

    if script_from_step1 is not None:
        use_step1_script = st.radio(
            "Where is your narration script coming from?",
            ["Use the Excel from Step 1 (already loaded)", "Upload a different Excel"],
            index=0,
            key="step2_script_source_radio",
            help="Step 1's Excel already has id + script_text — no need to re-upload unless you want to use a different file.",
        )
    else:
        use_step1_script = "Upload a different Excel"
        st.info("No Excel loaded from Step 1 yet — please upload your narration Excel below.")

    if use_step1_script == "Use the Excel from Step 1 (already loaded)":
        script_df = script_from_step1.copy()
        st.success(f"✅ Using the {len(script_df)}-row Excel already loaded from Step 1.")
    else:
        script_file = st.file_uploader(
            "Narration Excel (.xlsx) — columns: id · script_text",
            type=["xlsx", "xls"],
            key="step2_script_upload",
        )

        if not script_file:
            st.info("Upload your narration Excel to continue.")
            if st.button("Download sample script template", key="step2_sample_btn"):
                sample = pd.DataFrame({
                    "id": ["scene_01", "scene_02", "scene_03"],
                    "script_text": [
                        "In 1983, nobody expected India to win the World Cup.",
                        "The team walked onto the field at Lord's, underdogs once again.",
                        "Kapil Dev led from the front, calm under pressure.",
                    ],
                })
                buf = io.BytesIO()
                sample.to_excel(buf, index=False)
                st.download_button(
                    "⬇️ script_template.xlsx",
                    data=buf.getvalue(),
                    file_name="script_template.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="step2_sample_dl",
                )
            return

        try:
            script_df = pd.read_excel(script_file)
        except Exception as e:
            st.error(f"Could not read script Excel: {e}")
            return

    script_df.columns = [str(c).strip().lower() for c in script_df.columns]
    # Tolerate common spelling variants for the optional SFX / music columns
    # (e.g. the shipped template has a "Backgound Music" typo).
    _col_aliases = {
        "backgound music": "background music",
        "bg music": "background music",
        "bgm": "background music",
    }
    script_df.columns = [_col_aliases.get(c, c) for c in script_df.columns]
    if "id" not in script_df.columns or "script_text" not in script_df.columns:
        st.error("Script Excel must have columns: id · script_text")
        return
    has_excel_sfx_col = "sfx" in script_df.columns and script_df["sfx"].notna().any()
    has_excel_music_col = (
        "background music" in script_df.columns and script_df["background music"].notna().any()
    )

    script_df = script_df[script_df["script_text"].notna()].reset_index(drop=True)

    # ── Extract media to disk ──────────────────────────────────────────────────
    work_dir = tempfile.mkdtemp(prefix="shorts_")
    images_dir = os.path.join(work_dir, "images")
    clips_dir  = os.path.join(work_dir, "prebuilt_clips")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(clips_dir,  exist_ok=True)

    if using_prebuilt_clips:
        with zipfile.ZipFile(clips_zip) as zf:
            zf.extractall(clips_dir)
    elif source_choice == "Use images from Step 1 (already loaded)":
        for sid, (fname, ibytes) in images_from_step1.items():
            with open(os.path.join(images_dir, fname), "wb") as f:
                f.write(ibytes)
    else:
        with zipfile.ZipFile(images_zip) as zf:
            zf.extractall(images_dir)

    image_files = []
    for root, _, files in os.walk(images_dir):
        for f in files:
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                image_files.append(os.path.join(root, f))

    clip_files_prebuilt = []
    for root, _, files in os.walk(clips_dir):
        for f in files:
            if f.lower().endswith((".mp4", ".mov", ".webm")):
                clip_files_prebuilt.append(os.path.join(root, f))

    def find_media(scene_id: str):
        """Find image OR pre-built clip for a scene by matching filename stem to id."""
        sid = str(scene_id).strip()
        search_pool = clip_files_prebuilt if using_prebuilt_clips else image_files
        for path in search_pool:
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem == sid:
                return path
        for path in search_pool:
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem.startswith(sid + "_") or stem.startswith(sid + "-"):
                return path
        return None

    # Keep backward-compat alias
    find_image = find_media

    script_df["image_path"] = script_df["id"].apply(find_image)
    missing = script_df[script_df["image_path"].isna()]

    st.subheader("Scene matching")
    preview_df = script_df[["id", "script_text", "image_path"]].copy()
    preview_df["image_path"] = preview_df["image_path"].apply(
        lambda p: f"✅ {os.path.basename(p)}" if isinstance(p, str) else "❌ not found"
    )
    st.dataframe(preview_df, use_container_width=True)

    if len(missing) > 0:
        st.warning(
            f"{len(missing)} scene(s) have no matching image and will be skipped: "
            f"{', '.join(missing['id'].astype(str).tolist())}"
        )

    valid_df = script_df[script_df["image_path"].notna()].reset_index(drop=True)
    if valid_df.empty:
        st.error("No matched scenes to process.")
        return

    # ── Auto-split long scenes ─────────────────────────────────────────────────
    # Scenes over the threshold risk audio overlap / drift.
    # Auto-split them into smaller chunks, reusing the same image for each chunk.
    MAX_WORDS = st.session_state.get("step2_max_words", 22)

    def split_into_sentences(text: str) -> list:
        """Split on sentence-ending punctuation — but NOT on em-dash (—)
        which Hindi uses as a pause mid-sentence, not a sentence boundary."""
        import re
        parts = re.split(r'(?<=[।.!?])\s+', text.strip())
        return [p.strip() for p in parts if p.strip()]

    def auto_split_df(df: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for _, row in df.iterrows():
            text = str(row["script_text"]).strip()
            words = text.split()
            if len(words) <= MAX_WORDS:
                rows.append(row.to_dict())
                continue
            # Split into sentence-aware chunks
            sentences = split_into_sentences(text)
            chunk, chunk_idx = [], 1
            for sent in sentences:
                trial = chunk + [sent]
                if len(" ".join(trial).split()) > MAX_WORDS and chunk:
                    new_row = row.to_dict()
                    new_row["id"] = f"{row['id']}_p{chunk_idx}"
                    new_row["script_text"] = " ".join(chunk)
                    rows.append(new_row)
                    chunk = [sent]
                    chunk_idx += 1
                else:
                    chunk.append(sent)
            if chunk:
                new_row = row.to_dict()
                new_row["id"] = f"{row['id']}_p{chunk_idx}"
                new_row["script_text"] = " ".join(chunk)
                rows.append(new_row)
        return pd.DataFrame(rows).reset_index(drop=True)

    original_count = len(valid_df)
    valid_df = auto_split_df(valid_df)
    split_count = len(valid_df) - original_count

    if split_count > 0:
        st.info(
            f"✂️ **Auto-split:** {split_count} long scene(s) were automatically split into "
            f"shorter chunks (max {MAX_WORDS} words each) to prevent audio overlap. "
            f"Total scenes after split: **{len(valid_df)}**"
        )
        st.dataframe(
            valid_df[["id", "script_text"]].assign(
                words=valid_df["script_text"].str.split().str.len()
            ),
            use_container_width=True,
        )


    def generate_voiceover(text: str, out_path: str):
        last_err = None
        for attempt in range(3):
            try:
                audio_iter = el_client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=text,
                    model_id=tts_model,
                    output_format="mp3_44100_128",
                    voice_settings={
                        "stability": voice_stability,
                        "similarity_boost": voice_similarity,
                        "style": voice_style,
                        "speed": voice_speed,
                    },
                )
                with open(out_path, "wb") as f:
                    for chunk in audio_iter:
                        f.write(chunk)
                return
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(3)
        raise last_err

    def get_duration(path: str) -> float:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(r.stdout.strip())

    # ── AI effect classifier ──────────────────────────────────────────────────
    EFFECT_KEYWORDS = {
        "Ken Burns (zoom + diagonal pan)": [
            "war", "battle", "fight", "intense", "fierce", "danger", "crisis",
            "explosion", "attack", "charge", "dramatic", "epic", "climax",
            "death", "shock", "desperate", "collapse", "storm",
        ],
        "Slow zoom in": [
            "focus", "reveal", "discover", "realise", "realize", "moment",
            "pause", "silence", "stood", "stared", "watched", "waited",
            "emotional", "grief", "tears", "smile", "proud", "achieve",
        ],
        "Slow zoom out": [
            "vast", "wide", "crowd", "stadium", "horizon", "land", "field",
            "world", "sky", "open", "landscape", "spread", "panorama",
            "everyone", "nation", "country", "million",
        ],
        "Pan left → right": [
            "march", "walk", "move", "journey", "travel", "progress",
            "forward", "advance", "cross", "enter", "parade",
        ],
        "Pan right → left": [
            "return", "retreat", "back", "history", "memory", "past",
            "recall", "remind", "once", "before", "ago",
        ],
        "Pan top → bottom": [
            "fall", "drop", "descend", "collapse", "down", "below",
            "ground", "earth", "kneel", "bow",
        ],
        "Fade in / Fade out": [
            "begin", "start", "end", "final", "last", "first", "dawn",
            "night", "sleep", "dream", "quiet", "peace", "calm",
        ],
        "Cross dissolve": [
            "then", "next", "after", "later", "meanwhile", "suddenly",
            "transition", "change", "transform", "become", "turned",
        ],
        "Handheld shake": [
            "run", "rush", "chase", "hurry", "panic", "chaos", "crowd",
            "noise", "confusion", "scramble", "flee", "escape",
        ],
    }

    def ai_pick_effect(text: str) -> str:
        """Score the scene text against keyword lists and return the best effect."""
        t = text.lower()
        scores = {effect: 0 for effect in EFFECT_KEYWORDS}
        for effect, keywords in EFFECT_KEYWORDS.items():
            for kw in keywords:
                scores[effect] += len(re.findall(r"\b" + kw + r"\b", t))
        best = max(scores, key=scores.get)
        # If no keyword matched, cycle through neutral effects based on text length
        if scores[best] == 0:
            neutral = ["Slow zoom in", "Pan left → right", "Slow zoom out", "Pan right → left"]
            return neutral[len(text) % len(neutral)]
        return best

    # ── Motion effect renderer ─────────────────────────────────────────────────
    def make_clip(image_path: str, duration: float, out_path: str, effect: str):
        """Render one scene clip with the chosen motion effect."""
        fps = 25
        frames = max(int(duration * fps), 1)
        TARGET = "1080x1920"
        SCALE_UP = "scale=2160:3840,"   # upscale so zoompan has room to crop

        if effect == "Slow zoom in":
            vf = (
                f"{SCALE_UP}zoompan=z='min(zoom+0.0015,1.3)':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={TARGET}:fps={fps}"
            )

        elif effect == "Slow zoom out":
            vf = (
                f"{SCALE_UP}zoompan=z='if(eq(on,1),1.3,max(zoom-0.0015,1.0))':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={TARGET}:fps={fps}"
            )

        elif effect == "Pan left → right":
            # Pan horizontally: x moves from 0 → (iw - iw/zoom)
            vf = (
                f"{SCALE_UP}zoompan=z='1.2':d={frames}:"
                f"x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)':s={TARGET}:fps={fps}"
            )

        elif effect == "Pan right → left":
            vf = (
                f"{SCALE_UP}zoompan=z='1.2':d={frames}:"
                f"x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)':s={TARGET}:fps={fps}"
            )

        elif effect == "Pan top → bottom":
            vf = (
                f"{SCALE_UP}zoompan=z='1.2':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*on/{frames}':s={TARGET}:fps={fps}"
            )

        elif effect == "Ken Burns (zoom + diagonal pan)":
            # Zoom in while panning diagonally top-left → bottom-right
            vf = (
                f"{SCALE_UP}zoompan=z='min(zoom+0.002,1.4)':d={frames}:"
                f"x='(iw-iw/zoom)*on/{frames}':y='(ih-ih/zoom)*on/{frames}':s={TARGET}:fps={fps}"
            )

        elif effect == "Fade in / Fade out":
            fade_dur = min(0.5, duration * 0.15)
            fade_frames = max(int(fade_dur * fps), 1)
            start_fade_out = max(frames - fade_frames, 1)
            # Static centre + fade in + fade out
            vf = (
                f"{SCALE_UP}zoompan=z='1.0':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={TARGET}:fps={fps},"
                f"fade=t=in:st=0:d={fade_dur},"
                f"fade=t=out:st={start_fade_out/fps:.3f}:d={fade_dur}"
            )

        elif effect == "Cross dissolve":
            # Gentle slow zoom in + fade in/out for smooth scene transitions
            fade_dur = min(0.4, duration * 0.15)
            fade_frames = max(int(fade_dur * fps), 1)
            start_fade_out = max(frames - fade_frames, 1)
            vf = (
                f"{SCALE_UP}zoompan=z='min(zoom+0.001,1.15)':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={TARGET}:fps={fps},"
                f"fade=t=in:st=0:d={fade_dur},"
                f"fade=t=out:st={start_fade_out/fps:.3f}:d={fade_dur}"
            )

        elif effect == "Handheld shake":
            # Subtle random shake using sine waves on x/y with slight zoom
            vf = (
                f"{SCALE_UP}zoompan=z='1.08':d={frames}:"
                f"x='iw/2-(iw/zoom/2)+8*sin(on*0.7)':y='ih/2-(ih/zoom/2)+5*sin(on*1.1)'"
                f":s={TARGET}:fps={fps}"
            )

        else:
            # Fallback: static centre
            vf = (
                f"{SCALE_UP}zoompan=z='1.0':d={frames}:"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={TARGET}:fps={fps}"
            )

        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", image_path, "-vf", vf,
             "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
            capture_output=True, check=True,
        )

    def make_outro_clip(duration: float, out_path: str, last_image_path: str = None):
        """
        Build the subscribe outro clip using PIL to burn text directly onto
        the image — avoids all ffmpeg drawtext escaping issues entirely.
        """
        import textwrap
        from PIL import ImageDraw, ImageFont

        TARGET_W, TARGET_H = 1080, 1920
        fps = 25

        # ── Build background frame ────────────────────────────────────────────
        if last_image_path and os.path.isfile(last_image_path):
            bg = Image.open(last_image_path).convert("RGB")
            bg = ensure_9x16(bg, TARGET_W, TARGET_H)
            # Dim the background
            overlay = Image.new("RGB", bg.size, (0, 0, 0))
            bg = Image.blend(bg, overlay, alpha=0.55)
        else:
            bg = Image.new("RGB", (TARGET_W, TARGET_H), (26, 26, 46))

        draw = ImageDraw.Draw(bg)

        # ── Load fonts (fall back to default if DejaVu not available) ─────────
        def load_font(size):
            font_paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            ]
            for fp in font_paths:
                if os.path.isfile(fp):
                    return ImageFont.truetype(fp, size)
            return ImageFont.load_default()

        font_body = load_font(52)
        font_sub  = load_font(80)

        # ── Draw main outro text (wrapped) ────────────────────────────────────
        lines = textwrap.wrap(outro_text, width=26)
        line_h = 64
        total_text_h = len(lines) * line_h
        y_start = TARGET_H // 2 - total_text_h // 2 - 60

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font_body)
            tw = bbox[2] - bbox[0]
            x = (TARGET_W - tw) // 2
            y = y_start + i * line_h
            # Shadow
            draw.text((x + 2, y + 2), line, font=font_body, fill=(0, 0, 0, 180))
            draw.text((x, y), line, font=font_body, fill=(255, 255, 255))

        # ── Draw red Subscribe button ─────────────────────────────────────────
        sub_text = "▶  Subscribe"
        bbox = draw.textbbox((0, 0), sub_text, font=font_sub)
        sw, sh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        sx = (TARGET_W - sw) // 2
        sy = int(TARGET_H * 0.70)
        # Red pill background
        pad = 24
        draw.rounded_rectangle(
            [sx - pad, sy - pad, sx + sw + pad, sy + sh + pad],
            radius=20, fill=(220, 30, 30)
        )
        draw.text((sx, sy), sub_text, font=font_sub, fill=(255, 255, 255))

        # ── Save frame as PNG and build video from it ─────────────────────────
        frame_path = os.path.join(work_dir, "outro_frame.png")
        bg.save(frame_path, "PNG")

        subprocess.run(
            ["ffmpeg", "-y",
             "-loop", "1", "-i", frame_path,
             "-vf", f"fade=t=in:st=0:d=0.4,scale={TARGET_W}:{TARGET_H}",
             "-t", str(duration),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
            capture_output=True, check=True,
        )

    def mux(video: str, audio: str, out: str):
        subprocess.run(
            ["ffmpeg", "-y", "-i", video, "-i", audio,
             "-c:v", "copy", "-c:a", "aac", "-shortest", out],
            capture_output=True, check=True,
        )

    def concat(clips: list, out: str):
        list_file = os.path.join(work_dir, "concat_list.txt")
        with open(list_file, "w") as f:
            for p in clips:
                f.write(f"file '{p}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out],
            capture_output=True, check=True,
        )

    def mix_music(video_in: str, music: str, out: str, lufs: int):
        duration = get_duration(video_in)
        fc = (
            f"[1:a]loudnorm=I={lufs}:TP=-2:LRA=11[music];"
            f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_in, "-stream_loop", "-1", "-i", music,
             "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-t", str(duration), out],
            capture_output=True, check=True,
        )

    def mix_layered_sfx_into_clip(clip_in: str, layers: list, out: str):
        """Overlay one or more SFX layers onto a single scene clip, under the
        voiceover already muxed into clip_in. Each layer is a dict:
            {"path": str, "volume": float, "sustain": bool, "fade_in": bool}
        - sustain=True (ambient folders, or any 'fade_in' cue) loops/trims the
          file to fill the whole scene. sustain=False plays once from the
          start and is trimmed if it runs long — never extends the scene.
        - fade_in=True ramps the layer in from silence instead of starting
          at full volume immediately.
        Never raises past this point being reached — if a layer's ffmpeg
        input is bad, the caller has already validated the path exists."""
        duration = get_duration(clip_in)
        cmd = ["ffmpeg", "-y", "-i", clip_in]
        filter_parts = []
        mix_labels = ["[0:a]"]

        for idx, layer in enumerate(layers, start=1):
            if layer["sustain"]:
                cmd += ["-stream_loop", "-1", "-i", layer["path"]]
            else:
                cmd += ["-i", layer["path"]]

            chain = f"[{idx}:a]volume={layer['volume']:.2f}"
            if layer["fade_in"]:
                fade_len = min(duration, 4.0) if not layer["sustain"] else duration
                chain += f",afade=t=in:st=0:d={fade_len:.2f}"
            chain += f",atrim=0:{duration:.3f}[a{idx}]"
            filter_parts.append(chain)
            mix_labels.append(f"[a{idx}]")

        filter_parts.append(
            f"{''.join(mix_labels)}amix=inputs={len(layers) + 1}:"
            f"duration=first:dropout_transition=0.3[aout]"
        )
        fc = ";".join(filter_parts)
        cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", out]
        subprocess.run(cmd, capture_output=True, check=True)

    def build_scene_music_track(scene_moods: list, scene_durations: list, out_path: str,
                                 lufs: int, crossfade_sec: float = 1.0):
        """Build one continuous background-music track for the whole video from
        a per-scene list of moods, crossfading whenever the mood changes between
        consecutive scenes. Scenes with the same mood as their neighbour just
        flow into one longer segment (no crossfade needed inside a run).
        Returns None if no mood could be resolved for any scene."""
        # Group consecutive scenes with the same mood into segments
        segments = []  # list of [mood, total_duration]
        for mood, dur in zip(scene_moods, scene_durations):
            if mood is None:
                mood = "calm"  # last-resort fallback, never leaves a scene silent
            if segments and segments[-1][0] == mood:
                segments[-1][1] += dur
            else:
                segments.append([mood, dur])

        if not segments:
            return None

        # Render each segment to its own trimmed/looped audio file
        seg_files = []
        for idx, (mood, dur) in enumerate(segments):
            track = pick_music_track(mood, MUSIC_DIR)
            if track is None:
                continue
            # Pad each segment by the crossfade amount (except the last) so
            # joins never come up short once acrossfade trims them back down.
            pad = crossfade_sec if idx < len(segments) - 1 else 0
            seg_out = os.path.join(work_dir, f"_music_seg_{idx}.m4a")
            subprocess.run(
                ["ffmpeg", "-y", "-stream_loop", "-1", "-i", track,
                 "-t", str(dur + pad),
                 "-af", f"loudnorm=I={lufs}:TP=-2:LRA=11",
                 "-c:a", "aac", seg_out],
                capture_output=True, check=True,
            )
            seg_files.append(seg_out)

        if not seg_files:
            return None
        if len(seg_files) == 1:
            subprocess.run(["ffmpeg", "-y", "-i", seg_files[0], "-c:a", "aac", out_path],
                            capture_output=True, check=True)
            return out_path

        # Chain acrossfade across all segments in order
        current = seg_files[0]
        for idx in range(1, len(seg_files)):
            step_out = os.path.join(work_dir, f"_music_join_{idx}.m4a")
            subprocess.run(
                ["ffmpeg", "-y", "-i", current, "-i", seg_files[idx],
                 "-filter_complex",
                 f"[0:a][1:a]acrossfade=d={crossfade_sec}:c1=tri:c2=tri[aout]",
                 "-map", "[aout]", "-c:a", "aac", step_out],
                capture_output=True, check=True,
            )
            current = step_out
        os.replace(current, out_path)
        return out_path

    def mix_prebuilt_music_track(video_in: str, music_path: str, out: str):
        """Mix a music track that's already the right length (built per-scene)
        into the final video, without the stream_loop/duration-matching that
        mix_music() does for a single repeating track."""
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_in, "-i", music_path,
             "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[aout]",
             "-map", "0:v", "-map", "[aout]",
             "-c:v", "copy", "-c:a", "aac", "-shortest", out],
            capture_output=True, check=True,
        )

    # ── Main generate button ──────────────────────────────────────────────────
    if st.button("🎬 Generate voiceover + video", type="primary", key="step2_generate_btn"):
        progress = st.progress(0.0)
        status = st.empty()
        results = []
        clip_paths = []
        scene_durations = []   # per-scene voiceover duration, in final order
        scene_moods = []       # per-scene resolved background-music mood, or None
        total = len(valid_df)
        auto_mode = effect_mode == "🤖 Auto — AI picks per scene"

        per_scene_music_active = use_excel_sfx_music and has_excel_music_col
        per_scene_sfx_active = use_excel_sfx_music and has_excel_sfx_col
        last_seen_mood = None   # carries forward across blank "background music" cells

        for i, row in valid_df.iterrows():
            sid = str(row["id"])
            text = str(row["script_text"]).strip()
            media_path = row["image_path"]   # could be image OR pre-built clip

            chosen_effect = ai_pick_effect(text) if auto_mode else manual_effect

            if using_prebuilt_clips:
                status.write(f"Processing {i+1}/{total}: `{sid}` — using pre-built clip")
            else:
                status.write(f"Processing {i+1}/{total}: `{sid}` — effect: *{chosen_effect}*")

            audio_path = os.path.join(work_dir, f"{safe_name(sid)}.mp3")
            video_only = os.path.join(work_dir, f"{safe_name(sid)}_video.mp4")
            clip_final = os.path.join(work_dir, f"{safe_name(sid)}_final.mp4")

            try:
                generate_voiceover(text, audio_path)
                dur = get_duration(audio_path)

                if using_prebuilt_clips:
                    # Trim or loop the pre-built clip to match voiceover duration
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", media_path,
                         "-t", str(dur),
                         "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p",
                         "-an", video_only],
                        capture_output=True, check=True,
                    )
                else:
                    make_clip(media_path, dur, video_only, chosen_effect)

                mux(video_only, audio_path, clip_final)

                # ── Optional per-scene SFX (from Excel "SFX" column) ────────────
                # Supports a single simple cue ("coins falling") and a
                # multi-line/bulleted cell with several layered cues
                # ("Deep ocean waves\n* Low cinematic drone\n* Distant
                # thunder\n* Heartbeat beginning in the background").
                sfx_used = []
                if per_scene_sfx_active:
                    sfx_cell = row.get("sfx")
                    if isinstance(sfx_cell, str) and sfx_cell.strip():
                        cues = parse_sfx_cues(sfx_cell)
                        layers = []
                        for cue in cues:
                            hit = pick_sfx_clip(cue["text"], SFX_DIR)
                            if hit is None:
                                status.write(f"  ⚠️ No SFX match for '{cue['text']}' — skipping that layer.")
                                continue
                            sfx_path, folder_name = hit
                            layers.append({
                                "path": sfx_path,
                                "volume": cue["volume"],
                                "sustain": folder_name in _SFX_AMBIENT_FOLDERS or cue["fade_in"],
                                "fade_in": cue["fade_in"],
                            })
                            sfx_used.append(os.path.basename(sfx_path))
                        if layers:
                            sfx_out = os.path.join(work_dir, f"{safe_name(sid)}_sfx.mp4")
                            mix_layered_sfx_into_clip(clip_final, layers, sfx_out)
                            clip_final = sfx_out

                clip_paths.append(clip_final)

                # ── Optional per-scene background-music mood (from Excel) ───────
                scene_mood = None
                if per_scene_music_active:
                    bgm_cue = row.get("background music")
                    if isinstance(bgm_cue, str) and bgm_cue.strip():
                        cue = bgm_cue.strip().lower()
                        scene_mood = cue if cue in _MOOD_FOLDERS else classify_mood(cue)
                        last_seen_mood = scene_mood
                    else:
                        scene_mood = last_seen_mood  # carry forward, may still be None
                scene_durations.append(dur)
                scene_moods.append(scene_mood)

                results.append({
                    "id": sid, "script_text": text,
                    "effect": "pre-built clip" if using_prebuilt_clips else chosen_effect,
                    "duration_sec": round(dur, 2), "status": "✅ OK",
                    "sfx": ", ".join(sfx_used), "music_mood": scene_mood or "",
                })
            except Exception as e:
                results.append({
                    "id": sid, "script_text": text,
                    "effect": "pre-built clip" if using_prebuilt_clips else chosen_effect,
                    "duration_sec": None, "status": f"❌ {e}",
                    "sfx": "", "music_mood": "",
                })

            progress.progress((i + 1) / total)

        results_df = pd.DataFrame(results)
        st.subheader("Per-scene results")
        st.dataframe(results_df, use_container_width=True)

        n_ok = (results_df["status"] == "✅ OK").sum()
        st.success(f"{n_ok}/{total} scenes processed.")

        if not clip_paths:
            st.error("No scenes succeeded — nothing to stitch.")
            return

        # ── Subscribe outro ──────────────────────────────────────────────────
        if add_outro and outro_text.strip():
            status.write("Generating subscribe outro...")
            try:
                outro_audio_path = os.path.join(work_dir, "outro.mp3")
                outro_video_only = os.path.join(work_dir, "outro_video.mp4")
                outro_final = os.path.join(work_dir, "outro_final.mp4")

                generate_voiceover(outro_text.strip(), outro_audio_path)
                outro_voice_dur = get_duration(outro_audio_path)
                outro_total_dur = outro_voice_dur + outro_duration_extra

                # Use the last successfully processed scene's image as a dimmed backdrop
                last_img = valid_df.iloc[-1]["image_path"] if len(valid_df) > 0 else None

                make_outro_clip(outro_total_dur, outro_video_only, last_image_path=last_img)
                mux(outro_video_only, outro_audio_path, outro_final)
                clip_paths.append(outro_final)
                # Keep the per-scene music track's total length in sync with
                # the actual video length by covering the outro too.
                scene_durations.append(outro_total_dur)
                scene_moods.append(last_seen_mood)
                st.success("✅ Subscribe outro added.")
            except Exception as e:
                st.warning(f"⚠️ Outro generation failed, continuing without it: {e}")

        status.write("Stitching scenes together...")
        final_path = os.path.join(work_dir, f"{project_id}.mp4")
        try:
            concat(clip_paths, final_path)

            if enable_music and per_scene_music_active:
                # ── Per-scene mode: crossfade between moods as they change ──────
                # Backfill any leading scenes that had a blank cell before the
                # first mood was ever specified.
                first_mood = next((m for m in scene_moods if m), None)
                filled_moods = [m or first_mood for m in scene_moods]

                status.write("Building per-scene background music (with crossfades)...")
                composite_path = os.path.join(work_dir, "_music_composite.m4a")
                composite = build_scene_music_track(
                    filled_moods, scene_durations, composite_path, music_target_lufs
                )
                if composite is None:
                    st.warning(
                        "No music files found for any of the requested moods — skipping "
                        "background music. Add .mp3 files under music/<mood>/."
                    )
                else:
                    with_music = os.path.join(work_dir, f"{project_id}_with_music.mp4")
                    mix_prebuilt_music_track(final_path, composite, with_music)
                    final_path = with_music
                    import itertools
                    mood_sequence = " → ".join(m for m, _ in itertools.groupby(filled_moods))
                    st.info(f"🎵 Per-scene music (crossfaded): **{mood_sequence}**")

            elif enable_music:
                mood = (
                    classify_mood(" ".join(valid_df["script_text"].astype(str)))
                    if music_override == "Auto-detect from script"
                    else music_override.replace("Force: ", "")
                )
                music_track = pick_music_track(mood, MUSIC_DIR)
                if music_track is None:
                    st.warning(
                        f"No music files found for mood '{mood}' in music/{mood}/ — skipping. "
                        "Add .mp3 files to that folder to enable background music."
                    )
                else:
                    status.write(f"Adding background music (mood: {mood})...")
                    with_music = os.path.join(work_dir, f"{project_id}_with_music.mp4")
                    mix_music(final_path, music_track, with_music, music_target_lufs)
                    final_path = with_music
                    st.info(f"🎵 Music: **{os.path.basename(music_track)}** (mood: {mood})")

            status.write("Done ✅")
            with open(final_path, "rb") as f:
                final_bytes = f.read()

            st.video(final_bytes)
            st.download_button(
                "⬇️ Download finished Short (.mp4)",
                data=final_bytes,
                file_name=f"{project_id}.mp4",
                mime="video/mp4",
                key="step2_video_dl",
            )

            log_buf = io.BytesIO()
            results_df.to_excel(log_buf, index=False)
            st.download_button(
                "⬇️ Download scene log (.xlsx)",
                data=log_buf.getvalue(),
                file_name=f"{project_id}_log.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="step2_log_dl",
            )

        except subprocess.CalledProcessError as e:
            st.error(f"ffmpeg error: {e.stderr.decode() if e.stderr else e}")


# ===========================================================================
# Main layout
# ===========================================================================
st.title("🎬 Shorts Maker")
st.caption("Excel → OpenAI images (9:16) → ElevenLabs voice → pan/zoom video → YouTube Short")

tab1, tab15, tab2 = st.tabs(["① Generate Images", "① .5 Animate (fal.ai Wan)", "② Assemble Video"])
with tab1:
    render_step1()
with tab15:
    render_step15()
with tab2:
    render_step2()
