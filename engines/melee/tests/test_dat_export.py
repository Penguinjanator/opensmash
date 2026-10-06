"""Decode the browser DAT export using the existing independent Python oracle."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import numpy as np
from opensmash_melee.archive import Archive
from opensmash_melee.gx import material, batches
from opensmash_melee.skeleton import joints
from test_pipeline import fixture, mesh_fixture, decode_vertices

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node is required for the browser exporter')
class DatExportTests(unittest.TestCase):
    def build(self, count=36, triangles=None, mutate=''):
        a = fixture()
        sk = joints(a, 'custom_joint')
        dobj = sk[0]['dobj']
        mat = material(a, mesh_fixture()['image'])
        a.pointer(dobj+8, mat)
        target = dict(mode='humanoid', templateColor=0, originalBounds=[0, 2],
                      jointOffsets=[j['offset'] for j in sk], dobj=dobj,
                      image=a.ptr(a.ptr(mat+8)+76))
        source = dict(n=count, triangleCount=len(triangles)//3 if triangles else count//3,
                      triangles=triangles or list(range(count)), uv=[0., 1.]*count, textureSize=4)
        fitted = dict(positions=[0., 0., 0., 1., 1., 0., 0., 2., 0.]*(count//3),
                      normals=[0., 0., 1.]*count,
                      joints=[0, 1, 0xffffffff, 0xffffffff]*count,
                      weights=[v for i in range(count) for v in ((i+1)/(count+1), 1-(i+1)/(count+1), 0., 0.)])
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder/'input.dat').write_bytes(a.serialize())
            (folder/'input.json').write_text(json.dumps(dict(source=source, target=target, fitted=fitted)))
            script = f"""
import fs from 'node:fs';
import {{buildStandardCostume}} from {json.dumps((ROOT/'runtime/fitting/costume.mjs').as_uri())};
const folder=process.argv[1],{{source,target,fitted}}=JSON.parse(fs.readFileSync(folder+'/input.json'));
const raw=fs.readFileSync(folder+'/input.dat');
{mutate}
const out=buildStandardCostume(raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength),source,target,fitted,new Uint8Array(64),0);
fs.writeFileSync(folder+'/output.dat',new Uint8Array(out));
"""
            result = subprocess.run(['node', '--input-type=module', '-e', script, tmp], capture_output=True, text=True)
            if mutate:
                self.assertNotEqual(result.returncode, 0)
                return result.stderr
            self.assertEqual(result.returncode, 0, result.stderr)
            output = Archive.read(folder/'output.dat')
        self.assertEqual(output.roots(), a.roots())
        self.assertEqual(joints(output, 'custom_joint'), sk)
        decoded, offsets = decode_vertices(output, output.ptr(dobj+12))
        self.assertEqual(len(decoded), source['triangleCount'])
        for vertex, index in zip((v for tri in decoded for v in tri), source['triangles']):
            np.testing.assert_array_equal(vertex[0], np.asarray(fitted['positions'][index*3:index*3+3], dtype=np.float32))
            np.testing.assert_array_equal(vertex[1], np.asarray(fitted['normals'][index*3:index*3+3], dtype=np.float32))
            np.testing.assert_array_equal(vertex[2], [0, 1])
            expected = [(target['jointOffsets'][j], float(np.float32(w))) for j, w in zip(fitted['joints'][index*4:index*4+4], fitted['weights'][index*4:index*4+4]) if w]
            self.assertEqual(vertex[3], expected)

    def test_matrix_palette_split_and_weight_roundtrip(self):
        self.build()

    def test_gx_vertex_count_limit_splits_display_lists(self):
        self.build(count=3, triangles=[0, 1, 2]*21846)

    def test_rejects_invalid_surface_weights_joints_and_slot(self):
        for change in ('source.triangles[0]=source.n;', 'fitted.weights[0]=NaN;',
                       'fitted.weights[0]=2;', 'fitted.joints[0]=999;',
                       'fitted.positions[0]=Infinity;', 'target.templateColor=1;'):
            with self.subTest(change=change):
                self.build(mutate=change)

    def test_rejects_polygon_chains_that_can_overflow_stock_hsd_stack(self):
        self.assertIn('too complex', self.build(count=3465, mutate='// Deliberately distinct weights.'))

    def test_real_neutral_roster_fixtures(self):
        base = ROOT/'build/native-fit/local'
        if not (base/'sources/alanturing.json').is_file() or not (ROOT/'build/native-fit/fit.wasm').is_file():
            self.skipTest('Prepare local source, target and WASM fixtures first')
        with tempfile.TemporaryDirectory() as tmp:
            script = f"""
import fs from 'node:fs';
import {{exportDat}} from {json.dumps((ROOT/'tools/export_dat.mjs').as_uri())};
const root=process.argv[1],out=process.argv[2];
for(const name of fs.readdirSync(root+'/build/native-fit/local/targets').filter(n=>n.endsWith('.json')))
  await exportDat(root,'alanturing',name.slice(0,-5),out+'/'+name.slice(0,-5),{{evidence:true}});
"""
            subprocess.run(['node', '--input-type=module', '-e', script, str(ROOT), tmp], check=True)
            for folder in Path(tmp).iterdir():
                with self.subTest(target=folder.name):
                    rig = json.loads((base/'targets'/f'{folder.name}.json').read_text())
                    target = dict(rig, **rig['layouts'][0])
                    original = Archive.read(base/'targets'/f'{folder.name}-0.dat')
                    archive = Archive.read(folder/rig['slots'][0]['filename'])
                    self.assertEqual(archive.roots(), original.roots())
                    self.assertEqual(joints(archive, rig['slots'][0]['symbol']), joints(original, rig['slots'][0]['symbol']))
                    expected = json.loads((folder/'expected.json').read_text())
                    decoded, _ = decode_vertices(archive, archive.ptr(target['dobj']+12))
                    slots = 5 if target['mode']=='round' else 4
                    flat = [v for triangle in decoded for v in triangle]
                    self.assertEqual(len(flat), len(expected['triangles']))
                    envelopes = [tuple((j, w) for j, w in zip(expected['joints'][i:i+slots], expected['weights'][i:i+slots]) if w)
                                 for i in range(0, len(expected['joints']), slots)]
                    optimized = list(batches(dict(envelopes=envelopes, triangles=np.asarray(expected['triangles']).reshape(-1, 3))))
                    self.assertLessEqual(len(optimized), 384)
                    ix = np.asarray([v for _, tris in optimized for tri in tris for v in tri])
                    for key, field, width in [('positions', 0, 3), ('normals', 1, 3), ('uv', 2, 2)]:
                        np.testing.assert_array_equal(np.asarray([v[field] for v in flat]), np.asarray(expected[key], dtype=np.float32).reshape(-1, width)[ix])
                    for vertex, index in zip(flat, ix):
                        actual = {}
                        for joint, weight in vertex[3]:
                            j = target['jointOffsets'].index(joint)
                            actual[j] = actual.get(j, 0)+weight
                        wanted = {j: w for j, w in zip(expected['joints'][index*slots:(index+1)*slots], expected['weights'][index*slots:(index+1)*slots]) if w}
                        self.assertEqual(actual, wanted)
                    image = target['image']
                    self.assertEqual(archive.data[archive.ptr(image):archive.ptr(image)+archive.unpack('H', image+4)[0]**2*4], (base/'sources/alanturing.rgba8').read_bytes())


if __name__ == '__main__':
    unittest.main()
