"""Rehearse a site's browser cache change at one unchanged HTTPS origin."""

import argparse
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import subprocess
from uuid import uuid4

from release_config import digest
from role_service import RoleService
from stage1 import Lab, SOURCE
from stage5_probe import prepare
from verify import require
from verify_r2_release import TLSProxy, client, run_candidate, tls_proxy
from verify_stage4 import OWNER


class SwitchableOrigin(TLSProxy):
    old = True
    previous_web = None

    def do_POST(self):
        if self.path == '/__rehearsal/candidate':
            type(self).old = False
            self.send_response(204)
            self.end_headers()
        else:
            super().do_POST()

    def do_GET(self):
        if not self.old or self.path.startswith('/api/'):
            return super().do_GET()
        assets = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
                  '/sw.js': ('sw.js', 'text/javascript'), '/app.js': ('app.js', 'text/javascript'),
                  '/chat.js': ('chat.js', 'text/javascript'), '/chat.css': ('chat.css', 'text/css'),
                  '/push.js': ('push.js', 'text/javascript'),
                  '/style.css': ('style.css', 'text/css'), '/manifest.json': ('manifest.json', 'application/json'),
                  '/icons/icon-192.png': ('icons/icon-192.png', 'image/png'),
                  '/icons/icon-512.png': ('icons/icon-512.png', 'image/png')}
        if self.path not in assets:
            self.send_error(404)
            return
        file, content_type = assets[self.path]
        directory = self.previous_web if self.previous_web and file in {'index.html', 'sw.js', 'chat.js', 'chat.css'} else SOURCE.parent / 'web'
        source = directory / file
        if not source.is_file():
            self.send_error(404)
            return
        data = source.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-cache' if file.endswith(('.html', '.js', '.css')) else 'public, max-age=86400')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


BROWSER = r"""async page => {
  const fixture = __FIXTURE__;
  const context = await page.context().browser().newContext({ ignoreHTTPSErrors: true, viewport: {width:390,height:844} });
  try {
    const prior = await context.newPage();
    await prior.goto(fixture.origin);
    await prior.evaluate(async () => {
      await navigator.serviceWorker.register('/sw.js');
      await navigator.serviceWorker.ready;
      await caches.open('unrelated-app-cache');
      localStorage.setItem('ella.history.test', '["old browser data retained"]');
    });
    await prior.waitForFunction(() => navigator.serviceWorker.controller);
    if (fixture.previousHarness) {
      await prior.getByLabel('会话凭据').fill(fixture.token);
      await prior.getByRole('button', {name:'进入', exact:true}).click();
      await prior.getByText('搭档 · 隔离会话', {exact:true}).waitFor();
    }
    await prior.evaluate(() => { window.priorWorker = navigator.serviceWorker.controller; });
    if (!(await prior.evaluate(() => caches.keys())).includes(fixture.previousCache)) throw Error('Previous shell did not cache');
    await prior.evaluate(() => fetch('/__rehearsal/candidate', {method:'POST'}));
    await prior.evaluate(async () => (await navigator.serviceWorker.getRegistration('/')).update());
    await prior.waitForFunction(async previousCache => {
      const keys = await caches.keys();
      const current = navigator.serviceWorker.controller;
      return current && current !== window.priorWorker && current.state === 'activated' &&
        keys.includes('ella-shell-harness-v3') && !keys.includes(previousCache);
    }, fixture.previousCache);
    await prior.close();

    const current = await context.newPage();
    const navigation = await current.goto(fixture.origin);
    const marker = 'id="attachment-dock"';
    const coldNewShell = (await navigation.text()).includes(marker);
    if (!coldNewShell) {
      const fetched = await current.evaluate(async () => (await fetch('/', {cache:'no-store'})).text());
      const refreshed = await current.reload({waitUntil:'domcontentloaded'});
      if (!(await refreshed.text()).includes(marker)) {
        throw Error(`Cold open and reload both served the old shell; live fetch new shell=${fetched.includes(marker)}`);
      }
    }
    if (!fixture.previousHarness) {
      await current.getByLabel('会话凭据').fill(fixture.token);
      await current.getByRole('button', {name:'进入', exact:true}).click();
    }
    await current.getByText('搭档 · 隔离会话', {exact:true}).waitFor();
    const state = await current.evaluate(async () => ({
      entries: (await (await caches.open('ella-shell-harness-v3')).keys()).map(request => new URL(request.url).pathname),
      keys: await caches.keys(),
      prior: localStorage.getItem('ella.history.test'),
    }));
    if (state.entries.length !== 6 || state.entries.some(name => name.startsWith('/api')) ||
        !state.keys.includes('unrelated-app-cache') || !state.prior?.includes('old browser data retained')) throw Error('Cache scope is wrong');
    await context.setOffline(true);
    await current.reload({waitUntil:'domcontentloaded'});
    await current.getByText('连接中断，正在重连', {exact:true}).waitFor();
    await context.setOffline(false);
    await current.getByText('搭档 · 隔离会话', {exact:true}).waitFor();
    return {updatedLegacyWorker:true, previousCache:fixture.previousCache, priorSessionRetained:fixture.previousHarness,
      coldOpen:true, coldNewShell, offlineShell:true, recoveredSession:true, cachedApiResponses:0};
  } finally {
    await context.close();
  }
}"""


def main(openssl, previous_web=None):
    cli = shutil.which('playwright-cli')
    require(cli, 'The Playwright CLI package is required')
    playwright = Path(cli).parent / 'node_modules/@playwright/cli/node_modules/playwright'
    require(playwright.is_dir(), 'Could not locate the installed Playwright package')
    root = SOURCE / '.stage1' / 'r2-validation' / ('cache-' + uuid4().hex[:10])
    print(f'Evidence directory: {root}', flush=True)
    lab = Lab(root, 18183, 18184)
    prepare(lab)
    RoleService(lab.database, root).store.close()
    config = SOURCE.parent / 'config'
    token = token_urlsafe(32)
    SwitchableOrigin.old = True
    SwitchableOrigin.previous_web = previous_web.resolve() if previous_web else None
    previous_cache = 'ella-shell-harness-v2' if previous_web else 'ella-shell-quality-v2-time'
    with tls_proxy(lab, openssl, SwitchableOrigin) as (origin, cert):
        manifest = root / 'release.json'
        manifest.touch(mode=0o600)
        manifest.write_text(json.dumps({'origin': origin, 'owner': OWNER, 'accounts': {OWNER: digest(token)},
                                        'settings_file': str(config / 'settings.toml'),
                                        'persona_file': str(config / 'persona.toml')}), encoding='utf-8')
        script = root / 'browser-cache.cjs'
        test = BROWSER.replace('__FIXTURE__', json.dumps({'origin': origin, 'token': token,
                                                         'previousHarness': bool(previous_web), 'previousCache': previous_cache}))
        script.write_text(f'const test = {test};\n' + r"""
const {createHash, X509Certificate} = require('node:crypto');
const {readFileSync} = require('node:fs');
const {chromium} = require(process.argv[2]);
(async () => {
  const certificate = new X509Certificate(readFileSync(process.argv[3]));
  const key = certificate.publicKey.export({type:'spki', format:'der'});
  const pin = createHash('sha256').update(key).digest('base64');
  const browser = await chromium.launch({channel:'msedge', args:[`--ignore-certificate-errors-spki-list=${pin}`]});
  try {
    console.log(JSON.stringify(await test(await browser.newPage())));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
""", encoding='utf-8')
        with run_candidate(lab, manifest, 'https-cache', client(origin, cert)):
            try:
                env = {**os.environ, 'TEMP': str(root), 'TMP': str(root)}
                result = subprocess.run([lab.node, script, playwright, cert], cwd=root, env=env, encoding='utf-8',
                                        capture_output=True)
                require(result.returncode == 0, result.stderr or result.stdout)
                evidence = json.loads(result.stdout)
                require(evidence['coldOpen'] and evidence['recoveredSession'], 'Missing cache transition evidence')
                (root / 'browser-cache-result.json').write_text(result.stdout, encoding='utf-8')
                print(result.stdout.strip())
            finally:
                script.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openssl', default=shutil.which('openssl'), required=shutil.which('openssl') is None)
    parser.add_argument('--previous-web', type=Path)
    options = parser.parse_args()
    main(options.openssl, options.previous_web)
