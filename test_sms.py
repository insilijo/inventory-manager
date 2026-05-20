#!/usr/bin/env python3
"""
Send a fake SMS to the local server for testing.
Usage:
    python test_sms.py "3 lemon boxes 4 apple boxes"
    python test_sms.py "sold $150 today"
    python test_sms.py "used 2 bread loaves"
"""
import sys
import requests

body = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "3 lemon boxes 4 apple boxes"
resp = requests.post(
    "http://localhost:8000/sms",
    data={"From": "+16175550142", "Body": body},
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
print(f"Status: {resp.status_code}")
print(resp.text[:200])
