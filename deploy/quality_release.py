#!/usr/bin/env python3
"""Preview/apply a quality release, preserving data and model settings. Linux/systemd only."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.request

SERVICE='giftia-qq'


def files(source):
    picked=[]
    for folder in ['giftia','adapters','tools','web','docs','deploy']:
        for path in (source/folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py','.js','.css','.html','.json','.png','.svg','.ico','.md','.toml'}:
                picked.append(path.relative_to(source))
    return sorted(set(picked+[Path('config/persona.toml'),Path('config/settings.toml'),Path('run.py'),Path('README.md')]))


def safe_path(root, relative):
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Unsafe release path')
    path=root/relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Release path leaves target directory')
    return path


def model_config(path, override):
    result=tomllib.loads(path.read_text(encoding='utf-8-sig')).get('model',{})
    if override.exists():result.update(tomllib.loads(override.read_text(encoding='utf-8-sig')).get('model',{}))
    return result


def run(*args, **kwargs):
    return subprocess.run(args,check=True,**kwargs)


def service_python(app, code, *args):
    return run('sudo','-n','-u','giftia','env','PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(app),
               'python3','-c',code,*args,cwd=app,capture_output=True,text=True)


def copy_code(source, target, relative):
    src=safe_path(source,relative);dst=safe_path(target,relative)
    existed=dst.exists()
    ancestor=dst.parent
    while not ancestor.exists():ancestor=ancestor.parent
    previous=dst.stat() if existed else ancestor.stat()
    dst.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(src,dst)
    os.chmod(dst,previous.st_mode & 0o777 if existed and relative.parts[0]=='config' else 0o644)
    os.chown(dst,previous.st_uid,previous.st_gid)


def repair_completion_mood(db_path):
    """Only correct the proved completion-word false positive, with exact prior state retained."""
    from giftia.mood import tone_of
    conn=sqlite3.connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        instance=dict(conn.execute("SELECT key,value FROM meta WHERE key='instance_no'")).get('instance_no','1')
        prefix='mood:i'+str(instance)+':'
        state=dict(conn.execute("SELECT key,value FROM state WHERE key LIKE ?", (prefix+'%',)))
        if state.get(prefix+'key')!='worried' or state.get(prefix+'source')!='his_tone':return None
        row=conn.execute("SELECT id,content FROM messages WHERE instance_no=? AND scope='private' AND role='user' AND ts=? ORDER BY id DESC LIMIT 1",
                         (int(instance),state.get(prefix+'since',''))).fetchone()
        if not row or not re.search(r'(?:吃|做|看|读|写|说|用|忙|弄|学|考|买|卖|办|洗|打|处理)完了',row[1]) or tone_of(row[1])!=0:
            return None
        now=datetime.now().isoformat(timespec='seconds')
        applied={prefix+'key':'calm',prefix+'source':'quality-repair',prefix+'intensity':'0.0',prefix+'since':now,prefix+'until':now}
        old={k:state.get(k) for k in applied}
        conn.executemany('INSERT OR REPLACE INTO state(key,value) VALUES(?,?)',applied.items())
        conn.commit()
        return {'source_message_id':row[0],'previous':old,'applied':applied}
    finally:
        conn.close()


def fix_sidecar_ownership(app):
    db=app/'data/giftia.db';st=db.stat()
    for suffix in ['-wal','-shm']:
        path=Path(str(db)+suffix)
        if path.exists():os.chown(path,st.st_uid,st.st_gid)


def restore(app, backup, manifest):
    run('systemctl','stop',SERVICE)
    # Revert annotations with the new tool before restoring old code. Later conversation data remains intact.
    if manifest.get('repair_applied'):
        service_python(app,"from pathlib import Path; from tools.quality_repair import repair; "
                       "repair(Path('data/giftia.db').resolve(),rollback=True)")
    if manifest.get('mood_correction'):
        correction=manifest['mood_correction'];conn=sqlite3.connect(app/'data/giftia.db')
        current=dict(conn.execute('SELECT key,value FROM state'))
        if all(current.get(k)==v for k,v in correction['applied'].items()):
            for key,value in correction['previous'].items():
                if value is None:conn.execute('DELETE FROM state WHERE key=?',(key,))
                else:conn.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,value))
            conn.commit()
        conn.close();fix_sidecar_ownership(app)
    for name, info in manifest['files'].items():
        rel=Path(name);dst=safe_path(app,rel)
        if info['existed']:
            copy_code(backup/'code',app,rel)
            os.chmod(dst,info['mode']);os.chown(dst,info['uid'],info['gid'])
        elif dst.exists():
            dst.unlink()  # individually verified source files, never a recursive removal
    fix_sidecar_ownership(app)
    if manifest.get('active'):
        run('systemctl','start',SERVICE)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--app',type=Path,default=Path('/opt/giftia'))
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--repair-memories',action='store_true')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    source=args.source.resolve();app=args.app.resolve()
    if os.name!='posix' or not (app/'giftia/brain.py').is_file() or app in [Path('/'),Path('/opt')]:
        raise SystemExit('Target must be an existing Linux Giftia installation')
    if args.rollback:
        if os.geteuid()!=0:raise SystemExit('Rollback requires root')
        backup=args.rollback.resolve()
        if not backup.is_relative_to((app/'data/releases').resolve()):raise SystemExit('Rollback path must be inside data/releases')
        manifest=json.loads((backup/'manifest.json').read_text())
        restore(app,backup,manifest)
        print('Code and release annotation policies restored; live database was not replaced.')
        return 0
    selected=files(source)
    for rel in selected:
        safe_path(source,rel);safe_path(app,rel)
        if rel.name=='.env' or rel.name.endswith('.local.toml'):raise RuntimeError('Protected configuration in release')
    old_model=model_config(app/'config/settings.toml',app/'config/settings.local.toml')
    new_model=model_config(source/'config/settings.toml',app/'config/settings.local.toml')
    if old_model!=new_model:raise SystemExit('This release is not allowed to change any model settings')
    print(json.dumps({'mode':'apply' if args.apply else 'preview','files':len(selected),'model_unchanged':True,
                      'repair_memories':args.repair_memories},ensure_ascii=False),flush=True)
    if not args.apply:return 0
    if os.geteuid()!=0:raise SystemExit('Apply requires root')
    if source==app:raise SystemExit('Use an isolated source directory for a recoverable release')
    stamp=datetime.now().strftime('quality-%Y%m%d-%H%M%S')
    backup=app/'data/releases'/stamp
    backup.mkdir(parents=True,mode=0o700)
    manifest={'version':stamp,'active':subprocess.run(['systemctl','is-active','--quiet',SERVICE]).returncode==0,
              'files':{},'repair_applied':False}
    for rel in selected:
        dst=safe_path(app,rel)
        info={'existed':dst.exists()}
        if dst.exists():
            st=dst.stat();info.update(mode=st.st_mode & 0o777,uid=st.st_uid,gid=st.st_gid,
                                      sha256=hashlib.sha256(dst.read_bytes()).hexdigest())
            target=safe_path(backup/'code',rel);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dst,target)
        manifest['files'][rel.as_posix()]=info
    manifest_path=backup/'manifest.json'
    def save_manifest():manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    save_manifest()
    print('Release backup: '+str(backup),flush=True)
    run('systemctl','stop',SERVICE)
    try:
        db=sqlite3.connect((app/'data/giftia.db').as_uri()+'?mode=ro',uri=True)
        snap=sqlite3.connect(str(backup/'giftia.db'))
        try:
            db.backup(snap)
            if snap.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup integrity check failed')
        finally:
            snap.close();db.close()
        fix_sidecar_ownership(app)
        for name in ['push_subs.json','push_last.json','web_requests.json']:
            if (app/'data'/name).exists():shutil.copy2(app/'data'/name,backup/name)
        for rel in selected:copy_code(source,app,rel)
        service_python(app,"from giftia.store import Store; s=Store('data/giftia.db'); s.close()")
        if args.repair_memories:
            preview=service_python(app,"from pathlib import Path; import json; from tools.quality_repair import repair; "
                                   "print(json.dumps(repair(Path('data/giftia.db').resolve()),ensure_ascii=False))")
            (backup/'memory-repair-preview.json').write_text(preview.stdout,encoding='utf-8')
            # Write intent before the transaction, so rollback also covers a crash after it commits.
            manifest['repair_applied']=True;save_manifest()
            applied=service_python(app,"from pathlib import Path; import json; from tools.quality_repair import repair; "
                                   "print(json.dumps(repair(Path('data/giftia.db').resolve(),apply=True),ensure_ascii=False))")
            print(applied.stdout.strip(),flush=True)
            mood=service_python(app,"from pathlib import Path; import json; from deploy.quality_release import repair_completion_mood; "
                                "print(json.dumps(repair_completion_mood(Path('data/giftia.db').resolve()),ensure_ascii=False))")
            manifest['mood_correction']=json.loads(mood.stdout);save_manifest()
            if manifest['mood_correction']:
                print('Corrected a completion-word mood false positive with prior state saved.',flush=True)
        if manifest['active']:
            run('systemctl','start',SERVICE)
            ready=False
            for _ in range(30):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8111/api/health',timeout=2) as response:
                        if response.status==200:ready=True;break
                except OSError:time.sleep(1)
            if not ready:raise RuntimeError('HTTP health did not recover')
            run('systemctl','is-active','--quiet',SERVICE)
        manifest['completed']=True;save_manifest()
        print('Release complete. Model and protected local configuration unchanged.',flush=True)
        return 0
    except BaseException:
        print('Release failed; reverting code and release annotations, preserving the live database.',flush=True)
        restore(app,backup,manifest)
        raise


if __name__=='__main__':raise SystemExit(main())
