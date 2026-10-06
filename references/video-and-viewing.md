# Video encoding, 360 metadata and the local viewer

## ffmpeg discovery (`ue360.py doctor` shows the result)
Order searched: `UE360_FFMPEG` → `ffmpeg` on PATH → the `imageio_ffmpeg` Python package → the uv / pip caches of `imageio-ffmpeg` (`%LOCALAPPDATA%\uv\cache\archive-v0\*\imageio_ffmpeg\binaries\`, `site-packages\imageio_ffmpeg\binaries\`) → winget / scoop / chocolatey / `C:\ffmpeg` / Program Files / Homebrew / `/usr/bin`.

A binary only counts if `ffmpeg -encoders` lists an encoder for the requested codec. Learned the hard way: bundled ffmpegs in creative apps (TouchDesigner's, for example) often contain only `libvpx-vp9`. The simplest fix on a machine without a full build: `pip install imageio-ffmpeg` (a self-contained static ffmpeg with libx264/libx265/vp9).

| `--codec` | encoders tried, in order | container |
|---|---|---|
| `h264` (default) | `libx264`, `libopenh264`, `h264_mf`, `h264_nvenc` | mp4 |
| `h265` | `libx265`, `hevc_mf`, `hevc_nvenc` | mp4 (`-tag:v hvc1`) |
| `vp9` | `libvpx-vp9` | webm (no spherical metadata injection) |

## Settings used
```
ffmpeg -y -framerate FPS -i frames/clip_%04d.png
       -vf [scale=W:H,]scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p
       -c:v libx264 -preset slow -crf 22  -movflags 0  out.mp4
```
- `FPS` defaults to `sequence fps / step` (15 for a 30 fps sequence at step 2) so the video plays at real speed.
- Presets: `demo` CRF 22 (default), `quality` CRF 18, `small` CRF 28. `--crf N` overrides, `--scale 2048:1024` resamples.
- Even dimensions and `yuv420p` are required by most players/headsets.
- `-movflags 0` on purpose: **no `+faststart`**. The 360 metadata is inserted afterwards as an extra box inside the video track and only works when `moov` sits after `mdat` (no chunk offsets move). Streaming-optimised files would need the offsets patched; not implemented.
- Noisy scenes (snow, rain, film grain) are expensive: 12 s of 1024×512 flakes at CRF 22 is ~9 MB, the clean demo scene is 0.2 MB.

## 360 metadata
`ue360.py video` finishes by inserting Google's *spherical video v1* XML (`uuid` box `ffcc8263-f855-4a93-8814-587a02521fdd`, `ProjectionType = equirectangular`, `Stitched = true`) into the video `trak`, growing `trak` and `moov` sizes accordingly. Verified: ffmpeg then prints `spherical: equirectangular` for the stream and the file decodes cleanly. YouTube, VLC and most headset players read this tag; if a player ignores it, choose the equirectangular/360 projection manually. Re-muxing with ffmpeg afterwards drops the box — inject last.

A player that does not understand the tag shows the flat 2:1 image (expected); use `ue360.py view`, VLC, YouTube or a headset instead.

## Local viewer (`ue360.py view`)
`viewer/viewer.html` is a dependency-free WebGL page: a fragment shader turns each screen pixel into a view ray and looks up the equirect texture (video or image). Keys: drag = look, wheel = zoom (FOV), space = pause, A = auto-rotate, R = reset, F = fullscreen; `?src=file.mp4|png`.

It must be served over HTTP: browsers refuse to upload a `file://` video into a WebGL texture (tainted canvas). `ue360.py view` copies the viewer into the output folder, serves that folder on `http://127.0.0.1:8360` (loopback only, `--port`), opens the browser, and runs until Ctrl+C — start it in the background when driving it from an agent, then stop the server afterwards (`Get-NetTCPConnection -LocalPort 8360` → `Stop-Process`). `--no-open` prints the URL only.

Checked in a Chromium-based pane: the video plays, `currentTime` advances, the yaw auto-rotates, no console errors. Pointer events make one-finger touch dragging work in principle (untested); pinch-zoom is not implemented.
