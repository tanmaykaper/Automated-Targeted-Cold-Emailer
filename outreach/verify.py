"""Mandatory multi-layer email verification gate.

Layers, in order (each can decide; later layers only run when earlier ones are inconclusive):
  1. syntax           RFC-ish structure, typo TLDs
  2. policy           role accounts (info@, support@ …), disposable domains
  3. dns              domain resolves, has MX (or implicit A), no null-MX
  4. smtp             RCPT TO probe + catch-all detection. Auto-disabled for the run when
                      port 25 is blocked (always the case on GitHub-hosted runners).
  5. api verifier     first configured of Hunter / ZeroBounce / MillionVerifier / Reoon / Bouncer
  6. provider proof   the source itself verified the mailbox (Apollo "Verified", Hunter "valid"…)

Outcome → VERIFIED | CATCH_ALL (+confidence) | UNVERIFIED_QUARANTINED | INVALID.
A pattern guess can only become VERIFIED through layer 4 or 5 — never through layer 6.
"""
from __future__ import annotations

import logging
import random
import re
import smtplib
import socket
import string
import time
from datetime import timedelta

from .enrich.domain import WEBMAIL
from .http import Http, ProviderError
from .models import EmailCandidate, EmailStatus, VerificationResult, utcnow
from .profile import ROLE_EMAIL_PREFIXES
from .settings import Settings

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._%+'-]{0,62}[a-z0-9])?@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.[a-z]{2,24}$"
)
TYPO_TLDS = {"con", "cmo", "vom", "comm", "cm", "ocm", "om", "c0m", "nte", "ner", "coom", "conm"}
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
    "trashmail.com", "getnada.com", "sharklasers.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
    "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com", "tempinbox.com", "spamgourmet.com",
}
# Mailing lists / group aliases: a "verified" mailbox here is a list that fans out to many people.
LIST_DOMAINS = {
    "googlegroups.com", "groups.google.com", "groups.io", "yahoogroups.com", "freelists.org",
    "lists.sourceforge.net", "groups.outlook.com", "simplelists.com", "mailman.org",
}
LIST_SUBDOMAIN_PREFIXES = ("lists.", "list.", "listserv.", "groups.", "mailman.", "googlegroups.")
PROVIDER_PROOF = {"verified", "valid"}
PROVIDER_PROOF_MAX_AGE = timedelta(days=180)


def syntax_ok(email: str) -> tuple[bool, str]:
    e = (email or "").strip().lower()
    if len(e) > 254 or not EMAIL_RE.match(e) or ".." in e:
        return False, "malformed address"
    if e.rsplit(".", 1)[-1] in TYPO_TLDS:
        return False, "typo TLD"
    return True, "ok"


def policy_ok(email: str) -> tuple[bool, str]:
    local, domain = email.lower().split("@", 1)
    base = re.split(r"[.+_-]", local)[0]
    if local in ROLE_EMAIL_PREFIXES or (base in ROLE_EMAIL_PREFIXES and len(base) >= 4):
        return False, f"role account ({local}@)"
    if domain in DISPOSABLE:
        return False, "disposable domain"
    if domain in LIST_DOMAINS or domain.startswith(LIST_SUBDOMAIN_PREFIXES):
        return False, "mailing-list / group address"
    return True, "ok"


class DNS:
    def __init__(self):
        self.cache: dict[str, tuple[str, list[str]]] = {}

    def mx(self, domain: str) -> tuple[str, list[str]]:
        """('ok'|'implicit'|'null_mx'|'nxdomain'|'no_mail'|'error', hosts)."""
        if domain in self.cache:
            return self.cache[domain]
        import dns.exception
        import dns.resolver
        res: tuple[str, list[str]]
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=6)
            hosts = [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
            res = ("null_mx", []) if hosts in ([""], ["."]) or all(h in ("", ".") for h in hosts) else ("ok", hosts)
        except dns.resolver.NXDOMAIN:
            res = ("nxdomain", [])
        except dns.resolver.NoAnswer:
            try:
                dns.resolver.resolve(domain, "A", lifetime=6)
                res = ("implicit", [domain])
            except Exception:
                res = ("no_mail", [])
        except (dns.exception.DNSException, OSError) as e:
            log.debug("DNS error for %s: %s", domain, e)
            res = ("error", [])
        self.cache[domain] = res
        return res


class SMTPProbe:
    """Best-effort RCPT TO probe. Detects port-25 blocking once and disables itself."""

    def __init__(self, settings: Settings):
        self.enabled = settings.smtp_probe_enabled
        self.helo = settings.smtp_helo_domain
        self.unreachable_streak = 0
        self.disabled_reason = ""
        self.catch_all: dict[str, bool] = {}

    def _connect(self, host: str) -> smtplib.SMTP:
        smtp = smtplib.SMTP(timeout=5)
        smtp.connect(host, 25)
        smtp.helo(self.helo)
        smtp.mail(f"probe@{self.helo}")
        return smtp

    def probe(self, email: str, mx_hosts: list[str]) -> str:
        """'valid' | 'invalid' | 'catch_all' | 'unknown' | 'unavailable'."""
        if not self.enabled or self.disabled_reason or not mx_hosts:
            return "unavailable"
        domain = email.split("@", 1)[1]
        try:
            smtp = self._connect(mx_hosts[0])
        except (OSError, smtplib.SMTPException) as e:
            self.unreachable_streak += 1
            if self.unreachable_streak >= 2:
                self.disabled_reason = f"port 25 unreachable ({type(e).__name__}) — SMTP probing disabled for this run"
                log.warning(self.disabled_reason)
            return "unavailable"
        self.unreachable_streak = 0
        try:
            if domain not in self.catch_all:
                junk = "".join(random.choices(string.ascii_lowercase, k=14))
                code, _ = smtp.rcpt(f"{junk}@{domain}")
                self.catch_all[domain] = code in (250, 251)
            if self.catch_all[domain]:
                return "catch_all"
            code, _ = smtp.rcpt(email)
            if code in (250, 251):
                return "valid"
            if code in (550, 551, 553, 554):
                return "invalid"
            return "unknown"
        except (OSError, smtplib.SMTPException):
            return "unknown"
        finally:
            try:
                smtp.quit()
            except Exception:
                pass


class APIVerifiers:
    """Each returns ('valid'|'invalid'|'catch_all'|'unknown', confidence, provider)."""

    ORDER = ("millionverifier", "zerobounce", "reoon", "bouncer", "hunter")

    def __init__(self, http: Http, settings: Settings):
        self.http = http
        self.s = settings

    def available(self) -> list[str]:
        return [p for p in self.ORDER if getattr(self.s.keys, p) and self.http.available(p)]

    def verify(self, email: str) -> tuple[str, int, str]:
        for p in self.available():
            try:
                outcome, conf = getattr(self, f"_{p}")(email)
                return outcome, conf, p
            except ProviderError as e:
                log.info("verifier %s failed for %s: %s", p, email, e.category)
        return "unknown", 0, ""

    def _hunter(self, email):
        for attempt in range(3):
            resp = self.http.request("GET", "https://api.hunter.io/v2/email-verifier", provider="hunter",
                                     params={"email": email, "api_key": self.s.keys.hunter}, expect_json=False,
                                     ok_statuses=(202, 222), timeout=(6, 40))
            if resp.status_code == 200:
                d = resp.json().get("data") or {}
                st = (d.get("status") or "").lower()
                score = int(d.get("score") or 0)
                if st == "valid":
                    return "valid", max(score, 85)
                if st in ("invalid", "disposable"):
                    return "invalid", 0
                if st == "accept_all":
                    return "catch_all", score
                return "unknown", score  # webmail / unknown
            time.sleep(4 * (attempt + 1))  # 202: still verifying; 222: SMTP hiccup
        return "unknown", 0

    def _zerobounce(self, email):
        d = self.http.request("GET", "https://api.zerobounce.net/v2/validate", provider="zerobounce",
                              params={"api_key": self.s.keys.zerobounce, "email": email, "ip_address": ""})
        st = (d.get("status") or "").lower()
        return {"valid": ("valid", 92), "invalid": ("invalid", 0), "catch-all": ("catch_all", 50),
                "spamtrap": ("invalid", 0), "abuse": ("invalid", 0), "do_not_mail": ("invalid", 0)}.get(st, ("unknown", 0))

    def _millionverifier(self, email):
        d = self.http.request("GET", "https://api.millionverifier.com/api/v3/", provider="millionverifier",
                              params={"api": self.s.keys.millionverifier, "email": email, "timeout": 20},
                              timeout=(6, 30))
        st = (d.get("result") or "").lower()
        return {"ok": ("valid", 92), "catch_all": ("catch_all", 50), "invalid": ("invalid", 0),
                "disposable": ("invalid", 0)}.get(st, ("unknown", 0))

    def _reoon(self, email):
        d = self.http.request("GET", "https://emailverifier.reoon.com/api/v1/verify", provider="reoon",
                              params={"email": email, "key": self.s.keys.reoon, "mode": "power"}, timeout=(6, 60))
        st = (d.get("status") or "").lower()
        return {"safe": ("valid", 92), "valid": ("valid", 90), "invalid": ("invalid", 0), "disabled": ("invalid", 0),
                "disposable": ("invalid", 0), "spamtrap": ("invalid", 0), "catch_all": ("catch_all", 50),
                "role_account": ("invalid", 0)}.get(st, ("unknown", 0))

    def _bouncer(self, email):
        d = self.http.request("GET", "https://api.usebouncer.com/v1.1/email/verify", provider="bouncer",
                              params={"email": email}, headers={"x-api-key": self.s.keys.bouncer}, timeout=(6, 30))
        st = (d.get("status") or "").lower()
        if st == "deliverable":
            return "valid", 92
        if st == "undeliverable":
            return "invalid", 0
        if st == "risky" and (d.get("reason") or "").lower() == "accept_all":
            return "catch_all", 50
        return "unknown", 0


class Verifier:
    def __init__(self, http: Http, settings: Settings):
        self.s = settings
        self.dns = DNS()
        self.smtp = SMTPProbe(settings)
        self.apis = APIVerifiers(http, settings)

    def verify(self, cand: EmailCandidate) -> VerificationResult:
        email = cand.email.strip().lower()
        r = VerificationResult(email=email)
        ok, why = syntax_ok(email)
        r.checks["syntax"] = why
        if not ok:
            return self._done(r, EmailStatus.INVALID, 0, why, "syntax")
        ok, why = policy_ok(email)
        r.checks["policy"] = why
        if not ok:
            return self._done(r, EmailStatus.INVALID, 0, why, "policy")
        domain = email.split("@", 1)[1]
        mx_state, hosts = self.dns.mx(domain)
        r.checks["dns"] = mx_state
        if mx_state in ("nxdomain", "null_mx", "no_mail"):
            return self._done(r, EmailStatus.INVALID, 0, f"domain cannot receive mail ({mx_state})", "dns")
        webmail = domain in WEBMAIL

        smtp = "unavailable" if webmail or mx_state == "error" else self.smtp.probe(email, hosts)
        r.checks["smtp"] = smtp if not self.smtp.disabled_reason else "unavailable (port 25 blocked)"
        if smtp == "invalid":
            return self._done(r, EmailStatus.INVALID, 0, "mailbox rejected at RCPT TO", "smtp")
        if smtp == "valid":
            return self._done(r, EmailStatus.VERIFIED, 95, "mailbox accepted at RCPT TO (non-catch-all server)", "smtp")
        catch_all = smtp == "catch_all" or bool(cand.catch_all_hint)

        api, api_conf, api_name = self.apis.verify(email) if self.apis.available() else ("unknown", 0, "")
        r.checks["api"] = f"{api_name}:{api}" if api_name else "none configured"
        if api == "invalid":
            return self._done(r, EmailStatus.INVALID, 0, f"{api_name} says undeliverable", f"api:{api_name}")
        if api == "valid" and not catch_all:
            return self._done(r, EmailStatus.VERIFIED, api_conf, f"{api_name} verified the mailbox", f"api:{api_name}")
        if api in ("valid", "catch_all"):
            catch_all = True

        fresh = cand.provider_checked_at is None or utcnow() - cand.provider_checked_at <= PROVIDER_PROOF_MAX_AGE
        proof = cand.source != "pattern" and cand.provider_status in PROVIDER_PROOF and fresh
        r.checks["provider"] = f"{cand.source}:{cand.provider_status or 'none'}" + ("" if fresh else " (stale)")
        if proof and not catch_all:
            return self._done(r, EmailStatus.VERIFIED, min(cand.confidence or 85, 90),
                              f"{cand.source} verified the mailbox; MX ok", "provider")
        if catch_all:
            # Catch-all servers accept everything; confidence = best mailbox-level evidence we have.
            if proof:
                conf = 75  # provider-verified on a catch-all server: likely, never certain
            elif cand.provider_status == "accept_all":
                conf = cand.confidence
            elif api_conf:
                conf = min(api_conf, 50)
            else:
                conf = 25 if cand.source == "pattern" else min(cand.confidence, 50)
            return self._done(r, EmailStatus.CATCH_ALL, conf, "catch-all domain; mailbox cannot be confirmed", "catch_all")
        return self._done(r, EmailStatus.UNVERIFIED_QUARANTINED, cand.confidence if not cand.source == "pattern" else 0,
                          "no mailbox-level evidence (SMTP blocked/inconclusive, no API verifier result)", "none")

    @staticmethod
    def _done(r: VerificationResult, status: EmailStatus, conf: int, reason: str, method: str) -> VerificationResult:
        r.status, r.confidence, r.reason, r.method = status, int(conf), reason, method
        return r


def sendable(v: VerificationResult | None, settings: Settings) -> bool:
    if v is None:
        return False
    # Re-apply address policy so records verified before a policy change can never send.
    if not syntax_ok(v.email)[0] or not policy_ok(v.email)[0]:
        return False
    if v.status == EmailStatus.VERIFIED:
        return True
    return v.status == EmailStatus.CATCH_ALL and v.confidence >= settings.catch_all_min_confidence
