#!/usr/bin/env python3
"""SMB file operations with environment-only credentials and safe JSON output."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fnmatch
import json
import logging
import ntpath
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid

OPEN_REPARSE_POINT = 0x00200000


class FolderError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def fail(code, message):
    raise FolderError(code, message)


def normalized_error(exc):
    # SMBOSError is not a FileNotFoundError/FileExistsError subclass.
    status = getattr(exc, 'ntstatus', getattr(exc, 'status', None))
    if status in (0xC0000022, 0xC000006D, 0xC000006A, 0xC0000064):
        return PermissionError()
    if isinstance(exc, OSError):
        kind = {errno.ENOENT: FileNotFoundError, errno.EEXIST: FileExistsError,
                errno.EACCES: PermissionError, errno.EPERM: PermissionError,
                errno.ENOTDIR: NotADirectoryError, errno.EISDIR: IsADirectoryError,
                errno.ETIMEDOUT: TimeoutError}.get(exc.errno)
        if kind is not None:
            return kind()
    return exc


def component_valid(part):
    return (part not in ('', '.', '..') and not part.endswith((' ', '.'))
            and not re.search(r'[\x00-\x1f\x7f<>:"|?*]', part))


@dataclass(repr=False)
class Config:
    root: str
    username: str
    password: str

    @property
    def server(self):
        return self.root.split('\\')[2]

    @classmethod
    def load(cls, env=None):
        env = os.environ if env is None else env
        names = ('NETWORK_FOLDER_PATH', 'NETWORK_FOLDER_USERNAME', 'NETWORK_FOLDER_PASSWORD')
        if any(not env.get(name, '').strip() for name in names):
            fail('configuration_error', 'Set all NETWORK_FOLDER environment variables.')
        root, username, password = (env[name] for name in names)
        root = root.replace('/', '\\').rstrip('\\')
        parts = root[2:].split('\\')
        if (not root.startswith('\\\\') or len(parts) < 2
                or any(not component_valid(p) for p in parts)
                or parts[0] in ('?', '.') or any(c.isspace() for c in parts[0])):
            fail('configuration_error', 'NETWORK_FOLDER_PATH must be a valid UNC folder path.')
        if any(ord(c) < 32 or ord(c) == 127 for c in username + password):
            fail('configuration_error', 'Credentials must not contain control characters.')
        return cls(root, username, password)

    def redact(self, value):
        # Apply at the output boundary, including relative names supplied by a server.
        if isinstance(value, str):
            for token in sorted({self.root, self.root.replace('\\', '/'), self.server,
                                 self.username, self.password}, key=len, reverse=True):
                value = re.sub(re.escape(token), lambda _: '[redacted]', value, flags=re.I)
            return value
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value


def relative(value):
    value = value.replace('\\', '/')
    if value in ('', '.'):
        return '.'
    if value.startswith('/') or any(not component_valid(p) for p in value.split('/')):
        fail('invalid_path', 'Use a relative path without traversal, streams or wildcards.')
    return value


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def metadata(path, info):
    kind = 'link' if is_link(info) else ('directory' if stat.S_ISDIR(info.st_mode) else 'file')
    return {'path': path, 'type': kind, 'size': info.st_size,
            'modified_utc': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()}


@contextmanager
def quiet_library():
    # Third-party diagnostics must never escape the CLI JSON boundary.
    with open(os.devnull, 'w') as sink:
        from contextlib import redirect_stdout, redirect_stderr
        with redirect_stdout(sink), redirect_stderr(sink):
            yield


class Client:
    def __init__(self, config, backend=None):
        self.config = config
        if backend is None:
            try:
                import smbclient as backend
            except ImportError:
                fail('dependency_missing', 'Install the skill requirements in this Python environment.')
        self.smb = backend
        self.smb.ClientConfig(skip_dfs=True)
        self.smb.register_session(config.server, username=config.username, password=config.password,
                                  auth_protocol='ntlm', connection_timeout=15, require_signing=True)

    def close(self):
        self.smb.reset_connection_cache()

    def smb_stat(self, path):
        try:
            return self.smb.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise normalized_error(exc) from None

    def path(self, value, missing=False, mutate=False):
        rel = relative(value)
        if mutate and rel == '.':
            fail('invalid_path', 'The configured root cannot be modified.')
        full = self.config.root if rel == '.' else ntpath.join(self.config.root, *rel.split('/'))
        # Inspect ancestors starting at the share, including the configured root.
        parts = full[2:].split('\\')
        for end in range(2, len(parts) + 1):
            candidate = '\\\\' + '\\'.join(parts[:end])
            try:
                info = self.smb_stat(candidate)
            except FileNotFoundError:
                if missing and end == len(parts):
                    break
                raise
            if is_link(info):
                fail('unsafe_link', 'Links and reparse points cannot be traversed.')
            if end < len(parts) and not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError()
        return rel, full

    def info(self, value):
        rel, path = self.path(value)
        return metadata(rel, self.smb_stat(path))

    def entries(self, value):
        rel, path = self.path(value)
        result = []
        with self.smb.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                if not component_valid(name) or '/' in name or '\\' in name:
                    fail('invalid_entry', 'The server returned an unsupported file name.')
                child = name if rel == '.' else rel + '/' + name
                result.append(metadata(child, entry.stat(follow_symlinks=False)))
        return sorted(result, key=lambda entry: entry['path'].casefold())

    def find(self, pattern, value, recursive):
        if not pattern or re.search(r'[\\/\x00-\x1f\x7f]', pattern):
            fail('invalid_pattern', 'Use a file name mask without directory separators.')
        pending, result = [relative(value)], []
        while pending:
            for entry in self.entries(pending.pop()):
                if fnmatch.fnmatchcase(entry['path'].rsplit('/', 1)[-1].casefold(), pattern.casefold()):
                    result.append(entry)
                if recursive and entry['type'] == 'directory':
                    pending.append(entry['path'])
        return sorted(result, key=lambda entry: entry['path'].casefold())

    def ensure_type(self, path, directory=False):
        info = self.smb_stat(path)
        if is_link(info):
            fail('unsafe_link', 'Links and reparse points cannot be used.')
        if directory and not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError()
        if not directory and not stat.S_ISREG(info.st_mode):
            fail('invalid_type', 'This operation requires a regular file.')

    def destination(self, value, overwrite=False):
        rel, path = self.path(value, missing=True, mutate=True)
        try:
            self.ensure_type(path)
        except FileNotFoundError:
            pass
        else:
            if not overwrite:
                raise FileExistsError()
        return rel, path

    def upload(self, local, remote, overwrite):
        rel, target = self.destination(remote, overwrite)
        temp_rel = ntpath.dirname(rel.replace('/', '\\'))
        temp_rel = relative(ntpath.join(temp_rel, '.network-folder-' + uuid.uuid4().hex + '.part'))
        _, temp_path = self.path(temp_rel, missing=True, mutate=True)
        created, attempted = False, False
        try:
            with open(local, 'rb') as source:
                attempted = True
                with self.smb.open_file(temp_path, mode='xb', create_options=OPEN_REPARSE_POINT) as dest:
                    created = True
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
            # Repeat checks immediately before commit; rename enforces no-clobber on server.
            self.destination(rel, overwrite)
            operation = self.smb.replace if overwrite else self.smb.rename
            operation(temp_path, target)
            created = False
        except Exception as exc:
            exc = normalized_error(exc)
            if attempted and not created and not isinstance(exc, FileExistsError):
                # CREATE may have succeeded on the server before the reply was lost.
                raise FolderError('operation_failed',
                                  'Transfer state is uncertain; inspect target and temporary file before retrying.',
                                  temporary_path=temp_rel) from None
            if created:
                try:
                    self.path(temp_rel, mutate=True)
                    self.smb.remove(temp_path)
                except Exception:
                    raise FolderError('operation_failed',
                                      'Transfer failed; inspect target and temporary file before retrying.',
                                      temporary_path=temp_rel) from None
            raise exc from None
        return {'path': rel}

    def download(self, remote, local, overwrite):
        rel, source_path = self.path(remote)
        self.ensure_type(source_path)
        target = Path(local).absolute()
        if target.is_symlink() or (target.exists() and not overwrite):
            raise FileExistsError()
        if target.exists() and not target.is_file():
            fail('invalid_type', 'The local destination must be a regular file.')
        fd, temp_name = tempfile.mkstemp(prefix='.network-folder-', suffix='.part', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as dest:
                with self.smb.open_file(source_path, mode='rb', create_options=OPEN_REPARSE_POINT) as source:
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
                dest.flush()
                os.fsync(dest.fileno())
            if overwrite:
                os.replace(temp_name, target)
            else:
                # A hard link commits without a race that could overwrite another file.
                os.link(temp_name, target)
                os.unlink(temp_name)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return {'path': rel}

    def execute(self, args):
        command = args.command
        if command == 'check':
            self.entries('.')
            return {}
        if command == 'list':
            return {'entries': self.entries(args.path)}
        if command == 'find':
            return {'entries': self.find(args.pattern, args.path, args.recursive)}
        if command == 'stat':
            return self.info(args.path)
        if command == 'upload':
            return self.upload(args.local, args.path, args.overwrite)
        if command == 'download':
            return self.download(args.path, args.output, args.overwrite)
        if command == 'move':
            src_rel, src = self.path(args.source, mutate=True)
            dst_rel, dst = self.path(args.destination, missing=True, mutate=True)
            # rename() itself refuses existing destinations, including directories.
            self.smb.rename(src, dst)
            return {'source': src_rel, 'path': dst_rel}
        rel, path = self.path(args.path, missing=command == 'mkdir', mutate=True)
        if command == 'mkdir':
            self.smb.mkdir(path)
        elif command == 'delete':
            self.ensure_type(path)
            self.smb.remove(path)
        elif command == 'rmdir':
            self.ensure_type(path, directory=True)
            self.smb.rmdir(path)
        return {'path': rel}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail('invalid_arguments', 'Invalid arguments; use --help for command syntax.')


def parser():
    result = Parser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    for name in ('check-env', 'check'):
        commands.add_parser(name)
    for name in ('list', 'stat', 'mkdir', 'delete', 'rmdir'):
        sub = commands.add_parser(name)
        sub.add_argument('path', nargs='?' if name == 'list' else None,
                         default='.' if name == 'list' else None)
    sub = commands.add_parser('find')
    sub.add_argument('pattern')
    sub.add_argument('path', nargs='?', default='.')
    sub.add_argument('--recursive', action='store_true')
    sub = commands.add_parser('upload')
    sub.add_argument('local')
    sub.add_argument('path')
    sub.add_argument('--overwrite', action='store_true')
    sub = commands.add_parser('download')
    sub.add_argument('path')
    sub.add_argument('--output', required=True)
    sub.add_argument('--overwrite', action='store_true')
    sub = commands.add_parser('move')
    sub.add_argument('source')
    sub.add_argument('destination')
    return result


def safe_error(exc):
    exc = normalized_error(exc)
    if isinstance(exc, FolderError):
        return {'ok': False, 'error': exc.code, 'message': exc.message, **exc.details}
    for kind, code, message in (
        (FileNotFoundError, 'not_found', 'A file or directory was not found.'),
        (FileExistsError, 'already_exists', 'Destination already exists.'),
        (PermissionError, 'access_denied', 'Access denied; check credentials and permissions.'),
        (NotADirectoryError, 'not_directory', 'A path component is not a directory.'),
        (IsADirectoryError, 'is_directory', 'This operation requires a file.'),
        (TimeoutError, 'timeout', 'Timed out; inspect state before retrying changes.'),
    ):
        if isinstance(exc, kind):
            return {'ok': False, 'error': code, 'message': message}
    return {'ok': False, 'error': 'operation_failed',
            'message': 'Operation failed; check connectivity, authorization and state before retrying changes.'}


def main(argv=None, backend=None, env=None):
    config, client = None, None
    previous_logging = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        args = parser().parse_args(argv)
        config = Config.load(env)
        if args.command == 'check-env':
            result = {'ok': True}
        else:
            with quiet_library():
                client = Client(config, backend)
                result = {'ok': True, **client.execute(args)}
    except Exception as exc:
        result = safe_error(exc)
    finally:
        if client is not None:
            try:
                with quiet_library():
                    client.close()
            except Exception:
                pass
        logging.disable(previous_logging)
    print(json.dumps(config.redact(result) if config else result, ensure_ascii=False))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.exit(main())
