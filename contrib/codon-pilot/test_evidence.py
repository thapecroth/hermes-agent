"""Focused regressions for qualification evidence and overlapping profile spans."""
from pathlib import Path
import tempfile
import unittest

from qualify import file_hash, probe_fingerprint, probe_reusable
from render_profile import union


class EvidenceTests(unittest.TestCase):
    def test_resume_rejects_overwritten_source_changed_compiler_or_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'probe.codon'
            binary = root / 'probe'
            source.write_text('print(7)\n')
            binary.write_bytes(b'qualified binary')
            toolchain = {'compiler': 'compiler-sha', 'cpu': 'cortex-a72'}
            old = {'build': {'returncode': 0, 'command': [str(source)]},
                   'execution': {'returncode': 0},
                   'fingerprint': probe_fingerprint(source.read_text(), toolchain),
                   'executable_sha256': file_hash(binary)}
            self.assertTrue(probe_reusable(old, source.read_text(), toolchain, binary))
            source.write_text('print(8)\n')
            self.assertFalse(probe_reusable(old, source.read_text(), toolchain, binary))
            source.write_text('print(7)\n')
            self.assertFalse(probe_reusable(old, source.read_text(), {'compiler': 'changed'}, binary))
            binary.write_bytes(b'changed executable')
            self.assertFalse(probe_reusable(old, source.read_text(), toolchain, binary))

    def test_resume_rejects_incomplete_or_timed_out_work(self):
        toolchain = {'compiler': 'sha'}
        old = {'fingerprint': probe_fingerprint('source', toolchain),
               'build': {'timed_out': True}}
        self.assertFalse(probe_reusable(old, 'source', toolchain, Path('/missing')))
        old['build'] = {'returncode': 1}
        self.assertTrue(probe_reusable(old, 'source', toolchain, Path('/missing')))
        old['build'] = {'returncode': 1, 'timed_out': True}
        self.assertFalse(probe_reusable(old, 'source', toolchain, Path('/missing')))

    def test_wall_union_does_not_double_count_parallel_or_nested_spans(self):
        self.assertEqual(union([(0, 10), (1, 3), (5, 12), (20, 21)]), 13)
        self.assertEqual(union([]), 0)


if __name__ == '__main__':
    unittest.main()
