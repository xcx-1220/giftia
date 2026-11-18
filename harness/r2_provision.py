"""Prepare a protected production release manifest without disclosing account secrets."""

import argparse
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
from secrets import token_hex

from giftia.config import load_env_file
from r2_preflight import reader
from release_config import DIGEST, WebRelease
from web_history import datum, history_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--account-digests', required=True, type=Path)
    parser.add_argument('--config-dir', required=True, type=Path)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--old-ledger', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    digests = json.loads(args.account_digests.read_text(encoding='utf-8'))
    if set(digests) != {'owner', 'guest'} or len(set(digests.values())) != 2 or not all(
            isinstance(value, str) and DIGEST.fullmatch(value) for value in digests.values()):
        raise ValueError('Expected distinct SHA-256 digests for owner and guest')
    with reader(args.database) as db:
        db.execute('BEGIN')
        owner = datum(db, 'state', 'handler_id')
        instance, matches, bound, watermark = history_identity(db, owner)
        if not matches:
            raise ValueError('The Giftia database has no bound owner')
    load_env_file(args.env_file)
    secret = os.environ.get('AILA_WEB_SECRET', '').strip()
    if not secret:
        raise ValueError('The old owner credential is missing')
    epoch = [instance, owner, bound, watermark]
    token = 'owner.' + hmac.new(secret.encode('utf-8'), b'aila-web', sha256).hexdigest()
    old_owner = sha256((token + ':' + json.dumps(epoch)).encode()).hexdigest()
    guest = 'web-r2-guest-' + token_hex(12)
    with args.output.open('x', encoding='utf-8') as protected:
        json.dump({'origin': 'https://giftia.icu', 'owner': owner,
                   'accounts': {owner: digests['owner'], guest: digests['guest']},
                   'settings_file': str(args.config_dir.resolve() / 'settings.toml'),
                   'persona_file': str(args.config_dir.resolve() / 'persona.toml'),
                   'legacy': {'epoch': epoch, 'principals': {owner: old_owner},
                              'ledger': str(args.old_ledger.resolve())}}, protected)
    WebRelease.load(args.output)
    print('Prepared protected manifest for the bound owner and a new independent guest')


if __name__ == '__main__':
    main()
