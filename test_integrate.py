#!/usr/bin/env python3
"""Focused tests for resource linking and overlay APK packaging (stdlib only)."""
import json
from pathlib import Path
import tempfile
import unittest
import warnings
import xml.etree.ElementTree as ET
import zipfile
import integrate as kit


class ResourcesTest(unittest.TestCase):
    def test_allocates_without_changing_existing_ids(self):
        target = {'string/title': 0x7f030000, 'string/vendor': 0x7f030009,
                  'id/vendor': 0x7f050005}
        source = {'string/title': 0x7f010000, 'string/tzb_title': 0x7f010001,
                  'layout/tzb_screen': 0x7f020000}
        result = kit.allocate_resources(target, source, ['string/tzb_title', 'layout/tzb_screen'])
        self.assertEqual(0x7f03000a, result['string/tzb_title'])
        self.assertEqual(0x7f060000, result['layout/tzb_screen'])
        for name, value in target.items():
            self.assertEqual(value, result[name])

    def test_collision_rejected(self):
        with self.assertRaises(kit.Incompatible):
            kit.allocate_resources({'string/tzb_title': 0x7f010000},
                                   {'string/tzb_title': 0x7f010001}, ['string/tzb_title'])

    def test_high16_and_fields_relinked_without_touching_text_or_integer_max(self):
        result = kit.remap_smali('''
.field public static layout:I = 0x7f010000
    const/high16 v2, 0x7f010000
    const-string v0, "0x7f010000 # keep this"
    const v1, 0x7fffffff
''', {0x7f010000: 0x7f020003})
        self.assertIn('.field public static layout:I = 0x7f020003', result)
        self.assertIn('const v2, 0x7f020003', result)
        self.assertIn('"0x7f010000 # keep this"', result)
        self.assertIn('const v1, 0x7fffffff', result)

    def test_packed_switch_becomes_sorted_sparse_when_ids_change_order(self):
        source = '''
    packed-switch v0, :keys
    :keys
    .packed-switch 0x7f010001
        :first
        :second
    .end packed-switch
'''
        result = kit.remap_smali(source, {0x7f010001: 0x7f010002, 0x7f010002: 0x7f010001})
        self.assertIn('sparse-switch v0, :keys', result)
        self.assertIn('0x7f010001 -> :second', result)
        self.assertIn('0x7f010002 -> :first', result)
        self.assertLess(result.index('-> :second'), result.index('-> :first'))

    def test_sparse_switch_and_array_are_not_remapped_twice(self):
        source = '''
    .sparse-switch
        0x7f010001 -> :first
        0x7f010002 -> :second
    .end sparse-switch
    .array-data 4
        0x7f010001
        0x7f010002
    .end array-data
'''
        result = kit.remap_smali(source, {0x7f010001: 0x7f010002, 0x7f010002: 0x7f010001})
        self.assertIn('0x7f010001 -> :second', result)
        self.assertIn('.array-data 4\n        0x7f010002\n        0x7f010001', result)

    def test_canonical_code_allows_id_and_comment_changes_but_rejects_logic_changes(self):
        before = '    const/high16 v0, 0x7f010000 # resource\n    return v0\n'
        after = '    .line 10\n    const v0, 0x7f040005 # new ID\n    return v0\n'
        self.assertEqual(kit.normalized_smali(before, {0x7f010000: 0x7f010000}),
                         kit.normalized_smali(after, {0x7f040005: 0x7f010000}))
        self.assertNotEqual(kit.normalized_smali(before, {}),
                            kit.normalized_smali(before.replace('return v0', 'return v1'), {}))

    def test_code_preserves_strings_with_escaped_quotes_and_comment_characters(self):
        line = '    const-string v0, "a \\"quoted\\" # 0x7f010000" # trailing'
        self.assertIn('# 0x7f010000"', kit.code_part(line))
        self.assertNotIn('trailing', kit.code_part(line))

    def test_xml_format_changes_do_not_hide_actual_changes(self):
        first = ET.fromstring('<TextView a="x" b="y"><View /></TextView>')
        second = ET.fromstring('<TextView b="y" a="x">\n <View />\n</TextView>')
        self.assertEqual(kit.xml_shape(first), kit.xml_shape(second))
        second.set('a', 'z')
        self.assertNotEqual(kit.xml_shape(first), kit.xml_shape(second))

    def test_register_counts_and_branch_label_aliases_normalize_after_assembly(self):
        source = '''.class public LTest;
.super Ljava/lang/Object;
.method public run(J)V
    .registers 4
    if-eqz v0, :done
    goto :done
    :done
    return-void
.end method'''
        decoded = '''.class public LTest;
.super Ljava/lang/Object;
.method public run(J)V
    .locals 1
    if-eqz v0, :cond_0
    goto :goto_0
    :cond_0
    :goto_0
    return-void
.end method'''
        self.assertEqual(kit.normalized_smali(source, {}), kit.normalized_smali(decoded, {}))

    def test_method_reordering_preserves_contract_but_changed_body_does_not(self):
        a = '.method public a()V\n.locals 0\nreturn-void\n.end method\n'
        b = '.method public b()V\n.locals 0\nreturn-void\n.end method\n'
        self.assertEqual(kit.normalized_smali(a + b, {}), kit.normalized_smali(b + a, {}))
        self.assertNotEqual(kit.normalized_smali(a + b, {}),
                            kit.normalized_smali(a + b.replace('return-void', 'throw p0'), {}))


class PackagingTest(unittest.TestCase):
    def test_variant_selection_defaults_to_open_and_gated_replaces_only_gate_classes(self):
        with tempfile.TemporaryDirectory() as t:
            payload = Path(t)
            for name in kit.GATE_VARIANT_FILES:
                for folder, data in [('smali', b'always-enabled'), ('gated-smali', b'gated')]:
                    path = payload / folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            unrelated = payload / 'smali/Other.smali'
            unrelated.write_bytes(b'unchanged feature code')
            kit.select_variant(payload)
            for name in kit.GATE_VARIANT_FILES:
                self.assertEqual(b'always-enabled', (payload / 'smali' / name).read_bytes())
            kit.select_variant(payload, gated=True)
            for name in kit.GATE_VARIANT_FILES:
                self.assertEqual(b'gated', (payload / 'smali' / name).read_bytes())
            self.assertEqual(b'unchanged feature code', unrelated.read_bytes())

    def test_missing_variant_is_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(kit.Incompatible):
                kit.select_variant(Path(t), gated=True)

    def test_payload_extracts_resources_and_smali_only_in_temporary_storage(self):
        with tempfile.TemporaryDirectory() as t:
            bundle = Path(t) / 'kit'
            bundle.mkdir()
            files = {'res/values/strings.xml': b'<resources/>',
                     'gated-smali/com/example/Gate.smali': b'.class public Lcom/example/Gate;',
                     'smali/com/example/Screen.smali': b'.class public Lcom/example/Screen;'}
            with zipfile.ZipFile(bundle / 'payload.zip', 'w') as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            metadata = {'payload_files': {name: kit.sha(data) for name, data in files.items()}}
            with tempfile.TemporaryDirectory(dir=t) as scratch:
                dest = Path(scratch) / 'payload'
                kit.extract_payload(bundle, dest, metadata)
                for name, data in files.items():
                    self.assertEqual(data, (dest / name).read_bytes())
                self.assertFalse((bundle / 'payload').exists())
            self.assertFalse(dest.exists())
            self.assertTrue((bundle / 'payload.zip').is_file())

    def test_payload_rejects_paths_outside_scratch_and_symlinks(self):
        paths = ['../escaped', '/absolute', 'res/../../escaped',
                 'res\\..\\escaped', 'res/C:/escaped', 'res/link']
        for name in paths:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as t:
                bundle = Path(t)
                item = zipfile.ZipInfo(name)
                if name == 'res/link':
                    item.create_system = 3
                    item.external_attr = 0o120777 << 16
                with zipfile.ZipFile(bundle / 'payload.zip', 'w') as archive:
                    archive.writestr(item, b'outside')
                with self.assertRaises(kit.Incompatible):
                    kit.extract_payload(bundle, bundle / 'unpacked',
                                        {'payload_files': {name: kit.sha(b'outside')}})
                self.assertFalse((bundle / 'unpacked').exists())

    def test_payload_rejects_changed_missing_extra_and_duplicate_files(self):
        name, content = 'res/values/strings.xml', b'<resources/>'
        cases = [[(name, b'changed')], [], [(name, content), ('res/extra.xml', b'extra')],
                 [(name, content), (name, content)]]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as t:
                bundle = Path(t)
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    with zipfile.ZipFile(bundle / 'payload.zip', 'w') as archive:
                        for path, data in entries:
                            archive.writestr(path, data)
                with self.assertRaises(kit.Incompatible):
                    kit.extract_payload(bundle, bundle / 'unpacked',
                                        {'payload_files': {name: kit.sha(content)}})

    def test_original_dexes_preserved_and_numbered_after_overlay(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with zipfile.ZipFile(p / 'original.apk', 'w') as z:
                z.writestr('classes.dex', b'original one')
                z.writestr('classes2.dex', b'original two')
            with zipfile.ZipFile(p / 'built.apk', 'w') as z:
                z.writestr('classes.dex', b'overlay')
                z.writestr('resources.arsc', b'resources')
                z.writestr('META-INF/CERT.RSA', b'old signature')
                z.writestr('assets/dexopt/baseline.prof', b'old profile')
            dex, count = kit.pack_apk(p / 'built.apk', p / 'original.apk', p / 'out.apk')
            self.assertEqual(b'overlay', dex)
            self.assertEqual(2, count)
            with zipfile.ZipFile(p / 'out.apk') as z:
                self.assertEqual(b'original one', z.read('classes2.dex'))
                self.assertEqual(b'original two', z.read('classes3.dex'))
                self.assertNotIn('META-INF/CERT.RSA', z.namelist())
                self.assertNotIn('assets/dexopt/baseline.prof', z.namelist())

    def test_missing_dex_number_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            for name, items in [('original.apk', ['classes.dex', 'classes3.dex']), ('built.apk', ['classes.dex'])]:
                with zipfile.ZipFile(p / name, 'w') as z:
                    for item in items:
                        z.writestr(item, b'dex')
            with self.assertRaises(kit.Incompatible):
                kit.pack_apk(p / 'built.apk', p / 'original.apk', p / 'out.apk')

    def test_manifest_keeps_target_attributes_and_existing_components(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            base = ET.fromstring('''<manifest xmlns:android="http://schemas.android.com/apk/res/android"
                package="com.android.mms" android:versionCode="12" android:versionName="vendor">
                <application android:name="com.android.mms.MmsApp" android:label="Vendor">
                <activity android:name="VendorScreen" /></application></manifest>''')
            p.joinpath('manifest-additions.xml').write_text('''<manifest xmlns:android="http://schemas.android.com/apk/res/android">
                <uses-permission android:name="PERMISSION" /><application>
                <receiver android:name="TzibReceiver" android:exported="true" />
                </application></manifest>''')
            kit.merge_manifest(base, p, None)
            self.assertEqual('13', base.get(kit.A + 'versionCode'))
            self.assertEqual('Vendor', base.find('application').get(kit.A + 'label'))
            self.assertEqual('VendorScreen', base.find('application/activity').get(kit.A + 'name'))
            self.assertEqual('TzibReceiver', base.find('application/receiver').get(kit.A + 'name'))

    def test_modified_payload_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            (p / 'classes.dex').write_bytes(b'altered')
            with self.assertRaises(kit.Incompatible):
                kit.verify_bundle(p, {'files': {'classes.dex': kit.sha(b'original')}})


if __name__ == '__main__':
    unittest.main()
