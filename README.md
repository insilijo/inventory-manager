# FSFN Inventory

SMS → Claude → dashboard. Volunteers text inventory updates, they appear live on the web.

## Stack

- **FastAPI** — webhook receiver + API + dashboard server
- **SQLite** — no-config database, file on disk
- **Anthropic API** — parses freetext SMS into structured JSON
- **Twilio** — provides the phone number and fires the webhook

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in ANTHROPIC_API_KEY and TWILIO_AUTH_TOKEN

# Disable Twilio validation for local testing
export VALIDATE_TWILIO=false

uvicorn main:app --reload
# Dashboard: http://localhost:8000
```

### Test without Twilio

```bash
python test_sms.py "3 lemon boxes 4 apple boxes"
python test_sms.py "sold 150 dollars today"
python test_sms.py "used 2 bread loaves gave out 5 bags"
```

## Twilio setup

1. Buy a number at twilio.com (~$1/month)
2. Set the webhook URL to `https://your-app.railway.app/sms` (HTTP POST)
3. Add your `TWILIO_AUTH_TOKEN` to the environment

## Deploy to Railway

```bash
# Install Railway CLI: https://docs.railway.app/develop/cli
railway login
railway init
railway up

# Set env vars
railway variables set ANTHROPIC_API_KEY=sk-ant-...
railway variables set TWILIO_AUTH_TOKEN=...
railway variables set VALIDATE_TWILIO=true
```

Railway will auto-detect the Python app and run `uvicorn main:app --host 0.0.0.0 --port $PORT`.

## Data model

**`sms_events`** — every incoming text, raw + parsed  
**`inventory`** — current quantity per item (upserted on each update)  
**`sales_log`** — sales transactions with dollar amounts  

## Extending later

- Add a `locations` table and parse location from message (or map phone → location)
- Switch SQLite → Postgres when you want multiple writers or hosted backups
- Add auth to the dashboard (HTTP Basic is fine to start)
- Add a `/api/export` endpoint returning CSV for reporting
