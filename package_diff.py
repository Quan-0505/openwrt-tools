#!/usr/bin/env python3
"""Attach Diff Studio entry points while sharing the installer runtimes.

Native binaries are already fingerprinted by installer/package_gui.py. Renaming
the macOS bundle directory selects diff.html without changing signed contents.
Only Diff Studio's files enter diff-tool.json; no firmware/ADB/installer payload
is included when the tool subsequently exports a patch updater.
"""
import hashlib
import copy
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import zipfile

TOP=Path(__file__).resolve().parent
SUPPORT=('__init__.py','resources.py','host.py','ssh_runtime.py','mu300_update.py','native_boot.py','ota_engine.py','ota_package.py','release_index.py','ota_desktop.py',
         'ota_bundle.py','ota_runtime.py','boot.py','rpc.py','proxy.sh','lz4raw-aarch64.so',
         'errors.js','unisoc-ota.js','unisoc-ota.json')


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1<<20),b''):h.update(block)
    return h.hexdigest()


def export_standalone(release, output):
    """Export only the verified tool files, retaining signed app contents/modes."""
    output=Path(output)
    if output.exists():
        raise ValueError('Select a new output ZIP; existing packages are not overwritten')
    with zipfile.ZipFile(release) as source:
        if len(source.namelist())!=len(set(source.namelist())):
            raise ValueError('Duplicate archive entries')
        manifest=json.loads(source.read('diff-tool.json'))
        files=manifest.get('files',{})
        required={'OpenWrt-Diff-Studio.exe','OpenWrt-Diff-Studio.py',
                  'OpenWrt-Diff-Studio.app/Contents/MacOS/openwrt-installer',
                  'tools/backend/openwrt-backend.exe','tools/macos/backend/openwrt-backend'}
        if manifest.get('protocol')!=1 or not required.issubset(files):
            raise ValueError('Release does not contain a complete Diff Studio')
        for name,wanted in files.items():
            path=PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or '\\' in name or
                (name not in required and not name.startswith(('backend/','tools/backend/',
                 'tools/macos/backend/','OpenWrt-Diff-Studio.app/')))):
                raise ValueError('Unexpected Diff Studio file: '+name)
            with source.open(name) as stream:
                hashed=hashlib.sha256()
                for block in iter(lambda:stream.read(1<<20),b''):hashed.update(block)
                if hashed.hexdigest()!=wanted:
                    raise ValueError('Diff Studio checksum mismatch: '+name)
        guide=source.read('docs/DIFF-STUDIO.md').decode('utf-8')
        output.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as target:
            for name in sorted(files):
                with source.open(name) as src, target.open(copy.copy(source.getinfo(name)),'w',force_zip64=True) as dst:
                    shutil.copyfileobj(src,dst,1<<20)
            target.writestr('diff-tool.json',source.read('diff-tool.json'))
            target.writestr('START-HERE.md',guide.replace('完整解压固件 ZIP','完整解压本工具 ZIP'))
    with zipfile.ZipFile(output) as result:
        if result.testzip():
            raise ValueError('Standalone ZIP verification failed')
    checksum=digest(output)
    Path(str(output)+'.sha256').write_text(checksum+'  '+output.name+'\n',encoding='utf-8')
    print(str(output)+'\nSHA256 '+checksum)
    return manifest


def attach(package, *, include_android=True):
    """Attach Diff Studio and, by default, the signed Android control app."""
    package=Path(package)
    if not include_android:
        # E5 firmware bundles deliberately omit the signed Android control app.
        # Remove stale copies when refreshing a package in place; the distinct
        # Android system-switcher under extras/ is not owned by this package.
        for name in ('OpenWrt-Tools.apk','docs/GUI-BUILD-android.json','docs/ANDROID-INSTALLER.md'):
            path=package/name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                raise ValueError('Android installer path is not a file: '+name)
        start=package/'START-HERE.md'
        if start.is_file():
            guide=start.read_text(encoding='utf-8')
            guide='\n'.join(line for line in guide.splitlines()
                if 'OpenWrt-Tools.apk' not in line and 'docs/ANDROID-INSTALLER.md' not in line).strip('\n')+'\n'
            start.write_text(guide,encoding='utf-8',newline='\n')
    sys.path.insert(0,str(TOP))
    from package_gui import sources
    current=sources();native={};owned=set()
    for platform,label in (('windows-x64','GUI-BUILD.json'),('macos-arm64','GUI-BUILD-macos.json')):
        build=json.loads((package/'docs'/label).read_text())
        if build.get('protocol')!=1 or build.get('platform')!=platform or build.get('source')!=current:
            raise ValueError('Diff Studio requires current native installer: '+platform)
        for name,wanted in build['files'].items():
            path=(package/name).resolve()
            if not path.is_relative_to(package.resolve()) or not path.is_file() or digest(path)!=wanted:
                raise ValueError('Diff Studio native input mismatch: '+name)
        native[platform]=build
    destinations=('OpenWrt-Diff-Studio.exe','OpenWrt-Diff-Studio.app','OpenWrt-Diff-Studio.py','diff-tool.json','backend')
    if any((package/n).exists() or (package/n).is_symlink() for n in destinations):
        raise ValueError('Diff Studio staging already exists; do not overwrite it')
    shutil.copy2(package/'OpenWrt-Installer.exe',package/'OpenWrt-Diff-Studio.exe')
    shutil.copytree(package/'OpenWrt-Installer.app',package/'OpenWrt-Diff-Studio.app')
    owned.add('OpenWrt-Diff-Studio.exe')
    owned.update(p.relative_to(package).as_posix() for p in (package/'OpenWrt-Diff-Studio.app').rglob('*') if p.is_file())
    shutil.copy2(TOP/'ota-tool.py',package/'OpenWrt-Diff-Studio.py')
    owned.add('OpenWrt-Diff-Studio.py');(package/'backend').mkdir()
    plugin=TOP/'runtime/luci-app-unisoc-ota'
    extras={'errors.js':plugin/'htdocs/luci-static/resources/unisoc-ota/errors.js',
            'unisoc-ota.js':plugin/'htdocs/luci-static/resources/view/system/unisoc-ota.js',
            'unisoc-ota.json':plugin/'root/usr/share/rpcd/acl.d/unisoc-ota.json',
            'lz4raw-aarch64.so':TOP/'ota-native/lz4raw-aarch64.so',
            'ota_engine.py':plugin/'root/usr/lib/unisoc-ota/engine.py'}
    for name in SUPPORT:
        source=extras.get(name) or (plugin/'root/usr/lib/unisoc-ota'/name if name in ('boot.py','rpc.py','proxy.sh') else TOP/'backend'/name)
        shutil.copy2(source,package/'backend'/name);owned.add('backend/'+name)
    # Both platforms use the already installed tools/ backend. Do not copy it
    # into a second tool directory, nor include unrelated ADB binaries.
    for platform,prefix in (('windows-x64','tools/backend/'),('macos-arm64','tools/macos/backend/')):
        owned.update(n for n in native[platform]['files'] if n.startswith(prefix))
    manifest={'protocol':1,'platform':'windows-x64+macos-arm64','source':current,
              'files':{n:digest(package/n) for n in sorted(owned)}}
    (package/'diff-tool.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
    guide=(TOP/'docs/DIFF-STUDIO.zh-CN.md').read_text(encoding='utf-8')
    (package/'docs/DIFF-STUDIO.md').write_text(guide,encoding='utf-8',newline='\n')
    if include_android:
        from package_android import attach as attach_android
        attach_android(package)
    start=package/'START-HERE.md'
    with start.open('a',encoding='utf-8',newline='\n') as stream:
        stream.write('\n## Diff Studio / 差异更新工具\n\n'
                     'Windows: **OpenWrt-Diff-Studio.exe**. Apple Silicon: **OpenWrt-Diff-Studio.app**.\n\n')
        if include_android:
            stream.write('Android: **OpenWrt-Tools.apk**（本目录直接安装 / install directly from this directory）. '
                         '[Android 使用说明 / guide](docs/ANDROID-INSTALLER.md).\n\n')
        stream.write('完整解压后打开对应程序；与安装器共用 tools 目录，请勿单独移动 EXE 或 APP。'
                     '选择旧、新完整包生成更新补丁；操作说明见 [DIFF-STUDIO.md](docs/DIFF-STUDIO.md)。\n')
    return manifest


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path)
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--from-release',type=Path)
    parser.add_argument('--output-zip',type=Path)
    args=parser.parse_args()
    if args.from_release or args.output_zip:
        if not args.from_release or not args.output_zip or args.package or args.prepare:
            parser.error('Use --from-release and --output-zip together without --package/--prepare')
        export_standalone(args.from_release,args.output_zip)
        sys.exit(0)
    if not args.package:
        parser.error('Supply --package or --from-release/--output-zip')
    args.package.mkdir(parents=True,exist_ok=True)
    if args.prepare:
        import runpy
        runpy.run_path(str(TOP/'package_gui.py'))['attach'](args.package)
    attach(args.package)
