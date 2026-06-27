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

st.set_page_config(page_title="Shorts Maker", page_icon="🎬", layout="wide")

MUSIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music")

# ---------------------------------------------------------------------------
# Shared session state — this is what lets Step 2 automatically see images
# Step 1 just generated, without you having to download + re-upload a ZIP.
# ---------------------------------------------------------------------------
if "generated_images" not in st.session_state:
    st.session_state.generated_images = {}  # scene_id -> (filename, bytes)
if "generated_images_log" not in st.session_state:
    st.session_state.generated_images_log = None  # results DataFrame from Step 1

# ---------------------------------------------------------------------------
# Background music: mood keyword lexicon (used in Step 2)
# ---------------------------------------------------------------------------
MOOD_KEYWORDS = {
    "upbeat": [
        "win", "won", "victory", "celebrat", "happy", "joy", "exciting",
        "amazing", "success", "triumph", "champion", "festival", "fun",
        "bright", "smile", "achieve", "proud", "cheer",
    ],
    "dramatic": [
        "war", "battle", "fight", "danger", "crisis", "death", "fear",
        "tension", "struggle", "fierce", "darkness", "betray", "shock",
        "underdog", "pressure", "risk", "storm", "collapse", "desperate",
    ],
    "calm": [
        "calm", "quiet", "peace", "gentle", "slow", "reflect", "history",
        "memory", "story", "remember", "morning", "soft", "ordinary",
        "simple", "everyday",
    ],
}


def classify_mood(full_text: str) -> str:
    text = full_text.lower()
    scores = {mood: 0 for mood in MOOD_KEYWORDS}
    for mood, keywords in MOOD_KEYWORDS.items():
        for kw in keywords:
            scores[mood] += len(re.findall(kw, text))
    best_mood = max(scores, key=scores.get)
    if scores[best_mood] == 0:
        return "calm"
    return best_mood


def pick_music_track(mood: str, music_root: str):
    folder = os.path.join(music_root, mood)
    candidates = []
    for ext in ("*.mp3", "*.wav", "*.m4a"):
        candidates.extend(glob.glob(os.path.join(folder, ext)))
    if not candidates:
        return None
    return random.choice(candidates)


def safe_name(s: str) -> str:
    s = str(s).strip()
    keep = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(c for c in s if c in keep).strip().replace(" ", "_") or "scene"


# ---------------------------------------------------------------------------
# Step 1: Image Generator (Hugging Face, free)
# ---------------------------------------------------------------------------
def render_step1():
    st.header("🖼️ Step 1: Generate Scene Images")
    st.caption(
        "Upload one Excel with id, script_text, image_prompt → "
        "free Hugging Face image generation → images ready for Step 2"
    )

    if "HF_API_TOKEN" not in st.secrets:
        st.error(
            "Missing required secret: HF_API_TOKEN. "
            "Add it under App Settings → Secrets (Streamlit Cloud) "
            "or .streamlit/secrets.toml (local). "
            "Get a free token at https://huggingface.co/settings/tokens"
        )
        return

    HF_TOKEN = st.secrets["HF_API_TOKEN"]

    with st.sidebar:
        st.subheader("Step 1 settings")
        model_id = st.selectbox(
            "Hugging Face model",
            [
                "stabilityai/stable-diffusion-xl-base-1.0",
                "black-forest-labs/FLUX.1-schnell",
                "runwayml/stable-diffusion-v1-5",
            ],
            index=0,
            help=(
                "Free HF Inference API models. Availability and speed vary day to day "
                "since these run on shared free infrastructure — if one model errors "
                "or times out repeatedly, try another from this list."
            ),
            key="step1_model",
        )
        style_suffix = st.text_input(
            "Style suffix (added to every prompt)",
            value="cinematic, highly detailed, dramatic lighting",
            help="Appended to every image_prompt to keep a consistent look across scenes.",
            key="step1_style",
        )
        aspect = st.selectbox(
            "Aspect ratio",
            ["Vertical (1080x1920, for Shorts)", "Square (1024x1024)"],
            index=0,
            key="step1_aspect",
        )
        width, height = (768, 1344) if aspect.startswith("Vertical") else (1024, 1024)
        max_retries = st.slider(
            "Retries per image on failure", 0, 5, 2,
            help="Free HF endpoints can be slow to 'wake up' or briefly overloaded. Retrying helps.",
            key="step1_retries",
        )
        project_id_step1 = st.text_input(
            "Project ID",
            value=datetime.now().strftime("scenes_%Y%m%d_%H%M%S"),
            help="Used as the output ZIP filename.",
            key="step1_project_id",
        )

    script_file = st.file_uploader(
        "Scene Excel (.xlsx)", type=["xlsx", "xls"], key="step1_upload"
    )
    st.caption(
        "Excel needs columns: **id**, **script_text** (used later in Step 2), "
        "**image_prompt** (description of what the image should show)."
    )

    if not script_file:
        st.info("Upload your Excel to get started.")
        if st.button("Generate sample Excel template", key="step1_sample_btn"):
            sample = pd.DataFrame({
                "id": ["wc1983_01", "wc1983_02", "wc1983_03"],
                "script_text": [
                    "In 1983, nobody expected India to win the World Cup.",
                    "The team walked onto the field at Lord's, underdogs once again.",
                    "Kapil Dev led from the front, calm under pressure.",
                ],
                "image_prompt": [
                    "A cricket stadium in 1983, packed crowd, vintage photograph style",
                    "Indian cricket team walking onto the field at Lord's, dramatic wide shot",
                    "Kapil Dev standing confidently on the cricket pitch, determined expression",
                ],
            })
            buf = io.BytesIO()
            sample.to_excel(buf, index=False)
            st.download_button(
                "Download template.xlsx",
                data=buf.getvalue(),
                file_name="scene_template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="step1_sample_dl",
            )
        return

    try:
        df = pd.read_excel(script_file)
    except Exception as e:
        st.error(f"Could not read the Excel file: {e}")
        return

    df.columns = [str(c).strip().lower() for c in df.columns]
    required_cols = {"id", "script_text", "image_prompt"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        st.error(f"Excel is missing required column(s): {', '.join(sorted(missing_cols))}")
        return

    df = df[df["image_prompt"].notna()].reset_index(drop=True)
    st.success(f"Loaded {len(df)} scene(s).")
    st.dataframe(df[["id", "script_text", "image_prompt"]], use_container_width=True)

    def generate_image(prompt: str, model: str, w: int, h: int, retries: int) -> bytes:
        url = f"https://api-inference.huggingface.co/models/{model}"
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        payload = {
            "inputs": prompt,
            "parameters": {"width": w, "height": h},
            "options": {"wait_for_model": True},
        }
        last_error = None
        for attempt in range(retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=120)
                if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                    return resp.content
                try:
                    err_json = resp.json()
                    last_error = err_json.get("error", str(err_json))
                except Exception:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as e:
                last_error = str(e)
            if attempt < retries:
                time.sleep(8)
        raise RuntimeError(last_error or "Unknown error generating image")

    if st.button("🖼️ Generate all scene images", type="primary", key="step1_generate_btn"):
        progress = st.progress(0.0)
        status = st.empty()
        results = []
        image_bufs = {}

        total = len(df)
        for i, row in df.iterrows():
            scene_id = str(row["id"])
            prompt = f"{str(row['image_prompt']).strip()}, {style_suffix}".strip(", ")
            status.write(f"Generating {i+1}/{total}: `{scene_id}`")
            try:
                img_bytes = generate_image(prompt, model_id, width, height, max_retries)
                fname = f"{safe_name(scene_id)}.png"
                image_bufs[scene_id] = (fname, img_bytes)
                results.append({"id": scene_id, "image_prompt": prompt, "status": "✅ Success"})
            except Exception as e:
                results.append({"id": scene_id, "image_prompt": prompt, "status": f"❌ {e}"})
            progress.progress((i + 1) / total)

        results_df = pd.DataFrame(results)
        st.subheader("Per-scene results")
        st.dataframe(results_df, use_container_width=True)

        n_ok = (results_df["status"] == "✅ Success").sum()
        st.success(f"{n_ok}/{total} images generated successfully.")

        if n_ok == 0:
            st.error("No images succeeded — check your HF token and model selection, then try again.")
            return

        # Save into shared session state so Step 2 can pick these up automatically
        st.session_state.generated_images = image_bufs
        st.session_state.generated_images_log = results_df

        st.subheader("Preview")
        preview_cols = st.columns(4)
        for idx, (scene_id, (fname, img_bytes)) in enumerate(list(image_bufs.items())[:8]):
            with preview_cols[idx % 4]:
                st.image(img_bytes, caption=scene_id, use_container_width=True)

        st.success("✅ Images are ready — switch to the **Step 2** tab above to continue, no need to download/re-upload.")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for scene_id, (fname, img_bytes) in image_bufs.items():
                zf.writestr(fname, img_bytes)
        zip_buf.seek(0)

        st.download_button(
            "⬇️ Download scene images (.zip) — optional, for backup",
            data=zip_buf.getvalue(),
            file_name=f"{project_id_step1}.zip",
            mime="application/zip",
            key="step1_zip_dl",
        )

        log_buf = io.BytesIO()
        results_df.to_excel(log_buf, index=False)
        st.download_button(
            "⬇️ Download generation log (.xlsx)",
            data=log_buf.getvalue(),
            file_name=f"{project_id_step1}_log.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="step1_log_dl",
        )


# ---------------------------------------------------------------------------
# Step 2: Shorts Assembler (ElevenLabs voice + pan/zoom + music)
# ---------------------------------------------------------------------------
def render_step2():
    st.header("🎬 Step 2: Assemble Voiced Video")
    st.caption(
        "Uses images from Step 1 (or an uploaded ZIP) + narration script (Excel) → "
        "ElevenLabs voiceover + pan/zoom motion + background music + stitched final video"
    )

    if "ELEVENLABS_API_KEY" not in st.secrets:
        st.error(
            "Missing required secret: ELEVENLABS_API_KEY. "
            "Add it under App Settings → Secrets (Streamlit Cloud) "
            "or .streamlit/secrets.toml (local)."
        )
        return

    client = ElevenLabs(api_key=st.secrets["ELEVENLABS_API_KEY"])

    with st.sidebar:
        st.subheader("Step 2 settings")

        @st.cache_data(ttl=3600)
        def get_voices():
            resp = client.voices.get_all()
            return {v.name: v.voice_id for v in resp.voices}

        try:
            voice_options = get_voices()
        except Exception as e:
            st.error(f"Could not load voices from ElevenLabs: {e}")
            return

        voice_name = st.selectbox("Narrator voice", list(voice_options.keys()), key="step2_voice")
        voice_id = voice_options[voice_name]

        tts_model = st.selectbox(
            "ElevenLabs model",
            ["eleven_flash_v2_5", "eleven_multilingual_v2", "eleven_turbo_v2_5"],
            index=0,
            help=(
                "Flash v2.5 is cheapest and English-focused. For Hindi or other "
                "non-English scripts, use eleven_multilingual_v2."
            ),
            key="step2_tts_model",
        )

        zoom_direction = st.selectbox(
            "Pan/zoom style", ["Slow zoom in", "Slow zoom out"], index=0, key="step2_zoom"
        )

        st.divider()
        st.subheader("Background music")
        enable_music = st.checkbox("Add background music", value=True, key="step2_music_enable")
        music_target_lufs = st.slider(
            "Music level under narration (LUFS)", -40, -20, -30,
            help=(
                "How loud the music sits relative to the voice, auto-normalized. "
                "Each track is measured first, then adjusted to this exact target — "
                "so quiet and loud source tracks all end up consistently leveled, "
                "instead of a flat dB cut that sounds different per track. "
                "-30 LUFS is a safe 'clearly under the voice' default; raise toward "
                "-24 for music that's more present, lower toward -36 for near-silent."
            ),
            disabled=not enable_music,
            key="step2_music_lufs",
        )
        music_override = st.selectbox(
            "Mood override",
            ["Auto-detect from script", "Force: upbeat", "Force: dramatic", "Force: calm"],
            index=0,
            disabled=not enable_music,
            help="Auto-detect scores your script's words against mood keyword lists.",
            key="step2_music_mood",
        )

        project_id = st.text_input(
            "Project ID",
            value=datetime.now().strftime("short_%Y%m%d_%H%M%S"),
            help="Used as the output filename prefix.",
            key="step2_project_id",
        )

    # -----------------------------------------------------------------
    # Image source: auto-use Step 1 output if present, else allow ZIP upload
    # -----------------------------------------------------------------
    images_from_step1 = st.session_state.generated_images

    col1, col2 = st.columns(2)
    with col1:
        if images_from_step1:
            st.success(f"✅ Using {len(images_from_step1)} image(s) generated in Step 1.")
            use_uploaded_zip = st.checkbox(
                "Use a different ZIP instead of Step 1 images", value=False, key="step2_override_zip"
            )
            images_zip = None
            if use_uploaded_zip:
                images_zip = st.file_uploader("Scene images (.zip)", type=["zip"], key="step2_zip_upload")
        else:
            st.info("No images from Step 1 yet — upload a ZIP, or go to Step 1 first.")
            images_zip = st.file_uploader("Scene images (.zip)", type=["zip"], key="step2_zip_upload_only")
            use_uploaded_zip = images_zip is not None

    with col2:
        script_file = st.file_uploader("Narration script (.xlsx)", type=["xlsx", "xls"], key="step2_script_upload")

    st.caption(
        "Excel needs columns: **id** (matches image filenames), "
        "**script_text** (the narration line for that scene)."
    )

    if not script_file:
        st.info("Upload your narration Excel to continue.")
        if st.button("Generate sample script template", key="step2_sample_btn"):
            sample = pd.DataFrame({
                "id": ["wc1983_01", "wc1983_02", "wc1983_03"],
                "script_text": [
                    "In 1983, nobody expected India to win the World Cup.",
                    "The team walked onto the field at Lord's, underdogs once again.",
                    "Kapil Dev led from the front, calm under pressure.",
                ],
            })
            buf = io.BytesIO()
            sample.to_excel(buf, index=False)
            st.download_button(
                "Download template.xlsx",
                data=buf.getvalue(),
                file_name="script_template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="step2_sample_dl",
            )
        return

    if not images_from_step1 and not images_zip:
        st.info("Provide images — either generate them in Step 1, or upload a ZIP above.")
        return

    try:
        script_df = pd.read_excel(script_file)
    except Exception as e:
        st.error(f"Could not read the script Excel file: {e}")
        return

    script_df.columns = [str(c).strip().lower() for c in script_df.columns]
    if "id" not in script_df.columns or "script_text" not in script_df.columns:
        st.error("Script Excel must have columns named 'id' and 'script_text'.")
        return

    script_df = script_df[script_df["script_text"].notna()].reset_index(drop=True)

    work_dir = tempfile.mkdtemp(prefix="shorts_")
    images_dir = os.path.join(work_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    # Write images to disk, either from Step 1's in-memory dict or an uploaded ZIP
    if images_from_step1 and not use_uploaded_zip:
        for scene_id, (fname, img_bytes) in images_from_step1.items():
            with open(os.path.join(images_dir, fname), "wb") as f:
                f.write(img_bytes)
    elif images_zip:
        with zipfile.ZipFile(images_zip) as zf:
            zf.extractall(images_dir)
    else:
        st.error("No image source available.")
        return

    image_files = []
    for root, _, files in os.walk(images_dir):
        for f in files:
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                image_files.append(os.path.join(root, f))

    def find_image_for_id(scene_id: str):
        scene_id = str(scene_id).strip()
        for path in image_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem == scene_id:
                return path
        for path in image_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem.startswith(scene_id + "_") or stem.startswith(scene_id + "-"):
                return path
        return None

    script_df["image_path"] = script_df["id"].apply(find_image_for_id)
    missing = script_df[script_df["image_path"].isna()]

    st.success(f"Loaded {len(script_df)} scene(s) from script.")
    if len(missing) > 0:
        st.warning(
            f"{len(missing)} scene id(s) have no matching image and will be skipped: "
            f"{', '.join(missing['id'].astype(str).tolist())}"
        )

    preview_df = script_df[["id", "script_text", "image_path"]].copy()
    preview_df["image_path"] = preview_df["image_path"].apply(
        lambda p: os.path.basename(p) if isinstance(p, str) else "❌ not found"
    )
    st.dataframe(preview_df, use_container_width=True)

    valid_df = script_df[script_df["image_path"].notna()].reset_index(drop=True)

    def generate_voiceover(text: str, out_path: str, max_retries: int = 2):
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                audio_iter = client.text_to_speech.convert(
                    voice_id=voice_id,
                    text=text,
                    model_id=tts_model,
                    output_format="mp3_44100_128",
                )
                with open(out_path, "wb") as f:
                    for chunk in audio_iter:
                        f.write(chunk)
                return
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(3)
        raise last_error

    def get_audio_duration(path: str) -> float:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())

    def make_panzoom_clip(image_path: str, duration: float, out_path: str, zoom_in: bool = True):
        fps = 25
        total_frames = max(int(duration * fps), 1)
        if zoom_in:
            zoom_expr = "min(zoom+0.0015,1.3)"
        else:
            zoom_expr = "if(eq(on,1),1.3,max(zoom-0.0015,1.0))"
        vf = (
            f"scale=2160:3840,"
            f"zoompan=z='{zoom_expr}':d={total_frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps={fps}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", image_path, "-vf", vf,
             "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path],
            capture_output=True, check=True,
        )

    def mux_video_audio(video_path: str, audio_path: str, out_path: str):
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-i", audio_path,
             "-c:v", "copy", "-c:a", "aac", "-shortest", out_path],
            capture_output=True, check=True,
        )

    def concat_clips(clip_paths: list, out_path: str):
        list_file = os.path.join(work_dir, "concat_list.txt")
        with open(list_file, "w") as f:
            for p in clip_paths:
                f.write(f"file '{p}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", out_path],
            capture_output=True, check=True,
        )

    def get_video_duration(path: str) -> float:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())

    def mix_background_music(video_in_path: str, music_path: str, out_path: str, target_lufs: int):
        """
        Auto-normalizes the music track to a consistent loudness (LUFS) before
        mixing, instead of applying a flat dB cut. This means a quietly-mastered
        track and a loudly-mastered track both end up at the same perceived
        loudness under the narration, rather than one sounding too soft and
        the other too loud at the same dB setting.

        loudnorm is a single-pass loudness filter (good enough for background
        music; the alternative two-pass mode needs an extra analysis run and
        isn't necessary for this use case).
        """
        duration = get_video_duration(video_in_path)
        filter_complex = (
            f"[1:a]loudnorm=I={target_lufs}:TP=-2:LRA=11[music];"
            f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_in_path,
                "-stream_loop", "-1", "-i", music_path,
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac",
                "-t", str(duration),
                out_path,
            ],
            capture_output=True, check=True,
        )

    if st.button("🎬 Generate voiceover + video for all scenes", type="primary", key="step2_generate_btn"):
        progress = st.progress(0.0)
        status = st.empty()
        results = []
        clip_paths = []

        total = len(valid_df)
        for i, row in valid_df.iterrows():
            scene_id = str(row["id"])
            text = str(row["script_text"]).strip()
            img_path = row["image_path"]
            status.write(f"Processing {i+1}/{total}: `{scene_id}`")

            audio_path = os.path.join(work_dir, f"{safe_name(scene_id)}.mp3")
            video_only_path = os.path.join(work_dir, f"{safe_name(scene_id)}_video.mp4")
            final_clip_path = os.path.join(work_dir, f"{safe_name(scene_id)}_final.mp4")

            try:
                generate_voiceover(text, audio_path)
                duration = get_audio_duration(audio_path)
                make_panzoom_clip(
                    img_path, duration, video_only_path,
                    zoom_in=(zoom_direction == "Slow zoom in"),
                )
                mux_video_audio(video_only_path, audio_path, final_clip_path)
                clip_paths.append(final_clip_path)
                results.append({
                    "id": scene_id, "script_text": text,
                    "duration_sec": round(duration, 2), "status": "✅ Success",
                })
            except Exception as e:
                results.append({
                    "id": scene_id, "script_text": text,
                    "duration_sec": None, "status": f"❌ {e}",
                })

            progress.progress((i + 1) / total)

        status.write("Stitching final video...")
        results_df = pd.DataFrame(results)
        st.subheader("Per-scene results")
        st.dataframe(results_df, use_container_width=True)

        n_ok = (results_df["status"] == "✅ Success").sum()
        st.success(f"{n_ok}/{total} scenes processed successfully.")

        if len(clip_paths) == 0:
            st.error("No scenes succeeded — nothing to stitch.")
            return

        final_output_path = os.path.join(work_dir, f"{project_id}.mp4")
        try:
            concat_clips(clip_paths, final_output_path)
            status.write("Stitching done.")

            music_used = None
            if enable_music:
                if music_override == "Auto-detect from script":
                    full_text = " ".join(valid_df["script_text"].astype(str).tolist())
                    mood = classify_mood(full_text)
                else:
                    mood = music_override.replace("Force: ", "")

                music_path = pick_music_track(mood, MUSIC_DIR)
                if music_path is None:
                    st.warning(
                        f"No music files found for mood '{mood}' in `music/{mood}/` — "
                        "skipping background music. Add .mp3 files to that folder to enable it."
                    )
                else:
                    status.write(f"Adding background music (mood: {mood})...")
                    with_music_path = os.path.join(work_dir, f"{project_id}_with_music.mp4")
                    mix_background_music(
                        final_output_path, music_path, with_music_path, music_target_lufs
                    )
                    final_output_path = with_music_path
                    music_used = {"mood": mood, "track": os.path.basename(music_path)}

            status.write("Done.")

            if music_used:
                st.info(f"🎵 Background music: **{music_used['track']}** (mood: {music_used['mood']})")

            with open(final_output_path, "rb") as f:
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
            st.error(f"Could not stitch final video: {e.stderr.decode() if e.stderr else e}")


# ---------------------------------------------------------------------------
# Main: tabs
# ---------------------------------------------------------------------------
st.title("🎬 Shorts Maker")
st.caption("Excel → AI images → voiced, music-backed vertical video, all in one place.")

tab1, tab2 = st.tabs(["① Generate Images", "② Assemble Video"])
with tab1:
    render_step1()
with tab2:
    render_step2()
