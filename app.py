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
from PIL import Image

st.set_page_config(page_title="Shorts Maker", page_icon="🎬", layout="wide")

MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")

# ---------------------------------------------------------------------------
# Shared session state
# ---------------------------------------------------------------------------
if "generated_images" not in st.session_state:
    st.session_state.generated_images = {}   # scene_id -> (filename, bytes)
if "generated_images_log" not in st.session_state:
    st.session_state.generated_images_log = None

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
    track from other mood folders if the requested mood folder is empty."""
    def tracks_in(folder):
        result = []
        for ext in ("*.mp3", "*.wav", "*.m4a"):
            result.extend(glob.glob(os.path.join(folder, ext)))
        return result

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

    if "OPENAI_API_KEY" not in st.secrets:
        st.error(
            "Missing secret: OPENAI_API_KEY. "
            "Add it under App Settings → Secrets or .streamlit/secrets.toml. "
            "Get one at https://platform.openai.com/api-keys"
        )
        return

    openai_client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.subheader("Step 1 — Image settings")

        quality = st.selectbox(
            "Image quality",
            ["medium", "low", "high"],
            index=0,
            help=(
                "low  → cheapest, ~$0.011/image — good for drafts\n"
                "medium → balanced, ~$0.042/image — recommended\n"
                "high → best quality, ~$0.167/image — use sparingly"
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

    # ── Cost info banner ──────────────────────────────────────────────────────
    cost_map = {"low": 0.011, "medium": 0.042, "high": 0.167}
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

    df = df[df["image_prompt"].notna()].reset_index(drop=True)
    n = len(df)
    est_cost = n * cost_map[quality]
    st.success(f"Loaded **{n} scene(s)**. Estimated cost: **${est_cost:.3f}**")
    st.dataframe(df[["id", "script_text", "image_prompt"]], use_container_width=True)

    # ── Generation ────────────────────────────────────────────────────────────
    def generate_image_openai(prompt: str) -> bytes:
        """Call gpt-image-1 at 1024×1792 (9:16) and return PNG bytes."""
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                response = openai_client.images.generate(
                    model="gpt-image-1",
                    prompt=prompt,
                    n=1,
                    size="1024x1792",   # native 9:16 — no cropping needed
                    quality=quality,
                )
                # gpt-image-1 returns base64 by default
                b64 = response.data[0].b64_json
                img_bytes = base64.b64decode(b64)
                # Verify & ensure exact 9:16 just in case
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                img = ensure_9x16(img, 1024, 1792)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
            except Exception as e:
                last_err = str(e)
                if attempt < max_retries:
                    time.sleep(5)
        raise RuntimeError(last_err or "Unknown OpenAI error")

    if st.button("🖼️ Generate all scene images", type="primary", key="step1_generate_btn"):
        progress = st.progress(0.0)
        status = st.empty()
        results = []
        image_bufs = {}

        for i, row in df.iterrows():
            scene_id = str(row["id"])
            prompt = f"{str(row['image_prompt']).strip()}, {style_suffix}".strip(", ")
            status.write(f"Generating {i+1}/{n}: `{scene_id}`")
            try:
                img_bytes = generate_image_openai(prompt)
                fname = f"{safe_name(scene_id)}.png"
                image_bufs[scene_id] = (fname, img_bytes)
                results.append({"id": scene_id, "status": "✅ OK", "prompt": prompt})
            except Exception as e:
                results.append({"id": scene_id, "status": f"❌ {e}", "prompt": prompt})
            progress.progress((i + 1) / n)
            if pace_delay > 0 and i < n - 1:
                time.sleep(pace_delay)

        results_df = pd.DataFrame(results)
        st.subheader("Results")
        st.dataframe(results_df, use_container_width=True)

        n_ok = (results_df["status"] == "✅ OK").sum()
        st.success(f"{n_ok}/{n} images generated.")

        if n_ok == 0:
            st.error("No images succeeded. Check the errors above and your API key/billing.")
            return

        # Store in session state → Step 2 picks them up automatically
        st.session_state.generated_images = image_bufs
        st.session_state.generated_images_log = results_df

        # Preview
        st.subheader("Preview")
        cols = st.columns(4)
        for idx, (sid, (fname, ibytes)) in enumerate(list(image_bufs.items())[:8]):
            with cols[idx % 4]:
                st.image(ibytes, caption=sid, use_container_width=True)

        # Download ZIP
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sid, (fname, ibytes) in image_bufs.items():
                zf.writestr(fname, ibytes)
        zip_buf.seek(0)

        st.download_button(
            "⬇️ Download all images (.zip)",
            data=zip_buf.getvalue(),
            file_name=f"{project_id_step1}.zip",
            mime="application/zip",
            key="step1_zip_dl",
        )

        st.success(
            "✅ Images ready — switch to **② Assemble Video** tab. "
            "They are already loaded; no need to re-upload."
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

        project_id = st.text_input(
            "Project ID",
            value=datetime.now().strftime("short_%Y%m%d_%H%M%S"),
            key="step2_project_id",
        )

    # ── Image source: two clear paths ─────────────────────────────────────────
    st.subheader("📁 Image source")

    images_from_step1 = st.session_state.generated_images

    source_options = ["Use images from Step 1 (already loaded)", "Upload a ZIP of pre-made images"]
    if not images_from_step1:
        # Force zip upload if Step 1 hasn't run
        source_choice = "Upload a ZIP of pre-made images"
        st.info("No images from Step 1 yet — please upload a ZIP of your images below.")
    else:
        source_choice = st.radio(
            "Where are your images coming from?",
            source_options,
            index=0,
            key="step2_source_radio",
            help=(
                "**Step 1 images** — generated moments ago in this session, loaded automatically.\n\n"
                "**Upload ZIP** — images you already have on disk, named `<id>.png` to match your Excel."
            ),
        )

    images_zip = None
    if source_choice == "Upload a ZIP of pre-made images":
        st.markdown(
            "**Naming rule:** each image filename must match the `id` in your Excel exactly. "
            "Example: if your Excel row has `id = scene_01`, the file must be `scene_01.png` (or .jpg). "
            "The **order in the Excel** controls the scene sequence in the final video."
        )
        images_zip = st.file_uploader(
            "Scene images (.zip)",
            type=["zip"],
            key="step2_zip_upload",
        )
        if not images_zip:
            st.info("Upload your ZIP to continue.")
            return

    # ── Narration script ──────────────────────────────────────────────────────
    st.subheader("📝 Narration script")
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
    if "id" not in script_df.columns or "script_text" not in script_df.columns:
        st.error("Script Excel must have columns: id · script_text")
        return

    script_df = script_df[script_df["script_text"].notna()].reset_index(drop=True)

    # ── Extract images to disk ─────────────────────────────────────────────────
    work_dir = tempfile.mkdtemp(prefix="shorts_")
    images_dir = os.path.join(work_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    if source_choice == "Use images from Step 1 (already loaded)":
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

    def find_image(scene_id: str):
        sid = str(scene_id).strip()
        for path in image_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem == sid:
                return path
        # Fuzzy fallback: prefix match
        for path in image_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem.startswith(sid + "_") or stem.startswith(sid + "-"):
                return path
        return None

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

    # ── ffmpeg helpers ────────────────────────────────────────────────────────
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

    # ── Main generate button ──────────────────────────────────────────────────
    if st.button("🎬 Generate voiceover + video", type="primary", key="step2_generate_btn"):
        progress = st.progress(0.0)
        status = st.empty()
        results = []
        clip_paths = []
        total = len(valid_df)
        auto_mode = effect_mode == "🤖 Auto — AI picks per scene"

        for i, row in valid_df.iterrows():
            sid = str(row["id"])
            text = str(row["script_text"]).strip()
            img_path = row["image_path"]

            # Pick effect
            chosen_effect = ai_pick_effect(text) if auto_mode else manual_effect
            status.write(f"Processing {i+1}/{total}: `{sid}` — effect: *{chosen_effect}*")

            audio_path = os.path.join(work_dir, f"{safe_name(sid)}.mp3")
            video_only = os.path.join(work_dir, f"{safe_name(sid)}_video.mp4")
            clip_final = os.path.join(work_dir, f"{safe_name(sid)}_final.mp4")

            try:
                generate_voiceover(text, audio_path)
                dur = get_duration(audio_path)
                make_clip(img_path, dur, video_only, chosen_effect)
                mux(video_only, audio_path, clip_final)
                clip_paths.append(clip_final)
                results.append({
                    "id": sid, "script_text": text,
                    "effect": chosen_effect,
                    "duration_sec": round(dur, 2), "status": "✅ OK",
                })
            except Exception as e:
                results.append({
                    "id": sid, "script_text": text,
                    "effect": chosen_effect,
                    "duration_sec": None, "status": f"❌ {e}",
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

        status.write("Stitching scenes together...")
        final_path = os.path.join(work_dir, f"{project_id}.mp4")
        try:
            concat(clip_paths, final_path)

            if enable_music:
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

tab1, tab2 = st.tabs(["① Generate Images", "② Assemble Video"])
with tab1:
    render_step1()
with tab2:
    render_step2()
