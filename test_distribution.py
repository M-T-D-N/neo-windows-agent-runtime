"""Fail-closed checks for the new downloader and portable installer boundary."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json
import sys
import asyncio
import subprocess
import threading
import urllib.request

import httpx

ROOT=Path(__file__).resolve().parent


def load(name, filename):
    spec=importlib.util.spec_from_file_location(name,ROOT/filename)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PayloadTests(unittest.TestCase):
    def check_download(self, status=206, corrupt=False, bad_range=False):
        download=load('payload','download-payload.py')
        payload=b'fictional installer test only'
        def handler(request):
            self.assertEqual(request.headers['Range'],f'bytes=0-{len(payload)-1}')
            return httpx.Response(status,headers={'Content-Range':f'bytes 0-{len(payload)-1}/{len(payload)+int(bad_range)}'},
                content=b'x'*len(payload) if corrupt else payload)
        client=httpx.Client(transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as folder:
            target=Path(folder)/'fictional.bin'
            with patch.object(download.httpx,'Client',return_value=client):
                download.download('https://cdn.browseros.com/releases/browserclaw/test.exe',target,len(payload),hashlib.sha256(payload).hexdigest())
            self.assertEqual(target.read_bytes(),payload)

    def test_exact_range_and_whole_digest(self):self.check_download()
    def test_corrupted_body_is_not_accepted(self):
        with self.assertRaises(ValueError):self.check_download(corrupt=True)
    def test_wrong_range_is_not_accepted(self):
        with self.assertRaises(ValueError):self.check_download(bad_range=True)
    def test_ignored_range_is_not_accepted(self):
        with self.assertRaises(ValueError):self.check_download(status=200)


class InstallPathTests(unittest.TestCase):
    def test_relative_root_and_traversal_are_rejected(self):
        install=load('install_candidate','install.py')
        for path in (Path('relative'),Path(os.environ['WEB_AUTOMATION_TEST_ROOT'])/'..'/'escape'):
            with self.assertRaises(ValueError):install.safe_path(path)


class FixtureAssetsTests(unittest.TestCase):
    def test_editor_assets_are_served_from_installed_runtime(self):
        from web_automation.fixture import Fixture
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as folder:
            server=Fixture(Path(folder)).server()
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base=f'http://127.0.0.1:{server.server_port}'
                for path,mime,marker in (('/', 'text/html', b'Synthetic publishing fixture'),
                        ('/editor.html','text/html',b'Iframe rich body'),
                        ('/app.js','text/javascript',b'publishPost'),
                        ('/style.css','text/css',b'body')):
                    with urllib.request.urlopen(base+path,timeout=3) as response:
                        self.assertEqual(response.status,200)
                        self.assertIn(mime,response.headers['Content-Type'])
                        self.assertIn(marker,response.read())
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)
                self.assertFalse(thread.is_alive())


class EndpointIdentityTests(unittest.TestCase):
    def setUp(self):
        filename=ROOT/'neo_endpoint_identity.py'
        if not filename.exists():filename=ROOT/'runtime/neo_endpoint_identity.py'
        spec=importlib.util.spec_from_file_location('endpoint_identity',filename)
        self.identity=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.identity)
        base=Path(os.environ['WEB_AUTOMATION_TEST_ROOT'])/'fictional'
        self.plan={'mcp_endpoint':'http://127.0.0.1:19010/mcp','server_state_root':str(base/'server'),
            'proposed_runtime_root':str(base/'browser'),'profile_root':str(base/'profile'),'cdp_port':19111,
            'server_override':{'executable':str(base/'resources/bin/server.exe'),'resources_root':str(base/'resources'),'sha256':'fixture-hash'}}
        self.record={'server':{'ProcessId':20,'ParentProcessId':5,'CreationDateUtc':'2026-01-01T00:00:00+00:00'},
            'browser':{'ProcessId':10,'CreationDateUtc':'2026-01-01T00:00:00+00:00'}}
        self.server={'pid':20,'ppid':5,'create_time':1767225600.,'exe':self.plan['server_override']['executable'],
            'cmdline':[self.plan['server_override']['executable'],'--config='+str(base/'server/recovery-server-config.json')]}
        self.browser={'pid':10,'create_time':1767225600.,'exe':str(base/'browser/Chrome-bin/chrome.exe'),
            'cmdline':['chrome.exe','--disable-browseros-server','--user-data-dir='+self.plan['profile_root']]}
        self.config={'ports':{'server':19010,'cdp':19111},'directories':{'resources':str(base/'resources')}}

    def validate(self,listeners=None):
        return self.identity.validate(self.plan,self.record,self.server,self.browser,
            [('127.0.0.1',19010,20)] if listeners is None else listeners,self.config,'fixture-hash')

    def test_exact_installation_identity(self):self.validate()
    def test_foreign_listener_is_rejected(self):
        with self.assertRaises(ValueError):self.validate([('127.0.0.1',19010,99)])
    def test_other_profile_is_rejected(self):
        self.browser['cmdline'][-1]+='-other'
        with self.assertRaises(ValueError):self.validate()

    def test_reused_server_pid_is_rejected(self):
        self.server['create_time']+=1
        with self.assertRaises(ValueError):self.validate()

    def test_exact_server_with_stopped_browser_is_not_ready(self):
        self.browser=None
        self.assertIs(self.validate(),False)

    def test_stopped_browser_does_not_excuse_foreign_listener(self):
        self.browser=None
        with self.assertRaises(ValueError):self.validate([('127.0.0.1',19010,99)])


class EgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from neo_mcp_bridge import Bridge, EndpointIdentityError
        except ImportError:
            self.skipTest('Run exported tests against an installed portable runtime')
        self.IdentityError=EndpointIdentityError
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.sent=[];self.checked=[]
        def handler(request):
            self.sent.append(request)
            raise AssertionError('Unverified endpoint was contacted')
        def reject(endpoint):
            self.checked.append(endpoint)
            raise ValueError('Foreign listener fixture')
        self.bridge=Bridge('http://127.0.0.1:19010/mcp',lambda value:None,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            endpoint_guard=reject,state_root=Path(self.temp.name),task_id='00000000-0000-4000-8000-000000000001')
        self.addAsyncCleanup(self.bridge.close)
        self.bridge.initialize={'params':{'capabilities':{},'clientInfo':{'name':'fixture','version':'1'}}}
        self.bridge.tool_session='previous-session'
        self.bridge.execution_id='new-execution'
        self.bridge.task_state.record['execution_incomplete']=False
        self.bridge.task_state._write_record()
        self.before={p.name:p.read_bytes() for p in Path(self.temp.name).glob('*.json')}

    def unchanged(self):
        self.assertEqual(self.sent,[])
        self.assertEqual({p.name:p.read_bytes() for p in Path(self.temp.name).glob('*.json')},self.before)

    async def test_internal_post_is_guarded_before_send(self):
        with self.assertRaises(self.IdentityError):
            await self.bridge.post({'jsonrpc':'2.0','id':1,'method':'ping'})
        self.unchanged()

    async def test_event_get_stops_without_contact_or_retry(self):
        self.bridge.session='old-http-session'
        await asyncio.wait_for(self.bridge.events('old-http-session'),1)
        self.assertEqual(len(self.checked),1)
        self.unchanged()

    async def test_event_get_does_not_poll_an_off_runtime(self):
        self.bridge.endpoint_guard=lambda endpoint:False
        self.bridge.session='old-http-session'
        await asyncio.wait_for(self.bridge.events('old-http-session'),1)
        self.unchanged()

    async def test_stopping_owned_runtime_receives_no_post(self):
        from neo_mcp_bridge import NotReady
        self.bridge.endpoint_guard=lambda endpoint:False
        with self.assertRaises(NotReady):
            await self.bridge.post({'jsonrpc':'2.0','id':1,'method':'ping'})
        self.unchanged()

    async def test_explicit_request_recovers_stopped_owned_runtime(self):
        ready=False;starts=[];sent=[]
        async def start():
            nonlocal ready
            starts.append(True);ready=True
        def handler(request):
            self.assertTrue(ready)
            message=json.loads(request.content);sent.append(message['method'])
            result={'protocolVersion':'2025-06-18'} if message['method']=='initialize' else {}
            return httpx.Response(200,json={'jsonrpc':'2.0','id':message.get('id'),'result':result})
        await self.bridge.client.aclose()
        self.bridge.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.bridge.endpoint_guard=lambda endpoint:ready
        self.bridge.starter=start
        self.bridge.initialize.update(jsonrpc='2.0',method='initialize',id=1)
        await self.bridge.ensure_ready()
        self.assertEqual(starts,[True])
        self.assertEqual(sent,['initialize'])

    async def test_cleanup_and_resume_do_not_rewrite_records_on_mismatch(self):
        task='00000000-0000-4000-8000-000000000001'
        for action in ('check','resume','inventory'):
            args={'action':action,'task_id':task}
            if action=='inventory':args['group_id']='fixture-group'
            await self.bridge.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workspace_cleanup','arguments':args}})
            self.unchanged()

    async def test_selected_alternate_endpoint_is_passed_to_guard(self):
        self.bridge.endpoint='http://127.0.0.1:19210/mcp'
        with self.assertRaises(self.IdentityError):
            await self.bridge.post({'jsonrpc':'2.0','id':1,'method':'ping'})
        self.assertEqual(self.checked,['http://127.0.0.1:19210/mcp'])
        self.unchanged()


class CatalogAndReadinessTests(unittest.TestCase):
    def test_catalog_mismatch_preserves_existing_catalog_without_http(self):
        try:import neo_mcp_bridge
        except ImportError:self.skipTest('Run exported installed tests')
        runtime=Path(neo_mcp_bridge.__file__).parent
        if not (runtime/'neo_endpoint_identity.py').exists():self.skipTest('Portable runtime only')
        spec=importlib.util.spec_from_file_location('catalog_candidate',runtime/'refresh_catalog.py')
        catalog=importlib.util.module_from_spec(spec);spec.loader.exec_module(catalog)
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as folder:
            root=Path(folder);exe=root/'synthetic.bin';exe.write_bytes(b'synthetic server')
            (root/'runtime-plan.json').write_text(json.dumps({'mcp_endpoint':'http://127.0.0.1:19010/mcp','server_override':{
                'executable':str(exe),'sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'version':'0.0.65'}}),encoding='utf-8')
            existing=root/'official-tools.json';existing.write_bytes(b'preserve existing catalog')
            with (patch.object(catalog,'assert_owned_endpoint',side_effect=ValueError('foreign listener')),
                    patch.object(catalog.httpx,'Client',side_effect=AssertionError('HTTP must not be constructed'))):
                with self.assertRaises(ValueError):catalog.refresh(root)
            self.assertEqual(existing.read_bytes(),b'preserve existing catalog')

    @unittest.skipUnless(sys.platform=='win32','Windows PowerShell')
    def test_readiness_mismatch_sends_zero_http(self):
        try:import neo_mcp_bridge
        except ImportError:self.skipTest('Run exported installed tests')
        runtime=Path(neo_mcp_bridge.__file__).parent
        code="""$ErrorActionPreference='Stop'
. (Join-Path '__ROOT__' 'neo-runtime-common.ps1')
$script:sent=0;$script:checked=0
function Test-NeoEndpointIdentity {param($Endpoint) $script:checked++;return $false}
function Invoke-WebRequest {$script:sent++;throw 'HTTP must not be sent'}
$plan=[pscustomobject]@{mcp_endpoint='http://127.0.0.1:19010/mcp'}
if(Test-McpReady){throw 'Foreign listener was reported ready'}
if($script:sent -ne 0 -or $script:checked -ne 1){throw 'Unexpected identity/HTTP counts'}
""".replace('__ROOT__',str(runtime).replace("'","''"))
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',code],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,(result.stdout+result.stderr).decode('utf-8',errors='replace'))


if __name__=='__main__':unittest.main()
