"""Release 27 (F1): Tableau number-format strings, checked against what Tableau Cloud displays."""
import glob
import os
import re
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tableau_format import format_number  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TableauFormatTests(unittest.TestCase):
    def test_values_seen_on_tableau_cloud(self):
        cases = [
            ('c"$"#,##0;-"$"#,##0', 1201846.4, '$1,201,846'),
            ('c"$"#,##0;-"$"#,##0', -250.0, '-$250'),
            ('n#,##0" days";-#,##0" days"', 67.2, '67 days'),           # literal text
            ('*▲ 0.0%', 0.0352, '▲ 3.5%'),                               # glyph + space, percent
            ('*▼ 0.0%', 0.0093, '▼ 0.9%'),
            ('*0.0%; 0.0%', -0.056, ' 5.6%'),                            # negative section without a minus
            ('*0.0%; 0.0%', 0.041, '4.1%'),
            ('n#,##0.0;-#,##0.0', 4393084.12, '4,393,084.1'),
            ('c"$"#,##0.0;-"$"#,##0.0', 53992.78, '$53,992.8'),
            ('p0.0%', 0.25, '25.0%'),
            ('n#,##0;-#,##0', 138, '138'),
            ('#,##0.0,"K"', 1234567, '1,234.6K'),                        # display units: trailing comma
            ('#,##0,,"M"', 7283824.8, '7M'),
            ('0.##', 2.5, '2.5'),
            ('0.##', 2.0, '2'),
            ('*#,##0;-;-', -12, '-'),                                    # text-only negative / zero sections
            ('*#,##0;-;-', 0, '-'),
            ('*#,##0;-;-', 1234, '1,234'),
            ('c"$"#,##0;("$"#,##0)', -1234.5, '($1,235)'),
            ('n#,##0,.0K;-#,##0,.0K', 1234567, '1,234.6K'),
        ]
        for pattern, value, shown in cases:
            with self.subTest(pattern=pattern, value=value):
                self.assertEqual(format_number(value, pattern), shown)

    def test_unsupported_patterns_return_none(self):
        for pattern in ('C1033', 'N1033%', '', None, '0.00E+00'):
            self.assertIsNone(format_number(1.5, pattern))
        self.assertIsNone(format_number('abc', '0.0'))
        self.assertIsNone(format_number(float('nan'), '0.0'))

    def test_every_number_format_in_the_corpus_formats_or_declines(self):
        """No workbook format string may crash the formatter; each one formats a number or returns None."""
        seen = set()
        for path in glob.glob(os.path.join(ROOT, 'Workbooks', '**', '*.twb*'), recursive=True):
            try:
                if path.endswith('.twbx'):
                    z = zipfile.ZipFile(path)
                    names = [n for n in z.namelist() if n.endswith('.twb')]
                    xml = z.read(names[0]).decode('utf-8', 'replace') if names else ''
                else:
                    with open(path, encoding='utf-8', errors='replace') as fh:
                        xml = fh.read()
            except (zipfile.BadZipFile, OSError):
                continue
            for m in re.finditer(r"(?:default-format|attr='text-format' [^>]*value)='([^']+)'", xml):
                seen.add(m.group(1).replace('&quot;', '"').replace('&amp;', '&'))
        self.assertGreater(len(seen), 10)
        for pattern in seen:
            for value in (0, 1234.567, -0.0425):
                out = format_number(value, pattern)
                self.assertTrue(out is None or isinstance(out, str), pattern)


if __name__ == '__main__':
    unittest.main()
