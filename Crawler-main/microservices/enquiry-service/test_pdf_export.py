"""The enquiry PDF must not fail on characters outside Latin-1 (arrows, bullets, curly quotes, rupee sign)."""
import unittest
from datetime import datetime

import pdf_export
from models import Enquiry

TRICKY = "Order \u2192 revised \u2022 \u201cquoted\u201d \u2013 \u20b9 500 \u2026 \u4e2d"


class LatinOneTests(unittest.TestCase):
    def test_substitutes_and_replaces(self):
        self.assertEqual(pdf_export._to_latin1("A \u2192 B"), "A -> B")
        self.assertEqual(pdf_export._to_latin1("\u2022 item"), "- item")
        self.assertEqual(pdf_export._to_latin1("\u201chi\u201d"), '"hi"')
        self.assertEqual(pdf_export._to_latin1("caf\u00e9"), "caf\u00e9")  # Latin-1 letters are kept
        self.assertEqual(pdf_export._to_latin1("\u4e2d"), "?")
        pdf_export._to_latin1(TRICKY).encode("latin-1")  # never raises

    def test_a_pdf_is_built_for_an_enquiry_with_such_characters(self):
        enquiry = Enquiry(enquiry_id="ENQ-X", subject=TRICKY, description=TRICKY, customer_name=TRICKY,
                          remarks=TRICKY, status="open", priority="high", channel="email", received_at=datetime(2026, 11, 17, 9, 30))
        data = pdf_export.build_pdf(enquiry)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 1000)


if __name__ == "__main__":
    unittest.main()
