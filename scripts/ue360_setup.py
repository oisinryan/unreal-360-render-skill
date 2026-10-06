"""ue360_setup.py - builds the 360 camera rig inside a level. Runs INSIDE Unreal (headless is fine:
UnrealEditor-Cmd -run=pythonscript -nullrhi). Driven by the JSON file named in env UE360_CONFIG; use `ue360.py setup`.

Creates (all under cfg.folder, default /Game/UE360):
  RT_Cube360       cube render target the capture component draws into (float RGBA, 'hdr' on)
  M_Equirect360    unlit material: cubemap -> equirectangular lat/long image (RowX/RowY/RowZ = world->cubemap axis matrix)
  M_Marker360      bright unlit colour used by the calibration spheres
  LS_Camera360     (only when a source camera track is given) level sequence with the rig's own transform track
and a SceneCaptureCube actor (cfg.label) in cfg.map. The actor never renders by itself - ue360_capture.py triggers it.

Where the rig goes (first match wins):
  source_sequence + source_binding : copy that camera's location/yaw keys (pitch/roll zeroed unless keep_tilt) into LS_Camera360
  source_actor                      : copy that actor's current transform
  location [+ yaw/pitch/roll]       : fixed spot
"""
import json
import os
import traceback

import unreal

CFG = json.load(open(os.environ['UE360_CONFIG']))
FOLDER = CFG.get('folder', '/Game/UE360')
LABEL = CFG.get('label', 'Camera360')
SEQ_NAME = CFG.get('sequence_name', 'LS_Camera360')
CUBE = int(CFG.get('cube_size', 512))

atools = unreal.AssetToolsHelpers.get_asset_tools()
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
mel = unreal.MaterialEditingLibrary
EAL = unreal.EditorAssetLibrary


def log(m):
    unreal.log('UE360 ' + m)


def warn(m):
    unreal.log_warning('UE360 ' + m)


def get_or_create(name, cls, factory):
    path = '%s/%s' % (FOLDER, name)
    if EAL.does_asset_exist(path):
        return unreal.load_asset(path)
    return atools.create_asset(name, FOLDER, cls, factory)


def setp(obj, **props):
    for k, v in props.items():
        try:
            obj.set_editor_property(k, v)
        except Exception as e:
            warn('set %s failed: %s' % (k, e))


# ----------------------------------------------------------------------------- assets
def make_cube_rt():
    rt = get_or_create('RT_Cube360', unreal.TextureRenderTargetCube, unreal.TextureRenderTargetCubeFactoryNew())
    setp(rt, size_x=CUBE, hdr=True)          # cube RTs have no format enum; 'hdr' selects float RGBA
    EAL.save_loaded_asset(rt)
    log('cube RT %s px/face' % CUBE)
    return rt


class Graph:
    """Tiny helper over MaterialEditingLibrary so the node code below stays readable."""

    def __init__(self, mat):
        self.mat, self.n = mat, 0

    def node(self, cls):
        self.n += 1
        return mel.create_material_expression(self.mat, cls, -1500 + self.n * 70, (self.n % 9) * 130)

    def const(self, v):
        n = self.node(unreal.MaterialExpressionConstant)
        n.set_editor_property('r', float(v))
        return n

    def op(self, cls, a, b):
        n = self.node(cls)
        mel.connect_material_expressions(a if not isinstance(a, (int, float)) else self.const(a), '', n, 'A')
        mel.connect_material_expressions(b if not isinstance(b, (int, float)) else self.const(b), '', n, 'B')
        return n

    def mul(self, a, b):
        return self.op(unreal.MaterialExpressionMultiply, a, b)

    def sub(self, a, b):
        return self.op(unreal.MaterialExpressionSubtract, a, b)

    def mask(self, a, **k):
        n = self.node(unreal.MaterialExpressionComponentMask)
        setp(n, r=k.get('r', False), g=k.get('g', False), b=k.get('b', False), a=k.get('a', False))
        mel.connect_material_expressions(a, '', n, '')
        return n

    def trig(self, cls, a):
        n = self.node(cls)
        n.set_editor_property('period', 1.0)            # input measured in turns: 1.0 = 360 degrees
        mel.connect_material_expressions(a, '', n, '')
        return n

    def append(self, a, b):
        n = self.node(unreal.MaterialExpressionAppendVector)
        mel.connect_material_expressions(a, '', n, 'A')
        mel.connect_material_expressions(b, '', n, 'B')
        return n

    def vparam(self, name, rgb):
        n = self.node(unreal.MaterialExpressionVectorParameter)
        setp(n, parameter_name=name, default_value=unreal.LinearColor(rgb[0], rgb[1], rgb[2], 0.0))
        return n


def fresh_material(name):
    mat = get_or_create(name, unreal.Material, unreal.MaterialFactoryNew())
    try:
        mel.delete_all_material_expressions(mat)
    except Exception as e:
        warn('clear %s: %s' % (name, e))
    setp(mat, shading_model=unreal.MaterialShadingModel.MSM_UNLIT)
    return mat


def make_equirect_material(cube_rt):
    """Output pixel (u,v) -> direction (lon=(u-.5)*360, lat=(.5-v)*180) -> M * dir -> cubemap sample.
    Row params are overwritten per frame by ue360_capture.py (rig orientation); identity = rig looks down +X."""
    mat = fresh_material('M_Equirect360')
    g = Graph(mat)
    uv = g.node(unreal.MaterialExpressionTextureCoordinate)
    u, v = g.mask(uv, r=True), g.mask(uv, g=True)
    lon = g.sub(u, 0.5)                                  # turns, 0 = straight ahead, + = to the right
    lat = g.mul(g.sub(0.5, v), 0.5)                      # turns, +0.25 up .. -0.25 down
    cos_lat, sin_lat = g.trig(unreal.MaterialExpressionCosine, lat), g.trig(unreal.MaterialExpressionSine, lat)
    cos_lon, sin_lon = g.trig(unreal.MaterialExpressionCosine, lon), g.trig(unreal.MaterialExpressionSine, lon)
    d = g.append(g.append(g.mul(cos_lat, cos_lon), g.mul(cos_lat, sin_lon)), sin_lat)        # X fwd, Y right, Z up
    rows = []
    for name, rgb in (('RowX', (1, 0, 0)), ('RowY', (0, 1, 0)), ('RowZ', (0, 0, 1))):
        p = g.mask(g.vparam(name, rgb), r=True, g=True, b=True)
        rows.append(g.op(unreal.MaterialExpressionDotProduct, p, d))
    cube = g.node(unreal.MaterialExpressionTextureSampleParameterCube)
    setp(cube, parameter_name='CubeTex', texture=cube_rt)
    # COLOR is required: LINEAR_COLOR makes the material fail to compile ("Sampler type is Linear Color, should be Color")
    # and DrawMaterialToRenderTarget then silently draws nothing (all-black output).
    setp(cube, sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_COLOR)
    ok = False
    for pin in ('Coordinates', 'UVs'):
        ok = mel.connect_material_expressions(g.append(g.append(rows[0], rows[1]), rows[2]), '', cube, pin) or ok
    mel.connect_material_property(cube, 'RGB', unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    log('M_Equirect360 built (cubemap pin connected: %s)' % ok)
    return mat


def make_marker_material():
    """Calibration spheres: bright unlit colour. (Opaque on purpose: a translucent, depth-test-off version was not drawn by the cube
    capture at all.)"""
    mat = fresh_material('M_Marker360')
    g = Graph(mat)
    mel.connect_material_property(g.mul(g.vparam('Color', (1, 1, 1)), 4.0), '', unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return mat


# ----------------------------------------------------------------------------- rig
def read_source_keys(seq_path, binding_name):
    seq = unreal.load_asset(seq_path)
    if seq is None:
        raise RuntimeError('source sequence not found: ' + seq_path)
    names = [str(b.get_display_name()) for b in seq.get_bindings()]
    binding = next((b for b in seq.get_bindings() if str(b.get_display_name()) == binding_name), None)
    if binding is None:
        raise RuntimeError('binding %r not in %s; available: %s' % (binding_name, seq_path, names))
    track = next((t for t in binding.get_tracks() if isinstance(t, unreal.MovieScene3DTransformTrack)), None)
    if track is None or not track.get_sections():
        raise RuntimeError('binding %r has no transform track with a section' % binding_name)
    ch = track.get_sections()[0].get_all_channels()          # loc x y z, rot roll pitch yaw, scale x y z
    def frame_of(t):                                         # get_time() returns a FrameTime (frame_number + sub_frame)
        return int(getattr(t, 'frame_number', t).value)
    series = [[(frame_of(k.get_time()), float(k.get_value())) for k in c.get_keys()] for c in ch[:6]]
    return seq, series


def key_series(channel, series, constant):
    if series:
        for t, v in series:
            channel.add_key(unreal.FrameNumber(t), v)
    else:
        channel.add_key(unreal.FrameNumber(0), constant)


def add_rig(cube_rt):
    world_path = CFG['map']
    if not les.load_level(world_path):
        raise RuntimeError('could not load map ' + world_path)
    for a in eas.get_all_level_actors():
        if a.get_actor_label() == LABEL:
            eas.destroy_actor(a)
            log('removed old %s' % LABEL)
    rig = eas.spawn_actor_from_class(unreal.SceneCaptureCube, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    rig.set_actor_label(LABEL)
    rig.set_folder_path('/UE360')
    cc = rig.get_editor_property('capture_component_cube')
    # always_persist_rendering_state keeps the view history between captures (cloud / TAA / Lumen converge across repeats)
    setp(cc, texture_target=cube_rt, capture_source=unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR,
         capture_every_frame=False, capture_on_movement=False, always_persist_rendering_state=True)

    info = {'rig_label': LABEL, 'sequence': None, 'fps': None, 'start': 0, 'end': 0, 'cube_rt': cube_rt.get_path_name()}
    if CFG.get('source_sequence'):
        src, s = read_source_keys(CFG['source_sequence'], CFG['source_binding'])
        rate = src.get_display_rate()
        fps = float(rate.numerator) / float(rate.denominator)
        start, end = int(src.get_playback_start()), int(src.get_playback_end())
        EAL.make_directory(FOLDER)
        path = '%s/%s' % (FOLDER, SEQ_NAME)
        if EAL.does_asset_exist(path):
            EAL.delete_asset(path)
        seq = atools.create_asset(SEQ_NAME, FOLDER, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
        seq.set_display_rate(rate)
        seq.set_playback_start(start)
        seq.set_playback_end(end)
        b = seq.add_possessable(rig)
        sec = b.add_track(unreal.MovieScene3DTransformTrack).add_section()
        sec.set_range(start, end)
        ch = sec.get_all_channels()
        keep = bool(CFG.get('keep_tilt'))
        for i in range(3):
            key_series(ch[i], s[i], 0.0)
        key_series(ch[3], s[3] if keep else [], 0.0)             # roll
        key_series(ch[4], s[4] if keep else [], 0.0)             # pitch
        key_series(ch[5], s[5], 0.0)                             # yaw (kept: panorama centre follows travel direction)
        for i in (6, 7, 8):
            ch[i].add_key(unreal.FrameNumber(0), 1.0)
        first = [series[0][1] if series else 0.0 for series in s]
        rig.set_actor_location_and_rotation(unreal.Vector(first[0], first[1], first[2]),
                                            unreal.Rotator(roll=first[3] if keep else 0.0, pitch=first[4] if keep else 0.0, yaw=first[5]),
                                            False, False)
        EAL.save_loaded_asset(seq)
        info.update(sequence=path, fps=fps, start=start, end=end)
        log('sequence %s: %d keys on X, range %d-%d @ %.3f fps' % (path, len(s[0]), start, end, fps))
    elif CFG.get('source_actor'):
        src = next((a for a in eas.get_all_level_actors() if a.get_actor_label() == CFG['source_actor']), None)
        if src is None:
            raise RuntimeError('source actor %r not found' % CFG['source_actor'])
        rig.set_actor_location_and_rotation(src.get_actor_location(), src.get_actor_rotation(), False, False)
    else:
        loc = CFG.get('location') or [0, 0, 200]
        rig.set_actor_location_and_rotation(unreal.Vector(*loc), unreal.Rotator(roll=CFG.get('roll', 0.0), pitch=CFG.get('pitch', 0.0),
                                                                                  yaw=CFG.get('yaw', 0.0)), False, False)
    if info['fps'] is None:
        info['fps'] = 30.0
    ok = unreal.EditorLoadingAndSavingUtils.save_map(ues.get_editor_world(), world_path)
    log('saved %s -> %s' % (world_path, ok))
    if not ok:
        raise RuntimeError('save_map failed (is the map open in another editor? a running editor locks its files)')
    return info


def main():
    result = {'ok': False}
    try:
        EAL.make_directory(FOLDER)
        rt = make_cube_rt()
        make_equirect_material(rt)
        make_marker_material()
        result.update(add_rig(rt))
        result['ok'] = True
    except Exception:
        warn('SETUP FAILED\n%s' % traceback.format_exc())
        result['error'] = traceback.format_exc()
    json.dump(result, open(CFG['result_path'], 'w'), indent=1)
    unreal.log('UE360_SETUP_DONE ok=%s' % result['ok'])


main()
