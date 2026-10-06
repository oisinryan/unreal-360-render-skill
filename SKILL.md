---
name: unreal-360-render
description: Add a 360° camera to an Unreal Engine 5 level and render equirectangular (lat/long) stills or video from it, scripted and offscreen with no plugins, then encode an MP4 tagged as 360 video and preview it in a local WebGL viewer. Use this whenever the user wants a 360 / panoramic / spherical / equirectangular / photosphere / VR-headset render out of Unreal Engine, wants a camera flight (Level Sequence) turned into 360 video, mentions SceneCaptureCube, cubemap-to-equirect, or YouTube/Quest/VLC playback of Unreal footage, or has offline Unreal renders where snow, rain, scrolling noise or other time-driven effects jump or flicker between frames — even if they never say "equirectangular". Also covers ffmpeg discovery/encoding and fixing hung or black offscreen renders. Tested on Windows with UE 5.8.
---

# Unreal 360 render

Turns any UE5 level into 360° equirectangular output. A `SceneCaptureCube` rig (flying the same path as an existing camera, or fixed) is captured into a cube render target; an unlit material converts the cubemap to a lat/long image; an offscreen editor scrubs the sequence frame by frame and exports one `.hdr` per frame; Python + ffmpeg turn those into PNG / MP4. Everything is driven by one CLI, `scripts/ue360.py`, so each step is a single command and settings persist in `<project>/Saved/UE360/session.json`.

Why not Movie Render Queue or the bundled PanoramicCapture plugin? Stock MRQ has no panoramic output, and the experimental plugin is a stereo slice-stitcher. A cube capture is one pass per frame, works with Nanite/Lumen/clouds, and the same rig also gives stills.

## Decide first
Ask (or infer from the conversation) before running anything heavy:
- **Project** (`.uproject`) and **level** (e.g. `/Game/Maps/Canyon`). The level gets one new actor (`Camera360`) and is saved.
- **Camera motion**: copy an existing camera in a Level Sequence (`--source-sequence /Game/LS_Flight --source-binding FlightCamera`; the binding's display name is shown in Sequencer), copy an actor's transform (`--source-actor`), or a fixed spot (`--location x,y,z --yaw ...`).
- **Output**: stills or clip, width (default 1024 → 1024×512, "low res"), clip length. Step 2 on a 30 fps sequence = 15 fps real time.
- **Is anything Time-driven in the look?** (snow/rain/flake post-process, scrolling noise, water, wind). If yes, run `patch-time` — otherwise those effects jump randomly between frames (see below).
- **Machine headroom**: only ONE heavy Unreal process at a time on a 16 GB machine; a second editor + shader compile ends in "paging file too small" crashes. The CLI warns about other editors and refuses below ~3 GB free RAM.

## Workflow
Run from anywhere: `python <skill>/scripts/ue360.py <command> --project <path/to/X.uproject> ...` (the project can be omitted once a `.uproject` is above the cwd or `UE360_PROJECT` is set). `--dry-run` prints the Unreal command lines.

```
ue360.py doctor                                    # engine, ffmpeg + encoders, numpy/Pillow, RAM, running editors
ue360.py testscene                                 # OPTIONAL: tiny demo level (/Game/UE360Demo) to prove the pipeline works first
ue360.py patch-time --material /Game/FX/M_Blizzard   # OPTIONAL: steady clock for Time-driven materials (repeat --material)
ue360.py setup --map /Game/Maps/Canyon --source-sequence /Game/Seq/LS_Flight --source-binding FlightCamera
ue360.py calib --frame 900                         # 6 coloured spheres prove the axis mapping; stores it in the session
ue360.py stills --frames 100,900,1800 --width 1024
ue360.py clip --start 600 --count 180 --step 2 --width 1024 [--settle 20]
ue360.py video my_demo                             # clip PNG frames -> my_demo.mp4 tagged equirectangular
ue360.py sheet | views still_f00900.png | check   # contact sheet, 6 perspective cut-outs, black-frame check
ue360.py view                                      # localhost WebGL viewer (drag/wheel/space/A/R/F); run it in the background
ue360.py clean --raw --frames                      # drop the big .hdr / PNG intermediates once the MP4 is good
```
Output lands in `<project>/Saved/UE360/out` (override with `--out`). Logs are in `Saved/UE360/logs/` — read them when something fails.

Typical session: `doctor` → (`patch-time`) → `setup` → `calib` → `stills` to look at a few frames → `clip` → `video` → `view`. Each render launch costs ~90 s of warm-up (shader compile, Nanite streaming) before the first frame; after that frames take ~0.25 s each (the 180-frame, 12 s snowstorm clip: ~90 s warm-up + 45 s of frames, about 4 minutes wall-clock including editor start-up).

### Pick frames that show something
The calibration spheres sit on the rig's six axes, up to 75–150 m out (the capture script tries to stop halfway to the first collidable surface, but that trace is best-effort and does not see meshes built without collision). A sphere buried inside geometry shows up as `NOT FOUND`, so calibrate at a frame with open space around the rig. For stills use a handful of frames spread over the sequence and look at the `sheet` before committing to a clip.

## Verify before you report success
A render that "finished" can still be wrong. Check, and tell the user what you checked:
1. `calib` prints `axis mapping OK: identity; N of 5 spheres confirmed within 15 deg`. 4 of 5 is normal when the `down` sphere is buried under the ground (it says which one is unconfirmed); fewer than 3 aborts without changing anything. If it says another mapping beats identity, look at `views` before trusting it. See `references/gotchas.md`.
2. `check` reports 0 black frames. All-black output almost always means `M_Equirect360` failed to compile (the CLI greps `Failed to compile Material` from the log) or the rig has no render target — re-run `setup`.
3. Open `views NAME.png`: the horizon runs through the middle row, the "front" tile looks like a normal camera view from the rig, no hard seams at ±180°.
4. For video, `ffmpeg -i file.mp4` should list `spherical: equirectangular`; the viewer should play it.
5. If you used a time fix, confirm effects move steadily (see `references/time-control.md` for the 1-px-per-frame test).

## The time problem (why `patch-time` exists)
Material `Time` follows the wall clock, but an offscreen editor scrubbing frame by frame runs at an arbitrary tick rate, so flakes, scrolling noise, water and wind jump between frames. `patch-time` rewires every `Time` node in the listed materials to `Lerp(Time, MPC_UE360Clock.Time, MPC_UE360Clock.UseOverride)`; renders then set the clock to `sequence frame / fps` (outside renders the override is 0, so nothing changes). Measured on the demo: frame-to-frame stripe shift 1,1,1,… px (spread 0.0) with the clock vs 1–2 px (spread 0.4) without. For things that can't be patched (Niagara, anything using world time), `clip --fixed-step` runs the editor at a fixed delta with a constant tick count per frame (verified for material Time only). Details and limits: `references/time-control.md`.

## Things that bite (full list with causes: `references/gotchas.md`)
- **Cube capture ignores the rig's rotation** (stays world-aligned); the equirect material is re-oriented per frame from the rig's forward/right/up vectors, so yaw, pitch and roll all work. Don't "fix" this by rotating the component.
- **Sampler type must be `Color`**, not `Linear Color`; otherwise the material fails to compile and the draw silently outputs black.
- **Screen-space effects are computed per cube face** (custom post-process materials, vignette, lens flare, DoF, motion blur): they repeat on all six faces and seam. Fine for a demo, visible in VR; prefer world-space effects or accept it and say so.
- **Git Bash** rewrites `/Game/...` arguments to `C:/Program Files/Git/Game/...`; the CLI repairs this, or set `MSYS_NO_PATHCONV=1`.
- **A running editor locks `.uasset`/`.umap` files it has open** (Windows error 32) — `setup`/`patch-time` fail to save. Close that editor or work on a copy of the level; the CLI says so when an editor is running.
- **An offscreen editor can hang at startup** with no log output (seen once). The launcher relaunches after 6 minutes of silence and kills runs whose log goes quiet for 8.
- **Unreal Python crash on `MP_MAX`**: `MaterialEditingLibrary.get_material_property_input_node(mat, MP_MAX)` is a null dereference. Skip sentinel enum values when looping over `MaterialProperty`.
- **`ComponentMask` defaults to R+G**: always set all four channels when authoring nodes from Python, otherwise float1 silently becomes float2 and the material won't compile.

## Output quality notes
- Cube faces default to 512 px, plenty for a 1024-wide equirect (tested). For 2048 wide use `setup --cube 1024` (not tested here; costs more RAM/time).
- Raw captures are linear light; PNG conversion uses gamma 2.0 (fitted against a reference screenshot of the same frame; exact sRGB with `--gamma 0` looks paler).
- `--repeat 2` (default) captures twice per frame so cloud/TAA/Lumen history converges a little; `--settle` is the number of editor ticks to wait after moving the rig (raise it to 20+ for streaming-heavy scenes).
- A noisy scene is expensive to encode: the 12 s 1024×512 snowstorm clip is ~9 MB at CRF 22. `--preset small` (CRF 28) or `--codec h265` shrinks it.
- Not stereoscopic. Windows is the tested platform; macOS/Linux paths exist in the code but are untested.

## Reference files
- `references/how-it-works.md` — the maths and data flow (read when changing the rig or debugging orientation).
- `references/gotchas.md` — symptom → cause → fix table, including Unreal Python API facts learned the hard way.
- `references/time-control.md` — `patch-time`, `--fixed-step`, how to measure steadiness, limits.
- `references/video-and-viewing.md` — ffmpeg discovery/encoders/presets, spherical metadata, players, the local viewer.
