# Shorts Maker (one app, two steps)

A single Streamlit app with two tabs:

- **① Generate Images** — Excel (with `image_prompt`) → Cloudflare Workers AI image generation (genuinely free, no billing setup required)
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

## 1. Get your credentials

**Cloudflare** (for images — free, 10,000 Neurons/day, no card required):
1. https://dash.cloudflare.com → sign up (free, no credit card)
2. Click **Workers & Pages** in the left sidebar → click **AI** (or search
   "Workers AI" in the dashboard)
3. Click **Use REST API** → **Create a Workers AI API Token** → review →
   **Create API Token** → copy it (this is `CLOUDFLARE_API_TOKEN`)
4. On the same page, copy your **Account ID** (this is `CLOUDFLARE_ACCOUNT_ID`)

**ElevenLabs** (for voice — you already have this):
1. https://elevenlabs.io → log in
2. Profile/settings → API Keys → copy your key

## 2. Configure secrets

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`:
```toml
CLOUDFLARE_ACCOUNT_ID = "your_real_account_id"
CLOUDFLARE_API_TOKEN = "your_real_api_token"
ELEVENLABS_API_KEY = "your_real_elevenlabs_key"
```

**Locally:** stays out of GitHub via `.gitignore`.
**Streamlit Cloud:** App → Settings → Secrets → paste all three lines.

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

## 3b. Optional: per-scene SFX and per-scene background music

You can add two extra columns to your script Excel. Both are fully
**optional** — leave them out (or leave cells blank) and the app behaves
exactly as before.

| id | script_text | image_prompt | SFX | Background Music |
|---|---|---|---|---|
| scene_01 | ... | ... | temple bell | dramatic |
| scene_02 | ... | ... | coins falling | dramatic |
| scene_03 | ... | ... |  |  |

- **`SFX`** — one simple cue ("coins falling", "temple bell") plays once at
  the start of that scene. You can also write a **multi-line/bulleted cell**
  to layer several sounds at once:
  ```
  Deep ocean waves
  * Low cinematic drone
  * Distant thunder
  * Submarine sonar ping (very faint)
  * Slow heartbeat beginning in the background
  ```
  Each line is matched independently against folders under `sfx/`:
  - **One-shot** folders (`whoosh`, `coins`, `temple_bell`, `sonar_ping`, ...)
    play once, trimmed to the scene length.
  - **Ambient** folders (`ocean_waves`, `cinematic_drone`, `thunder_distant`,
    `heartbeat_loop`, ...) loop to fill the entire scene.
  - `(faint)` / `(very faint)` / `(loud)` in a line lowers/raises that
    layer's volume; words like "beginning"/"building"/"fading in" make that
    layer fade in from silence instead of starting instantly.
  - Any line that doesn't match a folder is skipped with a warning — the
    rest of the layers still play.
- **`Background Music`** — a mood per scene (`upbeat`, `dramatic`, `calm`,
  `inspirational`, `suspense`, `sad`, `romantic`, `epic`, `energetic`,
  `nostalgic`). When the mood changes between scenes, the music crossfades
  to the new mood's track instead of playing one fixed track for the whole
  video. Leave a cell blank to keep the previous scene's mood playing.

If the whole `Background Music` column is blank, the app falls back to the
single auto-detected/forced mood behavior described above. Same for `SFX` —
if the whole column is blank, no SFX are added.

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
3. App Settings → Secrets → paste `CLOUDFLARE_ACCOUNT_ID`,
   `CLOUDFLARE_API_TOKEN`, and `ELEVENLABS_API_KEY`
4. Deploy — you get **one URL** for the whole tool.

## How to use it, step by step

1. Open the app → you'll see two tabs at the top: **① Generate Images** and
   **② Assemble Video**
2. In tab ①: upload your Excel, click "Generate all scene images" — typically
   fast (a few seconds per image)
3. Click over to tab ②: your images are already there. Upload your Excel
   again (same file, or a different one if you only changed wording),
   pick a voice, leave music on auto, click "Generate voiceover + video"
4. Download your finished `.mp4`

## Cost reality, honestly

- **Cloudflare free tier**: 10,000 Neurons/day, no card required, ever.
  How far that goes depends heavily on which model and resolution you pick:
  - `flux-1-schnell` (no size control, fixed square-ish output): **100+ free
    images/day** — cheapest by far, but can't produce true 9:16.
  - `phoenix-1.0` / `lucid-origin` at **540x960** (9:16, lower-res): **~9 free
    images/day**
  - Same models at **720x1280**: **~5 free images/day**
  - Same models at full **1080x1920**: **~2 free images/day** — a 20-scene
    Short would blow through several days' free allowance in one batch.
  - These are estimates based on Cloudflare's published per-tile/per-step
    pricing; actual Neuron cost can vary slightly.
- **Practical recommendation**: for a real 9:16 Short with many scenes, use
  540x960 or 720x1280 — both are genuinely vertical (not cropped) and look
  fine on a phone screen, while keeping your daily batch within the free
  allowance. Save full 1080x1920 for a handful of "hero" scenes, or enable
  billing if you want every scene at full resolution.
- **If you exceed 10,000 Neurons/day**: requests simply fail until the reset
  (00:00 UTC) unless you upgrade to a Workers Paid plan ($0.011/1,000
  Neurons beyond the free allowance).
- **ElevenLabs**: bills by character — a 90-120 second Short is roughly
  700-1000 characters, well within Starter plan limits for a handful of
  Shorts/month.

## Notes & gotchas

- **Output format**: Cloudflare's hosted models always return JPEG with no
  format option in their API — the app automatically converts every image to
  PNG before handing it to Step 2 (or before you download the backup ZIP),
  so you always get `.png` files.
- **Aspect ratio depends on model choice**: `phoenix-1.0` and `lucid-origin`
  support a real 9:16 vertical output (the sidebar lets you pick the exact
  resolution). `flux-1-schnell` has no size control at all — it always
  returns a fixed square-ish image, regardless of any sidebar setting. If
  you pick flux for its much larger free-image budget, Step 2's pan/zoom
  step will scale/crop it to vertical when building the video, but for
  true 9:16 framing from the start, use one of the Leonardo models instead.
- **If image generation fails**: double check both `CLOUDFLARE_ACCOUNT_ID`
  and `CLOUDFLARE_API_TOKEN` are correct, and that the API token has
  "Workers AI" permissions (the dashboard's "Create a Workers AI API Token"
  button sets this up correctly automatically). If you've generated a lot of
  images today, you may have hit the 10,000 Neuron/day free cap — it resets
  at 00:00 UTC.
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
- **Commercial rights (images)**: FLUX.1 [schnell] is released under its own
  license terms (Black Forest Labs) — check the current terms at
  https://bfl.ai/legal/terms-of-service before relying on this for
  monetized content.
- **Filename matching**: if a scene's `id` doesn't match any image filename,
  that scene is skipped (shown as a warning) rather than breaking the batch.
- **Processing time**: each scene takes a few seconds in both steps; a
  20-scene Short might take a few minutes total across both tabs.
