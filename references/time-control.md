# Keeping time-driven effects steady in offline renders

## The problem
A material's `Time` node is the engine's wall-clock (real time since the world started). An offscreen editor that scrubs a sequence frame by frame ticks at whatever rate the machine manages and waits a variable number of ticks per frame, so each output frame samples `Time` at an irregular moment. Anything driven by it — falling flakes, scrolling noise, water, wind sway, cloud scroll — jumps from frame to frame instead of moving smoothly. In the reference snowstorm the blizzard flakes moved several noise cells between frames: uncorrelated static instead of falling snow.

## Fix 1 — `patch-time` (per material, deterministic, verified)
```
ue360.py patch-time --material /Game/FX/M_Blizzard --material /Game/FX/M_Water
```
For each material `ue360_patch_time.py`:
1. creates (once) `/Game/UE360/MPC_UE360Clock`, a Material Parameter Collection with scalars `Time` (0) and `UseOverride` (0);
2. for every `Time` node adds `Lerp(A = Time, B = Collection(Time), Alpha = Collection(UseOverride))` and rewires every input that read the `Time` node (and direct material outputs) to the lerp;
3. recompiles and saves the material in place (the same asset, so references from volumes/meshes stay valid). A copy of the original `.uasset` goes to `Saved/UE360/backup/` first (`--no-backup` to skip).

`ue360_capture.py` then sets `Time = sequence_frame / fps` and `UseOverride = 1` on the MPC *instance of the running world* before each frame. The asset's defaults are never touched, so normal play, PIE and editing see `UseOverride = 0` and behave exactly as before.

Why an MPC and not a per-material dynamic instance: one clock drives every patched material wherever it is used (post-process volume, mesh, landscape, cloud layer) and nothing has to be swapped into volumes at render time. (The first version of this fix swapped a MID with `TimeOverride/UseTimeOverride` scalars into the post-process volume's blendables; that worked but only for post-process materials.)

Limits
- `Time` nodes **inside Material Functions** are not reached (patch the material that calls the function and expose Time, or use fixed-step).
- Niagara, Chaos, cloth, animation and anything else that keeps its own simulation time ignore the clock.
- `Time` with *Override Period* wraps; after patching the override value is not wrapped (harmless in practice).
- The patcher reads `get_material_expression_input_names` / `get_inputs_for_material_expression`; unusual expression types with unnamed inputs report the name `None` (handled: connected as an unnamed pin).

## Fix 2 — `clip --fixed-step` (global, experimental)
```
ue360.py clip --start 0 --count 120 --step 2 --fixed-step --clock off
```
Adds `-benchmark -fps=N` to the editor command line, which forces a fixed delta per tick. The capture script spends exactly `settle + 2` ticks per output frame, so world time advances `(settle + 2) / N` seconds per frame; the CLI picks `N = (fps / step) × (settle + 2)` so that equals the real-time frame interval (for a 30 fps sequence, step 2, settle 8: `-fps=150`). Verified for material `Time` on an **unpatched** material: identical steadiness to Fix 1. Not verified for Niagara or other simulations — it *should* help them, test before relying on it. The first frame of a clip waits `settle + 40` ticks, so frame 0 is not on the same schedule.

Use Fix 1 when you can (independent of tick counts); use Fix 2 when you cannot edit the material or need non-material time.

## Measure it yourself
Render the same short clip twice (clock on / `--clock off`, different `--out` folders) and track a moving pattern between consecutive frames, e.g. 1-D cross-correlation of a luminance profile over a band that only contains the effect:

```python
band = luminance[60:170, 412:612]          # sky rows, centre ±35° of a 1024×512 equirect
profile = band.mean(0); profile = (profile - profile.mean()) / profile.std()
# lag = argmax over -40..40 of mean(prev[lag:] * cur[:-lag]); a steady clock gives the same lag every frame
```
Result on the demo (moving dark stripes from `M_Demo_TimePost`, 40 frames, step 2):

| mode | lag per frame (px) | spread |
|---|---|---|
| render clock (`patch-time`) | −1 on all 39 steps | 0.0 |
| `--fixed-step`, unpatched | −1 on all 39 steps | 0.0 |
| neither | −1 / −2 mixed | 0.4 |

The spread without a fix is small here because the demo stripes move slowly; fast effects (flakes at several UV units per second) show it as visible jumping.
