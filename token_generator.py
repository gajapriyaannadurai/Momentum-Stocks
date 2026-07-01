#!/usr/bin/env python3
"""
token_generator.py
──────────────────
Generate a paid access token for the CPR Pro Scanner page.
Run this on your laptop. Paste the output URL into your Graphy members page.

Usage:
  python token_generator.py              # 1-day token
  python token_generator.py --days 30   # 30-day token (monthly plan)
  python token_generator.py --days 365  # yearly plan
"""

import base64, json, datetime, hashlib, os, argparse

SECRET       = os.environ.get("SCANNER_SECRET", "gp-stark-cpr-change-this-secret")
SCANNER_URL  = "https://gajapriyaannadurai.github.io/momentum-stocks/"


def make_token(days=1):
    exp = int((datetime.datetime.utcnow() + datetime.timedelta(days=days)).timestamp())
    sig = hashlib.sha256(f"{SECRET}{exp}".encode()).hexdigest()[:12]
    payload = {"plan": "paid", "exp": exp, "sig": sig}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", default=1, type=int)
    args = parser.parse_args()

    token   = make_token(args.days)
    url     = f"{SCANNER_URL}?token={token}"
    exp_ist = datetime.datetime.utcnow() + datetime.timedelta(days=args.days, hours=5, minutes=30)

    print("\n" + "="*60)
    print("  CPR Pro Scanner — Access Link")
    print("="*60)
    print(f"  Valid for : {args.days} day(s)")
    print(f"  Expires   : {exp_ist.strftime('%d %b %Y %I:%M %p IST')}")
    print("-"*60)
    print(f"  URL:\n  {url}")
    print("="*60)
    print("\n  Paste this URL as a button in your Graphy members page.\n")


if __name__ == "__main__":
    main()
