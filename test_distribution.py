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


def unqualified_browser_commands(command,profile):
    # Counterexamples from Chromium's post-argv semantics, plus conservative
    # canonical-input boundaries. Fixtures have their profile at argv[2].
    return {
        'terminator-before':command[:2]+['--']+command[2:],
        'terminator-after':command+['--'],
        'spaced-profile':command+[' --user-data-dir='+profile+'-other '],
        'spaced-type':command+[' --type=renderer '],
        'single-before':command[:2]+['--single-argument']+command[2:],
        'single-after':command+['--single-argument'],
        'single-alias':command[:2]+['/SINGLE-ARGUMENT=value']+command[2:],
        'program-only-profile':[command[2]]+command[1:2]+command[3:],
        'empty-program':['']+command[1:],
        'relative-profile':command[:2]+['--user-data-dir='+os.path.relpath(profile)]+command[3:],
        'unicode-space-profile':command+['\u00a0--user-data-dir='+profile+'-other\u00a0'],
        'format-profile':command+['\ufeff--user-data-dir='+profile+'-other\ufeff'],
        'profile-value-space':command[:2]+[command[2]+' ']+command[3:],
        'profile-value-tab':command[:2]+[command[2]+'\t']+command[3:],
    }


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
    def test_supplementary_symbol_in_other_argument_is_not_control(self):
        self.browser['cmdline'].append('--app='+chr(0x1F642))
        self.assertIs(self.validate(),True)
    def test_equivalent_config_path_spelling_is_accepted(self):
        value=self.server['cmdline'][1].split('=',1)[1]
        for spelling in (value.upper(),value.replace('\\','/')):
            with self.subTest(spelling=spelling):
                self.server['cmdline'][1]='--config='+spelling
                self.assertIs(self.validate(),True)

    def test_different_or_additional_config_is_rejected(self):
        command=list(self.server['cmdline'])
        for args in ([command[0],command[1]+'-other'],command+[command[1]],
                     [command[0],'--untrusted='+command[1].split('=',1)[1]]):
            with self.subTest(args=args),self.assertRaises(ValueError):
                self.server['cmdline']=args
                self.validate()

    def test_relative_config_does_not_borrow_identity_helper_cwd(self):
        prior=Path.cwd()
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as folder:
            self.plan['server_state_root']=folder
            self.server['cmdline'][1]='--config=recovery-server-config.json'
            try:
                os.chdir(folder)
                self.assertEqual(self.identity.path('recovery-server-config.json'),self.identity.path(Path(folder)/'recovery-server-config.json'))
                with self.assertRaises(ValueError):self.validate()
            finally:os.chdir(prior)
    def test_foreign_listener_is_rejected(self):
        with self.assertRaises(ValueError):self.validate([('127.0.0.1',19010,99)])
    def test_other_profile_is_rejected(self):
        self.browser['cmdline'][-1]+='-other'
        with self.assertRaises(ValueError):self.validate()

    def test_duplicate_profile_arguments_are_rejected_in_both_orders(self):
        original=list(self.browser['cmdline'])
        duplicate='--user-data-dir='+self.plan['profile_root']+'-other'
        for args in (original+[duplicate],original[:2]+[duplicate]+original[2:],
                     original+[original[-1]],original+['--user-data-dir']):
            with self.subTest(args=args),self.assertRaises(ValueError):
                self.browser['cmdline']=args
                self.validate()

    def test_all_profile_aliases_and_casings_are_counted(self):
        original=list(self.browser['cmdline'])
        for prefix in ('-user-data-dir=','/user-data-dir=','--USER-DATA-DIR=','-USER-DATA-DIR=','/USER-DATA-DIR='):
            for suffix in ('','-other'):
                alias=prefix+self.plan['profile_root']+suffix
                for args in (original+[alias],original[:2]+[alias]+original[2:],original[:2]+[alias]):
                    with self.subTest(prefix=prefix,suffix=suffix,args=args),self.assertRaises(ValueError):
                        self.browser['cmdline']=args
                        self.validate()

    def test_ambiguous_chromium_modes_never_qualify_browser_identity(self):
        original=list(self.browser['cmdline'])
        for label,args in unqualified_browser_commands(original,self.plan['profile_root']).items():
            with self.subTest(label=label),self.assertRaises(ValueError):
                self.browser['cmdline']=args
                self.validate()

    def test_reused_server_pid_is_rejected(self):
        self.server['create_time']+=1
        with self.assertRaises(ValueError):self.validate()

    def test_exact_server_with_stopped_browser_is_not_ready(self):
        self.browser=None
        self.assertIs(self.validate(),False)

    def test_stopped_browser_does_not_excuse_foreign_listener(self):
        self.browser=None
        with self.assertRaises(ValueError):self.validate([('127.0.0.1',19010,99)])


class BrowserRecordRepairTests(unittest.TestCase):
    """Real atomic record writes; OS observations are substituted, never live PCs."""
    def setUp(self):
        EndpointIdentityTests.setUp(self)
        import copy
        from types import SimpleNamespace
        self.SimpleNamespace=SimpleNamespace
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        state=Path(self.temp.name)
        self.plan.update(runtime_mode='DIRECT_LOOPBACK',server_state_root=str(state))
        self.exe=state/'server.exe';self.exe.write_bytes(b'fictional executable, never launched')
        self.plan['server_override']['executable']=str(self.exe)
        self.plan['server_override']['sha256']=hashlib.sha256(self.exe.read_bytes()).hexdigest()
        self.server.update(exe=str(self.exe),cmdline=[str(self.exe),'--config='+str(state/'recovery-server-config.json')])
        self.replacement=copy.deepcopy(self.browser)
        self.browser['ppid']=7
        self.replacement.update(pid=11,ppid=7,create_time=self.browser['create_time']+60)
        self.replacement['cmdline'].append('--browseros-cdp-port=19111')
        self.server['ppid']=5
        self.record_path=state/'recovery-server-process.json'
        self.config_path=state/'recovery-server-config.json'
        self.save()
        self.rows=[self.replacement]
        self.old_present=False
        self.username='fixture-user'
        self.current_session=1
        self.change=None
        self.connection_rows=[('127.0.0.1',19010,20),('127.0.0.1',19111,11)]

    def save(self):
        self.record_path.write_text(json.dumps(self.record),encoding='utf-8')
        self.config_path.write_text(json.dumps(self.config),encoding='utf-8')

    def repair(self):
        import contextlib,copy
        owner=self
        class Process:
            def __init__(self,pid=99):self.pid=pid
            def as_dict(self,attrs):
                row=owner.server if self.pid==20 else owner.browser if self.pid==10 else owner.replacement
                if owner.change and self.pid==11:
                    owner.change()
                return {key:copy.deepcopy(row[key]) for key in attrs}
            def username(self):return owner.username if self.pid==11 else 'fixture-user'
        processes=[self.SimpleNamespace(info={'name':'chrome.exe'},as_dict=lambda attrs,row=row:copy.deepcopy(row)) for row in self.rows]
        connections=[self.SimpleNamespace(status='LISTEN',laddr=self.SimpleNamespace(ip=ip,port=port),pid=pid) for ip,port,pid in self.connection_rows]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(self.identity.psutil,'Process',Process))
            stack.enter_context(patch.object(self.identity.psutil,'pid_exists',lambda pid:self.old_present))
            stack.enter_context(patch.object(self.identity.psutil,'process_iter',lambda attrs:processes))
            stack.enter_context(patch.object(self.identity.psutil,'net_connections',lambda **kw:connections))
            stack.enter_context(patch.object(self.identity,'session_id',lambda pid:self.current_session if pid==11 else 1))
            return self.identity.rebind_browser(self.plan)

    def test_exact_restarted_browser_updates_only_browser_record(self):
        self.assertTrue(self.repair())
        changed=json.loads(self.record_path.read_text(encoding='utf-8'))
        self.assertEqual(changed['server'],self.record['server'])
        self.assertEqual(changed['browser']['ProcessId'],11)
        self.assertTrue(self.identity.created(self.replacement,changed['browser']['CreationDateUtc']))

    def test_live_record_is_not_rewritten_during_readiness_wait(self):
        self.old_present=True
        before=self.record_path.read_bytes()
        self.assertIs(self.repair(),False)
        self.assertEqual(self.record_path.read_bytes(),before)

    def test_starting_server_without_listener_keeps_bounded_readiness_wait(self):
        self.old_present=True
        self.connection_rows=[]
        before=self.record_path.read_bytes()
        self.assertIs(self.repair(),False)
        self.assertEqual(self.record_path.read_bytes(),before)

    def test_uncertain_replacements_preserve_original_record(self):
        import copy
        original=copy.deepcopy(self.replacement)
        cases=('foreign-mcp','foreign-cdp','server-pid-reuse','browser-pid-reuse','other-user',
               'other-session','other-profile','ambiguous','duplicate-profile','config-changed')
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                before=self.record_path.read_bytes()
                if case=='foreign-mcp':self.connection_rows[0]=('127.0.0.1',19010,88)
                if case=='foreign-cdp':self.connection_rows[1]=('127.0.0.1',19111,88)
                if case=='server-pid-reuse':self.server['create_time']+=1
                if case=='browser-pid-reuse':self.old_present=True;self.browser['create_time']+=1
                if case=='other-user':self.username='another-user'
                if case=='other-session':self.current_session=2
                if case=='other-profile':self.replacement['cmdline'][2]+='-other'
                if case=='ambiguous':self.rows.append(copy.deepcopy(original))
                if case=='duplicate-profile':self.replacement['cmdline'].append(self.replacement['cmdline'][2])
                if case=='config-changed':self.change=lambda:self.config_path.write_text('{}',encoding='utf-8')
                with self.assertRaises(ValueError):self.repair()
                self.assertEqual(self.record_path.read_bytes(),before)

    def test_alias_duplicates_never_rebind_or_write_record(self):
        for prefix in ('-user-data-dir=','/user-data-dir=','--USER-DATA-DIR=','-USER-DATA-DIR=','/USER-DATA-DIR='):
            for suffix in ('','-other'):
                for first in (True,False):
                    with self.subTest(prefix=prefix,suffix=suffix,first=first):
                        self.setUp()
                        alias=prefix+self.plan['profile_root']+suffix
                        self.replacement['cmdline'].insert(1 if first else len(self.replacement['cmdline']),alias)
                        before=self.record_path.read_bytes()
                        with self.assertRaises(ValueError):self.repair()
                        self.assertEqual(self.record_path.read_bytes(),before)

    def test_ambiguous_modes_never_rebind_or_write_record(self):
        cases=unqualified_browser_commands(list(self.replacement['cmdline']),self.plan['profile_root'])
        for label in cases:
            with self.subTest(label=label):
                self.setUp()
                self.replacement['cmdline']=unqualified_browser_commands(list(self.replacement['cmdline']),self.plan['profile_root'])[label]
                before=self.record_path.read_bytes()
                with self.assertRaises(ValueError):self.repair()
                self.assertEqual(self.record_path.read_bytes(),before)


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
