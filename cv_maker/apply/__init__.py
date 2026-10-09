"""The apply assistant: fills in a job application in a private browser on this computer, supervised by you.

Separate from tailoring: you can stop once the CV and letter are written, or continue here.

How it works, one page at a time:
1. Read the form (labels, options, buttons). Never screenshots: the LLM only ever sees text.
2. Fill what can be filled by rule, on this computer, from your saved application details (name, contact,
   address, links, standard answers). The LLM never sees those values, only placeholders.
3. Ask the LLM about the rest (masked like every other request), then fill it in.
4. Stop and ask you whenever it isn't sure, a site wants an account, a CAPTCHA appears, or the
   application moves to another website. Voluntary diversity questions are declined or left to you.
5. Never submit by itself: you approve the final submit, after reviewing everything that was filled.

You can pause and take over the browser at any time from the app, and resume from where you left off.
"""
