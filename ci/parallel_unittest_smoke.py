"""Exercise actual child discovery, isolation, skips and failure propagation."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
with TemporaryDirectory() as raw:
    root = Path(raw)
    script = root / 'cases.py'
    script.write_text('''import os,sys,unittest
from pathlib import Path
sys.path.insert(0, %r)
from ci.parallel_unittest import main
class Cases(unittest.TestCase):
    def mark(self):
        path=Path(os.environ['RESULTS'])/self._testMethodName
        assert not path.exists(), 'test executed twice'
        path.write_text(str(os.getpid()))
    def test_a(self): self.mark()
    def test_b(self): self.mark()
    def test_c(self):
        self.mark()
        if os.environ.get('FAIL'): self.fail('deliberate child failure')
    @unittest.skip('deliberate skip')
    def test_d(self): self.mark()
if __name__=='__main__': main(sys.modules[__name__])
''' % str(ROOT))
    def run(folder, *args, fail=False):
        folder.mkdir()
        env = dict(os.environ, RESULTS=str(folder))
        if fail:
            env['FAIL'] = '1'
        return subprocess.run([sys.executable, str(script), *args], env=env,
                              capture_output=True, text=True, timeout=30)
    passing = root / 'pass'
    result = run(passing)
    assert result.returncode == 0, result
    assert {p.name for p in passing.iterdir()} == {'test_a', 'test_b', 'test_c'}
    assert len({p.read_text() for p in passing.iterdir()}) == 2
    assert 'Running all 4 tests' in result.stdout and 'skipped=1' in result.stdout
    result = run(root / 'fail', fail=True)
    assert result.returncode != 0 and 'deliberate child failure' in result.stdout
    selected = root / 'selected'
    result = run(selected, 'Cases.test_b')
    assert result.returncode == 0 and [p.name for p in selected.iterdir()] == ['test_b']
    result = run(root / 'missing', 'Cases.test_missing')
    assert result.returncode != 0
print('Parallel unittest: complete discovery, isolated children, skips, selection and failures PASS')
