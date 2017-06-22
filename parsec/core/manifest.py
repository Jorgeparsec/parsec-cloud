from copy import deepcopy
import json
from functools import partial
from datetime import datetime
import os

from parsec.backend.vlob_service import VlobNotFound
from parsec.core.file import File
from parsec.crypto import generate_sym_key, load_private_key, load_sym_key
from parsec.exceptions import ManifestError, ManifestNotFound
from parsec.tools import event_handler, from_jsonb64, to_jsonb64


class Manifest:

    def __init__(self, core, id=None):
        self.core = core
        self.id = id
        self.version = 1
        self.entries = {'/': None}
        self.dustbin = []
        self.original_manifest = {'entries': deepcopy(self.entries),
                                  'dustbin': deepcopy(self.dustbin),
                                  'versions': {}}
        self.handler = partial(event_handler, self.reload, reset=False)

    async def reload(self):
        raise NotImplementedError()

    async def is_dirty(self):
        current_manifest = json.loads(await self.dumps())
        diff = await self.diff(self.original_manifest, current_manifest)
        for category in diff.keys():
            for operation in diff[category].keys():
                if diff[category][operation]:
                    return True
        return False

    async def diff(self, old_manifest, new_manifest):
        diff = {}
        for category in new_manifest.keys():
            if category == 'dustbin':
                continue
            added = {}
            changed = {}
            removed = {}
            for key, value in new_manifest[category].items():
                try:
                    ori_value = old_manifest[category][key]
                    if ori_value != value:
                        changed[key] = (ori_value, value)
                except KeyError:
                    added[key] = value
            for key, value in old_manifest[category].items():
                try:
                    new_manifest[category][key]
                except KeyError:
                    removed[key] = value
            diff.update({category: {'added': added, 'changed': changed, 'removed': removed}})
        # Dustbin
        added = []
        removed = []
        for vlob in new_manifest['dustbin']:
            if vlob not in old_manifest['dustbin']:
                added.append(vlob)
        for vlob in old_manifest['dustbin']:
            if vlob not in new_manifest['dustbin']:
                removed.append(vlob)
        diff.update({'dustbin': {'added': added, 'removed': removed}})
        return diff

    async def patch(self, manifest, diff):
        new_manifest = deepcopy(manifest)
        for category in diff.keys():
            if category in ['dustbin', 'versions']:
                continue
            for path, entry in diff[category]['added'].items():
                if path in new_manifest[category] and new_manifest[category][path] != entry:
                    new_manifest[category][path + '-conflict'] = new_manifest[category][path]
                new_manifest[category][path] = entry
            for path, entries in diff[category]['changed'].items():
                old_entry, new_entry = entries
                if path in new_manifest[category]:
                    current_entry = new_manifest[category][path]
                    if current_entry not in [old_entry, new_entry]:
                        new_manifest[category][path + '-conflict'] = current_entry
                    new_manifest[category][path] = new_entry
                else:
                    new_manifest[category][path + '-deleted'] = new_entry
            for path, entry in diff[category]['removed'].items():
                if path in new_manifest[category]:
                    if new_manifest[category][path] != entry:
                        new_manifest[category][path + '-recreated'] = new_manifest[category][path]
                    del new_manifest[category][path]
        for entry in diff['dustbin']['added']:
            if entry not in new_manifest['dustbin']:
                new_manifest['dustbin'].append(entry)
        for entry in diff['dustbin']['removed']:
            if entry in new_manifest['dustbin']:
                new_manifest['dustbin'].remove(entry)
        return new_manifest

    async def diff_versions(self, old_version=None, new_version=None):
        raise NotImplementedError()

    async def history(self, first_version=1, last_version=None, summary=False):
        if first_version and last_version and first_version > last_version:
            raise ManifestError('bad_versions',
                                    'First version number higher than the second one.')
        if summary:
            diff = await self.diff_versions(first_version, last_version)
            return {'summary_history': diff}
        else:
            if not last_version:
                last_version = self.version
            history = []
            for current_version in range(first_version, last_version + 1):
                diff = await self.diff_versions(current_version - 1, current_version)
                diff['version'] = current_version
                history.append(diff)
            return {'detailed_history': history}

    async def get_version(self):
        return self.version if await self.is_dirty() else self.version - 1

    async def get_vlobs_versions(self):
        versions = {}
        for entry in list(self.entries.values()) + self.dustbin:
            if entry:
                try:
                    vlob = await self.core.synchronizer.vlob_read(entry['id'], entry['read_trust_seed'])
                except VlobNotFound:
                    versions[entry['id']] = None
                else:
                    versions[entry['id']] = vlob['version']
        return versions

    async def dumps(self, original_manifest=False):
        if original_manifest:
            return json.dumps(self.original_manifest)
        else:
            return json.dumps({'entries': self.entries,
                               'dustbin': self.dustbin,
                               'versions': await self.get_vlobs_versions()})

    async def reload_vlob(self, vlob_id):
        # TODO invalidate old cache
        pass

    async def add_file(self, path, vlob):
        path = '/' + path.strip('/')
        parent_folder = os.path.dirname(path)
        if parent_folder not in self.entries:
            raise ManifestNotFound('Destination Folder not found.')
        if path in self.entries:
            raise ManifestError('already_exists', 'File already exists.')
        self.entries[path] = vlob

    # async def replace_file(self, path, new_vlob):
    #     path = '/' + path.strip('/')
    #     parent_folder = os.path.dirname(path)
    #     if parent_folder not in self.entries:
    #         raise ManifestNotFound('Destination Folder not found.')
    #     if path not in self.entries:
    #         raise ManifestNotFound('File not found.')
    #     vlob = self.entries[path]
    #     vlob['id'] = new_vlob
    #     vlob['read_trust_seed'] = new_vlob['read_trust_seed']
    #     vlob['write_trust_seed'] = new_vlob['write_trust_seed']

    async def rename_file(self, old_path, new_path):
        old_path = '/' + old_path.strip('/')
        new_path = '/' + new_path.strip('/')
        new_parent_folder = os.path.dirname(new_path)
        if new_parent_folder not in self.entries:
            raise ManifestNotFound('Destination Folder not found.')
        if new_path in self.entries:
            raise ManifestError('already_exists', 'File already exists.')
        if old_path not in self.entries:
            raise ManifestNotFound('File not found.')
        for entry, vlob in self.entries.items():
            if entry.startswith(old_path):
                new_entry = new_path + entry[len(old_path):]
                self.entries[new_entry] = vlob
                del self.entries[entry]

    async def delete_file(self, path):
        path = '/' + path.strip('/')
        try:
            entry = self.entries[path]
        except KeyError:
            raise ManifestNotFound('File not found.')
        if not entry:
            raise ManifestError('path_is_not_file', 'Path is not a file.')
        file = await File.load(self.core.synchronizer,
                               entry['id'],
                               entry['key'],
                               entry['read_trust_seed'],
                               entry['write_trust_seed'])
        discarded = await file.discard()  # TODO discard or not?
        if not discarded:
            dustbin_entry = {'removed_date': datetime.utcnow().isoformat(), 'path': path}
            dustbin_entry.update(entry)
            self.dustbin.append(dustbin_entry)
        del self.entries[path]

    async def undelete_file(self, vlob):
        for entry in self.dustbin:
            if entry['id'] == vlob:
                path = entry['path']
                if path in self.entries:
                    raise ManifestError('already_exists', 'Restore path already used.')
                del entry['path']
                del entry['removed_date']
                self.dustbin[:] = [item for item in self.dustbin if item['id'] != vlob]
                self.entries[path] = entry
                folder = os.path.dirname(path)
                await self.make_folder(folder, parents=True)
                return
        raise ManifestNotFound('Vlob not found.')

    async def reencrypt_file(self, path):
        path = '/' + path.strip('/')
        try:
            entry = self.entries[path]
        except KeyError:
            raise ManifestNotFound('File not found.')
        new_vlob = await self.core.reencrypt(entry['id'])
        self.entries[path] = new_vlob

    async def stat(self, path):
        path = '/' + path.strip('/')
        if path != '/' and path not in self.entries:
            raise ManifestNotFound('Folder or file not found.')
        entry = self.entries[path]
        if entry:
            vlob = await self.core.synchronizer.vlob_read(entry['id'], entry['read_trust_seed'])
            encrypted_blob = vlob['blob']
            encrypted_blob = from_jsonb64(encrypted_blob)
            key = from_jsonb64(entry['key'])
            encryptor = load_sym_key(key)
            blob = encryptor.decrypt(encrypted_blob)
            blob = json.loads(blob.decode())
            # TODO which block index? Or add date in vlob_service ?
            block_stat = await self.core.synchronizer.block_stat(id=blob[-1]['blocks'][-1]['block'])
            size = 0
            for blocks_and_key in blob:
                for block in blocks_and_key['blocks']:
                    size += block['size']
            # TODO: don't provide atime field if we don't know it?
            return {
                'id': entry['id'],
                'type': 'file',
                'created': block_stat['creation_date'],
                'updated': block_stat['creation_date'],
                'size': size,
                'version': vlob['version']
            }
        else:
            # Skip mtime and size given that they are too complicated to provide for folder
            # TODO time except mtime
            children = {}
            for entry in self.entries:
                if entry != path and entry.startswith(path) and entry.count('/', len(path) + 1) == 0:
                    children[os.path.basename(entry)] = deepcopy(self.entries[entry])
            return {
                'type': 'folder',
                'items': sorted(list(children.keys()))
            }

    async def make_folder(self, path, parents=False):
        path = '/' + path.strip('/')
        if path in self.entries:
            if parents:
                return self.entries[path]
            else:
                raise ManifestError('already_exists', 'Folder already exists.')
        parent_folder = os.path.dirname(path)
        if parent_folder not in self.entries:
            if parents:
                await self.make_folder(parent_folder, parents=True)
            else:
                raise ManifestNotFound("Parent folder doesn't exists.")
        self.entries[path] = None
        return self.entries[path]

    async def remove_folder(self, path):
        path = '/' + path.strip('/')
        if path == '/':
            raise ManifestError('cannot_remove_root', 'Cannot remove root folder.')
        for entry, vlob in self.entries.items():
            if entry != path and entry.startswith(path):
                raise ManifestError('folder_not_empty', 'Folder not empty.')
            elif entry == path and vlob:
                raise ManifestError('path_is_not_folder', 'Path is not a folder.')
        try:
            del self.entries[path]
        except KeyError:
            raise ManifestNotFound('Folder not found.')

    async def show_dustbin(self, path=None):
        if not path:
            return self.dustbin
        else:
            path = '/' + path.strip('/')
        results = [entry for entry in self.dustbin if entry['path'] == path]
        if not results:
            raise ManifestNotFound('Path not found.')
        return results

    async def check_consistency(self, manifest):
        entries = [entry for entry in list(manifest['entries'].values()) if entry]
        entries += manifest['dustbin']
        for entry in entries:
            try:
                vlob = await self.core.synchronizer.vlob_read(
                    id=entry['id'],
                    trust_seed=entry['read_trust_seed'],
                    version=manifest['versions'][entry['id']])
                encrypted_blob = vlob['blob']
                encrypted_blob = from_jsonb64(encrypted_blob)
                key = from_jsonb64(entry['key']) if entry['key'] else None
                encryptor = load_sym_key(key)
                encryptor.decrypt(encrypted_blob)  # TODO check exception
            except VlobNotFound:
                return False
        return True


class GroupManifest(Manifest):

    @classmethod
    async def create(cls, core):
        vlob = await core.synchronizer.vlob_create()
        self = GroupManifest(core, vlob['id'])
        self.read_trust_seed = vlob['read_trust_seed']
        self.write_trust_seed = vlob['write_trust_seed']
        self.encryptor = generate_sym_key()
        self.version = 1
        blob = await self.dumps()
        encrypted_blob = self.encryptor.encrypt(blob.encode())
        encrypted_blob = to_jsonb64(encrypted_blob)
        await core.synchronizer.vlob_update(vlob['id'], 1, vlob['write_trust_seed'], encrypted_blob)
        return self

    @classmethod
    async def load(cls, core, id, key, read_trust_seed, write_trust_seed):
        self = GroupManifest(core, id)
        self.read_trust_seed = read_trust_seed
        self.write_trust_seed = write_trust_seed
        if key:
            self.encryptor = load_sym_key(from_jsonb64(key))
        else:
            self.encryptor = generate_sym_key()
        await self.reload(reset=True)
        return self

    async def get_vlob(self):
        return {'id': self.id,
                'key': to_jsonb64(self.encryptor.key),
                'read_trust_seed': self.read_trust_seed,
                'write_trust_seed': self.write_trust_seed}

    async def update_vlob(self, new_vlob):
        self.id = new_vlob['id']
        self.encryptor = load_sym_key(from_jsonb64(new_vlob['key']))
        self.read_trust_seed = new_vlob['read_trust_seed']
        self.write_trust_seed = new_vlob['write_trust_seed']

    async def diff_versions(self, old_version=None, new_version=None):
        empty_entries = {'/': None}
        empty_manifest = {'entries': empty_entries, 'dustbin': [], 'versions': {}}
        # Old manifest
        if old_version and old_version > 0:
            old_vlob = await self.core.synchronizer.vlob_read(
                id=self.id,
                trust_seed=self.read_trust_seed,
                version=old_version)
            old_blob = from_jsonb64(old_vlob['blob'])
            content = self.encryptor.decrypt(old_blob)
            old_manifest = json.loads(content.decode())
        elif old_version == 0:
            old_manifest = empty_manifest
        else:
            old_manifest = self.original_manifest
        # New manifest
        if new_version and new_version > 0:
            new_vlob = await self.core.synchronizer.vlob_read(
                id=self.id,
                trust_seed=self.read_trust_seed,
                version=new_version)
            blob = from_jsonb64(new_vlob['blob'])
            content = self.encryptor.decrypt(blob)
            new_manifest = json.loads(content.decode())
        elif new_version == 0:
            new_manifest = empty_manifest
        else:
            new_manifest = json.loads(await self.dumps())
        return await self.diff(old_manifest, new_manifest)

    async def reload(self, reset=False):
        # Subscribe to events
        await self.core.backend.connect_event('on_vlob_updated', self.id, self.handler)
        vlob = await self.core.synchronizer.vlob_read(id=self.id, trust_seed=self.read_trust_seed)
        blob = from_jsonb64(vlob['blob'])
        content = self.encryptor.decrypt(blob)
        if not reset and vlob['version'] <= await self.get_version():
            return
        new_manifest = json.loads(content.decode())
        backup_new_manifest = deepcopy(new_manifest)
        if not await self.check_consistency(new_manifest):
            raise ManifestError('not_consistent', 'Group manifest not consistent.')
        if not reset:
            diff = await self.diff_versions()
            new_manifest = await self.patch(new_manifest, diff)
        self.entries = new_manifest['entries']
        self.dustbin = new_manifest['dustbin']
        self.version = vlob['version']
        self.original_manifest = backup_new_manifest
        versions = new_manifest['versions']
        for vlob_id, version in versions.items():
            await self.core.file_restore(vlob_id, version)

    async def commit(self):
        if not await self.get_version() == 0 and not await self.is_dirty():
            return
        blob = await self.dumps()
        encrypted_blob = self.encryptor.encrypt(blob.encode())
        encrypted_blob = to_jsonb64(encrypted_blob)
        if self.id:
            await self.core.synchronizer.vlob_update(self.id, self.version, self.write_trust_seed, encrypted_blob)
        else:
            vlob = await self.core.synchronizer.vlob_create(encrypted_blob)
            self.id = vlob['id']
            self.read_trust_seed = vlob['read_trust_seed']
            self.write_trust_seed = vlob['write_trust_seed']
        self.original_manifest = json.loads(blob)
        new_vlob = await self.core.synchronizer.vlob_synchronize(self.id)
        if new_vlob:
            if new_vlob is not True:
                self.id = new_vlob['id']
                self.read_trust_seed = new_vlob['read_trust_seed']
                self.write_trust_seed = new_vlob['write_trust_seed']
                new_vlob = await self.get_vlob()
            self.version += 1
        return new_vlob

    async def reencrypt(self):
        # Reencrypt files
        for path, entry in self.entries.items():
            if entry:
                new_vlob = await self.core.file_reencrypt(entry['id'])
                self.entries[path] = new_vlob
        for index, entry in enumerate(self.dustbin):
            path = entry['path']
            removed_date = entry['removed_date']
            new_vlob = await self.core.file_reencrypt(entry['id'])
            new_vlob['path'] = path
            new_vlob['removed_date'] = removed_date
            self.dustbin[index] = new_vlob
        # Reencrypt manifest
        blob = await self.dumps()
        self.encryptor = generate_sym_key()
        encrypted_blob = self.encryptor.encrypt(blob.encode())
        encrypted_blob = to_jsonb64(encrypted_blob)
        new_vlob = await self.core.synchronizer.vlob_create(encrypted_blob)
        self.id = new_vlob['id']
        self.read_trust_seed = new_vlob['read_trust_seed']
        self.write_trust_seed = new_vlob['write_trust_seed']
        self.version = 1

    async def restore(self, version=None):
        if version is None:
            version = self.version - 1 if self.version > 1 else 1
        if version > 0 and version < self.version:
            vlob = await self.core.synchronizer.vlob_read(id=self.id,
                                                          trust_seed=self.read_trust_seed,
                                                          version=version)
            await self.core.synchronizer.vlob_update(self.id, self.version, self.write_trust_seed, vlob['blob'])
        elif version < 1 or version > self.version:
            raise ManifestError('bad_version', 'Bad version number.')
        await self.reload(reset=True)


class UserManifest(Manifest):

    @classmethod
    async def load(cls, core):  # TODO retrieve key from id
        self = UserManifest(core, core.identity.id)
        self.encryptor = core.identity.private_key
        try:
            await self.reload(reset=True)
        except ManifestNotFound:
            self.version = 1
            self.group_manifests = {}
            self.original_manifest = {'entries': deepcopy(self.entries),
                                      'dustbin': deepcopy(self.dustbin),
                                      'groups': deepcopy(self.group_manifests),
                                      'versions': {}}
            blob = await self.dumps()
            encrypted_blob = self.encryptor.pub_key.encrypt(blob.encode())
            encrypted_blob = to_jsonb64(encrypted_blob)
            await core.synchronizer.user_vlob_update(1, encrypted_blob)
        return self

    async def diff_versions(self, old_version=None, new_version=None):
        empty_entries = {'/': None}
        empty_manifest = {'entries': empty_entries, 'groups': {}, 'dustbin': [], 'versions': {}}
        # Old manifest
        if old_version and old_version > 0:
            old_vlob = await self.core.synchronizer.user_vlob_read(old_version)
            old_blob = from_jsonb64(old_vlob['blob'])
            content = self.encryptor.decrypt(old_blob)
            old_manifest = json.loads(content.decode())
        elif old_version == 0:
            old_manifest = empty_manifest
        else:
            old_manifest = self.original_manifest
        # New manifest
        if new_version and new_version > 0:
            new_vlob = await self.core.synchronizer.user_vlob_read(new_version)
            new_blob = from_jsonb64(new_vlob['blob'])
            content = self.encryptor.decrypt(new_blob)
            new_manifest = json.loads(content.decode())
        elif new_version == 0:
            new_manifest = empty_manifest
        else:
            new_manifest = json.loads(await self.dumps())
        return await self.diff(old_manifest, new_manifest)

    async def dumps(self, original_manifest=False):
        if original_manifest:
            manifest = deepcopy(self.original_manifest)
            manifest['groups'] = await self.get_group_vlobs()
            return json.dumps(manifest)
        else:
            return json.dumps({'entries': self.entries,
                               'dustbin': self.dustbin,
                               'groups': await self.get_group_vlobs(),
                               'versions': await self.get_vlobs_versions()})

    async def get_group_vlobs(self, group=None):
        if group:
            groups = [group]
        else:
            groups = self.group_manifests.keys()
        results = {}
        try:
            for group in groups:
                results[group] = await self.group_manifests[group].get_vlob()
        except KeyError:
            raise ManifestNotFound('Group not found.')
        return results

    async def get_group_manifest(self, group):
        try:
            return self.group_manifests[group]
        except KeyError:
            raise ManifestNotFound('Group not found.')

    async def reencrypt_group_manifest(self, group):
        try:
            group_manifest = self.group_manifests[group]
        except KeyError:
            raise ManifestNotFound('Group not found.')
        await group_manifest.reencrypt()

    async def create_group_manifest(self, group):
        if group in self.group_manifests:
            raise ManifestError('already_exists', 'Group already exists.')
        group_manifest = await GroupManifest.create(self.core)
        self.group_manifests[group] = group_manifest

    async def import_group_vlob(self, group, vlob):
        if group in self.group_manifests:
            await self.group_manifests[group].update_vlob(vlob)
            await self.group_manifests[group].reload(reset=False)
        group_manifest = await GroupManifest.load(self.core, **vlob)
        self.group_manifests[group] = group_manifest

    async def remove_group(self, group):
        # TODO deleted group is not moved in dusbin, but hackers could continue to read/write files
        try:
            del self.group_manifests[group]
        except KeyError:
            raise ManifestNotFound('Group not found.')

    async def reload(self, reset=False):
        vlob = await self.core.synchronizer.user_vlob_read()
        if not vlob['blob']:
            raise ManifestNotFound('User manifest not found.')
        blob = from_jsonb64(vlob['blob'])
        content = self.encryptor.decrypt(blob)
        if not reset and vlob['version'] <= self.version:
            return
        new_manifest = json.loads(content.decode())
        backup_new_manifest = deepcopy(new_manifest)
        if not await self.check_consistency(new_manifest):
            raise ManifestError('not_consistent', 'User manifest not consistent.')
        if not reset:
            diff = await self.diff_versions()
            new_manifest = await self.patch(new_manifest, diff)
        self.entries = new_manifest['entries']
        self.dustbin = new_manifest['dustbin']
        self.version = vlob['version']
        self.group_manifests = {}
        for group, group_vlob in new_manifest['groups'].items():
            await self.import_group_vlob(group, group_vlob)
        self.original_manifest = backup_new_manifest
        versions = new_manifest['versions']
        for vlob_id, version in versions.items():
            await self.core.restore_file(vlob_id, version)
        # Update event subscriptions
        # TODO update events subscriptions
        # Subscribe to events
        # TODO where to unsubscribe?

    async def commit(self, recursive=True):
        if self.version and not await self.is_dirty():
            return
        if recursive:
            for group_manifest in self.group_manifests.values():
                await group_manifest.commit()
        blob = await self.dumps()
        encrypted_blob = self.encryptor.pub_key.encrypt(blob.encode())
        encrypted_blob = to_jsonb64(encrypted_blob)
        await self.core.synchronizer.user_vlob_update(self.version, encrypted_blob)
        self.original_manifest = json.loads(blob)
        new_vlob = await self.core.synchronizer.user_vlob_synchronize()
        if new_vlob:
            self.version += 1
        return new_vlob

    async def restore(self, version=None):
        if version is None:
            version = self.version - 1 if self.version > 1 else 1
        if version > 0 and version < self.version:
            vlob = await self.core.synchronizer.user_vlob_read(version)
            await self.core.synchronizer.user_vlob_update(self.version, vlob['blob'])
        elif version < 1 or version > self.version:
            raise ManifestError('bad_version', 'Bad version number.')
        await self.reload(reset=True)

    async def check_consistency(self, manifest):
        if await super().check_consistency(manifest) is False:
            return False
        for entry in manifest['groups'].values():
            try:
                vlob = await self.core.synchronizer.vlob_read(
                    id=entry['id'],
                    trust_seed=entry['read_trust_seed'])
                encrypted_blob = vlob['blob']
                key = from_jsonb64(entry['key']) if entry['key'] else None
                encryptor = load_sym_key(key)
                encryptor.decrypt(encrypted_blob)
            except (VlobNotFound):
                return False
        return True