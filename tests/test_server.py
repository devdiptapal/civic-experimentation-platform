"""Tests for the web application and API, through real HTTP.

A server is started on an ephemeral port and driven with the standard
library's HTTP client, so these exercise exactly what a browser or a
calling service would: cookies, redirects, forms, CSRF tokens, headers,
and JSON.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from civicexp.lifecycle import REQUIRED_APPROVALS
from civicexp.web.app import App, make_server
from tests.helpers import ELIGIBLE, PASSWORD, ROLE_HOLDER, WorkspaceFixture, drive_traffic


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


class Client:
    """A browser stand-in: keeps cookies, does not follow redirects."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect
        )
        self.token = ""

    def request(self, method, path, *, data=None, headers=None, raw=None):
        body = raw
        heads = dict(headers or {})
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            heads.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=heads)
        try:
            with self.opener.open(req, timeout=30) as response:
                return response.status, dict(response.headers), response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode()

    def get(self, path, **kw):
        status, headers, body = self.request("GET", path, **kw)
        found = re.search(r'name="_csrf" value="([^"]+)"', body)
        if found:
            self.token = found.group(1)
        return status, headers, body

    def post(self, path, data=None, *, csrf=True):
        fields = dict(data or {})
        if csrf:
            fields.setdefault("_csrf", self.token)
        return self.request("POST", path, data=fields)

    def login(self, username, password=PASSWORD):
        _, _, page = self.get("/login")
        token = re.search(r'name="_login" value="([^"]+)"', page).group(1)
        status, headers, _ = self.post(
            "/login", {"_login": token, "username": username, "password": password,
                       "next": "/"}, csrf=False)
        if status == 303:
            self.get("/")
        return status, headers


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = WorkspaceFixture()
        cls.app = App(cls.fx.ws)
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.fx.cleanup()

    def client(self, username=None):
        client = Client(self.base)
        if username:
            status, _ = client.login(username)
            self.assertEqual(status, 303, f"could not sign in as {username}")
        return client

    def api(self, method, path, token, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=body, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")


class TestSignIn(ServerCase):
    def test_health_is_public_and_pages_are_not(self):
        status, _, body = self.client().get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        status, headers, _ = self.client().get("/experiments")
        self.assertEqual(status, 303)
        self.assertIn("/login?next=/experiments", headers["Location"])

    def test_session_cookie_and_headers_are_strict(self):
        client = Client(self.base)
        _, _, page = client.get("/login")
        token = re.search(r'name="_login" value="([^"]+)"', page).group(1)
        status, headers, _ = client.post("/login", {"_login": token, "username": "owner",
                                                    "password": PASSWORD}, csrf=False)
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict"):
            self.assertIn(flag, cookie)
        status, headers, _ = client.get("/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_wrong_password_is_refused_then_throttled(self):
        client = Client(self.base)
        for _ in range(5):
            status, _ = client.login("watcher", "not the password at all")
            self.assertEqual(status, 401)
        status, _ = client.login("watcher")
        self.assertEqual(status, 429)
        self.app.throttle.success("user:watcher", "ip:127.0.0.1")

    def test_next_parameter_cannot_redirect_off_site(self):
        client = Client(self.base)
        _, _, page = client.get("/login")
        token = re.search(r'name="_login" value="([^"]+)"', page).group(1)
        _, headers, _ = client.post("/login", {
            "_login": token, "username": "owner", "password": PASSWORD,
            "next": "//evil.example/steal"}, csrf=False)
        self.assertEqual(headers["Location"], "/")

    def test_state_changes_need_the_csrf_token(self):
        client = self.client("owner")
        status, _, body = client.post("/logout", {"_csrf": "forged"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertIn("expired", body)

    def test_signing_out_ends_the_session(self):
        client = self.client("watcher")
        client.post("/logout")
        status, _, _ = client.get("/")
        self.assertEqual(status, 303)


def assert_renders(case, body):
    """A page rendered as HTML: complete, and with no markup escaped into text."""
    case.assertIn("</html>", body)
    for escaped in ("&lt;table", "&lt;tr", "&lt;td", "&lt;span", "&lt;section", "&lt;p",
                    "&lt;div", "&amp;lt;", "&amp;quot;"):
        case.assertNotIn(escaped, body, "markup was escaped twice and shown as text")


class TestPagesAndRoles(ServerCase):
    def test_every_main_page_renders(self):
        client = self.client("lead")
        for path in ("/", "/experiments", "/experiments/new", "/experiments/new/renewal-reminder",
                     "/library", "/help", "/account"):
            with self.subTest(path=path):
                status, _, body = client.get(path)
                self.assertEqual(status, 200)
                assert_renders(self, body)

    def test_administration_is_for_administrators(self):
        self.assertEqual(self.client("owner").get("/admin")[0], 403)
        status, _, body = self.client("admin").get("/admin")
        self.assertEqual(status, 200)
        self.assertIn("Collection keys", body)

    def test_admin_creates_a_key_and_sees_it_once(self):
        admin = self.client("admin")
        admin.get("/admin")
        status, _, body = admin.post("/admin/keys", {"name": "test service",
                                                     "scopes": ["collect"]})
        self.assertEqual(status, 200)
        token = re.search(r"cxk_[0-9a-f]{12}_[A-Za-z0-9_-]+", body).group(0)
        self.assertIsNotNone(self.fx.ws.keys.verify(token))
        _, _, again = admin.get("/admin")
        self.assertNotIn(token, again)

    def test_untrusted_text_is_escaped(self):
        record = self.fx.new_experiment(name="<script>alert(1)</script> Plan")
        _, _, body = self.client("watcher").get(f"/experiments/{record.id}")
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)


class TestPilotThroughTheWeb(ServerCase):
    """One evaluation from plan to published case, the way staff would do it."""

    def test_the_whole_life_of_a_pilot(self):
        lead = self.client("lead")
        lead.get("/experiments/new/document-upload")
        status, headers, _ = lead.post("/experiments/new/document-upload", {
            "name": "Web flow pilot", "description": "A checklist replaces a paragraph.",
            "treatment_description": "Checklist", "hypothesis": "More people finish.",
            "baseline_rate": "62", "minimum_effect": "3", "monthly_volume": "12000",
            "duration_days": "28", "segments": "preferred_language, device_type",
        })
        self.assertEqual(status, 303, "plan was not created")
        path = headers["Location"]
        experiment_id = path.rsplit("/", 1)[1]

        # Every page of a draft renders, including the plan.
        for tab in ("", "/plan", "/review", "/results", "/activity", "/connect"):
            status, _, body = lead.get(path + tab)
            self.assertEqual(status, 200, tab)
            assert_renders(self, body)

        # A dry run, before review.
        lead.get(path + "/results")
        lead.post(f"{path}/actions/dry-run", {"control_rate": "60", "treatment_rate": "68",
                                              "units": "2000"})
        _, _, body = lead.get(path + "/results")
        self.assertIn("Simulated data", body)

        # Five reviewers, each signing for their own role.
        for role in REQUIRED_APPROVALS:
            reviewer = self.client(ROLE_HOLDER[role])
            reviewer.get(path + "/review")
            status, _, _ = reviewer.post(f"{path}/sign-off", {"role": role, "confirm": "1",
                                                              "note": "fine"})
            self.assertEqual(status, 303)
        owner = self.client("owner")
        owner.get(path)
        self.assertEqual(owner.post(f"{path}/actions/approve")[0], 303)
        ops = self.client("ops")
        ops.get(path)
        self.assertEqual(ops.post(f"{path}/actions/start")[0], 303)

        # The live service, through the API.
        _, token = self.fx.ws.create_key("test", "svc", scopes=["collect", "read"],
                                         experiments=[experiment_id])
        status, answer = self.api("POST", f"/api/v1/experiments/{experiment_id}/assign", token,
                                  {"unit_id": "web-1", "attributes": ELIGIBLE})
        self.assertEqual(status, 200)
        self.assertTrue(answer["in_analysis"])
        status, _ = self.api("POST", f"/api/v1/experiments/{experiment_id}/events", token,
                             {"unit_id": "web-1", "event_type": "submission_succeeded"})
        self.assertEqual(status, 201)
        drive_traffic(self.fx.ws.experiment(experiment_id), 2500,
                      completion={"control": 0.6, "treatment": 0.7})

        # Sealed while running, through the page and through the API.
        _, _, body = owner.get(path)
        self.assertIn("Main result sealed", body)
        assert_renders(self, body)
        status, data = self.api("GET", f"/api/v1/experiments/{experiment_id}", token)
        self.assertEqual(data["results"], "sealed")
        self.assertNotIn("primary", data["monitoring"][0])

        # A recorded look, by the only role allowed one.
        owner.get(path)
        self.assertEqual(owner.post(f"{path}/actions/look")[0], 403)
        lead.get(path)
        self.assertEqual(lead.post(f"{path}/actions/look")[0], 303)
        _, _, body = lead.get(path)
        self.assertIn("Look 1, taken by lead", body)

        # Pause and resume.
        ops.get(path)
        self.assertEqual(ops.post(f"{path}/actions/pause", {"reason": "outage"})[0], 303)
        status, answer = self.api("POST", f"/api/v1/experiments/{experiment_id}/assign", token,
                                  {"unit_id": "web-2", "attributes": ELIGIBLE})
        self.assertEqual(answer["serve"], "control")
        self.assertFalse(answer["collecting"])
        ops.get(path)
        ops.post(f"{path}/actions/resume")

        # The decision, recorded by the program owner, then published.
        _, _, preview = owner.get(f"{path}/conclude?outcome=complete")
        self.assertIn("End the pilot and record the decision", preview)
        status, headers, _ = owner.post(f"{path}/conclude", {"outcome": "complete", "note": ""})
        self.assertEqual(status, 303)
        _, _, body = owner.get(path + "/results")
        self.assertIn("Decision and approval record", body)
        self.assertIn("Download readout", body)
        assert_renders(self, body)
        for tab in ("", "/plan", "/review", "/activity", "/connect"):
            status, _, page = owner.get(path + tab)
            self.assertEqual(status, 200, tab)
            assert_renders(self, page)
        _, _, overview = owner.get(path)
        self.assertIn("Bar set before launch", overview)
        self.assertIn('<svg viewBox="0 0 640', overview)
        status, headers, _ = owner.post(f"{path}/actions/publish")
        self.assertEqual(status, 303)
        _, _, library = owner.get("/library")
        self.assertIn("Web flow pilot", library)
        _, _, case = owner.get(headers["Location"])
        self.assertIn("Plan a replication", case)


class TestApi(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.record = cls.fx.running(cls.fx.new_experiment(name="Api pilot"))
        _, cls.collect = cls.fx.ws.create_key("t", "collect", scopes=["collect"],
                                              experiments=[cls.record.id])
        _, cls.read = cls.fx.ws.create_key("t", "read", scopes=["read"])
        _, cls.elsewhere = cls.fx.ws.create_key("t", "other", scopes=["collect"],
                                                experiments=["somewhere-else"])

    def path(self, suffix=""):
        return f"/api/v1/experiments/{self.record.id}{suffix}"

    def test_keys_are_required_and_scoped(self):
        self.assertEqual(self.api("POST", self.path("/assign"), "nope", {"unit_id": "x"})[0], 401)
        self.assertEqual(self.api("POST", self.path("/assign"), self.read,
                                  {"unit_id": "x"})[0], 403)
        self.assertEqual(self.api("POST", self.path("/assign"), self.elsewhere,
                                  {"unit_id": "x"})[0], 403)
        self.assertEqual(self.api("GET", "/api/v1/alerts", self.collect)[0], 403)

    def test_errors_are_json_with_stable_codes(self):
        status, data = self.api("POST", self.path("/events"), self.collect,
                                {"unit_id": "never-assigned", "event_type": "submission_succeeded"})
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "EVENT_UNIT_NOT_ENROLLED")
        status, data = self.api("POST", self.path("/assign"), self.collect, {"attributes": {}})
        self.assertEqual(data["error"]["code"], "API_BAD_UNIT")

    def test_batches_report_each_row(self):
        self.api("POST", self.path("/assign"), self.collect,
                 {"unit_id": "b-1", "attributes": ELIGIBLE})
        status, data = self.api("POST", self.path("/events/batch"), self.collect, {"events": [
            {"unit_id": "b-1", "event_type": "submission_attempted"},
            {"unit_id": "b-unknown", "event_type": "submission_succeeded"},
            {"unit_id": "b-1", "event_type": "made_up"},
        ]})
        self.assertEqual(status, 207)
        self.assertEqual(data["accepted"], 1)
        self.assertEqual([r["index"] for r in data["rejected"]], [1, 2])

    def test_read_endpoints(self):
        status, data = self.api("GET", "/api/v1/experiments", self.read)
        self.assertEqual(status, 200)
        self.assertIn(self.record.id, [e["experiment_id"] for e in data["experiments"]])
        status, data = self.api("GET", "/api/v1/alerts", self.read)
        self.assertEqual(status, 200)
        self.assertIn("alerts", data)

    def test_oversized_bodies_are_refused_before_reading(self):
        # Declare a body over the limit and send none of it: the refusal must
        # come from the declared length, before the server reads anything.
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                                timeout=30)
        connection.putrequest("POST", self.path("/events/batch"))
        connection.putheader("Authorization", f"Bearer {self.collect}")
        connection.putheader("Content-Length", str(1024 * 1024 + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        connection.close()

    def test_the_javascript_client_is_served(self):
        with urllib.request.urlopen(self.base + "/static/civicexp-client.js") as response:
            text = response.read().decode()
        self.assertIn("failedOpen", text)
