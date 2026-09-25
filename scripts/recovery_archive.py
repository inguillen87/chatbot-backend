"""Offline validation of Render directory exports. No credentials or database access."""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
import hashlib
import gzip
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from scripts.recovery_payload import validate_payloads


class RecoveryArchiveError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ArchiveLimits:
    compressed_bytes: int = 512 * 1024 * 1024
    expanded_bytes: int = 1024 * 1024 * 1024
    member_bytes: int = 512 * 1024 * 1024
    members: int = 20000
    toc_bytes: int = 16 * 1024 * 1024
    logical_bytes: int = 1024 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _plain_path(path: Path, require_file: bool = False) -> None:
    for current in (path, *path.parents):
        if not current.exists() and not current.is_symlink():
            continue
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RecoveryArchiveError('linked_path_rejected')
    if require_file and not path.is_file():
        raise RecoveryArchiveError('archive_file_required')


def _member_path(name: str) -> PurePosixPath:
    while name.startswith('./'):
        name = name[2:]
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '\\' in name or ':' in name or '\x00' in name:
        raise RecoveryArchiveError('unsafe_archive_path')
    if any(part in ('', '.', '..') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,150}', part) for part in path.parts):
        raise RecoveryArchiveError('unsafe_archive_path')
    return path


def _unpack(archive: Path, target: Path, limits: ArchiveLimits) -> tuple[Path, dict]:
    verified = 0
    with gzip.open(archive, 'rb') as compressed:
        for block in iter(lambda: compressed.read(1024 * 1024), b''):
            verified += len(block)
            if verified > limits.expanded_bytes + limits.members * 2048 + 10240:
                raise RecoveryArchiveError('archive_size_limit')
    names, files = set(), []
    expanded, count = 0, 0
    with tarfile.open(archive, mode='r|gz') as bundle:
        for member in bundle:
            count += 1
            if count > limits.members:
                raise RecoveryArchiveError('too_many_archive_members')
            if member.isdir() and member.name in ('.', './'):
                continue
            relative = _member_path(member.name)
            folded = relative.as_posix().casefold()
            if folded in names:
                raise RecoveryArchiveError('duplicate_archive_path')
            names.add(folded)
            if member.issparse() or not (member.isdir() or member.isfile()):
                raise RecoveryArchiveError('unsupported_archive_member')
            if len(relative.parts) > 8 or len(relative.as_posix()) > 240:
                raise RecoveryArchiveError('archive_path_too_long')
            destination = target.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            expanded += member.size
            if member.size < 0 or member.size > limits.member_bytes or expanded > limits.expanded_bytes:
                raise RecoveryArchiveError('archive_size_limit')
            if relative.name == 'toc.dat' and member.size > limits.toc_bytes:
                raise RecoveryArchiveError('toc_size_limit')
            destination.parent.mkdir(parents=True, exist_ok=True)
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise RecoveryArchiveError('archive_member_unreadable')
            written = 0
            with extracted, destination.open('xb') as output:
                for block in iter(lambda: extracted.read(1024 * 1024), b''):
                    written += len(block)
                    if written > member.size:
                        raise RecoveryArchiveError('archive_member_size_mismatch')
                    output.write(block)
            if written != member.size:
                raise RecoveryArchiveError('archive_member_size_mismatch')
            files.append(destination)
    toc = [path for path in files if path.name == 'toc.dat']
    if len(toc) != 1 or any(path.parent != toc[0].parent for path in files):
        raise RecoveryArchiveError('one_directory_dump_required')
    with toc[0].open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise RecoveryArchiveError('postgres_dump_magic_missing')
    return toc[0].parent, {'files': len(files), 'expanded_bytes': expanded}


def _tool_environment() -> dict[str, str]:
    # --list never connects; also strip inherited database/credential settings.
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(('PG', 'NEON_', 'DATABASE_', 'MIGRATIONS_DATABASE_', 'RENDER_'))}


ENTRY_TYPES = ('MATERIALIZED VIEW DATA', 'FOREIGN TABLE', 'FK CONSTRAINT', 'TABLE DATA',
               'SEQUENCE SET', 'TABLE ATTACH', 'TABLE', 'SEQUENCE', 'CONSTRAINT',
               'INDEX', 'TRIGGER', 'FUNCTION', 'PROCEDURE', 'TYPE', 'VIEW')


def inspect_listing(text: str, dump: Path, source_database: str, major: int) -> dict:
    database = re.findall(r'^;\s+dbname:\s*(\S+)\s*$', text, flags=re.M)
    versions = re.findall(r'^;\s+Dumped from database version:\s*(\d+)', text, flags=re.M)
    if database != [source_database]:
        raise RecoveryArchiveError('source_database_mismatch')
    if versions != [str(major)]:
        raise RecoveryArchiveError('source_version_mismatch')
    counts, data_ids = Counter(), set()
    for line in text.splitlines():
        match = re.match(r'^(\d+);\s+\d+\s+\d+\s+(.*)$', line)
        if not match:
            continue
        for kind in ENTRY_TYPES:
            if match[2].startswith(kind + ' '):
                counts[kind] += 1
                if kind == 'TABLE DATA':
                    data_ids.add(match[1])
                break
    if not counts['TABLE'] or not counts['TABLE DATA']:
        raise RecoveryArchiveError('complete_data_dump_required')
    for entry in data_ids:
        if sum((dump / (entry + suffix)).is_file() for suffix in ('.dat', '.dat.gz', '.dat.lz4', '.dat.zst')) != 1:
            raise RecoveryArchiveError('dump_data_member_missing_or_ambiguous')
    return {'source_major': major, 'object_counts': dict(sorted(counts.items()))}


def _native_listing(tool: Path, dump: Path, major: int) -> str:
    _plain_path(tool, require_file=True)
    environment = _tool_environment()
    version = subprocess.run([str(tool), '--version'], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=10, env=environment)
    if version.returncode or not re.search(r'pg_restore \(PostgreSQL\) ' + str(major) + r'(?:\.|\s)', version.stdout):
        raise RecoveryArchiveError('pg_restore_major_mismatch')
    result = subprocess.run([str(tool), '--list', '--format=directory', str(dump)],
                            capture_output=True, text=True, encoding='utf-8', errors='strict',
                            timeout=30, env=environment)
    if result.returncode or len(result.stdout) > 16 * 1024 * 1024:
        raise RecoveryArchiveError('native_archive_validation_failed')
    return result.stdout


def stage_export(archive: Path, expected_sha256: str, destination: Path,
                 source_database: str, pg_restore: Path, major: int = 18,
                 limits: ArchiveLimits = ArchiveLimits()) -> dict:
    """Copy, validate and unpack one local export into a NEW folder. Never restore it."""
    archive, destination = archive.absolute(), destination.absolute()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise RecoveryArchiveError('explicit_sha256_required')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,63}', source_database) or major != 18:
        raise RecoveryArchiveError('invalid_expected_source')
    _plain_path(archive, require_file=True)
    _plain_path(destination)
    if destination.exists():
        raise RecoveryArchiveError('new_destination_required')
    size = archive.stat().st_size
    if not size:
        raise RecoveryArchiveError('empty_export_is_not_a_backup')
    if size > limits.compressed_bytes:
        raise RecoveryArchiveError('compressed_archive_too_large')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < size + limits.expanded_bytes + 64 * 1024 * 1024:
        raise RecoveryArchiveError('insufficient_staging_space')
    try:
        with tempfile.TemporaryDirectory(prefix='.recovery-stage-', dir=destination.parent) as temporary:
            temporary = Path(temporary)
            stable = temporary / 'export.dir.tar.gz'
            digest, copied = hashlib.sha256(), 0
            with archive.open('rb') as incoming, stable.open('xb') as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b''):
                    copied += len(block)
                    if copied > limits.compressed_bytes:
                        raise RecoveryArchiveError('compressed_archive_too_large')
                    digest.update(block)
                    outgoing.write(block)
            if digest.hexdigest() != expected_sha256 or copied != size:
                raise RecoveryArchiveError('archive_checksum_mismatch')
            unpacked = temporary / 'unpacked'
            unpacked.mkdir()
            dump, archive_info = _unpack(stable, unpacked, limits)
            try:
                logical_bytes = validate_payloads(dump, limits.logical_bytes)
            except (ValueError, OSError, EOFError):
                raise RecoveryArchiveError("dump_payload_validation_failed") from None
            listing = _native_listing(pg_restore.absolute(), dump, major)
            toc_info = inspect_listing(listing, dump, source_database, major)
            file_proofs = {path.name: _sha256(path) for path in sorted(dump.iterdir()) if path.is_file()}
            destination.mkdir(mode=0o700)
            try:
                for item in dump.iterdir():
                    item.rename(destination / item.name)
            except Exception:
                shutil.rmtree(destination)
                raise
    except RecoveryArchiveError:
        raise
    except (OSError, EOFError, ValueError, tarfile.TarError, subprocess.SubprocessError, UnicodeError):
        raise RecoveryArchiveError('archive_validation_failed') from None
    return {'contract_version': 'chatboc.recovery.archive.v1', 'archive_validated': True,
            'archive_sha256': expected_sha256, 'archive_bytes': size, 'logical_payload_bytes': logical_bytes, **archive_info, **toc_info,
            'file_sha256': file_proofs, 'source_name_matched': True, 'offline_only': True,
            'restore_executed': False, 'row_parity_verified': False, 'cutover_authorized': False}


def restore_arguments(pg_restore: Path, staged: Path, database: str) -> list[str]:
    """A reviewed plan, not an executor. Environment/host must be confirmed externally."""
    if not re.fullmatch(r'chatboc_recovery_[0-9]{8}(?:_[a-z0-9]+)?', database):
        raise RecoveryArchiveError('recovery_database_name_required')
    return [str(pg_restore), '--format=directory', '--single-transaction', '--exit-on-error',
            '--no-owner', '--no-privileges', '--no-tablespaces', '--no-password',
            '--dbname=' + database, str(staged)]
