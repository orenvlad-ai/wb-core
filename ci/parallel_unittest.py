"""Run disposable unittest cases in two isolated processes, without reducing discovery."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import unittest


def test_names(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_names(test)
        else:
            yield test.id().removeprefix('__main__.')


def main(module):
    # Explicit unittest selections retain the normal single-process interface.
    # Children receive explicit names, so they cannot recursively split again.
    if len(sys.argv) > 1:
        unittest.main(module=module)
        return
    names = list(test_names(unittest.defaultTestLoader.loadTestsFromModule(module)))
    if not names or len(set(names)) != len(names):
        raise SystemExit('Empty or duplicate unittest discovery')
    groups = [names[offset::2] for offset in range(min(2, len(names)))]
    script = str(Path(module.__file__).resolve())

    def run(group):
        return subprocess.run([sys.executable, script, *group],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=1800)

    print(f'Running all {len(names)} tests in {len(groups)} isolated processes', flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, groups))
    for index, (group, result) in enumerate(zip(groups, results), 1):
        print(f'Process {index}: {len(group)} tests', flush=True)
        print(result.stdout, end='', flush=True)
    raise SystemExit(int(any(result.returncode != 0 for result in results)))
