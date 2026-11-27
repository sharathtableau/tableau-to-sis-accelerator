"""E11: secret custody, endpoint allowlist, redaction. Written before the code.
  * The app talks to Tableau only over https, and only to Tableau Cloud (*.online.tableau.com, *.tableau.com) or a
    host the owner lists in TABLEAU_ALLOWED_HOSTS; anything else is refused before a request is made.
  * Error text that can reach the screen or a report never carries a credential: PAT secrets, X-Tableau-Auth
    tokens, passwords, JWTs and private keys are masked.
  * A missing PAT secret is reported by name only (no value, no partial value).
  * No packaged module of the app contains a credential (static scan of the bundle)."""
from pathlib import Path
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_security as S  # noqa: E402

PAT = 'q8Xk2LmN0pQr5sTuVw==:aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV'


class Allowlist(unittest.TestCase):
    def test_tableau_cloud_and_listed_hosts_only(self):
        self.assertTrue(S.tableau_url_allowed('https://prod-useast-b.online.tableau.com'))
        self.assertTrue(S.tableau_url_allowed('https://10ay.online.tableau.com/#/site/x'))
        self.assertFalse(S.tableau_url_allowed('http://prod-useast-b.online.tableau.com'))      # not https
        self.assertFalse(S.tableau_url_allowed('https://evil.example.com'))
        self.assertFalse(S.tableau_url_allowed('https://online.tableau.com.evil.com'))
        old = os.environ.get('TABLEAU_ALLOWED_HOSTS')
        os.environ['TABLEAU_ALLOWED_HOSTS'] = 'tableau.corp.example.com'
        try:
            self.assertTrue(S.tableau_url_allowed('https://tableau.corp.example.com/'))
        finally:
            if old is None:
                os.environ.pop('TABLEAU_ALLOWED_HOSTS')
            else:
                os.environ['TABLEAU_ALLOWED_HOSTS'] = old

    def test_the_client_refuses_before_any_request(self):
        import tableau_server as TS
        with self.assertRaises(TS.TableauAuthError) as cm:
            TS.TableauRestClient('https://evil.example.com')
        self.assertIn('ENDPOINT_NOT_ALLOWED', str(cm.exception))
        TS.TableauRestClient('https://prod-useast-b.online.tableau.com')          # allowed


class Redaction(unittest.TestCase):
    def test_masks_every_credential_shape(self):
        text = (f'<credentials personalAccessTokenName="ci" personalAccessTokenSecret="{PAT}"/> '
                f'X-Tableau-Auth: abcdEFGH1234ijklMNOP5678|xyz password=Hunter2! token="{PAT}" '
                'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbHVl '
                '-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----')
        out = S.redact(text)
        for secret in (PAT, 'abcdEFGH1234ijklMNOP5678', 'Hunter2!', 'eyJhbGciOiJIUzI1NiJ9', 'MIIEvQIBADANBg'):
            self.assertNotIn(secret, out)
        self.assertIn('personalAccessTokenName="ci"', out)          # names stay; values go

    def test_tableau_errors_are_redacted(self):
        import tableau_server as TS

        class Resp:
            status_code = 401
            content = b'not xml'
            text = f'bad token {PAT} for password=Hunter2!'
        with self.assertRaises(TS.TableauAuthError) as cm:
            TS.TableauRestClient('https://x.online.tableau.com')._check(Resp())
        self.assertNotIn(PAT, str(cm.exception))
        self.assertNotIn('Hunter2!', str(cm.exception))


class MissingSecret(unittest.TestCase):
    def test_named_never_valued(self):
        import tableau_server as TS
        for k in ('tableau_pat_USERNAME', 'tableau_pat_PASSWORD'):
            os.environ.pop(k, None)
        name, secret = TS._pat_from_sis_secret()
        self.assertEqual((name, secret), (None, None))
        diag = str(getattr(TS, '_PAT_LOOKUP_DIAGNOSTIC', ''))
        self.assertNotIn(PAT, diag)


class Bundle(unittest.TestCase):
    def test_no_credential_in_any_packaged_module(self):
        import v3_package as V
        files = V.modules(['v3_sis_app.py', 'v3_console.py', 'v3_app.py', 'v3_runtime.py'])
        found = {name: S.scan(payload.decode('utf-8', 'replace')) for name, payload in files.items()}
        self.assertEqual({k: v for k, v in found.items() if v}, {})

    def test_the_scan_finds_a_planted_secret(self):
        self.assertTrue(S.scan(f'TOKEN = "{PAT}"'))
        self.assertTrue(S.scan('password = "Hunter2!x"'))
        self.assertFalse(S.scan('password = os.environ.get("X")'))
        self.assertFalse(S.scan("SIS_SECRET_NAME = 'tableau_pat'"))


if __name__ == '__main__':
    unittest.main()
