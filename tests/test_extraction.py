import unittest
import threading
import time
from types import SimpleNamespace

from email_lead_scraper import extraction as extraction_module
from email_lead_scraper.extraction import (
    decode_cloudflare_email,
    extract_emails_with_context,
    classify_email,
    filter_emails_by_site_domain,
    is_site_relevant,
    role_match_score,
    validate_lead_rows,
)


class EmailParsingTests(unittest.TestCase):
    def test_cloudflare_email_decoding(self):
        # Encrypted string for 'dr.smith@clinic.com' with XOR key 0x2e
        cf_hex = "2e4a5c005d43475a466e4d424740474d004d4143"
        decoded = decode_cloudflare_email(cf_hex)
        self.assertEqual(decoded, "dr.smith@clinic.com")

    def test_extract_cloudflare_email_from_html(self):
        html = '''
        <div>
            <a href="/cdn-cgi/l/email-protection#2e4a5c005d43475a466e4d424740474d004d4143">Contact Us</a>
            <span data-cfemail="2e4a5c005d43475a466e4d424740474d004d4143"></span>
        </div>
        '''
        results = extract_emails_with_context(html)
        emails = {r["email"] for r in results}
        self.assertIn("dr.smith@clinic.com", emails)

    def test_extract_obfuscated_emails_from_text(self):
        html = '''
        <p>Reach out to Dr. John at john.smith [at] clinic [dot] com or jane.doe(at)hospital(dot)org</p>
        '''
        results = extract_emails_with_context(html)
        emails = {r["email"] for r in results}
        self.assertIn("john.smith@clinic.com", emails)
        self.assertIn("jane.doe@hospital.org", emails)

    def test_classify_healthcare_generic_emails(self):
        self.assertEqual(classify_email("intake@clinic.com"), "generic")
        self.assertEqual(classify_email("patientadvocate@hospital.org"), "generic")
        self.assertEqual(classify_email("billing@medgroup.com"), "generic")
        self.assertEqual(classify_email("dr.smith@clinic.com"), "personal")

    def test_department_and_executive_mailboxes_are_generic(self):
        for email in (
            "Diplomas@rcpe.ac.uk", "PACES@rcpe.ac.uk",
            "PhysicianRelations@honorhealth.com", "ceo@rcpe.ac.uk",
            "president@rcpe.ac.uk", "clinicaltrials@honorhealth.com",
        ):
            with self.subTest(email=email):
                self.assertEqual(classify_email(email), "generic")

    def test_public_suffix_domain_filter_rejects_lookalike_ac_uk(self):
        emails = {"valid@rcpe.ac.uk", "bad@rcpeac.uk", "other@ox.ac.uk"}
        self.assertEqual(
            filter_emails_by_site_domain(emails, "https://www.rcpe.ac.uk/"),
            {"valid@rcpe.ac.uk"},
        )

    def test_context_rejects_heading_as_person_and_sentence_as_title(self):
        html = '''
        <section><h3>Consultation Scenarios</h3>
        <p>Learn about our physician-led governance model.</p>
        <a href="mailto:diplomas@rcpe.ac.uk">Email</a></section>
        '''
        result = extract_emails_with_context(html)[0]
        self.assertEqual(result["person_name"], "")
        self.assertEqual(result["role_title"], "physician")
        self.assertEqual(result["email_type"], "generic")

    def test_role_filter_keeps_same_domain_personal_email_for_later_ranking(self):
        html = '<p>Staff contact: <a href="mailto:a.serelis@rcpe.ac.uk">email</a></p>'
        results = extract_emails_with_context(html, ["CEO"])
        self.assertEqual([row["email"] for row in results], ["a.serelis@rcpe.ac.uk"])

    def test_role_filter_still_rejects_unrelated_generic_email(self):
        html = '<p>Staff contact: <a href="mailto:info@rcpe.ac.uk">email</a></p>'
        self.assertEqual(extract_emails_with_context(html, ["CEO"]), [])

    def test_industry_word_does_not_promote_generic_email(self):
        for email in ("hello@amaehealth.com", "info@cases.org", "support@freeclinics.com"):
            with self.subTest(email=email):
                html = f'<p>Our clinic can help. Contact <a href="mailto:{email}">{email}</a></p>'
                self.assertEqual(
                    extract_emails_with_context(html, ["Clinic Director"]), []
                )

    def test_full_role_phrase_can_keep_generic_role_contact(self):
        html = '''
        <p>Contact our Clinic Director at
        <a href="mailto:office@exampleclinic.com">office@exampleclinic.com</a>.</p>
        '''
        results = extract_emails_with_context(html, ["Clinic Director"])
        self.assertEqual([row["email"] for row in results], ["office@exampleclinic.com"])

    def test_country_and_industry_relevance(self):
        healthcare = "<html><body>Hospital patient and physician services</body></html>"
        self.assertEqual(
            is_site_relevant("https://rcpe.ac.uk", healthcare, "Hospital", "United States")[0],
            False,
        )
        self.assertEqual(
            is_site_relevant("https://apple.com/leadership", "<p>Technology executives</p>",
                             "Hospital", "United States")[0],
            False,
        )
        self.assertEqual(
            is_site_relevant("https://honorhealth.com", healthcare,
                             "Hospital", "United States")[0],
            True,
        )
        self.assertEqual(
            is_site_relevant(
                "https://example.com",
                "<p>Our outpatient clinic and physicians</p>",
                ["Hospital", "Clinic", "Healthcare"],
                "United States",
            )[0],
            True,
        )

    def test_fuzzy_role_matching_tolerates_near_matches(self):
        self.assertGreaterEqual(
            role_match_score("Senior Clinic Director", ["Clinic Director"]),
            90,
        )

    def test_email_validator_normalizes_and_rejects_bad_syntax(self):
        rows = [
            {"email": "Jane.Smith@EXAMPLE.com"},
            {"email": "not-an-email"},
            {"email": ""},
        ]

        accepted, stats = validate_lead_rows(rows, check_mx=False)

        self.assertEqual(
            [row["email"] for row in accepted],
            ["Jane.Smith@example.com", ""],
        )
        self.assertEqual(stats["normalized"], 1)
        self.assertEqual(stats["invalid_syntax"], 1)

def test_mx_validation_runs_once_per_domain_and_in_parallel(monkeypatch):
    active = 0
    maximum_active = 0
    deliverability_calls = 0
    lock = threading.Lock()

    def fake_validate(email, *, check_deliverability, dns_resolver=None):
        nonlocal active, maximum_active, deliverability_calls
        if not check_deliverability:
            return SimpleNamespace(normalized=email.lower())
        with lock:
            active += 1
            deliverability_calls += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return SimpleNamespace(normalized=email.lower())

    monkeypatch.setattr(extraction_module, "validate_email", fake_validate)
    monkeypatch.setattr(extraction_module, "caching_resolver", lambda **_kwargs: object())
    rows = [
        {"email": "one@alpha.example"},
        {"email": "two@alpha.example"},
        {"email": "three@beta.example"},
    ]

    accepted, stats = extraction_module.validate_lead_rows(
        rows,
        check_mx=True,
        max_workers=2,
    )

    assert len(accepted) == 3
    assert stats["invalid_domain"] == 0
    assert deliverability_calls == 2
    assert maximum_active == 2


if __name__ == "__main__":
    unittest.main()
