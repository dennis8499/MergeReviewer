"""Redaction distinguishes program structure from embedded credential values."""
import unittest
import base64

from tests.fixtures import SCRIPTS
import review_content as content
import workspace_report as report


class SourceRedactionTests(unittest.TestCase):
    def test_connection_identifiers_and_configuration_calls_remain_readable(self):
        cases = [
            ("parameter", "public Task<Row> QueryAsync(string connectionString, Query query)"),
            ("lookup", 'var db = config.GetConnectionString("Default");'),
            ("assigned-lookup", 'var connectionString = builder.Configuration.GetConnectionString("Default");'),
            ("symbolic-lookup", 'connectionString = config.GetConnectionString(connectionName);'),
            ("expression", 'connectionString = settings.ConnectionString;'),
            ("section", '"ConnectionStrings": {'),
            ("token-argument", 'await Send(token: cancellationToken);'),
        ]
        for label, line in cases:
            with self.subTest(case=label):
                self.assertEqual(([line], False), content.redact_lines([line], "Service.cs"))

    def test_embedded_credentials_are_hidden_without_removing_adjacent_code(self):
        cases = [
            ("password", 'var password = "SYNTHETIC_PASSWORD";', "SYNTHETIC_PASSWORD"),
            ("token", '"Token": "SYNTHETIC_TOKEN",', "SYNTHETIC_TOKEN"),
            ("connection", 'var connectionString = "Server=example.invalid;Password=SYNTHETIC_DB;";', "SYNTHETIC_DB"),
            ("literal-secret-in-lookup", 'connectionString = config.GetConnectionString("Password=SYNTHETIC_LOOKUP");', "SYNTHETIC_LOOKUP"),
            ("url", 'var ftp = "ftp://user:SYNTHETIC_FTP@example.invalid/data";', "SYNTHETIC_FTP"),
            ("bearer", 'Authorization: Bearer SYNTHETIC_BEARER', "SYNTHETIC_BEARER"),
            ("quoted-assignment-bearer", 'token = "Bearer SYNTHETIC_QUOTED";', "SYNTHETIC_QUOTED"),
            ("bare-assignment-bearer", 'token = Bearer SYNTHETIC_BARE', "SYNTHETIC_BARE"),
        ]
        for label, line, secret in cases:
            with self.subTest(case=label):
                actual, changed = content.redact_lines(["before();", line, "after();"], "Demo.cs")
                self.assertTrue(changed)
                self.assertEqual(3, len(actual))
                self.assertEqual("before();", actual[0])
                self.assertEqual("after();", actual[-1])
                self.assertNotIn(secret, "\n".join(actual))
                self.assertNotIn(secret, report.redact(line))

    def test_private_key_blocks_and_key_files_preserve_rows_without_exposing_values(self):
        source = ["before();", "-----BEGIN PRIVATE KEY-----", "SYNTHETIC_KEY", "-----END PRIVATE KEY-----", "after();"]
        lines, changed = content.redact_lines(source, "data.txt")
        self.assertTrue(changed)
        self.assertEqual(["before();", *(["[私鑰內容已省略]"] * 3), "after();"], lines)
        self.assertEqual((["[私鑰內容已省略]"] * 5, True), content.redact_lines(source, "key.pem"))

    def test_report_excerpt_preserves_connection_structure_and_masks_literal_credentials(self):
        safe = ['public Task<Row> QueryAsync(string connectionString, Query query)',
                'var db = config.GetConnectionString("Default");',
                'connectionString = settings.ConnectionString;']
        excerpt, note = report.safe_excerpt(("\n".join(safe) + "\n").encode(), "Service.cs", 1, 3)
        self.assertEqual("\n".join(safe), excerpt)
        self.assertIsNone(note)
        self.assertEqual(excerpt, report.redact(excerpt))
        unsafe = 'var db = "Server=example.invalid;Password=SYNTHETIC_VALUE;";'
        excerpt, note = report.safe_excerpt((unsafe + "\n").encode(), "Service.cs", 1, 1)
        self.assertNotIn("SYNTHETIC_VALUE", excerpt)
        self.assertIn("遮蔽", note)
        for path in ("settings.json", "secret.pem"):
            excerpt, note = report.safe_excerpt(unsafe.encode(), path, 1, 1)
            self.assertIsNone(excerpt)
            self.assertTrue(note)

    def test_basic_and_constructor_credentials_are_masked_with_the_call_structure_preserved(self):
        basic = base64.b64encode(b'user:SYNTHETIC_BASIC_PASSWORD').decode()
        cases = [
            ('Authorization: Basic ' + basic, basic),
            ('var header = "Basic ' + basic + '";', basic),
            ('var header = new AuthenticationHeaderValue("Bearer", "SYNTHETIC_CONSTRUCTOR");', 'SYNTHETIC_CONSTRUCTOR'),
            ('client.SetPassword("SYNTHETIC_SETTER");', 'SYNTHETIC_SETTER'),
            ('client.SetPrivateKey("SYNTHETIC_PRIVATE_LITERAL");', 'SYNTHETIC_PRIVATE_LITERAL'),
            ('client.SetUserToken("SYNTHETIC_TOKEN_LITERAL");', 'SYNTHETIC_TOKEN_LITERAL'),
        ]
        for line, secret in cases:
            with self.subTest(line=line):
                visible, changed = content.redact_lines([line], "Service.cs")
                self.assertTrue(changed)
                self.assertNotIn(secret, visible[0])
                self.assertNotIn(secret, report.redact(line))
        line = 'var header = new AuthenticationHeaderValue("Bearer", "SYNTHETIC_CONSTRUCTOR");'
        visible, _ = content.redact_lines([line], "Service.cs")
        self.assertIn('AuthenticationHeaderValue("Bearer",', visible[0])
        safe = ['var header = new AuthenticationHeaderValue("Bearer", token);',
                'public Task Query(string connectionString, CancellationToken token)',
                '// Basic arithmetic remains readable.']
        self.assertEqual((safe, False), content.redact_lines(safe, "Service.cs"))

    def test_multiline_authentication_is_classified_before_selecting_the_report_range(self):
        source = ['var header = new AuthenticationHeaderValue(', '    "Bearer",', '    "SYNTHETIC_MULTILINE");', 'after();']
        visible, changed = content.redact_lines(source, "Service.cs")
        self.assertTrue(changed)
        self.assertEqual(4, len(visible))
        self.assertIn('"Bearer"', visible[1])
        self.assertNotIn('SYNTHETIC_MULTILINE', '\n'.join(visible))
        self.assertEqual('after();', visible[-1])
        excerpt, note = report.safe_excerpt(('\n'.join(source) + '\n').encode(), 'Service.cs', 3, 3)
        self.assertNotIn('SYNTHETIC_MULTILINE', excerpt)
        self.assertIn('遮蔽', note)
