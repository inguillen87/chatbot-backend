"""Bounded validation of nested gzip members in PostgreSQL directory exports."""
import gzip
from pathlib import Path
import re


def validate_payloads(directory: Path, maximum: int) -> int:
    total = 0
    for file in directory.iterdir():
        if not file.is_file() or not re.fullmatch(r'(?:toc\.dat|(?:\d+|blob_\d+)\.dat(?:\.gz)?|blobs(?:_\d+)?\.toc(?:\.gz)?)', file.name):
            raise ValueError('unsupported_dump_member')
        if file.name == 'toc.dat':
            continue
        opener = gzip.open if file.suffix == '.gz' else open
        with opener(file, 'rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                total += len(block)
                if total > maximum:
                    raise ValueError('uncompressed_payload_size_limit')
    return total
