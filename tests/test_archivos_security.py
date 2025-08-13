import pytest
import unittest

ALLOWED_EXTENSIONS = {
    'jpg',
    'jpeg',
    'png',
    'pdf',
    'xlsx',
    'xls',
    'csv',
    'docx',
    'txt',
}
ALLOWED_MIME_PREFIXES = [
    'image/',
    'application/pdf',
    'application/msword',
    'application/vnd.',
    'text/plain',
]


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_mime(mime: str) -> bool:
    return any(mime == pre or mime.startswith(pre) for pre in ALLOWED_MIME_PREFIXES)


class ArchivosSecurityTests(unittest.TestCase):
    def test_extension_no_permitida(self):
        self.assertFalse(allowed_file('malware.exe'))
        self.assertFalse(allowed_file('script.sh'))
        self.assertTrue(allowed_file('foto.jpg'))

    def test_mime_no_permitido(self):
        self.assertTrue(allowed_mime('image/png'))
        self.assertTrue(allowed_mime('application/pdf'))
        self.assertFalse(allowed_mime('application/x-msdownload'))
        self.assertFalse(allowed_mime('text/html'))

if __name__ == '__main__':
    unittest.main()
