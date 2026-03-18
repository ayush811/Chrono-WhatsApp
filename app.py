from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import requests
import os
import json
import pickle
from datetime import date, datetime, timedelta

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
TIMEZONE = "America/Indiana/Indianapolis"

user_last_event = {}
message_event_map = {}

def get_calendar_service():
    with open("token.pickle", "rb") as token:
        creds = pickle.load(token)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)

def create_calendar_event(event):
    service = get_calendar_service()

    if event.get("start_time"):
        start_dt = f"{event['date']}T{event['start_time']}:00"
        if event.get("end_time"):
            end_dt = f"{event['date']}T{event['end_time']}:00"
        else:
            start_obj = datetime.fromisoformat(start_dt)
            end_obj = start_obj + timedelta(hours=1)
            end_dt = end_obj.isoformat()

        time_obj = {"dateTime": start_dt, "timeZone": TIMEZONE}
        end_time_obj = {"dateTime": end_dt, "timeZone": TIMEZONE}
    else:
        time_obj = {"date": event["date"]}
        end_time_obj = {"date": event["date"]}

    body = {
        "summary": event["title"],
        "location": event.get("location", ""),
        "description": event.get("description", ""),
        "start": time_obj,
        "end": end_time_obj,
    }

    created = service.events().insert(calendarId="primary", body=body).execute()
    return created.get("id"), created.get("htmlLink")

def delete_calendar_event(event_id):
    service = get_calendar_service()
    service.events().delete(calendarId="primary", eventId=event_id).execute()

def parse_event(message):
    today = date.today().strftime("%Y-%m-%d")
    prompt = f"""
    Today's date is {today}. Use this to resolve relative dates like "tomorrow", "next Friday", "this weekend" etc.

    Extract calendar event details from this message and return ONLY a JSON object with these fields:
    - title
    - date (YYYY-MM-DD format)
    - start_time (HH:MM 24hr format, null if not mentioned)
    - end_time (HH:MM 24hr format, null if not mentioned)
    - location (null if not mentioned)
    - description (any extra details like dress code, what to bring, contact info, vibe etc. null if nothing extra)

    If this message doesn't seem like an event invitation, return {{"error": "not an event"}}.

    Message: {message}
    """
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    response = requests.post(GEMINI_URL, json=body)
    print(f"Gemini status: {response.status_code}")
    print(f"Gemini response: {response.text}")
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().replace("```json", "").replace("```", "")
    return json.loads(raw)

@app.route("/webhook", methods=["POST"])
def webhook():
    print(f"ALL PARAMS: {request.form}")
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From")
    original_replied_sid = request.form.get("OriginalRepliedMessageSid")
    resp = MessagingResponse()

    if incoming_msg.lower() in ["delete", "remove", "cancel"]:
        event_id = None

        if original_replied_sid and original_replied_sid in message_event_map:
            event_id = message_event_map[original_replied_sid]
        elif sender in user_last_event:
            event_id = user_last_event[sender]

        if event_id:
            try:
                delete_calendar_event(event_id)
                resp.message("🗑️ Event deleted from your calendar!")
            except Exception as e:
                print(f"Delete error: {e}")
                resp.message("Couldn't delete. It may have already been removed.")
        else:
            resp.message("No event found to delete!")
        return str(resp)

    try:
        event = parse_event(incoming_msg)

        if "error" in event:
            resp.message("That doesn't look like an event. Try sending an invitation or event details!")
        else:
            event_id, link = create_calendar_event(event)
            user_last_event[sender] = event_id

            end_display = event.get("end_time", "")
            time_display = f"{event.get('start_time', 'No time')} - {end_display if end_display else '+1hr'}" if event.get("start_time") else "All day"

            reply_text = (
                f"✅ Added to your calendar!\n"
                f"*{event['title']}*\n"
                f"📆 {event['date']}\n"
                f"⏰ {time_display}\n"
                f"📍 {event.get('location') or 'No location'}\n"
                f"🔗 {link}\n\n"
                f"Reply *delete* to remove it."
            )

            resp.message(reply_text)

            account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
            auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
            if account_sid and auth_token:
                client = Client(account_sid, auth_token)
                messages = client.messages.list(to=sender, limit=1)
                if messages:
                    message_event_map[messages[0].sid] = event_id

    except Exception as e:
        resp.message("Sorry, something went wrong. Try again!")
        print(f"Error: {e}")

    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)