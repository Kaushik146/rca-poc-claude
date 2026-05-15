"""Regression coverage for scripts/pii_scrubber.py."""
from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
import pii_scrubber as P  # type: ignore


class EmailPattern(unittest.TestCase):
    def test_scrubs_standard_email(self):
        self.assertIn("<EMAIL>", P.scrub_string("error for user alice@example.com"))
    def test_scrubs_email_with_plus_tag(self):
        out = P.scrub_string("notification sent to user+billing@corp.io")
        self.assertIn("<EMAIL>", out)
        self.assertNotIn("billing@corp.io", out)
    def test_scrubs_multiple_emails(self):
        counts: dict[str, int] = {}
        P.scrub_string("from a@b.com to c@d.com", counts)
        self.assertEqual(counts.get("emails"), 2)


class JWTPattern(unittest.TestCase):
    def test_scrubs_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out = P.scrub_string(f"Authorization header: {jwt}")
        self.assertIn("<JWT>", out)
        self.assertNotIn(jwt, out)


class BearerTokenPattern(unittest.TestCase):
    def test_scrubs_bearer_token(self):
        out = P.scrub_string("got 401 with Authorization: Bearer abc123def456ghi789")
        self.assertIn("<BEARER_TOKEN>", out)
    def test_scrubs_bare_bearer(self):
        out = P.scrub_string("auth='Bearer ya29.A0AVA9y1abc123xyz'")
        self.assertIn("<BEARER_TOKEN>", out)


class APIKeyPatterns(unittest.TestCase):
    def test_scrubs_openai_style_key(self):
        self.assertIn("<API_KEY>", P.scrub_string("OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345"))
    def test_scrubs_github_pat(self):
        self.assertIn("<API_KEY>", P.scrub_string("token: ghp_abcdefghijklmnopqrstuvwxyz0123456789"))
    def test_scrubs_stripe_live(self):
        self.assertIn("<API_KEY>", P.scrub_string("STRIPE_KEY=sk_live_abc123def456ghi789jkl"))
    def test_scrubs_slack_token(self):
        self.assertIn("<API_KEY>", P.scrub_string("SLACK_BOT=xoxb-1234567890-abcdef-1234567"))


class AWSKeyPattern(unittest.TestCase):
    def test_scrubs_aws_access_key(self):
        out = P.scrub_string("aws_access_key_id=AKIAIOSFODNN7EXAMPLE")
        self.assertIn("<AWS_KEY>", out)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)


class IPPattern(unittest.TestCase):
    def test_scrubs_ipv4(self):
        self.assertIn("<IP>", P.scrub_string("connection from 192.168.1.42 refused"))
    def test_does_not_scrub_invalid_ipv4(self):
        self.assertIn("999.999.999.999", P.scrub_string("look at 999.999.999.999"))


class SSNPattern(unittest.TestCase):
    def test_scrubs_ssn(self):
        self.assertIn("<SSN>", P.scrub_string("SSN: 123-45-6789 on file"))


class PhonePattern(unittest.TestCase):
    def test_scrubs_us_phone(self):
        self.assertIn("<PHONE>", P.scrub_string("contact at +1 (555) 123-4567"))


class CreditCardPattern(unittest.TestCase):
    def test_scrubs_credit_card(self):
        self.assertIn("<CC>", P.scrub_string("card 4111 1111 1111 1111 declined"))


class FalsePositiveGuards(unittest.TestCase):
    def test_uuid_not_scrubbed(self):
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        self.assertIn(uuid, P.scrub_string(f"trace_id={uuid}"))
    def test_git_sha_not_scrubbed(self):
        sha = "7c4f9a2cd3e5b8f1a9d2e7c6b4a3f2e1d8c5b9a7"
        self.assertIn(sha, P.scrub_string(f"commit {sha}"))
    def test_short_git_sha_not_scrubbed(self):
        self.assertIn("7c4f9a2", P.scrub_string("deployed 7c4f9a2"))
    def test_version_string_not_scrubbed(self):
        out = P.scrub_string("running v1.2.3-beta on python 3.11")
        self.assertIn("1.2.3", out)
        self.assertIn("3.11", out)
    def test_url_without_creds_not_scrubbed(self):
        self.assertIn("github.com/org/repo/pull/42",
                      P.scrub_string("see https://github.com/org/repo/pull/42"))
    def test_log_level_words_not_scrubbed(self):
        out = P.scrub_string("INFO 2026-05-08T12:00:00Z handled request")
        self.assertIn("INFO", out)
        self.assertIn("2026-05-08T12:00:00Z", out)


class JSONWalking(unittest.TestCase):
    def test_scrubs_nested_strings(self):
        data = {
            "logs": [
                {"message": "user alice@example.com failed", "level": "ERROR"},
                {"message": "retry from 10.0.0.5", "level": "WARN"},
            ],
            "metadata": {"reporter": "ops@team.io"},
        }
        out = P.scrub_obj(data)
        self.assertIn("<EMAIL>", out["logs"][0]["message"])
        self.assertIn("<IP>", out["logs"][1]["message"])
        self.assertIn("<EMAIL>", out["metadata"]["reporter"])
        self.assertEqual(out["logs"][0]["level"], "ERROR")
        self.assertEqual(out["logs"][1]["level"], "WARN")

    def test_keys_are_not_scrubbed(self):
        data = {"alice@example.com": "value"}
        self.assertIn("alice@example.com", P.scrub_obj(data))

    def test_non_string_types_passed_through(self):
        data = {"count": 42, "rate": 3.14, "enabled": True, "missing": None, "items": [1, 2, 3]}
        self.assertEqual(P.scrub_obj(data), data)

    def test_scrub_counts_aggregated_across_walk(self):
        counts: dict[str, int] = {}
        data = {"a": "alice@example.com", "b": ["bob@example.com", "carol@example.com"]}
        P.scrub_obj(data, counts)
        self.assertEqual(counts.get("emails"), 3)


class CLIInvocation(unittest.TestCase):
    def test_scrub_string_via_cli_flag(self):
        import subprocess
        scrubber = HERE.parents[1] / "pii_scrubber.py"
        res = subprocess.run(
            [sys.executable, str(scrubber), "--scrub-string", "user a@b.com pinged 10.0.0.1", "--no-stats"],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("<EMAIL>", res.stdout)
        self.assertIn("<IP>", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
