"""OTA reboot arms the installed Linux slot, retaining the saved default."""
from pathlib import Path
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

from backend import ota_engine
from boot import arm_next_linux


def control(slot):
    raw=bytearray(32);raw[:2]=('_'+slot).encode();raw[4:8]=b'BCAB';raw[8]=1;raw[9]=2
    raw[12:16]=bytes((0x2f,0,0x9e,0)) if slot=='a' else bytes((0x9e,0,0x2f,0))
    struct.pack_into('<I',raw,28,zlib.crc32(raw[:28]))
    return bytes(raw)


class RebootSelection(unittest.TestCase):
    def fixture(self,folder,family,slot):
        root=Path(folder).resolve();android='b' if slot=='a' else 'a'
        for name in ('run/e5linux','run/mu300','dev/block/by-name','etc/'+family,'etc/e5linux'):
            (root/name).mkdir(parents=True,exist_ok=True)
        (root/('etc/'+family+'/linux-slot')).write_text(slot+'\n')
        (root/('etc/'+family+'/android-slot')).write_text(android+'\n')
        template=root/('run/e5linux/misc-bc-slot-b-trial.bin' if family=='e5' else 'run/mu300/misc-bc-linux-trial.bin')
        template.write_bytes(control(slot))
        defaults=root/('etc/e5linux/default-boot' if family=='e5' else 'etc/mu300/default-boot')
        defaults.write_bytes(b'android\n')
        misc=root/'dev/block/by-name/misc';misc.write_bytes(b'x'*2048+control(android)+b'y'*2048)
        return root,misc,template,defaults

    def test_both_families_and_reverse_slots_preserve_misc_prefix_and_saved_default(self):
        for family in ('e5','mu300'):
            for slot in ('a','b'):
                with self.subTest(family=family,slot=slot),tempfile.TemporaryDirectory() as d:
                    root,misc,template,defaults=self.fixture(d,family,slot)
                    before=misc.read_bytes()
                    self.assertEqual(arm_next_linux(root,family),{'slot':slot,'changed':True})
                    self.assertEqual(misc.read_bytes(),before[:2048]+template.read_bytes()+before[2080:])
                    self.assertEqual(defaults.read_bytes(),b'android\n')
                    self.assertEqual(arm_next_linux(root,family),{'slot':slot,'changed':False})

    def test_invalid_crc_slot_conflict_and_unbootable_template_do_not_write_misc(self):
        for bad in ('crc','version','unbootable','slot','live-crc'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as d:
                root,misc,template,_=self.fixture(d,'e5','a')
                raw=bytearray(template.read_bytes())
                if bad=='crc':raw[28]^=1
                elif bad=='version':raw[8]=2
                elif bad=='unbootable':raw[12]=0x1f
                elif bad=='slot':(root/'etc/e5/android-slot').write_text('a\n')
                else:
                    live=bytearray(misc.read_bytes());live[2080-1]^=1;misc.write_bytes(live)
                if bad not in ('crc','slot','live-crc'):struct.pack_into('<I',raw,28,zlib.crc32(raw[:28]))
                template.write_bytes(raw);before=misc.read_bytes()
                with self.assertRaises(ValueError):arm_next_linux(root,'e5')
                self.assertEqual(misc.read_bytes(),before)

    def test_partial_write_and_bad_readback_restore_original_control(self):
        for fault in ('short-write','readback','fsync'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as d:
                root,misc,_,_=self.fixture(d,'mu300','b')
                before=misc.read_bytes();original=Path.open
                class FailingStream:
                    def __init__(self,stream):self.stream=stream;self.reads=0;self.writes=0
                    def __enter__(self):return self
                    def __exit__(self,*args):self.stream.close()
                    def __getattr__(self,name):return getattr(self.stream,name)
                    def read(self,size):
                        data=self.stream.read(size);self.reads+=1
                        return b'bad readback' if fault=='readback' and self.reads==2 else data
                    def write(self,data):
                        self.writes+=1
                        return self.stream.write(data[:8] if fault=='short-write' and self.writes==1 else data)
                def opened(path,*args,**kwargs):
                    stream=original(path,*args,**kwargs)
                    return FailingStream(stream) if path==misc and args[:1]==('r+b',) else stream
                with patch.object(Path,'open',opened), patch('boot.os.fsync',side_effect=[OSError('sync failed'),None] if fault=='fsync' else None):
                    with self.assertRaises(OSError):arm_next_linux(root,'mu300')
                self.assertEqual(misc.read_bytes(),before)


if __name__=='__main__':unittest.main()
