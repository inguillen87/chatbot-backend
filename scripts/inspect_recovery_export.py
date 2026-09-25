"""Validate one explicitly supplied local Render export; never fetch credentials or restore."""
import argparse
import json
from pathlib import Path
from scripts.recovery_archive import RecoveryArchiveError, stage_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--source-database', required=True)
    parser.add_argument('--new-directory', type=Path, required=True)
    parser.add_argument('--pg-restore', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Choose a new report path; existing evidence is never overwritten.')
    try:
        result = stage_export(args.archive, args.sha256, args.new_directory,
                              args.source_database, args.pg_restore)
        code = 0
    except RecoveryArchiveError as error:
        result = {'contract_version': 'chatboc.recovery.archive.v1', 'error_code': error.code,
                  'archive_validated': False, 'restore_executed': False, 'cutover_authorized': False}
        code = 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as output:
        json.dump(result, output, indent=2)
    print(json.dumps({key: value for key, value in result.items() if key != 'file_sha256'}))
    return code

if __name__ == '__main__':
    raise SystemExit(main())
