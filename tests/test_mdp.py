"""Artifact exchange integration and adversarial archive tests."""
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from mdp.cli import (Error, assemble, build, composition_lock, digest, encoded,
                     lock_project, pack, relative, sync, verify)


class MDPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'mdp.toml'
        (self.root / 'hello').write_bytes(b'hello world\n')
        self.project.write_text('''format = 1
[package]
name = "hello"
version = "1.0"
target = "test-target"
abi = "test-abi"
[build]
command = ["python3", "-c", "from pathlib import Path; Path('built').touch()"]
[[files]]
source = "hello"
area = "product"
destination = "bin/hello"
mode = 0o755
''')
        lock_project(self.project)
        self.archive = self.root / 'hello.tar.mdp'
        pack(self.project, self.archive)
        self.manifest = verify(self.archive)

    def archive_with(self, mutate=lambda m: None, extra=None, data=b'hello world\n'):
        manifest = copy.deepcopy(self.manifest)
        mutate(manifest)
        path = self.root / 'bad.tar.mdp'
        with tarfile.open(path, 'w') as tar:
            raw = encoded(manifest)
            info = tarfile.TarInfo('manifest.json')
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
            info = tarfile.TarInfo('payload/000000')
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            if extra:
                tar.addfile(extra)
        return path

    def composition(self):
        path = self.root / 'mdp.assembly.toml'
        path.write_text('''format = 1
target = "test-target"
abi = "test-abi"
[[artifacts]]
path = "hello.tar.mdp"
''')
        composition_lock(path)
        return self.root / 'mdp.assembly.lock'

    def test_deterministic_archive_and_portable_metadata(self):
        other = self.root / 'other.tar.mdp'
        pack(self.project, other)
        self.assertEqual(self.archive.read_bytes(), other.read_bytes())
        self.assertNotIn(str(self.root), json.dumps(self.manifest))

    def test_assembly_receipt_and_mode(self):
        lock = self.composition()
        out = self.root / 'result'
        assemble(lock, out)
        file = out / 'tree/product/bin/hello'
        self.assertEqual(file.read_bytes(), b'hello world\n')
        self.assertEqual(file.stat().st_mode & 0o777, 0o755)
        self.assertEqual(json.loads((out / 'receipt.json').read_text())['lock_sha256'], digest(lock))

    def test_reject_path_escape(self):
        for path in ('../escape', '/absolute', 'a/../b', 'a//b', '.', 'a\\b', 'a\x00b'):
            with self.subTest(path=path), self.assertRaises(Error):
                relative(path)

    def test_reject_bad_destination(self):
        bad = self.archive_with(lambda m: m['files'][0].update(destination='../../escape'))
        with self.assertRaises(Error):
            verify(bad)

    def test_reject_corrupt_payload(self):
        with self.assertRaises(Error):
            verify(self.archive_with(data=b'wrong data!\n'))

    def test_reject_extra_file(self):
        with self.assertRaises(Error):
            verify(self.archive_with(extra=tarfile.TarInfo('unlisted')))

    def test_reject_duplicate_member(self):
        with self.assertRaises(Error):
            verify(self.archive_with(extra=tarfile.TarInfo('payload/000000')))

    def test_reject_symlink_and_devices(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            extra = tarfile.TarInfo('payload/link')
            extra.type = kind
            extra.linkname = '/etc/passwd'
            with self.subTest(kind=kind), self.assertRaises(Error):
                verify(self.archive_with(extra=extra))

    def test_reject_source_symlink(self):
        (self.root / 'hello').unlink()
        (self.root / 'hello').symlink_to('/etc/passwd')
        with self.assertRaises(Error):
            pack(self.project, self.root / 'link.tar.mdp')

    def test_reject_stale_lock(self):
        value = json.loads((self.root / 'mdp.lock').read_text())
        value['dependencies'] = [{'name': 'unexpected'}]
        (self.root / 'mdp.lock').write_text(json.dumps(value))
        with self.assertRaises(Error):
            build(self.project)
        self.assertFalse((self.root / 'built').exists())

    def test_explicit_project_build(self):
        build(self.project)
        self.assertTrue((self.root / 'built').is_file())

    def test_assemble_never_builds(self):
        assemble(self.composition(), self.root / 'result')
        self.assertFalse((self.root / 'built').exists())

    def test_reject_changed_archive_without_output(self):
        lock = self.composition()
        with self.archive.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(Error):
            assemble(lock, self.root / 'result')
        self.assertFalse((self.root / 'result').exists())

    def test_reject_existing_output(self):
        out = self.root / 'result'
        out.mkdir()
        (out / 'keep').touch()
        with self.assertRaises(Error):
            assemble(self.composition(), out)
        self.assertTrue((out / 'keep').exists())

    def test_reject_target_or_abi_mismatch(self):
        path = self.composition()
        for field in ('target', 'abi'):
            value = json.loads(path.read_text())
            value[field] = 'incompatible'
            path.write_text(json.dumps(value))
            with self.subTest(field=field), self.assertRaises(Error):
                assemble(path, self.root / 'result')

    def test_reject_collision_between_packages(self):
        lock = self.composition()
        self.project.write_text(self.project.read_text().replace('name = "hello"', 'name = "other"'))
        other = self.root / 'other.tar.mdp'
        pack(self.project, other)
        value = json.loads(lock.read_text())
        value['artifacts'].append({'path': other.name, 'sha256': digest(other),
                                   'package': verify(other)['package']})
        lock.write_text(json.dumps(value))
        with self.assertRaises(Error):
            assemble(lock, self.root / 'result')
        self.assertFalse((self.root / 'result').exists())

    def test_sdk_lock_sync_and_build(self):
        sdk = self.root / 'sdk.tar.gz'
        sdk.write_bytes(b'opaque SDK artifact')
        self.project.write_text(self.project.read_text() + f'''
[[dependencies]]
name = "sdk"
kind = "sdk"
version = "1.0"
path = "sdk.tar.gz"
sha256 = "{digest(sdk)}"
host = "linux-x86_64"
target = "test-target"
''')
        lock_project(self.project)
        with self.assertRaises(Error):
            build(self.project)
        sync(self.project)
        build(self.project)
        cache = self.root / '.mdp/cache' / digest(sdk)
        cache.write_bytes(b'corrupt')
        with self.assertRaises(Error):
            build(self.project)
        sync(self.project)
        self.assertEqual(cache.read_bytes(), sdk.read_bytes())

    def test_reject_missing_artifact_dependency(self):
        self.project.write_text(self.project.read_text() + f'''
[[dependencies]]
name = "library"
kind = "artifact"
version = "1.0"
url = "https://example.invalid/library.tar.mdp"
sha256 = "{'a' * 64}"
''')
        lock_project(self.project)
        pack(self.project, self.archive)
        with self.assertRaises(Error):
            assemble(self.composition(), self.root / 'result')
        self.assertFalse((self.root / 'result').exists())

    def test_cli_errors_have_no_traceback(self):
        cli = Path(__file__).resolve().parents[1] / 'mdp'
        result = subprocess.run([sys.executable, str(cli), 'verify', '/does-not-exist'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
