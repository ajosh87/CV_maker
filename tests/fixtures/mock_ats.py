"""A small job site that behaves like the application systems the assistant meets (Workday and others).

Job 1: posting -> sign in / create account -> my information -> experience (uploads) -> questions ->
voluntary disclosures -> review -> thank you. Job 2 starts with a CAPTCHA-style check.
Test-only: it runs on localhost and accepts any account.
"""
import threading

from flask import Flask, redirect, request, session
from werkzeug.serving import make_server

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title} · Acme Careers</title>
<style>body{{font:16px sans-serif;margin:24px}} label{{display:block;margin-top:10px}} .error{{color:#b00}}</style></head>
<body><h1>{title}</h1>{body}</body></html>"""


def _page(title, body):
    return PAGE.format(title=title, body=body)


# Job 4's page. Each widget behaves like the real ones: the city typeahead keeps nothing unless a suggestion is
# picked, the marital-status dropdown builds its options only when opened, the notice question has no <label>, the
# relocation radios are named by aria-labelledby, and every change makes the site re-render (all ids gone).
HARD_FORM = """<form method="post" id="hard">
<label for="city">City or Town *</label>
<input id="city" role="combobox" aria-autocomplete="list" aria-controls="city-list" autocomplete="off">
<input type="hidden" name="city" id="city-value"><ul role="listbox" id="city-list"></ul>
<span id="ms-l">Marital status *</span>
<div id="ms" role="combobox" tabindex="0" aria-labelledby="ms-l" aria-controls="ms-list" aria-required="true">Select...</div>
<ul role="listbox" id="ms-list" hidden></ul><input type="hidden" name="marital" id="ms-value">
<div class="row"><div class="q">Notice period in days</div><input name="notice"></div>
<fieldset><legend>Which of these do you use at work? *</legend>
<label><input type="checkbox" name="tools" value="python"> Python</label>
<label><input type="checkbox" name="tools" value="sql"> SQL</label>
<label><input type="checkbox" name="tools" value="excel"> Excel</label></fieldset>
<p id="rel-q">Willing to relocate? *</p>
<div role="radiogroup" aria-labelledby="rel-q"><label><input type="radio" name="rel" value="yes"> Yes</label>
<label><input type="radio" name="rel" value="no"> No</label></div>
<label for="pin">Enter your PIN *</label><input id="pin" name="pin" required>
<button>Save and Continue</button></form>
<script>
const $ = id => document.getElementById(id);
const CITIES = ["London, England", "Leeds, England", "Lisbon, Portugal"];
$("city").addEventListener("input", () => {
  const q = $("city").value.toLowerCase();
  $("city-list").innerHTML = "";
  CITIES.filter(c => q && c.toLowerCase().startsWith(q.slice(0, 3))).forEach(c => {
    const li = document.createElement("li"); li.setAttribute("role", "option"); li.textContent = c;
    li.addEventListener("mousedown", e => { e.preventDefault(); $("city").value = c; $("city-value").value = c; $("city-list").innerHTML = ""; });
    $("city-list").appendChild(li);
  });
});
$("city").addEventListener("blur", () => { setTimeout(() => { if (!$("city-value").value) $("city").value = ""; }, 50); });
$("ms").addEventListener("click", () => {
  $("ms-list").innerHTML = "";
  ["Single", "Married", "Prefer not to say"].forEach(t => {
    const li = document.createElement("li"); li.setAttribute("role", "option"); li.textContent = t;
    li.addEventListener("click", () => { $("ms").textContent = t; $("ms-value").value = t; $("ms-list").hidden = true; });
    $("ms-list").appendChild(li);
  });
  $("ms-list").hidden = false;
});
document.addEventListener("keydown", e => { if (e.key === "Escape") $("ms-list").hidden = true; });
document.addEventListener("change", () => setTimeout(() => document.querySelectorAll("[data-cvt]").forEach(e => e.removeAttribute("data-cvt")), 30));
</script>"""


def create_mock_ats():
    app = Flask(__name__)
    app.secret_key = "mock-ats-only"
    app.received = {}  # what the "employer" got, for assertions

    def need(fields):
        return [f for f in fields if not request.form.get(f)]

    @app.route("/job/<int:n>")
    def job(n):
        return _page("Platform Engineer", f'<p>Acme is hiring in London. Python and Kubernetes.</p>'
                                          f'<a href="/apply/{n}/start">Apply</a>')

    @app.route("/apply/<int:n>/start", methods=["GET", "POST"])
    def start(n):
        if n == 2 and not session.get("human"):
            if request.method == "POST" and request.form.get("robot") == "no":
                session["human"] = True
                return redirect(f"/apply/{n}/start")
            return _page("Security check", '<div class="g-recaptcha" data-sitekey="test-key"></div>'
                                           '<form method="post"><label><input type="checkbox" name="robot" value="no">'
                                           " I'm not a robot</label><button>Continue</button></form>")
        if n == 4:
            return redirect("/apply/4/hard")
        if session.get("user"):
            return redirect(f"/apply/{n}/info")
        if request.method == "POST":
            if request.form.get("email") and request.form.get("password"):
                session["user"] = request.form["email"]
                return redirect(f"/apply/{n}/info")
        return _page("Sign in", '<form method="post"><label for="e">Email address</label><input id="e" name="email" type="email">'
                                '<label for="p">Password</label><input id="p" name="password" type="password">'
                                '<button>Sign In</button></form>'
                                f'<p>New here? <a href="/apply/{n}/create">Create Account</a></p>')

    @app.route("/apply/<int:n>/create", methods=["GET", "POST"])
    def create(n):
        error = ""
        if request.method == "POST":
            missing = need(["email", "password", "verify", "terms"])
            if not missing and request.form["password"] == request.form["verify"]:
                session["user"] = request.form["email"]
                app.received["account"] = {"email": request.form["email"], "password_length": len(request.form["password"])}
                return redirect(f"/apply/{n}/info")
            error = '<p role="alert" class="error">Fill in every field and accept the terms.</p>'
        return _page("Create Account", error + '<form method="post">'
                     '<label for="e">Email address</label><input id="e" name="email" type="email">'
                     '<label for="p">Password</label><input id="p" name="password" type="password">'
                     '<label for="v">Verify New Password</label><input id="v" name="verify" type="password">'
                     '<label><input type="checkbox" name="terms" value="yes" required> I agree to the Terms and Privacy Policy</label>'
                     '<button>Create Account</button></form>')

    def step(n, name, title, body, required, nxt):
        if request.method == "POST":
            missing = need(required)
            if not missing:
                app.received.setdefault(name, {}).update({k: v for k, v in request.form.items()})
                for key, file in request.files.items():
                    if file.filename:
                        app.received.setdefault(name, {})[key] = file.filename
                return redirect(f"/apply/{n}/{nxt}")
            body = f'<p role="alert" class="error">Please complete: {", ".join(missing)}</p>' + body
        return _page(title, f'<form method="post" enctype="multipart/form-data">{body}<button>Save and Continue</button></form>')

    @app.route("/apply/<int:n>/info", methods=["GET", "POST"])
    def info(n):
        return step(n, "info", "My Information",
                    '<label for="f">First Name *</label><input id="f" name="first" required>'
                    '<label for="l">Last Name *</label><input id="l" name="last" required>'
                    '<label for="m">Email *</label><input id="m" name="email" type="email" required>'
                    '<label for="ph">Phone Number</label><input id="ph" name="phone" type="tel">'
                    '<label for="c">City</label><input id="c" name="city">'
                    '<label for="co">Country *</label><select id="co" name="country" required><option value="">Select One</option>'
                    '<option>India</option><option>United Kingdom</option><option>United States</option></select>'
                    '<label for="h">How did you hear about us?</label><select id="h" name="heard"><option value="">Select One</option>'
                    '<option>Job board</option><option>LinkedIn</option><option>Referral</option></select>'
                    '<label><input type="checkbox" name="alerts"> Send me job alerts and marketing emails</label>',
                    ["first", "last", "email", "country"], "experience")

    @app.route("/apply/<int:n>/experience", methods=["GET", "POST"])
    def experience(n):
        letter = ('<label for="cl">Cover Letter *</label><input id="cl" name="letter" type="file" required>' if n == 3  # job 3 requires one
                  else '<label for="cl">Cover Letter</label><input id="cl" name="letter" type="file">')
        return step(n, "experience", "My Experience",
                    '<label for="r">Resume/CV *</label><input id="r" name="resume" type="file" accept=".pdf,.doc,.docx">'
                    + letter +
                    '<label for="li">LinkedIn Profile URL</label><input id="li" name="linkedin" type="url">'
                    '<label for="y">Years of experience with Python *</label><input id="y" name="years" required>',
                    ["years"], "questions")

    @app.route("/apply/<int:n>/questions", methods=["GET", "POST"])
    def questions(n):
        return step(n, "questions", "Application Questions",
                    '<fieldset><legend>Are you legally authorized to work in the United Kingdom? *</legend>'
                    '<label><input type="radio" name="auth" value="yes"> Yes</label><label><input type="radio" name="auth" value="no"> No</label></fieldset>'
                    '<fieldset><legend>Will you now or in the future require visa sponsorship? *</legend>'
                    '<label><input type="radio" name="sponsor" value="yes"> Yes</label><label><input type="radio" name="sponsor" value="no"> No</label></fieldset>'
                    '<label for="s">What are your salary expectations? *</label><input id="s" name="salary">',
                    ["auth", "sponsor", "salary"], "voluntary")

    @app.route("/apply/<int:n>/voluntary", methods=["GET", "POST"])
    def voluntary(n):
        return step(n, "voluntary", "Voluntary Disclosures",
                    '<label for="g">Gender</label><select id="g" name="gender"><option value="">Select One</option>'
                    '<option>Female</option><option>Male</option><option>Decline to self-identify</option></select>'
                    '<label for="v">Veteran Status</label><select id="v" name="veteran"><option value="">Select One</option>'
                    "<option>I am a veteran</option><option>I am not a veteran</option><option>I don't wish to answer</option></select>"
                    '<label><input type="checkbox" name="certify" value="yes" required> I certify that the information '
                    'I have provided is accurate</label>',
                    ["certify"], "review")

    @app.route("/apply/4/hard", methods=["GET", "POST"])
    def hard():
        """Job 4: the widgets real application systems use (Oracle, Workday), on one page."""
        if request.method == "POST":
            got = {"city": request.form.get("city", ""), "marital": request.form.get("marital", ""),
                   "tools": request.form.getlist("tools"), "relocate": request.form.get("rel", ""),
                   "pin": request.form.get("pin", ""), "notice": request.form.get("notice", "")}
            missing = [k for k in ("city", "marital", "tools", "relocate", "pin") if not got[k]]
            if not missing:
                app.received["hard"] = got
                return redirect("/apply/4/review")
            app.received.setdefault("hard_errors", []).append(missing)
        return _page("Additional Questions", HARD_FORM)

    @app.route("/apply/<int:n>/review", methods=["GET", "POST"])
    def review(n):
        if request.method == "POST":
            app.received["submitted"] = True
            return redirect(f"/apply/{n}/done")
        return _page("Review", '<p>Review your application before you submit it.</p><form method="post">'
                               f'<a href="/apply/{n}/voluntary">Back</a> <button>Submit Application</button></form>')

    @app.route("/apply/<int:n>/done")
    def done(n):
        return _page("Application received", "<p>Thank you for applying! We have received your application.</p>")

    return app


class Running:
    """The mock site on a free localhost port, in a background thread."""

    def __init__(self) -> None:
        self.app = create_mock_ats()
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
