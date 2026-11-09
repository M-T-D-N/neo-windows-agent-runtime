"""Run the inherited regression suite against one explicit installed candidate."""
import argparse
import json
import os
from pathlib import Path
import sys
import unittest

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True,type=Path)
    args=parser.parse_args()
    root=args.runtime.resolve(strict=True)
    plan=json.loads((root/'runtime-plan.json').read_text(encoding='utf-8'))
    scratch=Path(plan['scratch_root'])/'regression'
    scratch.mkdir(parents=True,exist_ok=True)
    os.environ['WEB_AUTOMATION_TEST_ROOT']=str(scratch)
    os.environ['TEMP']=os.environ['TMP']=os.environ['TMPDIR']=str(scratch)
    os.environ['PYTHONPATH']=str(root)
    sys.path.insert(0,str(root))
    suite=unittest.defaultTestLoader.discover(str(root/'tests'))
    distribution=Path(__file__).resolve().parent
    sys.path.insert(0,str(distribution))
    import test_distribution
    suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(test_distribution))
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())
