"""Offline tests: synthetic user-data directories only; never modifies real Edge data."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import edge_region as tool


def packed(number, value):
    return tool.encode_varint(number * 8 + 2) + tool.encode_varint(len(value)) + value


def seed(region='CN', major=157, signature=b'', *, payload=None,
         milestone=None, omit_version=False):
    body = {'Headers': {'CountryCode': region, 'ETag': 'keep-me'},
            'EdgeConfig': {tool.RULE: {'disableFeatures': [tool.FEATURE, 'KeepDisabled']},
                           'Unrelated': {'enableFeatures': ['LeaveMeAlone']}},
            'UnrelatedText': 'CN is text, not a country field.'}
    decoded = b''.join([
        packed(1, payload if payload is not None else json.dumps(body, ensure_ascii=False).encode()), packed(2, signature),
        tool.encode_varint(3 * 8) + tool.encode_varint(major if milestone is None else milestone),
        tool.encode_varint(4 * 8) + tool.encode_varint(123456789),
        packed(6, region.encode()), packed(7, region.encode()),
        b'' if omit_version else packed(8, f'{major}.0.4341.60'.encode()),
        packed(27, b'unknown-field-must-survive'),
        tool.encode_varint(28 * 8 + 5) + b'\x12\x34\x56\x78'])
    return tool.zstd.ZstdCompressor(write_checksum=True).compress(decoded)


def state(region='CN'):
    return ('{\n "variations_country" : "' + region + '",\n'
            ' "variations_permanent_consistency_country" : ["157.0.4341.60", "' + region + '"],\n'
            ' "unrelated" : { "文字": "保留 CN 和 US", "values": [1, true] }\n}').encode('utf-8')


class RegionTests(unittest.TestCase):
    def test_seed_changes_only_requested_countries_and_preserves_unknown_fields(self):
        original = seed()
        changed = tool.seed_patch(original, 'JP')
        a, old, _ = tool.decode_seed(original)
        b, new, _ = tool.decode_seed(changed)
        old['Headers']['CountryCode'] = 'JP'
        self.assertEqual(old, new)
        self.assertEqual({f.number: f.raw for f in a if f.number not in (1, 6, 7)},
                         {f.number: f.raw for f in b if f.number not in (1, 6, 7)})
        self.assertEqual(tool.unique_field(b, 6, 2).value, b'JP')
        self.assertEqual(tool.unique_field(b, 7, 2).value, b'JP')
        self.assertTrue(tool.zstd.get_frame_parameters(changed).has_checksum)

    def test_writing_rule_repair_is_explicit_and_preserves_other_features(self):
        unchanged_rule = tool.decode_seed(tool.seed_patch(seed(), 'US'))[1]['EdgeConfig'][tool.RULE]
        self.assertIn(tool.FEATURE, unchanged_rule['disableFeatures'])
        config = tool.decode_seed(tool.seed_patch(seed(), 'US', repair_writing=True))[1]['EdgeConfig']
        self.assertEqual(config[tool.RULE]['disableFeatures'], ['KeepDisabled'])
        self.assertEqual(config[tool.RULE]['enableFeatures'], [tool.FEATURE])
        self.assertEqual(config['Unrelated'], {'enableFeatures': ['LeaveMeAlone']})

    def test_state_preserves_version_unicode_and_unrelated_formatting(self):
        original = state()
        changed = tool.state_patch(original, 'US')
        self.assertEqual(changed, original.replace(b'"CN"', b'"US"'))
        self.assertIn('保留 CN 和 US'.encode(), changed)

    def test_already_matching_values_produce_identical_bytes(self):
        source = seed('US')
        self.assertEqual(tool.seed_patch(source, 'US'), source)
        self.assertEqual(tool.state_patch(state('US'), 'US'), state('US'))

    def test_refuses_new_schema_signed_seed_corruption_and_bad_country(self):
        for source in (seed(major=159), seed(signature=b'nonempty-signature'), b'not-zstd'):
            with self.assertRaises((tool.RepairError, tool.zstd.ZstdError)):
                tool.seed_patch(source, 'US')
        for source in (b'\x0a\xff', b'\x00', b'\x0a\x10truncated'):
            with self.assertRaises(tool.RepairError):
                tool.parse_fields(source)
        self.assertEqual(tool.country('jp'), 'JP')
        with self.assertRaises(tool.RepairError):
            tool.country('USA')

    def test_download_milestone_and_permanent_version_are_independent(self):
        source = seed(major=158, milestone=157)
        changed = tool.seed_patch(source, 'US')
        fields, _, version = tool.decode_seed(changed)
        self.assertEqual(tool.unique_field(fields, 3, 0).value, 157)
        self.assertEqual(version, '158.0.4341.60')

    def test_safe_seed_can_omit_permanent_version(self):
        source = seed(major=151, omit_version=True)
        changed = tool.seed_patch(source, 'US', version_context='158.0.4370.33')
        fields, _, version = tool.decode_seed(changed)
        self.assertEqual(version, '')
        self.assertFalse(any(f.number == 8 for f in fields))

    def test_sentinel_is_preserved_not_materialized_as_json(self):
        source = seed(major=158, milestone=157, payload=tool.SAFE_SEED_SENTINEL)
        fields, document, version = tool.decode_seed(source)
        self.assertIsNone(document)
        changed = tool.seed_patch(source, 'US', repair_writing=True)
        fields, document, _ = tool.decode_seed(changed)
        self.assertIsNone(document)
        self.assertEqual(tool.unique_field(fields, 1, 2).value, tool.SAFE_SEED_SENTINEL)
        self.assertEqual(tool.unique_field(fields, 7, 2).value, b'US')

    def test_sentinel_missing_safe_seed_fails_before_any_write(self):
        with tempfile.TemporaryDirectory(prefix='edge-region-offline-test-') as folder:
            root = self.create_root(folder)
            (root / 'VariationsSeedV2').write_bytes(seed(payload=tool.SAFE_SEED_SENTINEL))
            before = tool.file_pair(root)
            with mock.patch.object(tool, 'edge_running', return_value=False):
                with self.assertRaisesRegex(tool.RepairError, '备用文件'):
                    tool.set_region(root, 'US')
            self.assertEqual(tool.file_pair(root), before)
            self.assertFalse((root / 'Codex-Backups').exists())

    def test_shared_safe_seed_is_patched_backed_up_and_restored(self):
        with tempfile.TemporaryDirectory(prefix='edge-region-offline-test-') as folder:
            root = self.create_root(folder)
            state_obj = json.loads((root / 'Local State').read_bytes())
            state_obj['variations_last_launch_version'] = '158.0.4370.33'
            (root / 'Local State').write_text(json.dumps(state_obj), encoding='utf-8')
            (root / 'VariationsSeedV2').write_bytes(seed(major=158, milestone=157, payload=tool.SAFE_SEED_SENTINEL))
            (root / 'VariationsSafeSeedV2').write_bytes(seed(major=151, omit_version=True))
            before = tool.file_pair(root)
            with mock.patch.object(tool, 'edge_running', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                tool.set_region(root, 'US', repair_writing=True)
                status = tool.inspect(root)
                self.assertEqual(status['内部字段7_写作地区判定'], 'US')
                self.assertEqual(status['备用文件内部字段6'], 'US')
                self.assertFalse(status['已知写作禁用项'])
                manifest = next((root / 'Codex-Backups/RegionTool').glob('*/manifest.json'))
                self.assertEqual(set(json.loads(manifest.read_text(encoding='utf-8'))['files']), set(before))
                tool.restore(root, str(manifest.parent))
            self.assertEqual(tool.file_pair(root), before)

    def test_failure_with_shared_safe_seed_rolls_back_all_changed_files(self):
        with tempfile.TemporaryDirectory(prefix='edge-region-offline-test-') as folder:
            root = self.create_root(folder)
            (root / 'VariationsSeedV2').write_bytes(seed(payload=tool.SAFE_SEED_SENTINEL))
            (root / 'VariationsSafeSeedV2').write_bytes(seed(omit_version=True))
            before = tool.file_pair(root)
            real_write = tool.atomic_write
            calls = []
            def fail_third(path, value):
                calls.append(path.name)
                if len(calls) == 3:
                    raise OSError('injected final-file failure')
                return real_write(path, value)
            with mock.patch.object(tool, 'edge_running', return_value=False), mock.patch.object(tool, 'atomic_write', side_effect=fail_third):
                with self.assertRaises(tool.RepairError):
                    tool.set_region(root, 'US')
            self.assertEqual(tool.file_pair(root), before)

    def create_root(self, folder):
        root = Path(folder)
        (root / 'Local State').write_bytes(state())
        (root / 'VariationsSeedV2').write_bytes(seed())
        return root

    def test_running_edge_prevents_any_writes(self):
        with tempfile.TemporaryDirectory(prefix='edge-region-offline-test-') as folder:
            root = self.create_root(folder)
            before = tool.file_pair(root)
            with mock.patch.object(tool, 'edge_running', return_value=True):
                with self.assertRaises(tool.RepairError):
                    tool.set_region(root, 'US')
            self.assertEqual(tool.file_pair(root), before)
            self.assertFalse((root / 'Codex-Backups').exists())

    def test_backup_restore_roundtrip_and_hash_check(self):
        with tempfile.TemporaryDirectory(prefix='edge-region-offline-test-') as folder:
            root = self.create_root(folder)
            before = tool.file_pair(root)
            with mock.patch.object(tool, 'edge_running', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                tool.set_region(root, 'US')
                self.assertEqual(tool.inspect(root)['内部字段7_写作地区判定'], 'US')
                tool.restore(root, 'latest')
                self.assertEqual(tool.file_pair(root), before)
                backup = next((root / 'Codex-Backups/RegionTool').glob('*/manifest.json')).parent
                (backup / 'VariationsSeedV2').write_bytes(b'corrupted-backup')
                with self.assertRaises(tool.RepairError):
                    tool.restore(root, str(backup))
                self.assertEqual(tool.file_pair(root), before)

    def test_failed_second_write_rolls_back_first_file(self):
        with tempfile.TemporaryDirectory(prefix='edge-region-offline-test-') as folder:
            root = self.create_root(folder)
            before = tool.file_pair(root)
            real_write = tool.atomic_write
            calls = []
            def injected(path, value):
                calls.append(path.name)
                if len(calls) == 2:
                    raise OSError('injected write failure')
                return real_write(path, value)
            with mock.patch.object(tool, 'edge_running', return_value=False), mock.patch.object(tool, 'atomic_write', side_effect=injected):
                with self.assertRaises(tool.RepairError):
                    tool.set_region(root, 'US')
            self.assertEqual(tool.file_pair(root), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
