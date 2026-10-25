"""Exercise release/rollback against a temporary installation with systemd/network mocked."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from deploy import quality_release as release
from giftia.store import Store
from tools.quality_repair import repair


@unittest.skipUnless(os.name=='posix','Linux/systemd release simulation')
class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.app=self.root/'app';self.source=self.root/'source'
        for base in [self.app,self.source]:
            for folder in ['giftia','config','data']: (base/folder).mkdir(parents=True)
            for name,text in {'giftia/brain.py':'old','config/settings.toml':'[model]\nname="same"\ntemperature=1.15\n',
                              'config/persona.toml':'name="艾拉"\n','run.py':'pass\n','README.md':'test\n'}.items():
                (base/name).write_text(text,encoding='utf-8')
        (self.source/'giftia/brain.py').write_text('new')
        (self.source/'giftia/new_feature.py').write_text('new feature')
        (self.app/'.env').write_text('PROTECTED=value')
        (self.app/'config/settings.local.toml').write_text('[model]\ntemperature=1.15\n')
        store=Store(self.app/'data/giftia.db')
        store.add_memory(instance_no=1,created_ts='2026-10-25',happened_ts='2026-10-25',summary='艾拉答得很快并问够不够快')
        store.add_message('i1-m-20261025',1,'user','existing','2026-10-25')
        store.close()

    def tearDown(self):self.temp.cleanup()

    def execute(self,fail_health=False,rollback=None):
        appended=False
        def fake_run(*args,**kwargs):
            nonlocal appended
            if args[:2]==('systemctl','start') and fail_health and not appended:
                db=sqlite3.connect(self.app/'data/giftia.db')
                db.execute("INSERT INTO messages(session_id,instance_no,role,content,ts) VALUES('i1-m-20261025',1,'user','new after release','2026-10-25')")
                db.commit();db.close();appended=True
            return SimpleNamespace(returncode=0,stdout='')
        def fake_service(app,code,*args):
            if 'repair_completion_mood' in code:value=release.repair_completion_mood(app/'data/giftia.db')
            elif 'rollback=True' in code:value=repair((app/'data/giftia.db').resolve(),rollback=True)
            elif 'apply=True' in code:value=repair((app/'data/giftia.db').resolve(),apply=True)
            elif 'repair' in code:value=repair((app/'data/giftia.db').resolve())
            else:value=None
            return SimpleNamespace(stdout=json.dumps(value))
        @contextlib.contextmanager
        def health(*args,**kwargs):
            if fail_health:raise OSError('simulated failed health')
            yield SimpleNamespace(status=200)
        argv=['quality_release','--source',str(self.source),'--app',str(self.app),'--apply','--repair-memories']
        if rollback:argv=['quality_release','--app',str(self.app),'--rollback',str(rollback)]
        with patch.object(sys,'argv',argv),patch.object(release.os,'geteuid',return_value=0), \
             patch.object(release.os,'chown'),patch.object(release,'run',side_effect=fake_run), \
             patch.object(release.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
             patch.object(release,'service_python',side_effect=fake_service), \
             patch.object(release.urllib.request,'urlopen',side_effect=health), \
             patch.object(release.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            return release.main()

    def test_success_preserves_config_and_backs_up_database(self):
        self.assertEqual(self.execute(),0)
        self.assertEqual((self.app/'giftia/brain.py').read_text(),'new')
        self.assertEqual((self.app/'.env').read_text(),'PROTECTED=value')
        self.assertEqual((self.app/'config/settings.local.toml').read_text(),'[model]\ntemperature=1.15\n')
        backup=next((self.app/'data/releases').iterdir())
        db=sqlite3.connect(backup/'giftia.db');self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok');db.close()
        self.execute(rollback=backup)
        self.assertEqual((self.app/'giftia/brain.py').read_text(),'old')
        self.assertFalse((self.app/'giftia/new_feature.py').exists())

    def test_failed_health_restores_code_without_discarding_new_messages(self):
        with self.assertRaises(RuntimeError):self.execute(fail_health=True)
        self.assertEqual((self.app/'giftia/brain.py').read_text(),'old')
        self.assertFalse((self.app/'giftia/new_feature.py').exists())
        db=sqlite3.connect(self.app/'data/giftia.db')
        self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],2)
        self.assertEqual(db.execute('SELECT count(*) FROM memory_annotations').fetchone()[0],0)
        db.close()

    def test_changing_model_is_rejected_before_deployment(self):
        (self.source/'config/settings.toml').write_text('[model]\nname="different"\n')
        with self.assertRaises(SystemExit):self.execute()
        self.assertEqual((self.app/'giftia/brain.py').read_text(),'old')
        self.assertFalse((self.app/'data/releases').exists())


if __name__=='__main__':unittest.main(verbosity=2)
