"""Install the pinned read-only chat tools into an existing Giftia profile."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

from stage1 import HARNESS_VERSION, Lab, PROFILE, SOURCE

BUNDLE_DIR = SOURCE / 'chat-tool-bundle'
MANIFEST = json.loads((BUNDLE_DIR / 'package.json').read_text(encoding='utf-8'))
PACKAGE = MANIFEST['name']
MARKER = '- id: giftia-chat-tools\n'


def package_tools(lab):
    lab.pack_dir.mkdir(parents=True, exist_ok=True)
    archive = lab.pack_dir / f'{PACKAGE}-{MANIFEST["version"]}.tgz'
    subprocess.run([shutil.which('npm'), 'pack', '--ignore-scripts', '--pack-destination', str(lab.pack_dir)],
                   cwd=BUNDLE_DIR, env=lab.env, check=True)
    with tarfile.open(archive, 'r:gz') as bundle:
        if set(bundle.getnames()) != {'package/package.json', 'package/index.js', 'package/cordis.patch.yml'}:
            raise ValueError('Unexpected packaged chat tool files')
        manifest = json.load(bundle.extractfile('package/package.json'))
        if manifest['engines']['dsh'] != HARNESS_VERSION:
            raise ValueError('Chat tools must pin the deployed Harness version')
    return archive


def install(lab):
    config = lab.profile / 'cordis.patch.yml'
    prior = config.read_text(encoding='utf-8')
    if MARKER in prior:
        raise ValueError('The profile already has chat tool permissions')
    archive = package_tools(lab)
    lab.command('plugin', '--profile', PROFILE, 'add', str(archive))
    config.write_text(prior.rstrip() + '\n\n' + MARKER +
                      '  config:\n    permissions:\n      partner: true\n      guest: false\n', encoding='utf-8')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    print(install(Lab(parser.parse_args().root)))
