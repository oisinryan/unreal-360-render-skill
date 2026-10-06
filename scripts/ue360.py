#!/usr/bin/env python3
"""ue360 - Unreal Engine 360 (equirectangular) camera + render pipeline.

  ue360.py doctor     [--project P]                 check engine / ffmpeg / python libs / memory
  ue360.py testscene  --project P                   build /Game/UE360Demo (tiny demo level + camera path) to try the pipeline
  ue360.py setup      --project P --map /Game/M ... add the 360 camera rig (+ sequence) to a level
  ue360.py patch-time --project P --material /Game/X ...   make Time-driven materials follow the render clock
  ue360.py calib      [--frame N]                   verify the cubemap axis mapping with 6 coloured spheres
  ue360.py stills     --frames 100,900              render equirect stills
  ue360.py clip       --start 0 --count 120 --step 2    render a frame sequence (step = sequence frames per output frame)
  ue360.py png | check | views NAME | sheet | video NAME | view [NAME] | clean
Settings that must persist between commands (project, map, rig, sequence, fps, axes, clock) live in <project>/Saved/UE360/session.json.
Full docs: SKILL.md and references/. Windows is the tested platform.
"""
import argparse
import ctypes
import functools
import glob
import http.server
import json
import math
import os
import re
import shutil
import socketserver
import struct
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / 'scripts'
IS_WIN = os.name == 'nt'
GAMMA = 2.0       # the raw cube capture is linear light; 2.0 matches the editor viewport (fitted against a reference screenshot)


def unreal_path(p):
    """Content paths look like /Game/Folder/Asset. Git Bash (MSYS) rewrites such arguments into C:/Program Files/Git/Game/...;
    undo that so the commands work from any shell (or run with MSYS_NO_PATHCONV=1)."""
    if p and not p.startswith(('/Game/', '/Engine/', '/Script/')) and re.match(r'^[A-Za-z]:[/\\]', p):
        m = re.search(r'[/\\](Game|Engine|Script)([/\\].*)?$', p)
        if m:
            return '/' + m.group(1) + (m.group(2) or '').replace(chr(92), '/')
    return p


def say(*a):
    print(*a, flush=True)


def die(msg, code=2):
    print('ue360: ' + msg, file=sys.stderr, flush=True)
    sys.exit(code)


# ================================================================================= project / session
def find_project(args):
    p = getattr(args, 'project', None) or os.environ.get('UE360_PROJECT')
    if p:
        if IS_WIN:                                           # Git Bash style /c/Users/x -> C:/Users/x
            m = re.match(r'^[/\\]([A-Za-z])[/\\](.*)$', p)
            if m and not os.path.exists(p):
                p = '%s:/%s' % (m.group(1).upper(), m.group(2))
        p = Path(p)
        if p.is_dir():
            found = sorted(p.glob('*.uproject'))
            if not found:
                die('no .uproject in ' + str(p))
            p = found[0]
        if not p.exists():
            die('project not found: ' + str(p))
        return p.resolve()
    d = Path.cwd()
    for cand in [d] + list(d.parents):
        found = sorted(cand.glob('*.uproject'))
        if found:
            return found[0].resolve()
    die('pass --project <path to .uproject> (or set UE360_PROJECT)')


def work_dir(project):
    d = project.parent / 'Saved' / 'UE360'
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_session(project):
    f = work_dir(project) / 'session.json'
    return json.loads(f.read_text()) if f.exists() else {}


def save_session(project, **kv):
    s = load_session(project)
    s.update({k: v for k, v in kv.items() if v is not None})
    (work_dir(project) / 'session.json').write_text(json.dumps(s, indent=1))
    return s


def out_dir(project, args):
    s = load_session(project)
    d = Path(getattr(args, 'out', None) or s.get('out') or (work_dir(project) / 'out'))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ================================================================================= engine discovery
def read_uproject(project):
    return json.loads(project.read_text(encoding='utf-8-sig'))


def engine_roots(assoc):
    """Candidate engine root directories (folders containing Engine/) for an EngineAssociation like '5.8'."""
    found = []

    def add(p):
        if p and (Path(p) / 'Engine').is_dir() and str(Path(p)) not in found:
            found.append(str(Path(p)))

    for env in ('UE360_ENGINE', 'UE_ROOT'):
        add(os.environ.get(env))
    known = []
    manifest = Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'Epic' / 'UnrealEngineLauncher' / 'LauncherInstalled.dat'
    if manifest.exists():
        try:
            for it in json.loads(manifest.read_text(encoding='utf-8-sig')).get('InstallationList', []):
                known.append(it.get('InstallLocation', ''))
                if it.get('AppName', '') in ('UE_' + assoc, assoc):
                    add(it.get('InstallLocation'))
        except Exception:
            pass
    if IS_WIN:
        try:
            import winreg
            for hive, key, val in ((winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\EpicGames\Unreal Engine\%s' % assoc, 'InstalledDirectory'),
                                   (winreg.HKEY_CURRENT_USER, r'Software\Epic Games\Unreal Engine\Builds', assoc)):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        add(winreg.QueryValueEx(k, val)[0])
                except OSError:
                    pass
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\EpicGames\Unreal Engine') as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                            known.append(winreg.QueryValueEx(sk, 'InstalledDirectory')[0])
                    except OSError:
                        pass
        except Exception:
            pass
    parents = {str(Path(k).parent) for k in known if k} | {r'C:\Program Files\Epic Games', r'D:\Epic Games', r'C:\Epic Games',
                                                          '/Users/Shared/Epic Games', os.path.expanduser('~/UnrealEngine')}
    for par in parents:
        add(Path(par) / ('UE_' + assoc))
    return found


def find_engine(project, override=None):
    if override:
        if not (Path(override) / 'Engine').is_dir():
            die('--engine must be the folder that contains Engine/: ' + override)
        return Path(override)
    assoc = str(read_uproject(project).get('EngineAssociation', ''))
    roots = engine_roots(assoc)
    if not roots:
        die('could not locate Unreal Engine %r. Pass --engine <root> or set UE360_ENGINE. (Epic launcher manifest, registry and '
            'UE_%s folders were searched.)' % (assoc, assoc))
    return Path(roots[0])


def editor_exe(root, cmd=False):
    plat = 'Win64' if IS_WIN else ('Mac' if sys.platform == 'darwin' else 'Linux')
    name = 'UnrealEditor' + ('-Cmd' if cmd else '') + ('.exe' if IS_WIN else '')
    p = Path(root) / 'Engine' / 'Binaries' / plat / name
    if not p.exists():
        die('missing ' + str(p))
    return p


# ================================================================================= running Unreal
def free_ram_gb():
    if not IS_WIN:
        return None

    class MS(ctypes.Structure):
        _fields_ = [('l', ctypes.c_ulong), ('load', ctypes.c_ulong), ('tp', ctypes.c_ulonglong), ('ap', ctypes.c_ulonglong),
                    ('tpf', ctypes.c_ulonglong), ('apf', ctypes.c_ulonglong), ('tv', ctypes.c_ulonglong), ('av', ctypes.c_ulonglong),
                    ('ae', ctypes.c_ulonglong)]
    m = MS()
    m.l = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ap / 2 ** 30, m.tp / 2 ** 30


def unreal_processes():
    if not IS_WIN:
        return []
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq UnrealEditor.exe', '/FO', 'CSV', '/NH'], capture_output=True, text=True).stdout
    return [l.split('","')[1] for l in out.splitlines() if 'UnrealEditor.exe' in l]


def kill(proc):
    try:
        proc.kill()
    except Exception:
        pass


def short_script_path(script):
    """`py <path>` in -ExecCmds splits on spaces; copy the script to a space-free, writable folder when its path has one."""
    s = str(script)
    if ' ' not in s:
        return s
    import tempfile
    for base in (tempfile.gettempdir(), os.environ.get('SystemDrive', 'C:') + os.sep, '/tmp'):
        d = Path(base) / 'ue360_scripts'
        if ' ' in str(d):
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(script, d / Path(script).name)
            return str(d / Path(script).name)
        except OSError:
            continue
    die('the skill folder path contains spaces and no space-free folder is writable; move the skill to a path without spaces')


def as_cmd(cmdline):
    """Windows takes the command line as one string (UE parses -key="value with spaces" itself); POSIX needs an argv list."""
    if IS_WIN:
        return cmdline
    import shlex
    return shlex.split(cmdline)


def run_headless(args, project, script, cfg, tag):
    """Runs an in-editor script with UnrealEditor-Cmd -run=pythonscript -nullrhi (no rendering needed). Returns the script's result json."""
    root = find_engine(project, getattr(args, 'engine', None))
    wd = work_dir(project)
    cfg = dict(cfg, result_path=str(wd / (tag + '_result.json')))
    cfg_path = wd / (tag + '_config.json')
    cfg_path.write_text(json.dumps(cfg, indent=1))
    log = wd / 'logs' / (tag + '.log')
    log.parent.mkdir(exist_ok=True)
    Path(cfg['result_path']).unlink(missing_ok=True)
    cmd = '"%s" "%s" -run=pythonscript -script="%s" -unattended -nop4 -nosplash -stdout -FullStdOutLogOutput -nullrhi -abslog="%s"' % (
        editor_exe(root, cmd=True), project, short_script_path(SCRIPTS / script), log)         # the commandlet splits -script= on spaces
    say('engine: %s\nrunning %s headless (log: %s)' % (root, script, log))
    if args.dry_run:
        say('DRY RUN:', cmd)
        return {'ok': True, 'dry_run': True}
    free = free_ram_gb()
    if free and free[0] < 2.5:
        say('warning: only %.1f GB RAM free - close other Unreal processes first' % free[0])
    env = dict(os.environ, UE360_CONFIG=str(cfg_path))
    try:
        r = subprocess.run(as_cmd(cmd), env=env, timeout=args.timeout * 60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        die('timed out after %d min (see %s)' % (args.timeout, log))
    res = Path(cfg['result_path'])
    if not res.exists():
        die('script produced no result (exit %s). Look for Traceback / Error in %s' % (r.returncode, log))
    result = json.loads(res.read_text())
    if not result.get('ok'):
        die('script failed:\n' + str(result.get('error', result)))
    for line in grep(log, r'Warning: UE360|LogMaterial: Warning|Failed to compile Material'):
        say('  log: ' + line[:200])
    return result


def grep(path, pattern, limit=20):
    rx = re.compile(pattern)
    out = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if rx.search(line):
                    out.append(line.rstrip())
                    if len(out) >= limit:
                        break
    except FileNotFoundError:
        pass
    return out


def run_render(args, project, cfg, tag):
    """Launches a rendering (offscreen) editor running ue360_capture.py. Kills and relaunches a hung start (seen: no log output for
    25 min), kills a run whose log goes silent for 8 min, and returns the capture report."""
    sess = load_session(project)
    root = find_engine(project, getattr(args, 'engine', None))
    wd = work_dir(project)
    od = out_dir(project, args)
    raw = od / 'raw'
    raw.mkdir(exist_ok=True)
    cfg = dict(cfg, mode=tag if tag in ('calib', 'stills', 'clip') else 'stills', raw_dir=str(raw), label=sess.get('label', 'Camera360'),
               folder=sess.get('folder', '/Game/UE360'), sequence=sess.get('sequence'), fps=sess.get('fps', 30.0), axes=sess.get('axes'),
               clock=None if args.clock == 'off' else sess.get('clock'), width=args.width, settle=args.settle, repeat=args.repeat, warmup=args.warmup,
               result_path=str(wd / (tag + '_result.json')))
    if not sess.get('map'):
        die('no session: run `ue360.py setup --map ...` first')
    cfg_path = wd / (tag + '_config.json')
    cfg_path.write_text(json.dumps(cfg, indent=1))
    log = wd / 'logs' / (tag + '.log')
    log.parent.mkdir(exist_ok=True)
    script = short_script_path(SCRIPTS / 'ue360_capture.py')
    cmdline = '"%s" "%s" %s -ExecCmds="py %s" -RenderOffscreen -ResX=1280 -ResY=720 -windowed -nosplash -nop4 ' \
              '-ini:Engine:[DevOptions.Shaders]:NumUnusedShaderCompilingThreads=5 -abslog="%s"' % (
                  editor_exe(root), project, sess['map'], script, log)
    if getattr(args, 'fixed_step', False) and tag == 'clip':
        # Experimental: fixed-delta ticks. The capture script spends exactly settle+2 ticks per output frame, so world time (material
        # Time, Niagara, cloud scroll ...) advances (settle+2)/N seconds per frame; N is chosen so that equals the real-time frame interval.
        n = max(1, round((sess.get('fps') or 30.0) / cfg['step'] * (args.settle + 2)))
        cmdline += ' -benchmark -fps=%d' % n
        say('fixed time step: -benchmark -fps=%d (%d ticks per output frame)' % (n, args.settle + 2))
    say('engine: %s\nrendering %s -> %s (log: %s)' % (root, tag, raw, log))
    if args.dry_run:
        say('DRY RUN:', cmdline)
        return {'dry_run': True}
    others = unreal_processes()
    free = free_ram_gb()
    if others:
        say('note: %d other UnrealEditor.exe running (%s).%s' % (len(others), ', '.join(others), ' RAM free: %.1f GB.' % free[0] if free else ''))
    if free and free[0] < 3.0 and not args.force:
        die('only %.1f GB RAM free - a second Unreal process risks "paging file too small" crashes. Close an editor or pass --force.' % free[0])
    env = dict(os.environ, UE360_CONFIG=str(cfg_path))
    began_all = time.time()
    for attempt in range(args.retries + 1):
        for f in (log, Path(cfg['result_path'])):
            f.unlink(missing_ok=True)
        began = time.time()
        proc = subprocess.Popen(as_cmd(cmdline), env=env)
        reason = None
        while proc.poll() is None:
            time.sleep(5)
            started = bool(grep(log, r'UE360 start', 1))
            age = (time.time() - log.stat().st_mtime) if log.exists() else 1e9
            if not started and time.time() - began > args.startup * 60:
                reason = 'editor never reached the capture script within %d min (hung start)' % args.startup
            elif started and age > 8 * 60:
                reason = 'log silent for 8 min'
            elif time.time() - began > args.timeout * 60:
                reason = 'timeout'
            if reason:
                kill(proc)
                break
        res = Path(cfg['result_path'])
        if not reason and res.exists():
            report = json.loads(res.read_text())
            say('capture finished in %.0f s: %d files, %d errors' % (time.time() - began_all, len(report['written']), len(report['errors'])))
            for e in report['errors'][:3]:
                say('  error: ' + str(e)[:300])
            for line in grep(log, r'Failed to compile Material|LogMaterial: Warning.*(Equirect|Marker)', 5):
                say('  log: ' + line[:240])
            return report
        say('attempt %d failed: %s' % (attempt + 1, reason or 'editor exited without a result (crash?) - see %s' % log))
        time.sleep(15)
    die('render failed after %d attempts; see %s' % (args.retries + 1, log))


# ================================================================================= images / video (numpy + Pillow)
def need_imaging():
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        die('numpy and Pillow are required for image conversion: pip install numpy pillow')


def read_hdr(path):
    import numpy as np
    b = open(path, 'rb').read()
    hdr_end = b.find(b'\n\n') + 2
    nl = b.find(b'\n', hdr_end)
    res = b[hdr_end:nl].decode().split()                # -Y h +X w
    h, w = int(res[1]), int(res[3])
    data = b[nl + 1:]
    if len(data) == h * w * 4:
        rgbe = np.frombuffer(data, np.uint8).reshape(h, w, 4)
    else:                                                # new-style scanline RLE
        rgbe = np.zeros((h, w, 4), np.uint8)
        p = 0
        for y in range(h):
            assert data[p] == 2 and data[p + 1] == 2, 'unsupported .hdr encoding: ' + str(path)
            p += 4
            for c in range(4):
                x = 0
                while x < w:
                    n = data[p]
                    p += 1
                    if n > 128:
                        n -= 128
                        rgbe[y, x:x + n, c] = data[p]
                        p += 1
                    else:
                        rgbe[y, x:x + n, c] = np.frombuffer(data[p:p + n], np.uint8)
                        p += n
                    x += n
    e = rgbe[..., 3].astype(np.int32)
    scale = np.where(e == 0, 0.0, np.ldexp(1.0, e - 136)).astype(np.float32)
    return rgbe[..., :3].astype(np.float32) * scale[..., None]


def to_u8(img, gamma=GAMMA):
    import numpy as np
    x = np.clip(img, 0, 1)
    x = np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055) if gamma == 0 else x ** (1.0 / gamma)
    return (x * 255 + 0.5).astype(np.uint8)


def perspective(eq, yaw=0.0, pitch=0.0, hfov=90.0, w=320, h=180):
    """Perspective cut-out of an equirect float image (UE axes: X forward, Y right, Z up)."""
    import numpy as np
    t = math.tan(math.radians(hfov) / 2)
    gx, gy = np.meshgrid(((np.arange(w) + 0.5) / w * 2 - 1) * t, (1 - (np.arange(h) + 0.5) / h * 2) * t * h / w)
    d = np.stack([np.ones_like(gx), gx, gy], -1)
    cp, sp, cy, sy = math.cos(math.radians(pitch)), math.sin(math.radians(pitch)), math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    d = np.stack([d[..., 0] * cp - d[..., 2] * sp, d[..., 1], d[..., 0] * sp + d[..., 2] * cp], -1)
    d = np.stack([d[..., 0] * cy - d[..., 1] * sy, d[..., 0] * sy + d[..., 1] * cy, d[..., 2]], -1)
    lon, lat = np.arctan2(d[..., 1], d[..., 0]), np.arcsin(d[..., 2] / np.linalg.norm(d, axis=-1))
    H, W = eq.shape[:2]
    x, y = (lon / (2 * math.pi) + 0.5) * W - 0.5, (0.5 - lat / math.pi) * H - 0.5
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    xa, xb, ya, yb = x0 % W, (x0 + 1) % W, np.clip(y0, 0, H - 1), np.clip(y0 + 1, 0, H - 1)
    return (eq[ya, xa] * (1 - fx) + eq[ya, xb] * fx) * (1 - fy) + (eq[yb, xa] * (1 - fx) + eq[yb, xb] * fx) * fy


# calibration spheres: (name, expected lon, expected lat, hue in degrees). The white 'up' sphere is not scored (same as bright sky).
MARKERS = [('fwd', 0, 0, 0), ('right', 90, 0, 120), ('back', 180, 0, 240), ('left', -90, 0, 60), ('down', 0, -90, 300)]


def angdist(lon1, lat1, lon2, lat2):
    a, b = math.radians(lon1), math.radians(lat1)
    c, d = math.radians(lon2), math.radians(lat2)
    return math.degrees(math.acos(max(-1.0, min(1.0, math.sin(b) * math.sin(d) + math.cos(b) * math.cos(d) * math.cos(a - c)))))


def hsv_hue(eq):
    import numpy as np
    r, g, b = eq[..., 0], eq[..., 1], eq[..., 2]
    mx, mn = eq.max(-1), eq.min(-1)
    d = mx - mn
    dd = np.where(d > 1e-6, d, 1.0)
    h = np.zeros_like(mx)
    rc = (mx == r) & (d > 1e-6)
    gc = (mx == g) & (d > 1e-6) & ~rc
    bc = (d > 1e-6) & ~rc & ~gc
    h[rc] = (60 * ((g - b) / dd))[rc] % 360
    h[gc] = (60 * ((b - r) / dd) + 120)[gc]
    h[bc] = (60 * ((r - g) / dd) + 240)[bc]
    return h, d / np.maximum(mx, 1e-6), mx


def marker_directions(eq):
    """Where does each calibration sphere show up? -> {name: (lon, lat, pixels)}. Matching is by hue (bright daylight tone-maps the
    saturated spheres to pastels, so RGB distance is unreliable) on bright, coloured pixels; of those only the densest cluster counts, so
    a lit pillar of similar hue elsewhere cannot drag the result."""
    import numpy as np
    h_, w_ = eq.shape[:2]
    hue, sat, val = hsv_hue(eq)
    LON, LAT = np.meshgrid((np.arange(w_) + 0.5) / w_ * 360.0 - 180.0, 90.0 - (np.arange(h_) + 0.5) / h_ * 180.0)
    out = {}
    for name, _, _, target in MARKERS:
        dh = np.abs(hue - target)
        m = (np.minimum(dh, 360.0 - dh) < 30.0) & (sat > 0.2) & (val > 0.85)
        if int(m.sum()) < 12:
            out[name] = (None, None, int(m.sum()))
            continue
        lo, la = LON[m], LAT[m]
        hist, xe, ye = np.histogram2d(lo, la, bins=(36, 18), range=((-180, 180), (-90, 90)))
        i, j = np.unravel_index(np.argmax(hist), hist.shape)
        pk = ((xe[i] + xe[i + 1]) / 2, (ye[j] + ye[j + 1]) / 2)
        near = np.array([angdist(a, b, pk[0], pk[1]) < 25.0 for a, b in zip(lo, la)])
        lo, la = lo[near], la[near]
        wgt = np.cos(np.radians(la)) + 1e-3                                  # equal-area weighting
        out[name] = (math.degrees(math.atan2((np.sin(np.radians(lo)) * wgt).sum(), (np.cos(np.radians(lo)) * wgt).sum())),
                     float(np.average(la, weights=wgt)), int(near.sum()))
    return out


def marker_score(found):
    """Sum over the spheres of max(0, 1 - angular error / 30 degrees); 5 would be perfect."""
    sc = 0.0
    for name, lon, lat, _ in MARKERS:
        f = found[name]
        if f[0] is not None:
            sc += max(0.0, 1.0 - angdist(f[0], f[1], lon, lat) / 30.0)
    return sc


# ================================================================================= ffmpeg
ENCODERS = {'h264': ['libx264', 'libopenh264', 'h264_mf', 'h264_nvenc'], 'h265': ['libx265', 'hevc_mf', 'hevc_nvenc'], 'vp9': ['libvpx-vp9']}
PRESETS = {'demo': 22, 'quality': 18, 'small': 28}


def ffmpeg_candidates():
    c = [os.environ.get('UE360_FFMPEG')]
    c.append(shutil.which('ffmpeg'))
    try:
        import imageio_ffmpeg
        c.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    home = Path.home()
    pats = [str(home / 'AppData/Local/uv/cache/archive-v0/*/imageio_ffmpeg/binaries/ffmpeg*'),        # uv cache (pip install imageio-ffmpeg)
            str(home / 'AppData/Local/Programs/Python/*/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg*'),
            str(home / '.cache/uv/archive-v0/*/imageio_ffmpeg/binaries/ffmpeg*'),
            str(home / 'AppData/Local/Microsoft/WinGet/Packages/*/ffmpeg*/bin/ffmpeg.exe'),
            str(home / 'scoop/apps/ffmpeg/current/bin/ffmpeg.exe'), r'C:\ProgramData\chocolatey\bin\ffmpeg.exe',
            r'C:\ffmpeg\bin\ffmpeg.exe', r'C:\Program Files\ffmpeg\bin\ffmpeg.exe', r'C:\Program Files\*\bin\ffmpeg.exe',
            '/usr/bin/ffmpeg', '/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg']
    for p in pats:
        c += sorted(glob.glob(p))
    seen, out = set(), []
    for x in c:
        if x and x not in seen and os.path.exists(x):
            seen.add(x)
            out.append(x)
    return out


@functools.lru_cache(maxsize=None)
def encoders_of(ff):
    r = subprocess.run([ff, '-hide_banner', '-encoders'], capture_output=True, text=True)
    return set(re.findall(r'^\s*V\S*\s+(\S+)', r.stdout, re.M))


def pick_encoder(codec):
    """First ffmpeg on the machine that has a usable encoder for the codec. Many bundled ffmpegs (e.g. TouchDesigner's) only ship vp9."""
    for ff in ffmpeg_candidates():
        have = encoders_of(ff)
        for enc in ENCODERS[codec]:
            if enc in have:
                return ff, enc
    return None, None


SPHERICAL_XML = (b'<?xml version="1.0"?><rdf:SphericalVideo xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
                 b'xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/"><GSpherical:Spherical>true</GSpherical:Spherical>'
                 b'<GSpherical:Stitched>true</GSpherical:Stitched><GSpherical:StitchingSoftware>ue360</GSpherical:StitchingSoftware>'
                 b'<GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType></rdf:SphericalVideo>')
SPHERICAL_UUID = bytes.fromhex('ffcc8263f8554a938814587a02521fdd')


def inject_spherical(path):
    """Adds Google's spherical-video v1 metadata (a uuid box inside the video trak) so YouTube / VLC / headset players treat the mp4 as
    360 video. Needs moov AFTER mdat (ffmpeg's default; do not use +faststart) so no chunk offsets move. ffmpeg reports the result as
    'spherical: equirectangular'."""
    b = bytearray(open(path, 'rb').read())

    def boxes(lo, hi):
        p = lo
        while p + 8 <= hi:
            size, typ = struct.unpack('>I4s', b[p:p + 8])
            size = hi - p if size == 0 else size
            yield p, size, typ.decode('latin1')
            p += size

    top = {t: (p, s) for p, s, t in boxes(0, len(b))}
    moov_p, moov_s = top['moov']
    if moov_p < top['mdat'][0]:
        raise RuntimeError('moov is before mdat (+faststart): spherical metadata would shift chunk offsets')
    for tp, ts, tt in boxes(moov_p + 8, moov_p + moov_s):
        if tt != 'trak':
            continue
        video = any(ht == 'hdlr' and b[hp + 16:hp + 20] == b'vide' for mp, ms, mt in boxes(tp + 8, tp + ts) if mt == 'mdia'
                    for hp, hs, ht in boxes(mp + 8, mp + ms))
        if not video:
            continue
        payload = SPHERICAL_UUID + SPHERICAL_XML
        box = struct.pack('>I4s', 8 + len(payload), b'uuid') + payload
        b[tp + ts:tp + ts] = box
        b[tp:tp + 4] = struct.pack('>I', ts + len(box))
        b[moov_p:moov_p + 4] = struct.pack('>I', moov_s + len(box))
        open(path, 'wb').write(b)
        return True
    return False


# ================================================================================= commands
def cmd_doctor(args):
    say('python %s' % sys.version.split()[0])
    for mod in ('numpy', 'PIL'):
        try:
            __import__(mod)
            say('  %-6s ok' % mod)
        except ImportError:
            say('  %-6s MISSING (pip install numpy pillow)' % mod)
    free = free_ram_gb()
    if free:
        say('RAM: %.1f GB free of %.1f GB%s' % (free[0], free[1], '  <- tight: one Unreal process at a time' if free[1] < 20 else ''))
    procs = unreal_processes()
    say('UnrealEditor.exe running: %s' % (', '.join(procs) if procs else 'none'))
    ff = ffmpeg_candidates()
    say('ffmpeg binaries found: %d' % len(ff))
    for f in ff:
        have = [e for codec in ENCODERS.values() for e in codec if e in encoders_of(f)]
        say('  %s  -> encoders: %s' % (f, ', '.join(have) or 'none of h264/h265/vp9'))
    f, enc = pick_encoder('h264')
    say('h264 encoder to use: %s' % ('%s (%s)' % (enc, f) if enc else 'NONE - pip install imageio-ffmpeg, or set UE360_FFMPEG'))
    try:
        project = find_project(args)
    except SystemExit:
        say('(no project given: pass --project to check the engine)')
        return
    up = read_uproject(project)
    say('project: %s  EngineAssociation=%s' % (project, up.get('EngineAssociation')))
    plugins = {p['Name']: p.get('Enabled') for p in up.get('Plugins', [])}
    if not plugins.get('PythonScriptPlugin'):
        say('  WARNING: PythonScriptPlugin is not enabled in the .uproject (add {"Name":"PythonScriptPlugin","Enabled":true}; also EditorScriptingUtilities)')
    root = find_engine(project, args.engine)
    say('engine: %s' % root)
    say('  editor: %s' % editor_exe(root))
    say('  commandlet editor: %s' % editor_exe(root, cmd=True))
    say('session: %s' % (load_session(project) or 'none yet'))


def cmd_testscene(args):
    project = find_project(args)
    r = run_headless(args, project, 'ue360_testscene.py', {}, 'testscene')
    if r.get('dry_run'):
        return
    say('demo level ready: %s  (camera %s in %s)' % (r['map'], r['binding'], r['sequence']))
    say('next:\n  ue360.py patch-time --material %s\n  ue360.py setup --map %s --source-sequence %s --source-binding %s' % (
        r['post_material'], r['map'], r['sequence'], r['binding']))


def cmd_setup(args):
    project = find_project(args)
    cfg = {'map': args.map, 'folder': args.folder, 'label': args.label, 'cube_size': args.cube, 'sequence_name': 'LS_' + args.label,
           'source_sequence': args.source_sequence, 'source_binding': args.source_binding, 'source_actor': args.source_actor,
           'keep_tilt': args.keep_tilt, 'yaw': args.yaw, 'pitch': args.pitch, 'roll': args.roll}
    if args.location:
        cfg['location'] = [float(x) for x in args.location.split(',')]
    if bool(args.source_sequence) != bool(args.source_binding):
        die('--source-sequence and --source-binding go together')
    find_running_lock(project, args)
    r = run_headless(args, project, 'ue360_setup.py', cfg, 'setup')
    if r.get('dry_run'):
        return
    sess = load_session(project)
    sess.pop('axes', None)                      # a fresh rig must be re-calibrated
    (work_dir(project) / 'session.json').write_text(json.dumps(sess, indent=1))
    save_session(project, map=args.map, folder=args.folder, label=args.label, sequence=r.get('sequence'), fps=r.get('fps'),
                 start=r.get('start'), end=r.get('end'))
    say('rig %s ready%s' % (args.label, ' with sequence %s (%s-%s @ %.2f fps)' % (r['sequence'], r['start'], r['end'], r['fps']) if r.get('sequence') else ''))
    say('next: ue360.py calib --frame <a frame>   then   ue360.py stills --frames ...')


def find_running_lock(project, args):
    procs = unreal_processes()
    if procs:
        say('note: UnrealEditor.exe is running (%s). If it has the target map or sequence open, saving will fail with a sharing violation '
            '(Windows error 32) - close that editor first.' % ', '.join(procs))
    return bool(procs)


def cmd_patch_time(args):
    project = find_project(args)
    sess = load_session(project)
    cfg = {'materials': args.material, 'folder': args.folder or sess.get('folder', '/Game/UE360'), 'debug': bool(os.environ.get('UE360_DEBUG'))}
    if not args.no_backup and not args.dry_run:
        bk = work_dir(project) / 'backup'
        bk.mkdir(exist_ok=True)
        for m in args.material:
            f = project.parent / 'Content' / (m[len('/Game/'):].split('.')[0] + '.uasset')
            if f.exists():
                shutil.copy2(f, bk / f.name)
                say('backup: %s' % (bk / f.name))
    find_running_lock(project, args)
    r = run_headless(args, project, 'ue360_patch_time.py', cfg, 'patch_time')
    if r.get('dry_run'):
        return
    for m in r['materials']:
        say('  %s' % m)
    save_session(project, clock=r['mpc'])
    say('clock collection %s will drive patched materials during renders (UseOverride=0 outside renders: no behaviour change)' % r['mpc'])


def render_common(args, project):
    sess = load_session(project)
    if args.axes:
        save_session(project, axes=[float(x) for x in args.axes.split(',')])
    return sess


def post_render_checks(project, args, report, prefix=None):
    if report.get('dry_run'):
        return
    need_imaging()
    bad = check_dir(out_dir(project, args) / 'raw', report.get('written', []))
    if bad:
        say('WARNING: %d frame(s) are completely black: %s' % (len(bad), ', '.join(bad[:5])))
        say('  This almost always means M_Equirect360 failed to compile (see "Failed to compile Material" in the log) or the capture '
            'component has no render target. Re-run `ue360.py setup`, then retry.')
        sys.exit(3)


def check_dir(raw, names):
    import numpy as np
    bad = []
    for n in names:
        if n.startswith('cand_'):
            continue
        p = raw / n
        if p.exists() and float(read_hdr(p).max()) == 0.0:
            bad.append(n)
    return bad


def cmd_calib(args):
    project = find_project(args)
    render_common(args, project)
    args.width = 1024
    frames = [int(args.frame)] + ([int(x) for x in args.frames.split(',')] if args.frames else [])
    rep = run_render(args, project, {'frames': frames}, 'calib')
    if rep.get('dry_run'):
        return
    post_render_checks(project, args, rep)
    cmd_pick(args)


def cmd_stills(args):
    project = find_project(args)
    render_common(args, project)
    rep = run_render(args, project, {'frames': [int(x) for x in args.frames.split(',')]}, 'stills')
    post_render_checks(project, args, rep)
    if not rep.get('dry_run'):
        cmd_png(args)
        say('stills in %s' % out_dir(project, args))


def cmd_clip(args):
    project = find_project(args)
    render_common(args, project)
    rep = run_render(args, project, {'start': args.start, 'count': args.count, 'step': args.step}, 'clip')
    save_session(project, step=args.step)
    post_render_checks(project, args, rep)
    if not rep.get('dry_run'):
        cmd_png(args)
        sess = load_session(project)
        say('clip frames ready (%d). Encode with: ue360.py video NAME   (%d fps = real time)' % (args.count, round((sess.get('fps') or 30) / args.step)))


def cmd_pick(args):
    """Chooses the cubemap axis mapping from the calibration render. Identity is the known-correct mapping for UE 5.8 (verified on several
    projects), so another candidate must beat it clearly: a hidden sphere (typically 'down', buried under the ground) would otherwise let
    near-ties be decided by sort order."""
    need_imaging()
    project = find_project(args)
    raw = out_dir(project, args) / 'raw'
    cands = [[float(v) for v in l.split(',')] for l in (raw / 'candidates.txt').read_text().splitlines()]
    scored = []
    for i, m in enumerate(cands):
        found = marker_directions(read_hdr(raw / ('cand_%02d.hdr' % i)))
        scored.append((marker_score(found), i, found))
    ident_i = next(i for i, m in enumerate(cands) if m == [1, 0, 0, 0, 1, 0, 0, 0, 1])
    ident = next(t for t in scored if t[1] == ident_i)
    best = max(scored, key=lambda t: t[0])
    chosen = best if best[0] > ident[0] + 0.75 else ident
    for sc, i, _ in sorted(scored, key=lambda t: -t[0])[:3]:
        say('  cand %02d  score %.2f / %d  M=%s' % (i, sc, len(MARKERS), cands[i]))
    sc, ci, found = chosen
    errs = {n: (angdist(f[0], f[1], lon, lat) if f[0] is not None else None) for (n, f), (_, lon, lat, _) in zip(found.items(), MARKERS)}
    say('  sphere directions in the chosen candidate (lon/lat; expected 0/0, 90/0, 180/0, -90/0, 0/-90): ' + ', '.join(
        '%s=%s' % (n, ('%.0f/%.0f' % (f[0], f[1])) if f[0] is not None else 'NOT FOUND') for n, f in found.items()))
    good = [n for n, e in errs.items() if e is not None and e < 15.0]
    off = [n for n in errs if n not in good]
    if len(good) < 3:
        die('calibration inconclusive: only %d of %d spheres were found near their expected direction (%s). They were probably hidden by '
            'geometry or fog: calibrate at a frame with open space around the rig (--frame) and look at `ue360.py views`. Axes left unchanged.'
            % (len(good), len(MARKERS), ', '.join(n + ('=hidden' if errs[n] is None else '=%.0f deg off' % errs[n]) for n in off)), 4)
    if cands[ci] == [1, 0, 0, 0, 1, 0, 0, 0, 1]:
        say('axis mapping OK: identity; %d of %d spheres confirmed within 15 deg%s' % (
            len(good), len(MARKERS), '' if not off else ' (not confirmed: %s - usually hidden below the ground; other axes agree)' % ', '.join(off)))
    else:
        say('NOTE: mapping %s beats identity clearly (score %.2f vs %.2f) - using it for later renders. Check a still with `ue360.py views`.' % (
            cands[ci], sc, ident[0]))
    save_session(project, axes=cands[ci])


def cmd_png(args):
    need_imaging()
    from PIL import Image
    project = find_project(args)
    od = out_dir(project, args)
    n = 0
    for p in sorted((od / 'raw').glob('*.hdr')):
        if p.name.startswith('cand_'):
            continue
        folder = od / 'frames' if p.name.startswith('clip_') else od
        folder.mkdir(exist_ok=True)
        dst = folder / (p.stem + '.png')
        if dst.exists() and dst.stat().st_mtime >= p.stat().st_mtime:
            continue
        Image.fromarray(to_u8(read_hdr(p), args.gamma)).save(dst)
        n += 1
    say('converted %d image(s) -> %s' % (n, od))


def cmd_check(args):
    need_imaging()
    project = find_project(args)
    raw = out_dir(project, args) / 'raw'
    files = [p for p in sorted(raw.glob('*.hdr')) if not p.name.startswith('cand_')]
    bad = check_dir(raw, [p.name for p in files])
    say('%d frames checked, %d completely black%s' % (len(files), len(bad), (': ' + ', '.join(bad[:8])) if bad else ''))
    sys.exit(3 if bad else 0)


def cmd_views(args):
    need_imaging()
    import numpy as np
    from PIL import Image, ImageDraw
    project = find_project(args)
    od = out_dir(project, args)
    src = Path(args.name) if os.path.isabs(args.name) else od / args.name
    eq = np.asarray(Image.open(src).convert('RGB'), np.float32) / 255
    tiles = []
    for label, yaw, pitch in (('front', 0, 0), ('right', 90, 0), ('back', 180, 0), ('left', -90, 0), ('up', 0, 80), ('down', 0, -80)):
        im = Image.fromarray(to_u8(perspective(eq, yaw, pitch, 90, 320, 180), 0))
        ImageDraw.Draw(im).text((6, 4), label, fill=(255, 255, 0))
        tiles.append(im)
    hh = eq.shape[0] * 960 // eq.shape[1]
    sheet = Image.new('RGB', (960, 360 + hh))
    for k, im in enumerate(tiles):
        sheet.paste(im, ((k % 3) * 320, (k // 3) * 180))
    sheet.paste(Image.fromarray((np.clip(eq, 0, 1) * 255).astype(np.uint8)).resize((960, hh)), (0, 360))
    dst = od / ('views_' + src.name)
    sheet.save(dst)
    say('wrote ' + str(dst))


def cmd_sheet(args):
    need_imaging()
    from PIL import Image, ImageDraw
    project = find_project(args)
    od = out_dir(project, args)
    fs = sorted(od.glob('still_f*.png'))
    if not fs:
        die('no stills in ' + str(od))
    cols = 2
    rows = (len(fs) + cols - 1) // cols
    sheet = Image.new('RGB', (512 * cols, 256 * rows))
    for k, f in enumerate(fs):
        im = Image.open(f).convert('RGB').resize((512, 256))
        ImageDraw.Draw(im).text((6, 4), f.name, fill=(255, 255, 0))
        sheet.paste(im, ((k % cols) * 512, (k // cols) * 256))
    dst = od / 'stills_sheet.png'
    sheet.save(dst)
    say('wrote ' + str(dst))


def cmd_video(args):
    project = find_project(args)
    od = out_dir(project, args)
    ff, enc = pick_encoder(args.codec)
    if not ff:
        die('no ffmpeg with a %s encoder found. `pip install imageio-ffmpeg` (bundles a full build) or set UE360_FFMPEG=<path>. '
            'Run `ue360.py doctor` to see what was found.' % args.codec)
    sess = load_session(project)
    fps = args.fps or max(1, round((sess.get('fps') or 30) / (sess.get('step') or 2)))
    crf = args.crf if args.crf is not None else PRESETS[args.preset]
    ext = 'webm' if args.codec == 'vp9' else 'mp4'
    dst = od / (args.name + '.' + ext)
    frames = od / 'frames' / (args.prefix + '_%04d.png')
    if not (od / 'frames' / (args.prefix + '_0000.png')).exists():
        die('no frames at ' + str(frames))
    vcodec = {'libx264': ['-c:v', 'libx264', '-preset', 'slow', '-crf', str(crf)],
              'libx265': ['-c:v', 'libx265', '-preset', 'medium', '-crf', str(crf + 2), '-tag:v', 'hvc1'],
              'libvpx-vp9': ['-c:v', 'libvpx-vp9', '-crf', str(crf + 6), '-b:v', '0'],
              'libopenh264': ['-c:v', 'libopenh264', '-b:v', '8M'], 'h264_mf': ['-c:v', 'h264_mf', '-b:v', '8M'],
              'hevc_mf': ['-c:v', 'hevc_mf', '-b:v', '8M'], 'h264_nvenc': ['-c:v', 'h264_nvenc', '-cq', str(crf), '-preset', 'p5'],
              'hevc_nvenc': ['-c:v', 'hevc_nvenc', '-cq', str(crf), '-preset', 'p5', '-tag:v', 'hvc1']}[enc]
    vf = 'scale=%s,' % args.scale if args.scale else ''
    cmd = [ff, '-y', '-hide_banner', '-loglevel', 'error', '-framerate', str(fps), '-i', str(frames),
           '-vf', vf + 'scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p'] + vcodec + ['-movflags', '0', str(dst)]
    say('ffmpeg: %s  encoder: %s' % (ff, enc))
    if args.dry_run:
        say('DRY RUN:', ' '.join(cmd))
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        die('ffmpeg failed:\n' + r.stderr[-1500:])
    tagged = inject_spherical(str(dst)) if ext == 'mp4' else False
    say('encoded %s (%.1f MB), 360 metadata: %s' % (dst, dst.stat().st_size / 2 ** 20,
                                                   'injected (YouTube/VLC/headset players see equirectangular)' if tagged else
                                                   'not injected (webm: use the local viewer or a player that lets you pick equirect)'))


def cmd_view(args):
    project = find_project(args)
    od = out_dir(project, args)
    shutil.copy2(SKILL_DIR / 'viewer' / 'viewer.html', od / 'viewer.html')
    name = args.name
    if not name:
        media = sorted(list(od.glob('*.mp4')) + list(od.glob('*.webm')) + list(od.glob('still_f*.png')), key=lambda p: p.stat().st_mtime)
        if not media:
            die('nothing to show in ' + str(od))
        name = media[-1].name
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(od))
    handler.log_message = lambda *a, **k: None
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(('127.0.0.1', args.port), handler) as srv:
        url = 'http://127.0.0.1:%d/viewer.html?src=%s' % (srv.server_address[1], name)
        say('serving %s\nviewer: %s\n(drag = look, wheel = zoom, space = pause, A = auto-rotate, R = reset; Ctrl+C stops the server)' % (od, url))
        if not args.no_open:
            webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


def cmd_clean(args):
    project = find_project(args)
    od = out_dir(project, args)
    freed = 0
    targets = []
    if args.raw:
        targets += list((od / 'raw').glob('*.hdr'))
    if args.frames:
        targets += list((od / 'frames').glob('*.png'))
    for p in targets:
        freed += p.stat().st_size
        p.unlink()
    say('removed %d files, %.0f MB' % (len(targets), freed / 2 ** 20))


# ================================================================================= argparse
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p, render=False):
        p.add_argument('--project', help='path to the .uproject (or its folder); default: UE360_PROJECT or the .uproject above the cwd')
        p.add_argument('--engine', help='engine root (folder containing Engine/); default: auto-detected from EngineAssociation')
        p.add_argument('--out', help='output folder (default <project>/Saved/UE360/out)')
        p.add_argument('--dry-run', action='store_true', help='print the command lines instead of running Unreal')
        p.add_argument('--timeout', type=int, default=40, help='minutes before an Unreal run is killed')
        if render:
            p.add_argument('--width', type=int, default=1024, help='equirect width in px (height = width/2); cube faces are rendered at the size set by setup --cube')
            p.add_argument('--settle', type=int, default=8, help='editor ticks to wait after moving the rig before capturing (streaming)')
            p.add_argument('--repeat', type=int, default=2, help='scene captures per image (history converges: clouds, TAA, Lumen)')
            p.add_argument('--warmup', type=float, default=90, help='seconds to wait after launch (shader compile, Nanite streaming)')
            p.add_argument('--retries', type=int, default=2, help='relaunches after a hung/crashed editor')
            p.add_argument('--startup', type=int, default=6, help='minutes allowed for the editor to reach the script before a relaunch')
            p.add_argument('--force', action='store_true', help='run even when RAM is very low')
            p.add_argument('--axes', help='override the 9-number axis matrix (normally set by calib)')
            p.add_argument('--clock', choices=['on', 'off'], default='on', help='off = ignore the patched-material clock')
            p.add_argument('--fixed-step', action='store_true', help='EXPERIMENTAL (clip only): run the editor at a fixed time step so ALL world time advances steadily')
            p.add_argument('--gamma', type=float, default=GAMMA, help='display gamma for PNG output (0 = exact sRGB)')

    p = sub.add_parser('doctor'); common(p); p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser('testscene'); common(p); p.set_defaults(fn=cmd_testscene)
    p = sub.add_parser('setup'); common(p); p.set_defaults(fn=cmd_setup)
    p.add_argument('--map', required=True, type=unreal_path, help='level to add the rig to, e.g. /Game/Maps/MyLevel')
    p.add_argument('--label', default='Camera360')
    p.add_argument('--folder', default='/Game/UE360', type=unreal_path, help='content folder for the generated assets')
    p.add_argument('--cube', type=int, default=512, help='cube face resolution (512 is plenty for a 1024-2048 wide equirect)')
    p.add_argument('--source-sequence', type=unreal_path, help='level sequence holding the camera whose path the rig should follow')
    p.add_argument('--source-binding', help='display name of that camera in the sequence (e.g. FlightCamera)')
    p.add_argument('--source-actor', help='label of an actor whose current transform the rig should copy (static)')
    p.add_argument('--location', help='x,y,z for a fixed rig'); p.add_argument('--yaw', type=float, default=0.0)
    p.add_argument('--pitch', type=float, default=0.0); p.add_argument('--roll', type=float, default=0.0)
    p.add_argument('--keep-tilt', action='store_true', help='keep the source camera pitch/roll (default: level horizon, yaw only)')
    p = sub.add_parser('patch-time'); common(p); p.set_defaults(fn=cmd_patch_time)
    p.add_argument('--material', action='append', required=True, type=unreal_path, help='material asset path; repeat for several (post-process, mesh, cloud ...)')
    p.add_argument('--folder', type=unreal_path); p.add_argument('--no-backup', action='store_true')
    p = sub.add_parser('calib'); common(p, True); p.set_defaults(fn=cmd_calib)
    p.add_argument('--frame', type=int, default=0, help='sequence frame to calibrate at'); p.add_argument('--frames', help='extra stills to render in the same launch')
    p = sub.add_parser('stills'); common(p, True); p.set_defaults(fn=cmd_stills)
    p.add_argument('--frames', required=True, help='comma separated sequence frames (use 0 for a static rig)')
    p = sub.add_parser('clip'); common(p, True); p.set_defaults(fn=cmd_clip)
    p.add_argument('--start', type=int, default=0); p.add_argument('--count', type=int, default=120)
    p.add_argument('--step', type=int, default=2, help='sequence frames per output frame (30 fps source, step 2 -> 15 fps real time)')
    for name, fn in (('png', cmd_png), ('check', cmd_check), ('sheet', cmd_sheet), ('clean', cmd_clean), ('pick', cmd_pick)):
        p = sub.add_parser(name); common(p); p.set_defaults(fn=fn)
        if name == 'png':
            p.add_argument('--gamma', type=float, default=GAMMA)
        if name == 'clean':
            p.add_argument('--raw', action='store_true', help='delete raw .hdr files'); p.add_argument('--frames', action='store_true', help='delete clip PNG frames')
    p = sub.add_parser('views'); common(p); p.set_defaults(fn=cmd_views); p.add_argument('name', help='equirect png in the output folder')
    p = sub.add_parser('video'); common(p); p.set_defaults(fn=cmd_video); p.add_argument('name')
    p.add_argument('--fps', type=int, help='default: sequence fps / the clip step (15 for a 30 fps sequence at step 2)'); p.add_argument('--prefix', default='clip')
    p.add_argument('--codec', choices=list(ENCODERS), default='h264'); p.add_argument('--preset', choices=list(PRESETS), default='demo')
    p.add_argument('--crf', type=int); p.add_argument('--scale', help='e.g. 2048:1024')
    p = sub.add_parser('view'); common(p); p.set_defaults(fn=cmd_view)
    p.add_argument('name', nargs='?'); p.add_argument('--port', type=int, default=8360); p.add_argument('--no-open', action='store_true')

    args = ap.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
