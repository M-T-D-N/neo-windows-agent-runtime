"""Run the official AGY CLI and verify task tabs at its successful finish boundary.

Initialization and checking never launch a browser. Transport EOF is not treated
as task completion. Pending/uncertain tabs cause an explicit nonzero result;
the caller can resume the same AGY conversation to inspect and close them.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from neo_mcp_bridge import Bridge, GuardedProactorEventLoop
from neo_cleanup import has_pending_cleanup
from neo_transport import verified_backend_endpoint
from neo_endpoint_identity import assert_owned_endpoint
from process_environment import build_environment

ROOT=Path(__file__).parent

async def finish_check(plan, task_id, execution_id):
    root=Path(plan['ledger_root'])/'task-tabs'/'agy'
    path=root/(task_id+'.json')
    if not path.exists():
        return {'browserUsed':False,'pendingPageIds':[]}
    record=json.loads(path.read_text(encoding='utf-8'))
    if record.get('execution_id') != execution_id:
        return {'browserUsed':False,'pendingPageIds':[]}
    output=[]
    async def no_start():
        raise RuntimeError('Finish check must never start Neo')
    bridge=Bridge(plan['mcp_endpoint'],output.append,starter=no_start,state_root=root,task_id=task_id,
                  backend_resolver=None,
                  endpoint_guard=(lambda endpoint:assert_owned_endpoint(plan,endpoint)) if plan.get('runtime_mode') == 'DIRECT_LOOPBACK' else None)
    bridge.initialize={'params':{'capabilities':{},'clientInfo':{'name':'agy-finish-check','version':'1'}}}
    # This checks the same explicit CLI completion boundary, not another turn.
    # Matching execution_id prevents prior approvals from being carried forward.
    if record.get('finish_checked_execution') == execution_id:
        bridge.cleanup.review_required.clear()
        for item in getattr(bridge.cleanup,'used',{}).values():
            if item.get('deferred'): item['deferred']['review_required']=False
    else:
        bridge.cleanup.review_required = {tuple(k) for k in record.get('review_required', bridge.cleanup.retained)}
    try:
        await bridge.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            'name':'workspace_cleanup','arguments':{'action':'check','task_id':task_id}}})
        result=output[-1].get('result',{})
        check = result.get('structuredContent') or {'decision':'block','reason':'Finish inspection unavailable'}
        if check.get('cleanupVerified') is False:
            return dict(check, decision='block', reason='Finish inspection unverified; preserve task tabs and inspect them at the next browser use')
        if record.get('execution_incomplete') and check.get('browserUsed') is False:
            return {'decision':'block','reason':'Browser request failed before ownership inspection; resume the same task before reporting completion'}
        return check
    finally:
        await bridge.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id',required=True)
    parser.add_argument('--agy',default=str(Path(os.environ['LOCALAPPDATA'])/'agy/bin/agy.exe'))
    parser.add_argument('args',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    if str(uuid.UUID(args.task_id)) != args.task_id:
        raise ValueError('Use the actual stable task UUID')
    execution_id=str(uuid.uuid4())
    environment=build_environment(os.environ,{'WORKSPACE_NEO_TASK_ID':args.task_id,'WORKSPACE_NEO_EXECUTION_ID':execution_id})
    command=args.args[1:] if args.args[:1]==['--'] else args.args
    child=subprocess.run([args.agy,*command],env=environment,capture_output=True)
    sys.stdout.buffer.write(child.stdout);sys.stdout.buffer.flush()
    sys.stderr.buffer.write(child.stderr);sys.stderr.buffer.flush()
    if child.returncode:
        return child.returncode
    plan=json.loads((ROOT/'runtime-plan.json').read_text(encoding='utf-8'))
    with asyncio.Runner(loop_factory=GuardedProactorEventLoop if sys.platform=='win32' else None) as runner:
        check=runner.run(finish_check(plan,args.task_id,execution_id))
    if check.get('decision')=='block' or has_pending_cleanup(check):
        print('NEO_AGY_FINISH_INCOMPLETE: '+json.dumps(check,ensure_ascii=False),file=sys.stderr)
        return 6
    return 0

if __name__=='__main__':
    raise SystemExit(main())
