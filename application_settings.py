"""One protected API identity for all profiles; non-destructive legacy migration."""
import json
import time
from pathlib import Path
from profiles import Vault, Profiles, load_settings
from storage import save_json


class ApplicationSettings:
    def __init__(self, root):
        self.root = Path(root)
        self.vault = Vault(self.root / 'application')

    def credentials(self):
        value = self.vault.read('api')
        return json.loads(value) if value else {}

    def save_credentials(self, api_id, api_hash):
        old = self.credentials()
        api_hash = api_hash.strip() or old.get('api_hash', '')
        if not str(api_id).isdigit() or int(api_id) <= 0 or len(api_hash) != 32 or any(c not in '0123456789abcdefABCDEF' for c in api_hash):
            raise ValueError('Укажите положительный API ID и API Hash из 32 шестнадцатеричных символов в общих настройках.')
        self.vault.write('api', json.dumps({'api_id': int(api_id), 'api_hash': api_hash}))

    def migrate(self):
        if self.credentials():
            return
        profiles = Profiles(self.root)
        entries = sorted(profiles.data['profiles'], key=lambda p: p['id'] != profiles.data['active'])
        for profile in entries:
            directory = profiles.path(profile['id'])
            config = load_settings(directory / 'settings.json')
            value = Vault(directory).read('api_hash')
            if config.get('api_id') and value:
                # Original protected files/settings remain as rollback copies.
                self.save_credentials(config['api_id'], value)
                save_json(self.root / 'application' / 'migration.json',
                          {'from_profile': profile['id'], 'time': int(time.time()), 'originals_preserved': True})
                return


def remember_versions(config):
    versions = config.setdefault('template_versions', {})
    for name, content in config['templates'].items():
        history = versions.setdefault(name, [])
        version = {'text': content, 'format': config.get('template_formats', {}).get(name, 'plain')}
        version['entities'] = config.get('template_entities', {}).get(name, '[]') if version['format'] == 'telegram' else '[]'
        if not history or any(history[-1].get(k) != v for k, v in version.items()):
            history.append({**version, 'time': int(time.time())})
