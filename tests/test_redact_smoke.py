"""Smoke tests for the deterministic redaction pass.

The regex scrub is the reliable half of the privacy line (the model rewrite is
best-effort on top), so its behaviour is what must never regress: known secret
shapes get replaced, benign text is untouched, and what was removed is reported by
type without echoing the secret.
"""

from __future__ import annotations

from nixadmin.redact import Redaction, scrub


def test_scrub_removes_api_keys_and_tokens():
    out = scrub("my key is sk-abcdefghijklmnop12345 and token ghp_ABCDEFGHIJKLMNOP12345")
    assert "sk-abcdefghijklmnop12345" not in out.text
    assert "ghp_ABCDEFGHIJKLMNOP12345" not in out.text
    assert "[api-key]" in out.text and "[token]" in out.text
    assert out.removed  # reported, by placeholder type


def test_scrub_removes_email_ip_and_home_path():
    r = scrub("mail me at steven@posteo.de from 192.168.1.42, logs in /home/steve/.ssh")
    assert "steven@posteo.de" not in r.text and "[email]" in r.text
    assert "192.168.1.42" not in r.text and "[ip]" in r.text
    assert "/home/steve" not in r.text and "/home/[user]" in r.text


def test_scrub_leaves_benign_text_untouched():
    r = scrub("please install spotify and tell me if my wifi is working")
    assert r.text == "please install spotify and tell me if my wifi is working"
    assert r.removed == []


def test_redaction_changed_flag():
    assert Redaction(original="a", redacted="a").changed is False
    assert Redaction(original="a b", redacted="a").changed is True
