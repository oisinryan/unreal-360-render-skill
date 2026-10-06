"""ue360_patch_time.py - gives materials a controllable clock. Runs INSIDE Unreal (headless is fine). Use `ue360.py patch-time`.

Why: material `Time` runs on wall-clock. A 360 render scrubs the sequence frame by frame (an offscreen editor ticks ~30x faster or
slower than real time), so Time-driven effects - blizzard flakes, scrolling noise, water, wind, cloud scroll - jump randomly from
frame to frame instead of moving at a steady rate.

Fix: every Time node in each listed material is replaced by  Lerp(Time, MPC_UE360Clock.Time, MPC_UE360Clock.UseOverride).
With UseOverride = 0 (the asset default, i.e. normal play / editing) the material behaves exactly as before; ue360_capture.py sets
UseOverride = 1 and Time = sequence_frame / fps for the lifetime of the render session only (the MPC asset itself is never changed).
One collection drives every patched material (post-process, mesh, landscape, cloud ...) - no per-material instances to swap.

Limits: Time nodes inside Material Functions are not reached (patch the function's material instead), and Niagara / Chaos / cloth
keep their own simulation time.
"""
import json
import os
import traceback

import unreal

CFG = json.load(open(os.environ['UE360_CONFIG']))
FOLDER = CFG.get('folder', '/Game/UE360')
MPC_NAME = 'MPC_UE360Clock'
mel = unreal.MaterialEditingLibrary
EAL = unreal.EditorAssetLibrary


def log(m):
    unreal.log('UE360 ' + m)


def warn(m):
    unreal.log_warning('UE360 ' + m)


def dbg(m):
    if CFG.get('debug'):
        unreal.log('UE360 dbg ' + m)


def make_mpc():
    path = '%s/%s' % (FOLDER, MPC_NAME)
    if EAL.does_asset_exist(path):
        return unreal.load_asset(path)
    EAL.make_directory(FOLDER)
    mpc = unreal.AssetToolsHelpers.get_asset_tools().create_asset(MPC_NAME, FOLDER, unreal.MaterialParameterCollection,
                                                                  unreal.MaterialParameterCollectionFactoryNew())
    params = []
    for name in ('Time', 'UseOverride'):
        p = unreal.CollectionScalarParameter()
        p.set_editor_property('parameter_name', name)
        p.set_editor_property('default_value', 0.0)
        params.append(p)
    mpc.set_editor_property('scalar_parameters', params)
    EAL.save_loaded_asset(mpc)
    return mpc


def same(a, b):
    return a is not None and b is not None and a.get_path_name() == b.get_path_name()


# MP_MAX is a count sentinel, not a property: get_material_property_input_node(mat, MP_MAX) is a null dereference (hard engine crash).
MPROPS = [getattr(unreal.MaterialProperty, n) for n in dir(unreal.MaterialProperty)
          if n.startswith('MP_') and n not in ('MP_MAX', 'MP_LAST_CUSTOMIZED_U_VS') and 'CUSTOMIZED' not in n]


def patch(mat_path, mpc):
    mat = unreal.load_asset(mat_path)
    if mat is None:
        return {'material': mat_path, 'error': 'not found'}
    dbg('get_material_expressions')
    exprs = list(mel.get_material_expressions(mat))
    dbg('%d expressions: %s' % (len(exprs), [type(e).__name__ for e in exprs]))
    if any(isinstance(e, unreal.MaterialExpressionCollectionParameter) and same(e.get_editor_property('collection'), mpc) for e in exprs):
        return {'material': mat_path, 'skipped': 'already patched'}
    times = [e for e in exprs if isinstance(e, unreal.MaterialExpressionTime)]
    if not times:
        return {'material': mat_path, 'skipped': 'no Time nodes (Time inside a Material Function? patch the function\'s users)'}
    rewired, props = 0, []
    for t in times:
        x, y = int(t.get_editor_property('material_expression_editor_x')), int(t.get_editor_property('material_expression_editor_y'))
        dbg('create lerp at %d,%d' % (x, y))
        lerp = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, x + 180, y)
        for pname, pin, dy in (('Time', 'B', 80), ('UseOverride', 'Alpha', 140)):
            dbg('create collection param %s' % pname)
            c = mel.create_material_expression(mat, unreal.MaterialExpressionCollectionParameter, x + 20, y + dy)
            dbg('set collection')
            c.set_editor_property('collection', mpc)
            c.set_editor_property('parameter_name', pname)
            mel.connect_material_expressions(c, '', lerp, pin)
        mel.connect_material_expressions(t, '', lerp, 'A')
        # re-point everything that used the Time node (other than the new lerp) at the lerp
        for e in exprs:
            if e is t or same(e, t):
                continue
            dbg('inputs of %s' % type(e).__name__)
            names = list(mel.get_material_expression_input_names(e))
            ins = list(mel.get_inputs_for_material_expression(mat, e))
            dbg('  names=%s ins=%s' % (names, [type(i).__name__ if i else None for i in ins]))
            if len(ins) != len(names):
                continue
            for name, src in zip(names, ins):
                if same(src, t):
                    mel.connect_material_expressions(lerp, '', e, '' if name in ('None', '') else name)     # single unnamed input reports 'None'
                    rewired += 1
        for prop in MPROPS:                                    # Time wired straight into a material output
            dbg('property %s' % prop)
            try:
                if same(mel.get_material_property_input_node(mat, prop), t):
                    mel.connect_material_property(lerp, '', prop)
                    props.append(str(prop))
            except Exception:
                pass
    mel.recompile_material(mat)
    EAL.save_loaded_asset(mat)
    return {'material': mat_path, 'time_nodes': len(times), 'inputs_rewired': rewired, 'outputs_rewired': props}


def main():
    result = {'ok': False, 'materials': []}
    try:
        mpc = make_mpc()
        result['mpc'] = mpc.get_path_name().split('.')[0]
        for m in CFG['materials']:
            r = patch(m, mpc)
            log('patch %s' % r)
            result['materials'].append(r)
        result['ok'] = all('error' not in r for r in result['materials'])
    except Exception:
        warn('PATCH FAILED\n%s' % traceback.format_exc())
        result['error'] = traceback.format_exc()
    json.dump(result, open(CFG['result_path'], 'w'), indent=1)
    unreal.log('UE360_PATCH_DONE ok=%s' % result['ok'])


main()
