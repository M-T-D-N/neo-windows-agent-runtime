import asyncio
import copy
import json
import unittest
import httpx
from neo_transport import select_backend
from neo_mcp_bridge import Bridge
from test_mcp_bridge import INIT, CALL, response


class ManagedTransportTests(unittest.TestCase):
    def test_only_exact_managed_chain_and_exclusive_loopback_listener_is_selected(self):
        plan={'mcp_endpoint':'http://127.0.0.1:9010/mcp','profile_root':'D:/test/profile',
              'proposed_runtime_root':'D:/test/runtime','server_override':{'enabled':True,'executable':'D:/test/server.exe','sha256':'verified'}}
        browser={'pid':10,'ppid':1,'exe':'D:/test/runtime/Chrome-bin/chrome.exe','create_time':1,
                 'cmdline':['chrome.exe','--user-data-dir=D:/test/profile','--browseros-proxy-port=9010','--browseros-server-port=9210']}
        server={'pid':20,'ppid':10,'exe':'D:/test/server.exe','create_time':2,
                'cmdline':['server.exe','--config=D:/test/profile/.browseros/config.json']}
        listener={'address':'127.0.0.1','port':9210,'pid':20}
        def select(rows, listeners, digest='verified', port=9210):
            return select_backend(plan,rows,listeners,lambda _: {'ports':{'server':port}},lambda _:digest)
        self.assertEqual(select([browser,server],[listener])['endpoint'],'http://127.0.0.1:9210/mcp')
        cases=[([browser,dict(server,ppid=99)],[listener]),([browser,server,dict(server,pid=21)],[listener]),
               ([browser,server],[dict(listener,pid=99)]),([browser,server],[dict(listener,address='0.0.0.0')]),
               ([dict(browser,cmdline=['chrome.exe','--user-data-dir=D:/other']),server],[listener]),
               ([browser,dict(server,cmdline=['server.exe','--config=D:/other/config.json'])],[listener])]
        for rows, listeners in cases:
            with self.subTest(rows=rows,listeners=listeners):self.assertIsNone(select(rows,listeners))
        self.assertIsNone(select([browser,server],[listener],digest='changed'))
        self.assertIsNone(select([browser,server],[listener],port=9010))
        renderer=dict(browser,pid=11,ppid=10,cmdline=None)
        self.assertIsNotNone(select([browser,server,renderer],[listener]))
        self.assertIsNone(select([browser,server,dict(renderer,ppid=1)],[listener]))


class BridgeFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_start_resolves_once_again_and_reconnect_rejects_lost_identity(self):
        started=False; valid=True; probes=[]; posts=[]
        def resolve():
            probes.append(True)
            return 'http://127.0.0.1:9210/mcp' if started and valid else None
        async def start():
            nonlocal started
            if started:raise RuntimeError('No verified runtime available')
            started=True
        def handler(request):
            rpc=json.loads(request.content);posts.append((request.url.port,rpc['method']))
            if request.url.port==9010:raise httpx.ConnectError('proxy down',request=request)
            return response(request,{'protocolVersion':'2025-03-26'})
        b=Bridge('http://127.0.0.1:9010/mcp',lambda _:None,client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),starter=start,backend_resolver=resolve)
        self.addAsyncCleanup(b.close)
        await b.connect(INIT)
        self.assertEqual(len(probes),2)
        self.assertEqual(b.endpoint,'http://127.0.0.1:9210/mcp')
        valid=False;posts.clear()
        with self.assertRaisesRegex(RuntimeError,'No verified runtime'):
            await b.connect(INIT)
        self.assertTrue(posts)
        self.assertTrue(all(port==9010 for port,_ in posts))

    async def test_verified_backend_is_used_before_dispatch_and_uncertain_action_is_not_replayed(self):
        calls=[]; starts=[]; resolved=[]; output=[]
        def handler(request):
            rpc=json.loads(request.content);calls.append((request.url.port,rpc['method']))
            if request.url.port==9010:raise httpx.ConnectError('proxy bind failed',request=request)
            if rpc['method']=='initialize':return response(request,{'protocolVersion':'2025-03-26'})
            if rpc['method']=='tools/call':raise httpx.ReadError('response lost after dispatch',request=request)
            return response(request)
        async def start():starts.append(True)
        def resolve():resolved.append(True);return 'http://127.0.0.1:9210/mcp'
        bridge=Bridge('http://127.0.0.1:9010/mcp',output.append,client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),starter=start,backend_resolver=resolve)
        self.addAsyncCleanup(bridge.close)
        await bridge.handle(INIT)
        await bridge.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        await bridge.handle({'jsonrpc':'2.0','id':3,'method':'ping'})
        self.assertEqual(resolved,[])
        await bridge.handle(CALL)
        self.assertEqual(starts,[])
        self.assertEqual(resolved,[True])
        self.assertEqual([item for item in calls if item[1]=='tools/call'],[(9210,'tools/call')])
        self.assertTrue(output[-1]['result']['isError'])
        self.assertIn('OUTCOME',output[-1]['result']['content'][0]['text'])
