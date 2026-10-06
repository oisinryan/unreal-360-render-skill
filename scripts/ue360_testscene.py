"""ue360_testscene.py - builds a tiny self-contained demo level so the whole 360 pipeline can be tried (and verified) before touching a
real level. Runs INSIDE Unreal, headless is fine. Use `ue360.py testscene`.

  /Game/UE360Demo/Map_Demo     ground, a ring of 12 pillars coloured by compass direction (hue = azimuth), sun + sky,
                               and an unbound post-process volume running M_Demo_TimePost (moving dark stripes driven by the
                               material Time node - the thing ue360_patch_time.py makes steady)
  /Game/UE360Demo/LS_Demo      sequence with DemoCamera flying a straight line through the ring (240 frames @ 30 fps)
Source camera binding for `ue360.py setup`:  --source-sequence /Game/UE360Demo/LS_Demo --source-binding DemoCamera
"""
import colorsys
import json
import math
import os
import traceback

import unreal

CFG = json.load(open(os.environ['UE360_CONFIG']))
FOLDER = '/Game/UE360Demo'
atools = unreal.AssetToolsHelpers.get_asset_tools()
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
mel = unreal.MaterialEditingLibrary
EAL = unreal.EditorAssetLibrary
V = unreal.Vector


def log(m):
    unreal.log('UE360 ' + m)


def fresh(path, name, cls, factory):
    if EAL.does_asset_exist(path):
        EAL.delete_asset(path)
    return atools.create_asset(name, FOLDER, cls, factory)


def mat_color():
    mat = fresh(FOLDER + '/M_Demo_Color', 'M_Demo_Color', unreal.Material, unreal.MaterialFactoryNew())
    p = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -300, 0)
    p.set_editor_property('parameter_name', 'Color')
    p.set_editor_property('default_value', unreal.LinearColor(0.5, 0.5, 0.5, 1))
    mel.connect_material_property(p, '', unreal.MaterialProperty.MP_BASE_COLOR)
    mel.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return mat


def mat_time_post():
    """Post-process: darkens scrolling vertical stripes. Stripe position = f(screen x, Time) - steady only if Time is steady."""
    mat = fresh(FOLDER + '/M_Demo_TimePost', 'M_Demo_TimePost', unreal.Material, unreal.MaterialFactoryNew())
    mat.set_editor_property('material_domain', unreal.MaterialDomain.MD_POST_PROCESS)
    mat.set_editor_property('blendable_location', unreal.BlendableLocation.BL_SCENE_COLOR_AFTER_TONEMAPPING)
    n = [0]

    def node(cls):
        n[0] += 1
        return mel.create_material_expression(mat, cls, -1200 + n[0] * 80, (n[0] % 7) * 120)

    def op(cls, a, b):
        x = node(cls)
        for v, pin in ((a, 'A'), (b, 'B')):
            if isinstance(v, (int, float)):
                c = node(unreal.MaterialExpressionConstant)
                c.set_editor_property('r', float(v))
                v = c
            mel.connect_material_expressions(v, '', x, pin)
        return x

    def mask(src, out, r=False, g=False, b=False):
        # ComponentMask defaults to R+G: always set all four channels explicitly (a stray G turns float1 into float2)
        m = node(unreal.MaterialExpressionComponentMask)
        for k, v in (('r', r), ('g', g), ('b', b), ('a', False)):
            m.set_editor_property(k, v)
        mel.connect_material_expressions(src, out, m, '')
        return m

    scr = node(unreal.MaterialExpressionScreenPosition)
    ux = mask(scr, 'ViewportUV', r=True)
    t = node(unreal.MaterialExpressionTime)
    phase = op(unreal.MaterialExpressionAdd, op(unreal.MaterialExpressionMultiply, ux, 6.0), op(unreal.MaterialExpressionMultiply, t, 0.5))
    wave = node(unreal.MaterialExpressionSine)
    wave.set_editor_property('period', 1.0)
    mel.connect_material_expressions(phase, '', wave, '')
    stripe = node(unreal.MaterialExpressionSaturate)
    mel.connect_material_expressions(op(unreal.MaterialExpressionMultiply, wave, 6.0), '', stripe, '')
    dark = op(unreal.MaterialExpressionSubtract, 1.0, op(unreal.MaterialExpressionMultiply, stripe, 0.55))
    sc = node(unreal.MaterialExpressionSceneTexture)
    sc.set_editor_property('scene_texture_id', unreal.SceneTextureId.PPI_POST_PROCESS_INPUT0)
    mel.connect_material_expressions(scr, 'ViewportUV', sc, 'Coordinates')
    rgb = mask(sc, '', r=True, g=True, b=True)
    mel.connect_material_property(op(unreal.MaterialExpressionMultiply, rgb, dark), '', unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    mel.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return mat


def colored(parent, name, rgb):
    """Saved material instance (a runtime MID would not survive save_map)."""
    path = '%s/%s' % (FOLDER, name)
    if EAL.does_asset_exist(path):
        EAL.delete_asset(path)
    mic = atools.create_asset(name, FOLDER, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    mel.set_material_instance_parent(mic, parent)
    mel.set_material_instance_vector_parameter_value(mic, 'Color', unreal.LinearColor(rgb[0], rgb[1], rgb[2], 1.0))
    mel.update_material_instance(mic)
    EAL.save_loaded_asset(mic)
    return mic


def spawn_mesh(mesh, loc, scale, label, mid=None):
    a = eas.spawn_actor_from_class(unreal.StaticMeshActor, V(*loc), unreal.Rotator(0, 0, 0))
    a.static_mesh_component.set_static_mesh(unreal.load_asset(mesh))
    a.set_actor_scale3d(V(*scale))
    a.set_actor_label(label)
    if mid:
        a.static_mesh_component.set_material(0, mid)
    return a


def build_map(m_color, m_post):
    path = FOLDER + '/Map_Demo'
    if EAL.does_asset_exist(path):
        EAL.delete_asset(path)
    if not les.new_level(path):
        raise RuntimeError('new_level failed')
    world = ues.get_editor_world()
    spawn_mesh('/Engine/BasicShapes/Cube', (0, 0, -50), (400, 400, 1), 'Ground', colored(m_color, 'MI_Demo_Ground', (0.35, 0.37, 0.4)))   # cube: has collision
    for i in range(12):                                             # hue = azimuth; pillar 0 (red) stands on +X
        az = math.radians(i * 30.0)
        h = 300 + 160 * (i % 3)
        mid = colored(m_color, 'MI_Demo_Pillar_%02d' % i, colorsys.hsv_to_rgb(i / 12.0, 0.9, 0.9))
        spawn_mesh('/Engine/BasicShapes/Cube', (1500 * math.cos(az), 1500 * math.sin(az), h / 2.0), (1.2, 1.2, h / 100.0), 'Pillar_%02d' % i, mid)
    sun = eas.spawn_actor_from_class(unreal.DirectionalLight, V(0, 0, 500), unreal.Rotator(roll=0, pitch=-38, yaw=35))
    sun.set_actor_label('Sun')
    eas.spawn_actor_from_class(unreal.SkyAtmosphere, V(0, 0, 0), unreal.Rotator(0, 0, 0)).set_actor_label('SkyAtmosphere')
    sky = eas.spawn_actor_from_class(unreal.SkyLight, V(0, 0, 300), unreal.Rotator(0, 0, 0))
    sky.set_actor_label('SkyLight')
    sky.get_editor_property('light_component').set_editor_property('real_time_capture', True)
    ppv = eas.spawn_actor_from_class(unreal.PostProcessVolume, V(0, 0, 0), unreal.Rotator(0, 0, 0))
    ppv.set_actor_label('Demo_PostProcess')
    ppv.set_editor_property('unbound', True)
    s = ppv.get_editor_property('settings')
    wb = s.get_editor_property('weighted_blendables')
    wb.set_editor_property('array', [unreal.WeightedBlendable(weight=1.0, object=m_post)])
    s.set_editor_property('weighted_blendables', wb)
    ppv.set_editor_property('settings', s)
    return path, world


def build_sequence():
    frames, fps = 240, 30
    cam = eas.spawn_actor_from_class(unreal.CineCameraActor, V(-900, -300, 300), unreal.Rotator(0, 0, 0))
    cam.set_actor_label('DemoCamera')
    path = FOLDER + '/LS_Demo'
    if EAL.does_asset_exist(path):
        EAL.delete_asset(path)
    seq = atools.create_asset('LS_Demo', FOLDER, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    seq.set_display_rate(unreal.FrameRate(fps, 1))
    seq.set_playback_start(0)
    seq.set_playback_end(frames)
    b = seq.add_possessable(cam)
    sec = b.add_track(unreal.MovieScene3DTransformTrack).add_section()
    sec.set_range(0, frames)
    ch = sec.get_all_channels()
    yaw = math.degrees(math.atan2(600.0, 1800.0))
    for f in range(0, frames + 1, 40):
        t = f / float(frames)
        vals = [-900 + 1800 * t, -300 + 600 * t, 300 + 60 * math.sin(t * math.pi * 2), 0.0, -4.0, yaw]
        for i, v in enumerate(vals):
            ch[i].add_key(unreal.FrameNumber(f), float(v))
    for i in (6, 7, 8):
        ch[i].add_key(unreal.FrameNumber(0), 1.0)
    EAL.save_loaded_asset(seq)
    return path


def main():
    result = {'ok': False}
    try:
        EAL.make_directory(FOLDER)
        m_color, m_post = mat_color(), mat_time_post()
        map_path, world = build_map(m_color, m_post)
        seq_path = build_sequence()
        ok = unreal.EditorLoadingAndSavingUtils.save_map(world, map_path)
        result.update(ok=bool(ok), map=map_path, sequence=seq_path, binding='DemoCamera', post_material=FOLDER + '/M_Demo_TimePost')
        log('test scene saved: %s' % result)
    except Exception:
        result['error'] = traceback.format_exc()
        unreal.log_warning('UE360 TESTSCENE FAILED\n%s' % result['error'])
    json.dump(result, open(CFG['result_path'], 'w'), indent=1)
    unreal.log('UE360_TESTSCENE_DONE ok=%s' % result['ok'])


main()
