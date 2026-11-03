import sys
"""Native boot transactions on ordinary files; no connected device access."""
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch as mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from backend.ota_engine import Engine, Patch, descriptor, canonical
from backend.native_boot import codec
from backend.model_compat import mu300_android, mu300_runtime
from test_ota import file, manifest, package

boot = codec()


def framed(payload, size=1 << 20):
    payload += bytes(-len(payload) % 4096)
    metadata = b'AVB0'+bytes(252)
    footer = struct.pack('>4sIIQQQ28s',b'AVBf',1,0,len(payload),len(payload),len(metadata),bytes(28))
    return payload+metadata+bytes(size-len(payload)-len(metadata)-64)+footer


def image(kernel=b'KERNEL',ram=b'RAM',size=1 << 20):
    header=bytearray(4096);header[:8]=b'ANDROID!'
    struct.pack_into('<II',header,8,len(kernel),len(ram))
    struct.pack_into('<I',header,20,1584);struct.pack_into('<I',header,40,4)
    header[44:55]=b'loglevel=5\0'
    return framed(bytes(header)+kernel+bytes(-len(kernel)%4096)+ram,size)


def vendor(ram=b'OLD',dtb=b'THIS-UNIT-DTB',config=b'actual-bootconfig'):
    header=bytearray(4096);header[:8]=b'VNDRBOOT'
    struct.pack_into('<II',header,8,4,4096);struct.pack_into('<I',header,24,len(ram))
    struct.pack_into('<I',header,2100,len(dtb));struct.pack_into('<4I',header,2112,108,1,108,len(config))
    table=struct.pack('<III',len(ram),0,1)+bytes(96)
    return framed(bytes(header)+ram+bytes(-len(ram)%4096)+dtb+bytes(-len(dtb)%4096)+table+bytes(-len(table)%4096)+config)


def native_init(android='b',text=b''):
    content=(b'#!/bin/sh\n# U30PRO-MU300 storage bootstrap\n'+text+
             b"\nlog 'stage=switch-root platform=u30pro'\nexec switch_root /newroot /sbin/init\n")
    return boot.overlay({'init':(content,stat.S_IFREG|0o755),
        'etc/mu300-android-slot':((android+'\n').encode(),stat.S_IFREG|0o644),
        'etc/mu300-desktop-root-mode':(b'part\n',stat.S_IFREG|0o644)})


class NativeCodec(unittest.TestCase):
    def test_shared_codec_preserves_device_dtb_config_and_empties_android_init(self):
        templates={'boot':image(),'vendor_boot':vendor(),'init_boot':image(b'',b'ANDROID')}
        actual=boot.native_images(image(b'NEW',b'NEW-RAM'),templates)
        self.assertEqual(boot.native_boot_parts(actual['boot']),(b'NEW',b'NEW-RAM'))
        self.assertEqual(boot.native_vendor_parts(actual['vendor_boot'])[1:3],(b'THIS-UNIT-DTB',b'actual-bootconfig'))
        self.assertEqual(boot.native_vendor_parts(actual['vendor_boot'])[0],b'NEW-RAM')
        self.assertEqual(list(boot.entries(next(boot.archives(boot.native_boot_parts(actual['init_boot'])[1])))),[])

    def test_u30pro_model_identity_is_package_specific_and_unknown_still_warns(self):
        self.assertEqual(mu300_android('MU5358','MU5358','ums9632','u30pro'),('u30pro',None))
        self.assertIsNotNone(mu300_android('MU5358','MU5358','ums9632')[1])
        self.assertIsNotNone(mu300_android('unknown','variant','ums9632','u30pro')[1])
        self.assertIsNone(mu300_runtime('u30pro','u30pro'))
        self.assertIsNotNone(mu300_runtime('u30pro'))
        self.assertIsNone(mu300_runtime('f50'))

    def test_binder_preserves_native_slot_and_places_recovery_before_switch_root(self):
        current=image(b'CURRENT',native_init('b'),64 << 20)
        incoming=image(b'NEW',native_init('a',b'# new init'),64 << 20)
        bound=boot.bind(incoming,current,'mu300')
        fields=boot.metadata(bound,'mu300',lambda n:n in ('init','etc/mu300-android-slot'))
        self.assertEqual(fields['etc/mu300-android-slot'][0],b'b\n')
        init=fields['init'][0].decode()
        self.assertLess(init.index('# unisoc-ota-recovery-v2'),init.index("log 'stage=switch-root platform=u30pro'"))
        self.assertIn('mount --bind /dev "$ota_ram/target/dev"',init)

    def test_malformed_footer_fragment_table_and_overflow_are_rejected(self):
        templates={'boot':image(),'vendor_boot':vendor(),'init_boot':image(b'',b'ANDROID')}
        for problem in ('footer','table','extent'):
            bad=bytearray(templates['vendor_boot'])
            if problem=='footer':bad[-64:-60]=b'BAD!'
            elif problem=='table':struct.pack_into('<I',bad,2116,2)
            else:struct.pack_into('<I',bad,24,1 << 30)
            with self.subTest(problem=problem),self.assertRaises(ValueError):
                boot.native_images(image(),dict(templates,vendor_boot=bad))


class NativeInstaller(unittest.TestCase):
    def test_full_native_kernel_release_uses_the_actual_series(self):
        from backend.mu300_update import kernel_series
        for value,wanted in (('7.2','7.2.'),('6.18','6.18.'),('6.12.94-u30pro-p1','6.12.'),('6.12.94+native','6.12.')):
            self.assertEqual(kernel_series(value),wanted)
        with self.assertRaises(ValueError):kernel_series('7.2; command')

    def test_read_only_route_proves_matching_ramdisks_and_rejects_mixed_cohorts(self):
        import re
        canonical=image(b'NEW',native_init())
        parts=boot.native_images(canonical,{'boot':image(),'vendor_boot':vendor(),'init_boot':image(b'',b'ANDROID')})
        def region(command):
            name=re.search(r'by-name/(boot|vendor_boot|init_boot)_[ab]',command)[1]
            first=int(re.search(r'skip=(\d+)',command)[1]);count=int(re.search(r'count=(\d+)',command)[1])
            return parts[name][first*4096:(first+count)*4096]
        def shell(command):
            name=re.search(r'by-name/(boot|vendor_boot|init_boot)_[ab]',command)[1]
            if command.startswith('blockdev'):return str(len(parts[name]))
            length=int(re.search(r'head -c (\d+)',command)[1])
            return hashlib.sha256(region(command)[:length]).hexdigest()+'  -'
        for slot in ('a','b'):
            self.assertEqual(boot.native_inspect_route(region,shell,slot)['slot'],slot)
        bad=bytearray(parts['vendor_boot']);bad[4096]^=1;parts['vendor_boot']=bad
        with self.assertRaisesRegex(ValueError,'inconsistent'):boot.native_inspect_route(region,shell,'b')

    def test_ssh_preparation_is_binary_checked_and_preserves_actual_dtb(self):
        from backend.native_boot import prepare_ssh
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);canonical=folder/'boot.img';canonical.write_bytes(image(b'NEW',native_init()))
            originals={'vendor_boot':vendor(),'init_boot':image(b'',b'ANDROID')}
            def info(client,command):
                name='vendor_boot' if 'vendor_boot' in command else 'init_boot'
                return str(len(originals[name])//512)+'\n'+hashlib.sha256(originals[name]).hexdigest()+'  partition'
            def read(client,command,count):
                self.assertNotIn('of=',command)
                name='vendor_boot' if 'vendor_boot' in command else 'init_boot'
                self.assertEqual(count,len(originals[name]));return originals[name]
            outputs,manifest=prepare_ssh(None,info,read,canonical,'a',folder)
            self.assertEqual(manifest.read_text().splitlines()[0],'U30PRO-BOOT-V1 a')
            self.assertEqual(boot.native_vendor_parts(outputs['vendor_boot'].read_bytes())[1],b'THIS-UNIT-DTB')
            self.assertNotIn('boot',outputs)
            with self.assertRaisesRegex(ValueError,'readback'):
                prepare_ssh(None,info,lambda *args:b'corrupt',canonical,'b',folder)

    def test_native_uninstall_uses_storage_before_root_and_keeps_maintenance_flag(self):
        from backend.uninstall_boot import build,archives,entries
        init=b"#!/bin/sh\n# U30PRO-MU300 storage bootstrap\nlog 'stage=wait-storage'\nlog 'stage=storage-ready'\nDO_NOT_MOUNT_ROOT\n"
        ram=boot.overlay({'init':(init,stat.S_IFREG|0o755)})
        source=image(b'KERNEL',ram,64 << 20)
        for slot in ('a','b'):
            updated=build(source,'mu300',{},android_slot=slot)
            members={name:data for archive in archives(boot.native_boot_parts(updated)[1]) for name,data,mode in entries(archive)}
            self.assertNotIn(b'DO_NOT_MOUNT_ROOT',members['init'])
            self.assertIn(b'openwrt-uninstall/worker.sh',members['init'])
            self.assertEqual(members['etc/mu300-android-slot'],(slot+'\n').encode())
            templates={'boot':image(),'vendor_boot':vendor(),'init_boot':image(b'',b'ANDROID')}
            result=boot.native_images(updated,templates)
            self.assertIn(b'openwrt.uninstall=1',result['boot'][44:1580])
            self.assertEqual(boot.native_boot_parts(result['boot'])[1],boot.native_vendor_parts(result['vendor_boot'])[0])


@unittest.skipUnless(sys.platform=='linux','Linux xattr durable transaction engine')
class NativeOTA(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for folder in ('dev/block/by-name','etc/mu300','usr/lib','run/mu300'):(self.root/folder).mkdir(parents=True,exist_ok=True)
        (self.root/'etc/mu300/platform').write_text('u30pro\n')
        (self.root/'run/mu300/linux-slot').write_text('a\n')
        (self.root/'run/mu300/android-slot').write_text('b\n')
        (self.root/'etc/mu300/release.json').write_text(json.dumps(dict(profile='u30pro',image_version='u30pro-r1',kernel_release='6.12.94-u30pro-p1')))
        self.parts=self.root/'dev/block/by-name'
        current=image(b'OLD',native_init('b'),64 << 20)
        native=boot.native_images(current,{'boot':current,'vendor_boot':vendor(),'init_boot':image(b'',b'ANDROID')})
        for name,raw in native.items():
            (self.parts/(name+'_a')).write_bytes(raw)
            (self.parts/(name+'_b')).write_bytes(b'ANDROID-'+name.encode())
        self.before={p.name:boot.native_hash(p) for p in self.parts.iterdir()}
        (self.root/'usr/lib/test').write_bytes(b'old')
        (self.root/'usr/lib/user-plugin').write_bytes(b'keep')
        self.new=image(b'NEW',native_init('a',b'# latest init'),64 << 20)
        m=manifest();m.update(family='mu300',profile='u30pro',base_version='u30pro-r1',target_version='u30pro-r2',kernel_release='6.12.94-u30pro-p1')
        m['operations'][0]['before']=descriptor(self.root/'usr/lib/test')
        m['operations'][0]['after'].update(uid=os.getuid(),gid=os.getgid())
        h=hashlib.sha256(self.new).hexdigest();m['boot']={'sha256':h,'size':len(self.new)}
        self.patch=package(self.root/'update.ota',m,{h:self.new,hashlib.sha256(b'new').hexdigest():b'new'})
        self.engine=Engine(self.root)
        self.ram=self.root/'runtime';self.ram.mkdir()

    def apply(self):
        with mock.dict(os.environ,UNISOC_OTA_LIVE='1',UNISOC_OTA_RAM=str(self.ram)),Patch(self.patch) as patch:
            return self.engine.apply(patch)

    def test_update_then_undo_restores_all_three_and_preserves_plugins_android(self):
        self.assertEqual(self.apply()['status'],'complete')
        applied=(self.parts/'boot_a').read_bytes();ram=boot.native_boot_parts(applied)[1]
        self.assertEqual(boot.native_vendor_parts((self.parts/'vendor_boot_a').read_bytes())[0],ram)
        self.assertEqual(boot.native_boot_parts(applied)[0],b'NEW')
        self.assertEqual(boot.metadata(applied,'mu300',lambda n:n=='etc/mu300-android-slot')['etc/mu300-android-slot'][0],b'b\n')
        self.assertTrue(self.engine.rollback_check()['available'])
        journal=self.engine.state/'journal.json';value=json.loads(journal.read_text());value['status']='rolling_back';journal.write_bytes(canonical(value))
        self.engine.recover()
        self.assertEqual({p.name:boot.native_hash(p) for p in self.parts.iterdir()},self.before)
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'old')
        self.assertEqual((self.root/'usr/lib/user-plugin').read_bytes(),b'keep')

    def test_partial_companion_write_restores_bootset_and_root(self):
        original=boot.native_write_file;failed=False
        def fault(device,image,expected):
            nonlocal failed
            if not failed and device.name=='vendor_boot_a':
                failed=True
                with device.open('r+b') as stream:stream.write(b'PARTIAL')
                raise OSError('injected write interruption')
            return original(device,image,expected)
        # Engine imports the deployed boot module; the desktop codec is the
        # same source under a private import name for host resource resolution.
        import boot as live
        with mock.object(live,'native_write_file',fault),self.assertRaises(OSError):self.apply()
        self.assertEqual({p.name:boot.native_hash(p) for p in self.parts.iterdir()},self.before)
        self.assertEqual((self.root/'usr/lib/test').read_bytes(),b'old')

    def test_editing_a_companion_after_update_blocks_undo(self):
        self.apply()
        with (self.parts/'vendor_boot_a').open('r+b') as stream:stream.write(b'user changed this')
        result=self.engine.rollback_check()
        self.assertFalse(result['available'])
        self.assertTrue(any('vendor_boot_a' in row['path'] for row in result['conflicts']))

    def test_reverse_android_a_linux_b_uses_the_same_transaction(self):
        android={name:(self.parts/(name+'_b')).read_bytes() for name in boot.NATIVE_IMAGES}
        current=image(b'OLD',native_init('a'),64 << 20)
        native=boot.native_images(current,{'boot':current,'vendor_boot':vendor(),'init_boot':image(b'',b'ANDROID')})
        for name,raw in native.items():
            (self.parts/(name+'_a')).write_bytes(android[name])
            (self.parts/(name+'_b')).write_bytes(raw)
        (self.root/'run/mu300/linux-slot').write_text('b\n')
        (self.root/'run/mu300/android-slot').write_text('a\n')
        self.assertEqual(self.apply()['status'],'complete')
        for name,raw in android.items():self.assertEqual((self.parts/(name+'_a')).read_bytes(),raw)
        updated=(self.parts/'boot_b').read_bytes()
        self.assertEqual(boot.metadata(updated,'mu300',lambda n:n=='etc/mu300-android-slot')['etc/mu300-android-slot'][0],b'a\n')
        self.assertEqual(boot.native_boot_parts(updated)[1],boot.native_vendor_parts((self.parts/'vendor_boot_b').read_bytes())[0])

    def test_space_inspection_never_memory_maps_block_templates(self):
        with mock('mmap.mmap',side_effect=AssertionError('Block devices must not be mapped by st_size')):
            self.assertEqual(boot.native_scratch_bytes(self.root,'mu300',True),6 << 20)

    def test_file_only_guard_reuses_companion_files_and_matches_space_budget(self):
        backup=self.root/'scratch';backup.mkdir()
        guard=self.root/'guard.img';guard.write_bytes(self.new)
        rows=boot.native_prepare(self.root,'mu300',backup,guard,guard)
        self.assertFalse((backup/'native-final').exists())
        for row in rows:
            self.assertEqual(row['guard'],row['final'])
            boot.native_row_paths(self.root,backup,row,'final')
        self.assertEqual(sum(p.stat().st_size for p in backup.rglob('*') if p.is_file()),
                         boot.native_scratch_bytes(self.root,'mu300',False))


if __name__=='__main__':unittest.main(verbosity=2)
