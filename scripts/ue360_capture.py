"""ue360_capture.py - renders equirectangular 360 images from the rig. Runs INSIDE a *rendering* editor (not -nullrhi):
  UnrealEditor.exe Project.uproject /Game/Map -ExecCmds="py ue360_capture.py" -RenderOffscreen -windowed ...
Use `ue360.py calib|stills|clip`, which writes the JSON config (env UE360_CONFIG), launches the editor, watches for stalls and
checks the output.  Output: Radiance .hdr files (RGBE) in cfg.raw_dir; ue360.py converts them.

Per output frame: scrub the rig's sequence to the frame -> wait `settle` ticks (Nanite / texture streaming, cloud history) ->
capture the cube `repeat` times -> yaw/pitch/roll-compensated equirect material -> draw into a 2D RGBA16F target -> export .hdr.

Modes   calib : spawns 6 coloured spheres around the rig and renders all 48 signed axis permutations (256x128); `ue360.py pick`
                scores them (identity should win). Optional extra stills when cfg.frames is set.
        stills: one image per frame in cfg.frames          clip: cfg.count images, cfg.step sequence frames apart from cfg.start
"""
import itertools
import json
import os
import time
import traceback

import unreal

CFG = json.load(open(os.environ['UE360_CONFIG']))
MODE = CFG['mode']
RAW = CFG['raw_dir']
W = int(CFG.get('width', 1024))
H = W // 2
FPS = float(CFG.get('fps') or 30.0)
AXES = [float(x) for x in CFG.get('axes') or [1, 0, 0, 0, 1, 0, 0, 0, 1]]
REPEAT = max(1, int(CFG.get('repeat', 2)))
SETTLE = int(CFG.get('settle', 8))
WARMUP = float(CFG.get('warmup', 90))
LABEL = CFG.get('label', 'Camera360')
FOLDER = CFG.get('folder', '/Game/UE360')
os.makedirs(RAW, exist_ok=True)


def log(m):
    unreal.log('UE360 ' + m)


ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
world = ues.get_editor_world()
rig = next((a for a in eas.get_all_level_actors() if a.get_actor_label() == LABEL), None)
if rig is None:
    raise RuntimeError('rig %r not found in the loaded level - run `ue360.py setup` first (and open the same map)' % LABEL)
cc = rig.get_editor_property('capture_component_cube')
seq = unreal.load_asset(CFG['sequence']) if CFG.get('sequence') else None
equirect = unreal.load_asset(FOLDER + '/M_Equirect360')
mid = unreal.MaterialLibrary.create_dynamic_material_instance(world, equirect)
clock = unreal.load_asset(CFG['clock']) if CFG.get('clock') else None
FMT = unreal.TextureRenderTargetFormat.RTF_RGBA16F
rt_main = unreal.RenderingLibrary.create_render_target2d(world, W, H, FMT)
rt_small = unreal.RenderingLibrary.create_render_target2d(world, 256, 128, FMT) if MODE == 'calib' else None
log('start mode=%s rig=%s seq=%s clock=%s W=%d fps=%s' % (MODE, rig.get_name(), seq.get_name() if seq else None,
                                                          clock.get_name() if clock else None, W, FPS))
if seq:
    try:
        unreal.LevelSequenceEditorBlueprintLibrary.open_level_sequence(seq)
    except Exception as e:
        log('open_level_sequence failed: %s' % e)
report = {'mode': MODE, 'written': [], 'errors': []}


# ----------------------------------------------------------------------------- orientation
def set_rows(m):
    for name, row in zip(('RowX', 'RowY', 'RowZ'), (m[0:3], m[3:6], m[6:9])):
        mid.set_vector_parameter_value(name, unreal.LinearColor(row[0], row[1], row[2], 0.0))


def with_rig_rotation(m):
    """The cube capture stays aligned to the WORLD axes whatever the rig's rotation, so rotate the lookup instead:
    C = M * R, R's columns = the rig's forward / right / up vectors in world space. Panorama centre = rig forward,
    horizon level relative to the rig's own up. Works for yaw, pitch and roll."""
    f, r, u = rig.get_actor_forward_vector(), rig.get_actor_right_vector(), rig.get_actor_up_vector()
    R = [f.x, r.x, u.x, f.y, r.y, u.y, f.z, r.z, u.z]
    return [sum(m[i * 3 + k] * R[k * 3 + j] for k in range(3)) for i in range(3) for j in range(3)]


def candidates():
    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            m = [0.0] * 9
            for i in range(3):
                m[i * 3 + perm[i]] = float(signs[i])
            out.append(m)
    out.sort(key=lambda m: m != [1, 0, 0, 0, 1, 0, 0, 0, 1])      # identity first
    return out


# ----------------------------------------------------------------------------- jobs
MARKERS = [('fwd', (1, 0, 0), (1.0, 0.0, 0.0)), ('right', (0, 1, 0), (0.0, 1.0, 0.0)), ('back', (-1, 0, 0), (0.0, 0.0, 1.0)),
           ('left', (0, -1, 0), (1.0, 1.0, 0.0)), ('up', (0, 0, 1), (1.0, 1.0, 1.0)), ('down', (0, 0, -1), (1.0, 0.0, 1.0))]


def free_distance(loc, v, limit):
    """Distance to the first blocking hit along v (so a sphere can sit in open air), or `limit` when nothing blocks / no collision."""
    try:
        end = unreal.Vector(loc.x + v[0] * limit, loc.y + v[1] * limit, loc.z + v[2] * limit)
        r = unreal.SystemLibrary.line_trace_single(world, loc, end, unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, [rig],
                                                   unreal.DrawDebugTrace.NONE, True)
        blocking, hit = (r[0], r[1]) if isinstance(r, tuple) else (r.get_editor_property('blocking_hit'), r)
        if blocking:
            return float(hit.get_editor_property('distance'))
    except Exception as e:
        log('trace failed (%s) - using the default marker distance' % e)
    return limit


def do_markers():
    """Six emissive spheres around the rig in its own frame (red ahead, green right, blue behind, yellow left, white up, magenta down) -
    ground truth for the axis mapping. Each sits halfway to the first collidable surface (or `marker_distance` out in open space),
    ~17 degrees wide. Meshes without collision (e.g. Nanite terrain built without it) are not seen by the trace: choose a calibration
    frame with open space around the rig. Runtime-only actors."""
    limit = float(CFG.get('marker_distance', 15000.0))
    mm = unreal.load_asset(FOLDER + '/M_Marker360')
    sphere = unreal.load_asset('/Engine/BasicShapes/Sphere')
    loc = rig.get_actor_location()
    ax = (rig.get_actor_forward_vector(), rig.get_actor_right_vector(), rig.get_actor_up_vector())
    for name, d, col in MARKERS:
        v = [sum(d[k] * (ax[k].x, ax[k].y, ax[k].z)[c] for k in range(3)) for c in range(3)]
        dist = max(100.0, min(limit, 0.5 * free_distance(loc, v, limit)))
        dia = dist * 0.3
        a = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(loc.x + v[0] * dist, loc.y + v[1] * dist, loc.z + v[2] * dist),
                                       unreal.Rotator(0, 0, 0))
        a.static_mesh_component.set_static_mesh(sphere)
        a.set_actor_scale3d(unreal.Vector(dia / 100.0, dia / 100.0, dia / 100.0))
        a.static_mesh_component.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
        a.set_actor_label('UE360Marker_' + name)
        m = unreal.MaterialLibrary.create_dynamic_material_instance(world, mm)
        m.set_vector_parameter_value('Color', unreal.LinearColor(col[0], col[1], col[2], 1.0))
        a.static_mesh_component.set_material(0, m)
        a.static_mesh_component.set_cast_shadow(False)
        log('marker %s at %.0f uu (diameter %.0f)' % (name, dist, dia))


def capture():
    for _ in range(REPEAT):
        cc.capture_scene()


def render_to(rt, filename):
    unreal.RenderingLibrary.draw_material_to_render_target(world, rt, mid)
    unreal.RenderingLibrary.export_render_target(world, rt, RAW, filename)
    path = os.path.join(RAW, filename)
    ok = os.path.exists(path) and os.path.getsize(path) > 0
    report['written' if ok else 'errors'].append(filename)
    if not ok:
        log('WARNING: %s was not written' % filename)


def run(job):
    kind = job['kind']
    if kind == 'markers':
        do_markers()
    elif kind == 'unmarkers':
        for a in eas.get_all_level_actors():
            if a.get_actor_label().startswith('UE360Marker_'):
                eas.destroy_actor(a)
        log('markers removed')
    elif kind == 'cand':
        capture()
        cands = candidates()
        for i, m in enumerate(cands):
            set_rows(with_rig_rotation(m))
            render_to(rt_small, 'cand_%02d.hdr' % i)
        open(os.path.join(RAW, 'candidates.txt'), 'w').write('\n'.join(','.join('%g' % v for v in m) for m in cands))
    elif kind == 'eq':
        set_rows(with_rig_rotation(AXES))
        capture()
        render_to(rt_main, job['name'] + '.hdr')


jobs = []
frames = [int(x) for x in CFG.get('frames') or []]
if MODE == 'calib':
    f0 = frames[0] if frames else int(CFG.get('start', 0))
    jobs += [dict(kind='markers', frame=f0, settle=20), dict(kind='cand', frame=f0, settle=60), dict(kind='unmarkers', frame=f0, settle=2)]
if MODE in ('calib', 'stills'):
    jobs += [dict(kind='eq', frame=f, name='still_f%05d' % f, settle=SETTLE + 20) for f in frames]
elif MODE == 'clip':
    start, count, step = int(CFG.get('start', 0)), int(CFG.get('count', 1)), int(CFG.get('step', 2))
    jobs += [dict(kind='eq', frame=start + i * step, name='clip_%04d' % i, settle=(SETTLE + 40 if i == 0 else SETTLE)) for i in range(count)]
log('%d jobs' % len(jobs))

state = {'t0': time.time(), 'idx': 0, 'phase': 0, 'wait': 0, 'done_at': None, 'quit': False, 'busy': False}


def finish():
    report['jobs'] = len(jobs)
    json.dump(report, open(CFG['result_path'], 'w'), indent=1)
    log('done, quitting (%d jobs, %d files, %d errors)' % (len(jobs), len(report['written']), len(report['errors'])))
    unreal.SystemLibrary.execute_console_command(None, 'QUIT_EDITOR')


def tick(dt):
    now = time.time()
    if now - state['t0'] < WARMUP or state['busy']:         # busy: screenshots/exports can pump Slate ticks re-entrantly
        return
    i = state['idx']
    if i >= len(jobs):
        if state['done_at'] is None:
            state['done_at'] = now
        elif now - state['done_at'] > 8 and not state['quit']:
            state['quit'] = True
            finish()
        return
    job = jobs[i]
    state['busy'] = True
    try:
        if state['phase'] == 0:
            if seq:
                unreal.LevelSequenceEditorBlueprintLibrary.set_current_time(job['frame'])
            if clock:                                        # steady material clock = sequence time (see ue360_patch_time.py)
                unreal.MaterialLibrary.set_scalar_parameter_value(world, clock, 'Time', job['frame'] / FPS)
                unreal.MaterialLibrary.set_scalar_parameter_value(world, clock, 'UseOverride', 1.0)
            t = rig.get_actor_transform()
            ues.set_level_viewport_camera_info(t.translation, rig.get_actor_rotation())      # keep streaming focused on the rig
            state['phase'], state['wait'] = 1, job['settle']
        elif state['wait'] > 0:
            state['wait'] -= 1
        else:
            run(job)
            if i % 10 == 0:
                log('job %d/%d (frame %s) done' % (i + 1, len(jobs), job['frame']))
            state['idx'], state['phase'] = i + 1, 0
    except Exception:
        report['errors'].append('job %s: %s' % (job, traceback.format_exc()))
        log('job failed: %s' % traceback.format_exc())
        state['idx'], state['phase'] = i + 1, 0
    finally:
        state['busy'] = False


unreal.register_slate_post_tick_callback(tick)
