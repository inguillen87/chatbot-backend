import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from scripts.recovery_archive import ArchiveLimits, RecoveryArchiveError, stage_export, restore_arguments

LISTING = ''';
;     dbname: chatboc_postgres_lw1n
;     Dumped from database version: 18.3
214; 1259 16384 TABLE public qa user
3010; 0 16384 TABLE DATA public qa user
215; 1259 16385 SEQUENCE public qa_id_seq user
3011; 0 0 SEQUENCE SET public qa_id_seq user
'''


class RecoveryArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.archive = self.root / 'export.dir.tar.gz'
        self.destination = self.root / 'prepared'
        self.limits = ArchiveLimits(expanded_bytes=1024*1024, logical_bytes=1024*1024)
        self.native = patch('scripts.recovery_archive._native_listing', return_value=LISTING).start()
        self.addCleanup(patch.stopall)

    def bundle(self, members=None):
        members = members if members is not None else [('export/db/toc.dat',b'PGDMPfixture'),('export/db/3010.dat.gz',gzip.compress(b'1\tSynthetic row\n'))]
        with tarfile.open(self.archive, 'w:gz') as bundle:
            for entry in members:
                if isinstance(entry, tarfile.TarInfo):
                    bundle.addfile(entry)
                else:
                    name, data = entry
                    info = tarfile.TarInfo(name); info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def inspect(self, digest=None, **kwargs):
        digest = digest or hashlib.sha256(self.archive.read_bytes()).hexdigest()
        return stage_export(self.archive, digest, self.destination, 'chatboc_postgres_lw1n',
                            Path(sys.executable), limits=kwargs.pop('limits',self.limits), **kwargs)

    def rejected(self, expected, digest=None, **kwargs):
        with self.assertRaises(RecoveryArchiveError) as error:
            self.inspect(digest, **kwargs)
        self.assertEqual(error.exception.code, expected)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.glob('.recovery-stage-*')), [])

    def test_valid_bundle_preserves_input_and_never_claims_restore(self):
        digest = self.bundle(); result = self.inspect(digest)
        self.assertTrue(result['archive_validated']); self.assertFalse(result['restore_executed'])
        self.assertFalse(result['cutover_authorized']); self.assertFalse(result['row_parity_verified'])
        self.assertEqual(result['object_counts']['TABLE DATA'],1)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(),digest)

    def test_empty_accepted_request_file_is_not_a_backup(self):
        self.archive.write_bytes(b'')
        self.rejected('empty_export_is_not_a_backup'); self.native.assert_not_called()

    def test_checksum_mismatch_never_reaches_parser(self):
        self.bundle(); self.rejected('archive_checksum_mismatch','0'*64)
        self.native.assert_not_called()

    def test_new_destination_is_mandatory(self):
        self.bundle(); self.destination.mkdir(); (self.destination/'keep').write_text('existing')
        with self.assertRaises(RecoveryArchiveError): self.inspect()
        self.assertEqual((self.destination/'keep').read_text(),'existing')
        self.native.assert_not_called()

    def test_path_traversal_absolute_and_windows_paths_are_rejected(self):
        for name in ('../escape','/escape','C:/escape','dir/../../escape','dir\\escape','dir/file:stream'):
            with self.subTest(name=name):
                self.bundle([(name,b'unsafe')]); self.rejected('unsafe_archive_path')
        self.assertFalse((self.root/'escape').exists())

    def test_links_and_device_members_are_rejected(self):
        for kind in (tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.CHRTYPE,tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                item=tarfile.TarInfo('linked'); item.type=kind; item.linkname='outside'
                self.bundle([item]); self.rejected('unsupported_archive_member')

    def test_case_collisions_do_not_overwrite_files(self):
        self.bundle([('db/toc.dat',b'PGDMPone'),('db/TOC.dat',b'PGDMPtwo')])
        self.rejected('duplicate_archive_path')

    def test_multiple_database_archives_are_rejected(self):
        self.bundle([('a/toc.dat',b'PGDMPone'),('b/toc.dat',b'PGDMPtwo')])
        self.rejected('one_directory_dump_required')

    def test_tar_expansion_is_bounded(self):
        self.bundle([('db/toc.dat',b'PGDMP'+b'x'*500)])
        self.rejected('archive_size_limit',limits=ArchiveLimits(expanded_bytes=100))

    def test_nested_compression_is_bounded(self):
        self.bundle([('db/toc.dat',b'PGDMP'),('db/3010.dat.gz',gzip.compress(b'x'*5000))])
        self.rejected('dump_payload_validation_failed',limits=ArchiveLimits(expanded_bytes=10000,logical_bytes=100))

    def test_invalid_payload_compression_is_rejected(self):
        self.bundle([('db/toc.dat',b'PGDMP'),('db/3010.dat.gz',b'not a gzip stream')])
        self.rejected('dump_payload_validation_failed'); self.native.assert_not_called()

    def test_missing_data_file_cannot_be_certified_by_table_of_contents(self):
        self.bundle([('db/toc.dat',b'PGDMP')])
        self.rejected('dump_data_member_missing_or_ambiguous')

    def test_wrong_source_database_is_rejected(self):
        self.bundle(); self.native.return_value=LISTING.replace('chatboc_postgres_lw1n','some_other_database')
        self.rejected('source_database_mismatch')

    def test_schema_only_dump_is_not_a_restorable_data_backup(self):
        self.bundle(); self.native.return_value='\n'.join(line for line in LISTING.splitlines() if 'TABLE DATA' not in line)
        self.rejected('complete_data_dump_required')

    def test_wrong_source_major_is_not_silently_upgraded(self):
        self.bundle(); self.native.return_value=LISTING.replace('18.3','19.1')
        self.rejected('source_version_mismatch')

    def test_native_tool_failure_does_not_leak_its_stderr(self):
        self.bundle(); self.native.side_effect=subprocess.TimeoutExpired('private_command',30,stderr='private row and password')
        self.rejected('archive_validation_failed')

    def test_archive_without_magic_is_not_passed_to_native_tool(self):
        self.bundle([('db/toc.dat',b'not PostgreSQL')]); self.rejected('postgres_dump_magic_missing')
        self.native.assert_not_called()

    def test_count_limit_prevents_archive_member_flood(self):
        self.bundle([('db/toc.dat',b'PGDMP'),('db/3010.dat',b'data')])
        self.rejected('too_many_archive_members',limits=ArchiveLimits(members=1))

    def test_unknown_payload_is_not_ignored(self):
        self.bundle([('db/toc.dat',b'PGDMP'),('db/3010.dat',b'data'),('db/run.sh',b'echo unsafe')])
        self.rejected('dump_payload_validation_failed')

    def test_plan_forbids_clean_create_and_existing_application_database_names(self):
        for name in ('neondb','main','chatboc_postgres_lw1n','postgres','../target'):
            with self.subTest(name=name),self.assertRaises(RecoveryArchiveError):
                restore_arguments(Path('pg_restore'),self.root,name)
        args=restore_arguments(Path('pg_restore'),self.root,'chatboc_recovery_20260925_qa')
        self.assertIn('--single-transaction',args); self.assertIn('--exit-on-error',args)
        self.assertNotIn('--clean',args); self.assertNotIn('--create',args)

    def test_truncated_outer_gzip_is_not_accepted(self):
        self.bundle(); self.archive.write_bytes(self.archive.read_bytes()[:-8])
        self.rejected('archive_validation_failed'); self.native.assert_not_called()

    def test_no_credentials_in_public_report(self):
        import json
        self.bundle(); report=json.dumps(self.inspect())
        self.assertNotIn('Synthetic row',report); self.assertNotIn('postgresql://',report)

if __name__=='__main__':unittest.main(verbosity=2)
