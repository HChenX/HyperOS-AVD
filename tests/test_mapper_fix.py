"""Guard the ranchu mapper fix against unrelated vendor updates."""
import hashlib
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
from patch_mapper import AFTER, BEFORE, MANIFEST, NATIVE, SITES, patch

SOURCE = REPO / 'work/mapper.ranchu.so' if (REPO / 'work/mapper.ranchu.so').is_file() else REPO / 'mapper.ranchu.so'


class MapperGuardTest(unittest.TestCase):
    def test_unknown_vendor_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Unsupported ranchu mapper'):
            patch(b'unknown vendor update')

    @unittest.skipUnless(SOURCE.is_file(), 'Verified vendor ELF is not bundled')
    def test_pinned_elf_matches_audited_sites(self):
        original = SOURCE.read_bytes()
        self.assertEqual(hashlib.sha256(original).hexdigest(), BEFORE)
        result = patch(original)
        self.assertEqual(hashlib.sha256(result).hexdigest(), AFTER)
        self.assertEqual(len(result), len(original))
        changed_offsets = [i for i, (a, b) in enumerate(zip(original, result)) if a != b]
        expected_offsets = []
        for offset, before, after in SITES:
            expected_offsets.extend(offset + i for i, (a, b) in enumerate(zip(before, after)) if a != b)
        self.assertEqual(changed_offsets, sorted(expected_offsets))
        self.assertEqual(patch(result), result)
        tampered = bytearray(original)
        tampered[-1] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'Unsupported ranchu mapper'):
            patch(tampered)
        self.assertEqual((MANIFEST['native'], MANIFEST['native_sha256']), (NATIVE, AFTER))


if __name__ == '__main__':
    unittest.main()
