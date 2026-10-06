# Gotchas: symptom → cause → fix

Everything here was hit while building and testing this skill (UE 5.8, Windows, 16 GB RAM, RTX 4060).

## Rendering / capture

| Symptom | Cause | Fix |
|---|---|---|
| Every frame completely black (`check` fails, 0.0 max) | `M_Equirect360` failed to compile: the cube sampler was `Linear Color`, the render target needs `Color`. `DrawMaterialToRenderTarget` then silently draws nothing. The error only appears in a **rendering** editor's log (`LogMaterial: Warning ... Failed to compile Material`), never in the `-nullrhi` setup run. | `SAMPLERTYPE_COLOR` (already in `ue360_setup.py`). The CLI greps the log for the message. |
| Panorama is rotated by the rig's heading (a sphere placed "ahead" shows at lon −158°) | `SceneCaptureComponentCube` is **world-aligned**, the rig's rotation is ignored | Rotate the lookup, not the component: rows of the equirect matrix = rig forward/right/up (done per frame in `ue360_capture.py`). |
| Calibration says "inconclusive / NOT FOUND" | A sphere is inside geometry or fog, or no sphere of that hue is bright enough | Calibrate at a frame with open space (`--frame`), check `views`. |
| Calibration spheres missing entirely when made translucent / depth-test-off | The cube capture did not draw them (opaque emissive spheres render fine) | Keep `M_Marker360` opaque (it is). |
| A coloured sphere is not detected although visible | In bright scenes the ×4 emissive saturates to a pastel; RGB-distance matching fails | Hue-based matching (done). |
| `down` sphere hidden under the ground | Sphere placed below the surface the rig stands on | The capture script tries to stop halfway to the first collidable surface; meshes without collision are not seen by that trace. Calibrate higher up. |
| Moving effects jump from frame to frame | `Time` is wall-clock; scrubbing frame by frame samples it irregularly | `patch-time`, or `--fixed-step`: `references/time-control.md`. |
| Screen-space effects repeat on each cube face / show seams | Post-process materials, vignette, lens flare, DoF, motion blur are computed per face in screen space | Not fixable in the capture; use world-space effects, or accept it. The demo's moving stripes show the repetition clearly. |
| Pop-in / low-detail geometry in the first frames | Nanite and texture streaming need ticks and a viewport near the rig | Longer `--warmup`, larger `--settle` (frame 0 already waits `settle + 40`). The capture script also moves the editor viewport to the rig each frame. |
| Colours darker/paler than the editor viewport | Raw capture is linear light | PNG gamma 2.0 (fitted). `--gamma 0` = exact sRGB (paler). |
| Auto-exposure differs between viewport and capture | Not investigated; scene captures have no eye-adaptation history | Prefer fixed exposure in a PostProcessVolume for 360 work. |

## Running Unreal

| Symptom | Cause | Fix |
|---|---|---|
| Crash "paging file too small" / out of memory | Two heavy Unreal processes (editor + capture, or capture during a build) on 16 GB | One process at a time; the CLI warns and refuses under ~3 GB free (`--force`). |
| Editor starts but the script never runs, log silent for many minutes | Observed once at startup of an offscreen editor (hang before the map load) | The CLI kills and relaunches after 6 min without the script's first log line (`--startup`, `--retries`), and kills runs whose log is silent for 8 min. |
| Editor quits right after the script starts | `-ExecutePythonScript` quits the editor when the script returns | Use `-ExecCmds="py script"` and drive the work from `register_slate_post_tick_callback` (done). |
| Same job runs several times in a row | `take_high_res_screenshot` / export can pump Slate ticks re-entrantly | Busy guard in the tick callback (done). If you add screenshot calls, keep it. |
| Saving fails with error 32 (sharing violation) | A running editor has that `.uasset`/`.umap` open and Windows locks it | Close that editor or work on a copy. `setup`/`patch-time` print a note when an editor is running. Giving the rig its own sequence asset avoids touching the flight sequence. |
| `-script=C:\path with spaces\x.py` fails; `-ExecCmds="py C:\a b\x.py"` runs a truncated path | The commandlet and the `py` console command split on spaces, quotes do not help | The CLI copies scripts to a space-free temp folder when needed (`short_script_path`). |
| Other Unreal startup chatter | PIX plugin "failed to initialize", `Unable to find target receipt`, SDK warnings | Harmless in a Blueprint project. |

## Unreal Python API facts

- `MaterialEditingLibrary.get_material_property_input_node(mat, MaterialProperty.MP_MAX)` **crashes the editor** (null dereference). Skip `MP_MAX`/customized-UV sentinels when iterating the enum.
- `MaterialExpressionComponentMask` defaults to **R+G**. Set `r,g,b,a` explicitly or a float1 becomes float2 ("Arithmetic between types float3 and float2 are undefined").
- `MaterialEditingLibrary.get_material_expression_input_names` reports `'None'` for single unnamed inputs; connect those with `''`. `SceneTexture` names its input `UVs`, not `Coordinates` (connecting `Coordinates` fails silently and the default viewport UV is used).
- Sequencer keys: `channel.get_keys()` → `key.get_time()` returns a **FrameTime**; frame = `.frame_number.value`. Transform channels: 0-2 location, 3-5 roll/pitch/yaw, 6-8 scale.
- A sequence file that an editor has open cannot be saved from another process (see error 32 above).
- Runtime `MaterialInstanceDynamic`s are transient: use `MaterialInstanceConstant` assets for anything saved in a level (the demo does).
- `TextureRenderTargetCube` has no format enum: `hdr = True` selects float RGBA. Its `srgb` flag defaults to true and requires sampler type Color.
- `unreal.MaterialLibrary.set_scalar_parameter_value(world, collection, name, value)` changes the **world's instance** of a Material Parameter Collection, not the asset.
- `RenderingLibrary.export_render_target(world, rt, dir, 'x.hdr')` flushes rendering commands, so draw + export in the same tick is safe.
- `unreal.log` lines appear in the `-abslog` file promptly, so tailing it is a reliable progress signal.
- Console `QUIT_EDITOR` via `SystemLibrary.execute_console_command(None, 'QUIT_EDITOR')` ends an offscreen editor cleanly.

## Reference screenshots (if you compare against the normal camera)

- A `CineCameraActor`'s default filmback is **not** 36 mm wide, so 18 mm is not 90° HFOV. Set `filmback.sensor_width = 36`, `sensor_height = 20.25` (then `field_of_view` reads 90).
- The first `take_high_res_screenshot` of a run is often dropped; take a throwaway one first.

## Shell and tooling

| Symptom | Cause | Fix |
|---|---|---|
| `/Game/Foo/Bar` arrives as `C:/Program Files/Git/Game/Foo/Bar` | Git Bash (MSYS) rewrites arguments that look like POSIX paths | The CLI repairs it (`unreal_path`); or `MSYS_NO_PATHCONV=1`. |
| `project not found: \c\Users\...` | `MSYS_NO_PATHCONV=1` leaves `/c/Users/...` drive paths unconverted | The CLI maps `/c/...` to `C:/...`. |
| No ffmpeg / ffmpeg without H.264 | Not on PATH; bundled ffmpegs (e.g. TouchDesigner's) may only ship VP9 | `pip install imageio-ffmpeg` (bundles a full build, found via the uv/pip caches) or set `UE360_FFMPEG`. `ue360.py doctor` lists candidates and encoders. |
| `ffmpeg` output has no 360 flag | Metadata injected before a later remux/`+faststart` | Inject last (the `video` command does); do not remux afterwards. See `video-and-viewing.md`. |
