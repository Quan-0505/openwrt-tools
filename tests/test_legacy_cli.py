"""Compatibility CLI uses shared policy/resources, with no target connection."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.legacy_cli import bind
from backend.model_compat import confirm_mu300_cli


class LegacyCli(unittest.TestCase):
    def test_mu300_unknown_model_accepts_only_exact_yes(self):
        for answer in ('no','YES',' yes','yes '):
            with self.subTest(answer=answer),patch('builtins.input',return_value=answer),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):confirm_mu300_cli('Compatible device\ncustom-board','ums9620')
        with patch('builtins.input',return_value='yes'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(confirm_mu300_cli('Compatible device\ncustom-board','ums9620'),'mu300-family')
        with patch('builtins.input',side_effect=AssertionError('known model prompted')):
            self.assertEqual(confirm_mu300_cli('V50\nMU3351','ums9620'),'v50')

    def test_both_cli_families_use_the_shared_storage_without_a_generic_frozen_file(self):
        for family in ('e5','mu300'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();module=SimpleNamespace(GUI='old', install=Mock())
                if family=='mu300':
                    (root/'desktop').mkdir();(root/'desktop/manifest.json').write_text('{}')
                else:
                    (root/'files').mkdir();(root/'files/device-profile.json').write_text(json.dumps({'profile':'custom','android_models':[]}))
                self.assertEqual(bind(module,root),family)
                self.assertIsNone(module.GUI)
                script=module._storage.library(root) if family=='mu300' else module.storage_api()['library']()
                self.assertIn(family+'_storage_identity()',script)
                if family=='e5':self.assertEqual(module.default_hotspot_name(),'custom-OpenWrt')

    def test_real_cli_entry_rebinds_an_older_package_before_running_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'desktop').mkdir();(root/'desktop/manifest.json').write_text('{}')
            (root/'OpenWrt-Installer.py').write_text("def install(*args): pass\ndef android_profile(*args): raise AssertionError('old model list executed')\ndef run_cli(args):\n print('CLI-OK:'+android_profile('Compatible\\nboard','ums9620'))\n return 0\n")
            result=subprocess.run([sys.executable,str(ROOT/'backend_entry.py'),'--cli','--package-root',str(root)],input='yes\n',capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('CLI-OK:mu300-family',result.stdout)

    def test_cli_boot_uses_shared_repair_and_nonboot_operations_stay_native(self):
        for family in ('e5','mu300'):
            with self.subTest(family=family),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve();module=SimpleNamespace(install=Mock())
                if family=='mu300':
                    (root/'desktop').mkdir();(root/'desktop/manifest.json').write_text('{}')
                else:
                    (root/'files').mkdir();(root/'files/device-profile.json').write_text(json.dumps({'profile':'e5'}))
                native=module.install
                with patch('backend.legacy_cli.boot_once',return_value='boot-result') as boot:
                    bind(module,root)
                    args=SimpleNamespace(boot_openwrt=True)
                    result=module.install(args,root,{}) if family=='mu300' else module.boot_openwrt(args)
                    self.assertEqual(result,'boot-result');boot.assert_called_once_with(root,args)
                    if family=='mu300':
                        other=SimpleNamespace(boot_openwrt=False)
                        module.install(other,root,{})
                        native.assert_called_once_with(other,root,{})


if __name__=='__main__':unittest.main()
