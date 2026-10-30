import contextlib
import errno
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'network_folder.py'
spec = importlib.util.spec_from_file_location('network_folder', SCRIPT)
nf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = nf
spec.loader.exec_module(nf)

# These values are intentionally fictional and must never appear in CLI output.
ENV = {'NETWORK_FOLDER_PATH': r'\\files.invalid\example$\Рабочие сканы',
       'NETWORK_FOLDER_USERNAME': r'EXAMPLE\test_user',
       'NETWORK_FOLDER_PASSWORD': 'fictional-test-secret'}


class Entry:
    def __init__(self, path):
        self.path, self.name = path, path.name

    def stat(self, follow_symlinks=True):
        return self.path.stat(follow_symlinks=follow_symlinks)


class FakeSMB:
    """Model SMB with a disposable local tree and the same no-clobber semantics."""
    def __init__(self, base):
        self.base = Path(base)
        self.root = self.base / 'Рабочие сканы'
        self.root.mkdir()
        self.reparse = set()
        self.events = []
        self.fail_read = False
        self.fail_cleanup = False
        self.race = None

    def local(self, path):
        parts = path[2:].split('\\')
        return self.base.joinpath(*parts[2:])

    def ClientConfig(self, **kwargs):
        self.events.append(('config', kwargs))

    def register_session(self, server, **kwargs):
        self.events.append(('session', server, kwargs))

    def reset_connection_cache(self):
        self.events.append(('close',))

    def stat(self, path, follow_symlinks=True):
        value = self.local(path).stat(follow_symlinks=follow_symlinks)
        if path in self.reparse:
            return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
                                   st_mtime=value.st_mtime, st_file_attributes=0x400)
        return value

    @contextlib.contextmanager
    def scandir(self, path):
        yield [Entry(p) for p in self.local(path).iterdir()]

    @contextlib.contextmanager
    def open_file(self, path, mode='r', **kwargs):
        self.events.append(('open', mode, kwargs))
        if mode == 'rb' and self.fail_read:
            class BrokenReader:
                def read(self, size):
                    raise TimeoutError('DO NOT LEAK ' + ENV['NETWORK_FOLDER_PATH'])
            yield BrokenReader()
        else:
            with self.local(path).open(mode) as stream:
                yield stream

    def mkdir(self, path):
        self.local(path).mkdir()

    def remove(self, path):
        if self.fail_cleanup:
            raise PermissionError(ENV['NETWORK_FOLDER_PASSWORD'])
        self.local(path).unlink()

    def rmdir(self, path):
        self.local(path).rmdir()

    def rename(self, source, dest):
        src, dst = self.local(source), self.local(dest)
        if self.race:
            self.race(dst)
            self.race = None
        if dst.exists() or dst.is_symlink():
            raise FileExistsError()
        src.rename(dst)

    def replace(self, source, dest):
        os.replace(self.local(source), self.local(dest))


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name) / 'share'
        base.mkdir()
        self.smb = FakeSMB(base)
        self.local_dir = Path(self.temp.name) / 'local'
        self.local_dir.mkdir()
        (self.smb.root / 'Входящие').mkdir()
        (self.smb.root / 'Входящие' / 'Скан 1.PDF').write_bytes(b'pdf-data')
        (self.smb.root / 'note.txt').write_text('text')

    def call(self, *args, env=None, backend=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nf.main(list(args), self.smb if backend is None else backend,
                           ENV if env is None else env)
        self.assertEqual(err.getvalue(), '')
        raw = out.getvalue()
        for value in (*ENV.values(), 'files.invalid'):
            self.assertNotIn(value.casefold(), raw.casefold())
        result = json.loads(raw)
        self.assertEqual(code, 0 if result['ok'] else 2)
        return result

    def test_configuration_requires_all_variables(self):
        for name in ENV:
            env = dict(ENV)
            del env[name]
            self.assertEqual(self.call('check-env', env=env)['error'], 'configuration_error')
        self.assertEqual(self.smb.events, [])

    def test_check_env_does_not_connect(self):
        self.assertEqual(self.call('check-env'), {'ok': True})
        self.assertEqual(self.smb.events, [])

    def test_invalid_configuration_is_private(self):
        for value in ('relative/path', r'\\files.invalid\example$\..\escape',
                      r'\\files.invalid\example$\bad:stream'):
            env = {**ENV, 'NETWORK_FOLDER_PATH': value}
            self.assertEqual(self.call('check-env', env=env)['error'], 'configuration_error')

    def test_check_reads_without_returning_names(self):
        self.assertEqual(self.call('check'), {'ok': True})
        self.assertIn(('config', {'skip_dfs': True}), self.smb.events)
        session = next(e for e in self.smb.events if e[0] == 'session')
        self.assertTrue(session[2]['require_signing'])
        self.assertEqual(session[2]['auth_protocol'], 'ntlm')
        self.assertEqual(self.smb.events[-1], ('close',))

    def test_list_and_metadata(self):
        result = self.call('list')
        self.assertEqual({e['path'] for e in result['entries']}, {'Входящие', 'note.txt'})
        info = self.call('stat', 'Входящие\\Скан 1.PDF')
        self.assertEqual(info['path'], 'Входящие/Скан 1.PDF')
        self.assertEqual(info['type'], 'file')
        self.assertEqual(info['size'], 8)
        self.assertTrue(info['modified_utc'].endswith('+00:00'))

    def test_find_recursive_and_case_insensitive(self):
        self.assertEqual(self.call('find', '*.pdf')['entries'], [])
        self.assertEqual([e['path'] for e in self.call('find', '*.pdf', '--recursive')['entries']],
                         ['Входящие/Скан 1.PDF'])
        self.assertEqual(len(self.call('find', 'скан*', 'Входящие')['entries']), 1)

    def test_traversal_absolute_streams_and_wildcards_rejected(self):
        for path in ('../outside', 'Входящие/../note.txt', '/etc/passwd',
                     r'\\other.invalid\share\x', 'C:\\test', 'note.txt:stream',
                     'Входящие//x', 'Входящие/./x', 'bad\nname', 'bad*', 'x. ', 'x.'):
            self.assertEqual(self.call('stat', path)['error'], 'invalid_path')

    def test_root_cannot_be_mutated(self):
        for command in ('mkdir', 'delete', 'rmdir'):
            self.assertEqual(self.call(command, '.')['error'], 'invalid_path')
        self.assertEqual(self.call('move', '.', 'elsewhere')['error'], 'invalid_path')

    def test_links_are_listed_but_never_traversed(self):
        outside = self.local_dir / 'outside'
        outside.mkdir()
        (outside / 'secret.pdf').write_bytes(b'secret')
        (self.smb.root / 'link').symlink_to(outside, target_is_directory=True)
        entries = self.call('find', '*', '--recursive')['entries']
        self.assertIn({'path': 'link', 'type': 'link'},
                      [{'path': e['path'], 'type': e['type']} for e in entries])
        self.assertFalse(any('secret.pdf' in e['path'] for e in entries))
        for args in (('stat', 'link/secret.pdf'), ('delete', 'link'),
                     ('upload', str(outside / 'secret.pdf'), 'link/upload.pdf')):
            self.assertEqual(self.call(*args)['error'], 'unsafe_link')

    def test_reparse_ancestor_including_configured_root_rejected(self):
        self.smb.reparse.add(ENV['NETWORK_FOLDER_PATH'])
        self.assertEqual(self.call('check')['error'], 'unsafe_link')
        self.assertEqual(self.call('stat', 'note.txt')['error'], 'unsafe_link')

    def test_mkdir_move_delete_rmdir(self):
        self.assertTrue(self.call('mkdir', 'Архив')['ok'])
        self.assertTrue(self.call('move', 'note.txt', 'Архив/новое имя.txt')['ok'])
        self.assertFalse((self.smb.root / 'note.txt').exists())
        self.assertFalse(self.call('rmdir', 'Архив')['ok'])
        self.assertTrue(self.call('delete', 'Архив/новое имя.txt')['ok'])
        self.assertTrue(self.call('rmdir', 'Архив')['ok'])
        self.assertEqual(self.call('delete', 'Входящие')['error'], 'invalid_type')

    def test_move_refuses_existing_file_and_directory(self):
        for destination in ('Входящие/Скан 1.PDF', 'Входящие'):
            self.assertEqual(self.call('move', 'note.txt', destination)['error'], 'already_exists')
        self.assertTrue((self.smb.root / 'note.txt').exists())

    def test_upload_download_and_overwrite(self):
        local = self.local_dir / 'result.txt'
        local.write_bytes(b'first')
        self.assertTrue(self.call('upload', str(local), 'новый файл.txt')['ok'])
        local.write_bytes(b'second')
        self.assertEqual(self.call('upload', str(local), 'новый файл.txt')['error'], 'already_exists')
        self.assertTrue(self.call('upload', str(local), 'новый файл.txt', '--overwrite')['ok'])
        dest = self.local_dir / 'download.txt'
        self.assertTrue(self.call('download', 'новый файл.txt', '--output', str(dest))['ok'])
        self.assertEqual(dest.read_bytes(), b'second')
        dest.write_bytes(b'old')
        self.assertEqual(self.call('download', 'новый файл.txt', '--output', str(dest))['error'],
                         'already_exists')
        self.assertTrue(self.call('download', 'новый файл.txt', '--output', str(dest), '--overwrite')['ok'])
        self.assertEqual(dest.read_bytes(), b'second')
        self.assertFalse(list(self.smb.root.glob('*.part')))
        self.assertFalse(list(self.local_dir.glob('*.part')))
        for event in self.smb.events:
            if event[0] == 'open':
                self.assertEqual(event[2]['create_options'], 0x00200000)

    def test_download_failure_preserves_target_and_cleans_partial(self):
        target = self.local_dir / 'existing.pdf'
        target.write_bytes(b'keep')
        self.smb.fail_read = True
        self.assertEqual(self.call('download', 'Входящие/Скан 1.PDF', '--output', str(target),
                                   '--overwrite')['error'], 'timeout')
        self.assertEqual(target.read_bytes(), b'keep')
        self.assertEqual(list(self.local_dir.iterdir()), [target])

    def test_upload_failure_preserves_target_and_cleans_partial(self):
        local = self.local_dir / 'large.txt'
        local.write_bytes(b'data')
        with patch.object(nf.shutil, 'copyfileobj', side_effect=TimeoutError('private details')):
            result = self.call('upload', str(local), 'note.txt', '--overwrite')
        self.assertEqual(result['error'], 'timeout')
        self.assertEqual((self.smb.root / 'note.txt').read_text(), 'text')
        self.assertFalse(list(self.smb.root.glob('*.part')))

    def test_cleanup_failure_reports_only_relative_temporary_path(self):
        local = self.local_dir / 'source'
        local.write_bytes(b'data')
        self.smb.fail_cleanup = True
        with patch.object(nf.shutil, 'copyfileobj', side_effect=TimeoutError()):
            result = self.call('upload', str(local), 'new.txt')
        self.assertEqual(result['error'], 'operation_failed')
        self.assertTrue(result['temporary_path'].startswith('.network-folder-'))
        self.assertTrue((self.smb.root / result['temporary_path']).exists())
        self.assertFalse((self.smb.root / 'new.txt').exists())

    def test_commit_race_does_not_overwrite(self):
        local = self.local_dir / 'source'
        local.write_bytes(b'mine')
        self.smb.race = lambda target: target.write_bytes(b'other-client')
        self.assertEqual(self.call('upload', str(local), 'race.txt')['error'], 'already_exists')
        self.assertEqual((self.smb.root / 'race.txt').read_bytes(), b'other-client')
        self.assertFalse(list(self.smb.root.glob('*.part')))

    def test_lost_create_reply_reports_uncertain_remote_state(self):
        local = self.local_dir / 'source'
        local.write_bytes(b'data')

        @contextlib.contextmanager
        def uncertain_create(path, **kwargs):
            self.smb.local(path).write_bytes(b'')
            raise TimeoutError(ENV['NETWORK_FOLDER_PATH'])
            yield  # pragma: no cover

        with patch.object(self.smb, 'open_file', side_effect=uncertain_create):
            result = self.call('upload', str(local), 'new.txt')
        self.assertEqual(result['error'], 'operation_failed')
        self.assertTrue((self.smb.root / result['temporary_path']).exists())
        self.assertFalse((self.smb.root / 'new.txt').exists())

    def test_download_commit_race_does_not_overwrite(self):
        target = self.local_dir / 'race.txt'
        real_link = os.link

        def race(source, destination):
            target.write_bytes(b'other-client')
            return real_link(source, destination)

        with patch.object(nf.os, 'link', side_effect=race):
            self.assertEqual(self.call('download', 'note.txt', '--output', str(target))['error'],
                             'already_exists')
        self.assertEqual(target.read_bytes(), b'other-client')
        self.assertEqual(list(self.local_dir.iterdir()), [target])

    def test_connection_exceptions_and_library_output_are_private(self):
        for error in (PermissionError, RuntimeError, TimeoutError):
            def broken(*args, **kwargs):
                print(' '.join(ENV.values()))
                print(' '.join(ENV.values()), file=sys.stderr)
                logging.error(' '.join(ENV.values()))
                raise error(' '.join(ENV.values()))
            with patch.object(self.smb, 'register_session', side_effect=broken):
                self.assertFalse(self.call('check')['ok'])

    def test_relative_names_cannot_expose_configuration(self):
        (self.smb.root / (ENV['NETWORK_FOLDER_PASSWORD'] + '.txt')).write_text('x')
        (self.smb.root / 'FILES.INVALID.txt').write_text('x')
        result = self.call('list')
        self.assertTrue(any('[redacted]' in e['path'] for e in result['entries']))

    def test_invalid_cli_does_not_echo_values(self):
        for args in ((ENV['NETWORK_FOLDER_PATH'],), ('stat',), ('upload',),
                     ('download', 'note.txt'), ('check', '--unknown', ENV['NETWORK_FOLDER_PASSWORD'])):
            self.assertEqual(self.call(*args)['error'], 'invalid_arguments')

    def test_invalid_search_mask(self):
        for pattern in ('dir/*.pdf', 'bad\nname'):
            self.assertEqual(self.call('find', pattern)['error'], 'invalid_pattern')

    def test_missing_and_non_directory_paths(self):
        self.assertEqual(self.call('stat', 'missing')['error'], 'not_found')
        self.assertEqual(self.call('stat', 'note.txt/child')['error'], 'not_directory')
        self.assertEqual(self.call('mkdir', 'missing/child')['error'], 'not_found')

    def test_smb_style_os_errors_allow_creating_missing_destinations(self):
        class SMBLikeError(OSError):
            pass

        real_stat = self.smb.stat

        def smb_stat(path, **kwargs):
            try:
                return real_stat(path, **kwargs)
            except FileNotFoundError:
                raise SMBLikeError(errno.ENOENT, ENV['NETWORK_FOLDER_PATH'])

        local = self.local_dir / 'source'
        local.write_bytes(b'data')
        with patch.object(self.smb, 'stat', side_effect=smb_stat):
            self.assertTrue(self.call('mkdir', 'new-dir')['ok'])
            self.assertTrue(self.call('upload', str(local), 'new-dir/file.txt')['ok'])
            self.assertEqual(self.call('stat', 'missing')['error'], 'not_found')

    def test_real_smb_exception_normalization_when_dependency_available(self):
        try:
            from smbprotocol.exceptions import SMBOSError, NtStatus
        except ImportError:
            self.skipTest('Optional SMB dependency is not installed.')
        for status, expected in ((NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, 'not_found'),
                                 (NtStatus.STATUS_OBJECT_NAME_COLLISION, 'already_exists'),
                                 (NtStatus.STATUS_ACCESS_DENIED, 'access_denied')):
            exc = SMBOSError(status, ENV['NETWORK_FOLDER_PATH'])
            self.assertEqual(nf.safe_error(exc)['error'], expected)

    def test_download_does_not_follow_local_symlink(self):
        original = self.local_dir / 'original'
        original.write_bytes(b'keep')
        target = self.local_dir / 'link'
        target.symlink_to(original)
        self.assertEqual(self.call('download', 'note.txt', '--output', str(target),
                                   '--overwrite')['error'], 'already_exists')
        self.assertEqual(original.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
