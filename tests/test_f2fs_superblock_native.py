import sys
"""Decode ordinary in-memory files with the real native helper; no device ioctl."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from backend.online_shrink import superblocks
from tests.test_online_shrink import sb, checksummed

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('cc'), 'Linux kernel headers and C compiler required')
class NativeSuperblocks(unittest.TestCase):
    def test_native_and_host_decode_agree(self):
        with tempfile.TemporaryDirectory(prefix='f2fs-superblocks-') as directory:
            source, binary = Path(directory)/'decode.c', Path(directory)/'decode'
            source.write_text('#define main helper_main\n#include "f2fs-online.c"\n#undef main\n'
                'int main(void) { unsigned char raw[8192]; struct geometry g; '
                'if (fread(raw, 1, sizeof(raw), stdin) != sizeof(raw)) return 2; '
                'if (decode(raw, &g)) return 1; '
                'printf("%" PRIu64 " %" PRIu64 "\\n", g.blocks, g.section_blocks); return 0; }\n')
            subprocess.run(['cc','-O2','-Wall','-Wextra','-Werror','-I',str(ROOT/'device'),
                            str(source),'-o',str(binary)],check=True)
            versions=bytearray(sb())
            versions[1024+0x684:1024+0x684+3]=b'new'
            versions[5120+0x684:5120+0x684+3]=b'old'
            fixtures=[sb(),versions,checksummed(sb()),checksummed(versions),
                      checksummed(versions,checksum_offset=3064)]
            for original in (bytearray(sb()),checksummed(versions)):
                for copy in (1024,5120):
                    for offset in (0,16,24,36,108,0x684,0x783,0x784,0x884,3068,3071):
                        bad=original.copy();bad[copy+offset]^=0x5a;fixtures.append(bad)
            for index, raw in enumerate(fixtures):
                with self.subTest(fixture=index):
                    result=subprocess.run([str(binary)],input=raw,capture_output=True,timeout=5)
                    try: geometry=superblocks(raw)
                    except ValueError:
                        self.assertEqual(result.returncode,1,result.stderr)
                    else:
                        self.assertEqual(result.returncode,0,result.stderr)
                        self.assertEqual(result.stdout.decode().strip(),
                                         f"{geometry['blocks']} {geometry['section_blocks']}")

    @unittest.skipUnless(shutil.which('aarch64-linux-gnu-gcc') and shutil.which('docker'),
                         'Existing ARM64 compiler and Docker runtime required')
    def test_actual_arm64_unit_binary(self):
        image='mu300-openwrt-base:25.12.5'
        subprocess.run(['docker','image','inspect',image],check=True,stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory(prefix='f2fs-superblocks-arm64-') as directory:
            binary=Path(directory)/'test'
            subprocess.run(['aarch64-linux-gnu-gcc','-static','-O2','-Wall','-Wextra','-Werror',
                            str(ROOT/'device/test-f2fs-online.c'),'-o',str(binary)],check=True)
            subprocess.run(['docker','run','--rm','--pull=never','--network=none',
                            '--platform=linux/arm64','-v',f'{binary}:/test:ro',
                            '--entrypoint','/test',image],check=True,timeout=45)


if __name__ == '__main__':
    unittest.main()
