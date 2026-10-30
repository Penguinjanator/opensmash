"""V3 wire contracts and the stable character-runner CLI contract."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, TRIPO_API_KEY='test-key'):
        spec.loader.exec_module(module)
    return module
tripo = load('tripo_v3', ROOT / 'pipeline/tripo.py')
mesh = load('mesh_v3', ROOT / 'web-prototype/tools/generate_mesh.py')
runner = load('runner_v3', ROOT / 'pipeline/run_character.py')
batch = load('batch_v3', ROOT / 'scripts/batch_characters.py')

def capture(fn, args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(args)
    return json.loads(out.getvalue())

class CharacterV3Tests(unittest.TestCase):
    def test_generation_keeps_model_and_face_limit(self):
        with patch.object(tripo, 'http', return_value={'code': 0}) as send:
            capture(tripo.cmd_img3d, SimpleNamespace(image_token='file_123', faces=4000, model='v3.0-20250812'))
        send.assert_called_once_with(tripo.BASE + '/generation/image-to-model', {
            'input': 'file_123', 'face_limit': 4000, 'texture': True, 'model': 'v3.0-20250812'})

    def test_rig_pins_existing_skeleton(self):
        with patch.object(tripo, 'http', return_value={'code': 0}) as send:
            capture(tripo.cmd_rig, SimpleNamespace(task_id='old-task'))
        send.assert_called_once_with(tripo.BASE + '/animations/rig', {
            'input': 'old-task', 'model': 'v1.0-20240301', 'spec': 'tripo', 'out_format': 'glb'})

    def test_upload_normalizes_token_for_existing_callers(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'code':0,'data':{'file_token':'file_123'}}))
        with patch.object(tripo.subprocess, 'run', return_value=response) as run:
            result = capture(tripo.cmd_upload, SimpleNamespace(image='ref.png'))
        self.assertEqual(result['image_token'], 'file_123')
        self.assertIn(tripo.BASE + '/files', run.call_args.args[0])

    def test_upload_rejects_failed_transport_and_api_errors(self):
        for response in [SimpleNamespace(returncode=22, stderr='HTTP 403', stdout=''),
                         SimpleNamespace(returncode=0, stdout='{"code":2008,"message":"blocked"}'),
                         SimpleNamespace(returncode=0, stdout='{"code":0,"data":{}}')]:
            with self.subTest(response=response), patch.object(tripo.subprocess, 'run', return_value=response):
                with self.assertRaises(RuntimeError):
                    capture(tripo.cmd_upload, SimpleNamespace(image='ref.png'))

    def test_status_is_complete_json_with_credits_and_failure_detail(self):
        data = {'task_id':'old-task','status':'failed','credits_consumed':0,
                'error_code':2008,'error_message':'blocked', 'output':{'model_url':'https://cdn/'+'x'*1000}}
        with patch.object(tripo, 'http', return_value={'data':data}) as send:
            result = capture(tripo.cmd_status, SimpleNamespace(task_id='old-task'))
        send.assert_called_once_with(tripo.BASE + '/tasks/old-task')
        self.assertEqual(result['consumed_credit'], 0)
        self.assertEqual(result['error_message'], 'blocked')
        self.assertEqual(result['output'], data['output'])

    def test_download_uses_v3_url_without_api_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'model.glb'
            with patch.object(tripo, 'http', return_value={'data':{'output':{'model_url':'https://cdn/model.glb'}}}), \
                 patch.object(tripo.urllib.request, 'urlopen', return_value=io.BytesIO(b'glTF')) as download:
                capture(tripo.cmd_download, SimpleNamespace(task_id='task',out=str(path)))
            self.assertEqual(path.read_bytes(), b'glTF')
            self.assertIsNone(download.call_args.args[0].get_header('Authorization'))

    def test_runner_reads_normalized_upload_and_credit_status(self):
        self.assertEqual(runner.tripo_json('{"image_token":"file_123"}')["image_token"], 'file_123')
        with patch.object(tripo, 'http', return_value={'data': {
                'task_id':'saved-task','status':'success','credits_consumed':30,
                'output':{'model_url':'https://cdn/'+'x'*1000}}}):
            status = capture(tripo.cmd_status, SimpleNamespace(task_id='saved-task'))
        parsed = runner.tripo_json(json.dumps(status))
        self.assertEqual(parsed['consumed_credit'], 30)
        self.assertEqual(parsed['status'], 'success')

    def test_terminal_failures_use_batch_retry_budget(self):
        for status in ('failed', 'banned', 'expired', 'cancelled'):
            self.assertEqual(batch.classify('RuntimeError: img3d ' + status)[0], 'tripo')

    def test_balance_endpoint(self):
        with patch.object(tripo, 'http', return_value={'data':{'balance':12}}) as send:
            self.assertEqual(capture(tripo.cmd_balance, None)['balance'],12)
        send.assert_called_once_with(tripo.BASE + '/account/balance')

    def test_http_rejects_application_error(self):
        with patch.object(tripo.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"code":2008,"message":"blocked"}')):
            with self.assertRaisesRegex(RuntimeError, '2008.*blocked'):
                tripo.http(tripo.BASE + '/tasks/task')

class AssetV3Tests(unittest.TestCase):
    def setUp(self):
        self.args=SimpleNamespace(prompt='cabinet',textured=False,negative_prompt=None,
                                  target_polycount=4000,model_version='P1-20260311')
    def request(self, fn, *args):
        with patch.object(mesh, 'api_key', return_value='test-key'), \
             patch.object(mesh, 'request_json', return_value={'code':0,'data':{'task_id':'task'}}) as send:
            self.assertEqual(fn(self.args,*args),'task')
        return send.call_args

    def test_text_default_is_explicit(self):
        self.args.model_version=None
        call=self.request(mesh.create_tripo_task)
        self.assertEqual(call.args[0],mesh.TRIPO_BASE_URL+'/generation/text-to-model')
        self.assertEqual(call.kwargs['body']['model'],'v2.5-20250123')
        self.assertNotIn('type',call.kwargs['body'])

    def test_image_preserves_p1(self):
        call=self.request(mesh.create_tripo_image_task,'file_123')
        self.assertEqual(call.args[0],mesh.TRIPO_BASE_URL+'/generation/image-to-model')
        self.assertEqual(call.kwargs['body'], {'input':'file_123','model':'P1-20260311',
                                             'texture':False,'pbr':False,'face_limit':4000})

    def test_multiview_preserves_order(self):
        tokens=['front','left','back','right']
        call=self.request(mesh.create_tripo_multiview_task,tokens)
        self.assertEqual(call.args[0],mesh.TRIPO_BASE_URL+'/generation/multiview-to-model')
        self.assertEqual(call.kwargs['body']['inputs'],tokens)
        self.assertNotIn('files',call.kwargs['body'])

    def test_upload_v3_file_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ref.png';path.write_bytes(b'png')
            with patch.object(mesh,'api_key',return_value='test-key'), \
                 patch.object(mesh.urllib.request,'urlopen',return_value=io.BytesIO(b'{"code":0,"data":{"file_token":"file_123"}}')) as send:
                self.assertEqual(mesh.upload_tripo_image(path),'file_123')
            self.assertEqual(send.call_args.args[0].full_url,mesh.TRIPO_BASE_URL+'/files')

    def test_poll_existing_task(self):
        with patch.object(mesh,'api_key',return_value='test-key'), \
             patch.object(mesh,'request_json',return_value={'code':0,'data':{'status':'success'}}) as send:
            self.assertEqual(mesh.get_tripo_task('old-task')['status'],'success')
        self.assertEqual(send.call_args.args[0],mesh.TRIPO_BASE_URL+'/tasks/old-task')

    def test_download_v3_output(self):
        for textured in [True,False]:
            self.assertEqual(mesh.task_model_url('tripo',{'output':{'model_url':'https://cdn/model'}},textured),'https://cdn/model')

    def test_failed_task_reports_v3_error(self):
        with patch.object(mesh,'get_tripo_task',return_value={'status':'failed','error_message':'blocked'}):
            with self.assertRaisesRegex(mesh.ApiError,'blocked'):
                mesh.wait_for_task('tripo','task',timeout=1,poll_interval=0)

if __name__=='__main__': unittest.main()
