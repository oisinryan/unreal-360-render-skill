# How it works

Read this when you need to change the rig, debug orientation, or explain the output to someone.

## Data flow

```
 ue360.py setup  (UnrealEditor-Cmd -run=pythonscript -nullrhi, ue360_setup.py)
   RT_Cube360        TextureRenderTargetCube, size_x = cube face px, hdr = true (float RGBA)
   M_Equirect360     unlit material: pixel (u,v) -> direction -> cubemap sample
   M_Marker360       bright unlit colour for calibration spheres
   Camera360         SceneCaptureCube actor in the level, texture_target = RT_Cube360, capture_source = FinalColorLDR,
                     capture_every_frame = false, always_persist_rendering_state = true
   LS_Camera360      (optional) own LevelSequence holding the rig's transform track, copied from a source camera
                     (location + yaw; pitch/roll zeroed unless --keep-tilt), same display rate and playback range

 ue360.py calib|stills|clip  (UnrealEditor -RenderOffscreen -ExecCmds="py ue360_capture.py", a REAL rendering editor)
   per output frame:  LevelSequence.set_current_time(frame)
                      set MPC clock (if patched)      -> Time = frame / fps, UseOverride = 1
                      wait `settle` editor ticks      -> Nanite / texture streaming, cloud history
                      Camera360.capture_scene() x repeat
                      MID(M_Equirect360): RowX/RowY/RowZ = M * [rig forward | right | up]
                      draw_material_to_render_target -> RGBA16F 2D target (width x width/2)
                      export_render_target -> frame.hdr   (Radiance RGBE)

 ue360.py png / video  (numpy + Pillow + ffmpeg)
   .hdr -> float -> gamma 2.0 -> PNG ;  PNG frames -> libx264 yuv420p -> mp4 + spherical metadata
```

Two Unreal processes are involved because creating assets/materials/sequences works headless (`-nullrhi`, fast, small), while capturing needs a GPU. Never run both at once on a 16 GB machine.

## Equirect math (inside `M_Equirect360`)

For output pixel `(u, v)` in `[0,1]²` (v = 0 is the top row):

```
lon = (u - 0.5) * 360°      0 = straight ahead, + = to the right
lat = (0.5 - v) * 180°      +90 = up
d   = ( cos lat cos lon,  cos lat sin lon,  sin lat )        Unreal axes: X forward, Y right, Z up
dir = M * d                 M rows are the vector parameters RowX, RowY, RowZ (dot products)
rgb = TextureCube(RT_Cube360, dir)
```
The material measures angles in *turns* (Sine/Cosine nodes with `period = 1`), so there are no 2π constants.

### Why the matrix M
`SceneCaptureComponentCube` renders its six faces aligned to the **world** axes whatever rotation the actor has (verified: with a rig yawed ~158° the "ahead" sphere appeared at lon -158°). To make the panorama centre the rig's forward direction with the horizon level relative to the rig's up, the lookup direction is rotated instead:

```
M = A · R        R = columns [forward, right, up] of the rig in world space, A = axis-mapping matrix from calibration (identity)
```
Using the actor's own vectors (not yaw only) makes pitch and roll work for free; a rig with yaw 30°, pitch 25°, roll 15° calibrates as well as a level one (score 4.65 / 5 vs 4.34 / 5).

### Calibration (`calib`)
`ue360_capture.py` spawns six unlit emissive spheres 17° wide on the rig's axes: red ahead, green right, blue behind, yellow left, white up, magenta down. It renders all 48 signed axis permutations at 256×128 (`cand_00..47.hdr`, identity first). `ue360.py pick` finds each sphere by **hue** (bright daylight tone-maps saturated spheres to pastels, so RGB distance is unreliable) on bright coloured pixels, keeps the densest cluster per sphere (a similarly coloured lit pillar elsewhere must not drag the centroid), and scores `Σ max(0, 1 − angular error / 30°)` over the five coloured spheres (white `up` is not scored — it is indistinguishable from bright sky). Identity is the known-correct mapping (it won in every test), so another candidate is only chosen when it beats identity by more than 0.75 — a sphere hidden below the ground would otherwise let a near-tie (identity vs Z-flipped) be decided by sort order. The pick is stored in `session.json` as `axes`; the report says how many spheres were confirmed within 15° and names any that were not.

The 48-candidate sweep exists because the UE cubemap sampling convention was not known up front (it turned out to be the identity); it also guards against engine changes.

## Colour
The cube capture uses `SCS_FINAL_COLOR_LDR` into a float cube, so values are tone-mapped but **linear** (not display-encoded). Comparing against a viewport screenshot of the same frame gave an implied gamma of 1.9–2.1; the PNG converter uses **gamma 2.0**. Exact sRGB (`--gamma 0`) is paler than the editor viewport.

## Why a float render target and `.hdr`
`RenderingLibrary.export_render_target` writes Radiance `.hdr` for float targets. Float (`RTF_RGBA16F`) avoids banding between the capture and the final gamma step; the files are small (4 bytes/px, flat RGBE, 2 MB for 1024×512) and trivially parsed with numpy (`read_hdr` also handles the RLE variant).

## Sizes and cost
- Cube face 512 px → 1024×512 equirect is ~2× oversampled at the face centres. Memory grows with the square of the face size.
- ~0.25 s per frame in the snowstorm scene at `repeat 2`, `settle 20` (180 frames: first frame after ~90 s of warm-up, the other 170 in 45 s). Editor start-up adds 30–60 s.
- Output size on disk: 2 MB per raw frame (delete with `clean --raw`), ~0.5 MB per PNG frame, ~9 MB for a noisy 12 s H.264 clip.

## Session file
`<project>/Saved/UE360/session.json` keeps: `map`, `folder`, `label`, `sequence`, `fps`, `start`, `end`, `axes`, `clock`, `step`. `setup` resets `axes` (a new rig must be re-calibrated). Delete the file to start over.
