import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('neo_update', Path(__file__).resolve().parents[1] / 'update-runtime.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def feed(url=None, name='0.51.0', signature='A' * 86 + '=='):
    url = url or f'http://cdn.browseros.com/releases/browserclaw/{name}/win/BrowserOS_neo_v{name}_x64_installer.exe'
    return f'''<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel><item>
    <sparkle:shortVersionString>{name}</sparkle:shortVersionString>
    <enclosure url="{url}" sparkle:os="windows" sparkle:edSignature="{signature}" length="137351224"/>
    </item></channel></rss>'''


class UpdateFeedTests(unittest.TestCase):
    def test_official_http_enclosure_is_upgraded_to_https(self):
        self.assertTrue(update.release(feed())['url'].startswith('https://cdn.browseros.com/'))

    def test_other_product_and_host_are_rejected(self):
        for url in ['https://cdn.browseros.com/releases/browseros/0.51.0/win/BrowserOS_neo_v0.51.0_x64_installer.exe',
                    'https://cdn.browseros.com.evil.test/releases/browserclaw/0.51.0/win/BrowserOS_neo_v0.51.0_x64_installer.exe']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                update.release(feed(url))

    def test_unsigned_or_invalid_version_is_rejected(self):
        for xml in [feed(signature=''), feed(name='../bad')]:
            with self.assertRaises(ValueError):
                update.release(xml)

    def test_versions_use_release_numbers(self):
        self.assertGreater(update.version('0.51.0'), update.version('0.50.5'))
        self.assertEqual(update.version('0.51.0'), update.version('0.51.0.0'))


class UpdatePreparationTests(unittest.TestCase):
    def setUp(self):
        import os, tempfile
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.plan=dict(version='0.50.5',proposed_runtime_root='D:/owned/releases/0.50.5',profile_root='D:/owned/profile')

    def client(self, payload, *, available='0.51.0', tamper=False):
        import base64, httpx
        from Crypto.PublicKey import ECC
        from Crypto.Signature import eddsa
        key=ECC.generate(curve='Ed25519')
        public=base64.b64encode(key.public_key().export_key(format='raw')).decode()
        signature=base64.b64encode(eddsa.new(key,'rfc8032').sign(payload)).decode()
        xml=feed(name=available,signature=signature).replace('137351224',str(len(payload)))
        self.requests=[]
        def handler(request):
            self.requests.append(str(request.url))
            if str(request.url)==update.FEED:return httpx.Response(200,content=xml.encode())
            return httpx.Response(200,content=(b'y'*len(payload) if tamper else payload))
        return httpx.Client(transport=httpx.MockTransport(handler)),public

    def test_no_update_does_not_create_candidate_or_download(self):
        from unittest.mock import patch
        with self.client(b'x'*1000000,available='0.50.5')[0] as client:
            with patch.object(update,'task_directory',side_effect=AssertionError('must stay local/no candidate')):
                result=update.check_update(client,self.plan,prepare=True)
        self.assertFalse(result['update_available'])
        self.assertEqual(self.requests,[update.FEED])
        self.assertEqual(list(self.root.iterdir()),[])

    def test_signed_candidate_is_prepared_without_changing_active_plan_and_reuses_download(self):
        import copy,json,hashlib
        from unittest.mock import patch
        self.plan.update(chromium_version='old-chromium', runtime_verified=True,
                         provisioning_method='native MCP verified',
                         server_override={'enabled': True, 'version': 'old-server',
                                          'activation_status': 'ACTIVE_NATIVE_MCP_VERIFIED'})
        original=copy.deepcopy(self.plan);payload=b'x'*1000000
        client,public=self.client(payload)
        with client,patch.object(update,'PUBLIC_KEY',public):
            result=update.check_update(client,self.plan,prepare=True,task_root=self.root)
            again=update.check_update(client,self.plan,prepare=True,task_root=self.root)
        candidate=json.loads(Path(result['candidate_plan']).read_text(encoding='utf-8'))
        self.assertFalse(result['active_plan_changed'])
        self.assertTrue(result['ed25519_verified'])
        self.assertEqual(candidate['installer_sha256'],hashlib.sha256(payload).hexdigest())
        self.assertEqual(candidate['provisioning_status'],'PREPARED_NOT_ACTIVE')
        self.assertIsNone(candidate['chromium_version'])
        self.assertFalse(candidate['server_override']['enabled'])
        self.assertEqual(candidate['server_override']['activation_status'], 'UNVERIFIED_NOT_ACTIVE')
        self.assertEqual(candidate['previous_server_override'], original['server_override'])
        self.assertFalse(candidate['runtime_verified'])
        self.assertEqual(self.plan,original)
        self.assertEqual(result,again)
        self.assertEqual(len([url for url in self.requests if url!=update.FEED]),1)

    def test_bad_signature_preserves_part_and_never_writes_candidate(self):
        from unittest.mock import patch
        client,public=self.client(b'x'*1000000,tamper=True)
        with client,patch.object(update,'PUBLIC_KEY',public),self.assertRaises(ValueError):
            update.check_update(client,self.plan,prepare=True,task_root=self.root)
        self.assertFalse((self.root/'candidate-plan.json').exists())
        self.assertTrue(list(self.root.glob('*.part')))
        self.assertFalse(list(self.root.glob('*.exe')))

    def test_prepare_rejects_unowned_path_before_download(self):
        client,_=self.client(b'x'*1000000)
        with client,self.assertRaises(ValueError):
            update.check_update(client,self.plan,prepare=True,task_root=Path('D:/Workspace/control'))
        self.assertEqual(self.requests,[update.FEED])

    def test_cli_prints_the_existing_json_contract(self):
        import contextlib,io,json,sys
        from unittest.mock import patch
        plan_path=self.root/'plan.json'
        plan_path.write_text(json.dumps(self.plan),encoding='utf-8-sig')
        client,_=self.client(b'x'*1000000)
        output=io.StringIO()
        with patch.object(sys,'argv',['update-runtime.py','--plan',str(plan_path)]),patch.object(update.httpx,'Client',return_value=client),contextlib.redirect_stdout(output):
            update.main()
        self.assertEqual(json.loads(output.getvalue()),dict(installed='0.50.5',available='0.51.0',update_available=True,feed=update.FEED,active_runtime=self.plan['proposed_runtime_root']))
        self.assertFalse((self.root/'candidate-plan.json').exists())
