# runner/notifier.py
#
# Sends email alerts for the daily pipeline. Used for:
#   - Newly flagged "would follow" trades (signal_flag, see pipeline/signal_flagger.py)
#   - Pipeline failures (see daily_runner.py)
#
# Requires EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env (Gmail SMTP + app
# password — https://myaccount.google.com/apppasswords). Sends to itself.
#
# If credentials are missing, send_email() logs and no-ops rather than
# raising — a missing email config shouldn't take down the pipeline run.

import os
import smtplib
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send_email(subject, body):
    address  = os.environ.get("EMAIL_ADDRESS")
    app_pass = os.environ.get("EMAIL_APP_PASSWORD")

    if not address or not app_pass:
        print("  WARNING: EMAIL_ADDRESS / EMAIL_APP_PASSWORD not set in .env — skipping email")
        return False

    msg            = MIMEText(body)
    msg["Subject"] = subject
    msg["From"]    = address
    msg["To"]      = address

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(address, app_pass)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"  WARNING: failed to send email: {e}")
        return False
