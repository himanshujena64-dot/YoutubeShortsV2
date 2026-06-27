# Shorts Maker (one app, two steps)

A single Streamlit app with two tabs:

- **① Generate Images** — Excel (with `image_prompt`) → free Hugging Face image generation
- **② Assemble Video** — images + Excel (with `script_text`) → ElevenLabs voiceover + pan/zoom + background music → final MP4

You don't need to download/re-upload anything between the tabs — once you
generate images in Step 1, Step 2 automatically picks them up (it remembers
them for your current browser session). You can also upload a ZIP of images
directly into Step 2 if you already have images from elsewhere.

## One Excel, used in both steps

| id | script_text | image_prompt |
|---|---|---|
| wc1983_01 | In 1983, nobody expected India to win the World Cup. | A cricket stadium in 1983, packed crowd, vintage photo style |
| wc1983_02 | The team walked onto the field at Lord's, underdogs once again. | Indian cricket team walking onto the field at Lord's |

Step 1 reads `id` + `image_prompt`. Step 2 reads `id` + `script_text`
(same file works for both — just upload it twice, once per tab).

## 1. Get your two API keys (both free to start)

**Hugging Face** (for images, free):
1. https://huggingface.co → sign up / log in
2. https://huggingface.co/settings/tokens → "New token" → role: Read

**ElevenLabs** (for voice — you already have this):
1. https://elevenlabs.io → log in
2. Profile/settings → API Keys → copy your key

## 2. Configure secrets

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`:
```toml
HF_API_TOKEN = "hf_your_real_token"
ELEVENLABS_API_KEY = "your_real_elevenlabs_key"
```

**Locally:** stays out of GitHub via `.gitignore`.
**Streamlit Cloud:** App → Settings → Secrets → paste both lines.

## 3. Set up background music (one-time, ~10 minutes)

No music API is used — you download a small set of royalty-free tracks once,
the app auto-picks based on detected mood.

1. Visit **Pixabay Music**: https://pixabay.com/music/ (free, commercial use,
   no attribution required) — or YouTube Audio Library inside YouTube Studio
2. Download 2-3 tracks per mood, drop the `.mp3` files into:
   - `music/upbeat/`
   - `music/dramatic/`
   - `music/calm/`
3. Done — no code changes needed.

The app scans your combined `script_text` for mood keywords and picks
accordingly; you can also force a mood manually in the sidebar.

## 4. Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```
Requires `ffmpeg` installed locally (`apt install ffmpeg` / `brew install ffmpeg`).

## 5. Deploy to Streamlit Community Cloud (one app, one repo)

1. Push this **entire folder** to a single GitHub repo — including
   `packages.txt` (installs ffmpeg) and the `music/` folder with your tracks.
2. share.streamlit.io → New app → this repo → main file `app.py`
3. App Settings → Secrets → paste both `HF_API_TOKEN` and `ELEVENLABS_API_KEY`
4. Deploy — you get **one URL** for the whole tool.

## How to use it, step by step

1. Open the app → you'll see two tabs at the top: **① Generate Images** and
   **② Assemble Video**
2. In tab ①: upload your Excel, click "Generate all scene images", wait
   (free tier can be slow — be patient, it retries automatically)
3. Click over to tab ②: your images are already there. Upload your Excel
   again (same file, or a different one if you only changed wording),
   pick a voice, leave music on auto, click "Generate voiceover + video"
4. Download your finished `.mp4`

## Notes & gotchas

- **Music levels auto-normalize**: instead of a flat volume cut, each music
  track is measured and adjusted to a consistent loudness (LUFS) before
  mixing under the narration. This means quiet-mastered and loud-mastered
  tracks both end up sounding similarly balanced under your voice — you
  shouldn't need to fiddle with levels per-track. The sidebar slider sets the
  *target* loudness if you want music more present (toward -24) or more
  background-only (toward -36).
- **Session memory only**: images generated in Step 1 are kept in memory for
  your current browser tab/session. If you refresh the page or the app
  restarts, you'll need to regenerate them (or use a backed-up ZIP — Step 1
  also gives you a ZIP download button for exactly this reason).
- **Hindi or other non-English scripts**: in Step 2's sidebar, set
  "ElevenLabs model" to `eleven_multilingual_v2` instead of the Flash/Turbo
  options, and pick a voice that handles your language well.
- **Commercial rights (voice)**: ElevenLabs plan must be Starter or above for
  commercial use / YouTube monetization.
- **Commercial rights (music)**: stick to Pixabay Music / YouTube Audio
  Library — sources explicitly cleared for monetized use.
- **Cost**: Hugging Face Step 1 is free (slower, less reliable). ElevenLabs
  Step 2 bills by character — a 90-120 second Short is roughly 700-1000
  characters, well within Starter plan limits for a handful of Shorts/month.
- **Filename matching**: if a scene's `id` doesn't match any image filename,
  that scene is skipped (shown as a warning) rather than breaking the batch.
- **Processing time**: each scene takes a few seconds in both steps; a
  20-scene Short might take a few minutes total across both tabs.
