from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import requests
import os
import json
import pickle
import base64
from datetime import date, datetime, timedelta

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
TIMEZONE = "America/Indiana/Indianapolis"

user_last_event = {}
message_event_map = {}
event_details_map = {}

def format_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y")

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
    If no month is specified, assume the current month and year.

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

def parse_events_from_image(image_url):
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

    image_response = requests.get(image_url, auth=(account_sid, auth_token))
    image_base64 = base64.b64encode(image_response.content).decode("utf-8")
    mime_type = image_response.headers.get("Content-Type", "image/jpeg")

    today = date.today().strftime("%Y-%m-%d")
    prompt = f"""
    Today's date is {today}.
    If no year is specified, assume the current year.
    If no month is specified, assume the current month.

    This image may contain one or multiple events. Extract ALL events and return ONLY a JSON array where each item has:
    - title
    - date (YYYY-MM-DD format)
    - start_time (HH:MM 24hr format, null if not mentioned)
    - end_time (HH:MM 24hr format, null if not mentioned)
    - location (null if not mentioned)
    - description (any extra details like dress code, what to bring, contact info, vibe etc. null if nothing extra)

    If the image contains no event details at all, return [{{"error": "not an event"}}].

    Return ONLY the JSON array, no other text.
    """

    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": image_base64}}
            ]
        }]
    }

    response = requests.post(GEMINI_URL, json=body)
    print(f"Gemini image status: {response.status_code}")
    print(f"Gemini image response: {response.text}")
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
                details = event_details_map.get(event_id, {})
                delete_calendar_event(event_id)
                reply = (
                    f"🗑️ Deleted from your calendar!\n"
                    f"*{details.get('title', 'Event')}*\n"
                    f"📆 {format_date(details['date']) if details.get('date') else ''}\n"
                    f"⏰ {details.get('time_display', 'No time')}\n"
                    f"📍 {details.get('location') or 'No location'}"
                )
                resp.message(reply)
            except Exception as e:
                print(f"Delete error: {e}")
                resp.message("Couldn't delete. It may have already been removed.")
        else:
            resp.message("No event found to delete!")
        return str(resp)

    try:
        num_media = int(request.form.get("NumMedia", 0))

        if num_media > 0:
            image_url = request.form.get("MediaUrl0")
            events = parse_events_from_image(image_url)

            if not events or "error" in events[0]:
                resp.message("That image doesn't seem to have any event details!")
                return str(resp)

            added = []
            last_event_id = None
            for event in events:
                if not event.get("date"):
                    continue
                event_id, link = create_calendar_event(event)
                last_event_id = event_id

                end_display = event.get("end_time", "")
                time_display = f"{event.get('start_time')} - {end_display}" if event.get("start_time") and end_display else f"{event.get('start_time')}+1hr" if event.get("start_time") else "All day"

                event_details_map[event_id] = {
                    "title": event["title"],
                    "date": event["date"],
                    "time_display": time_display,
                    "location": event.get("location")
                }
                added.append(f"• {event['title']} — {format_date(event['date'])} {event.get('start_time', '')}")

            user_last_event[sender] = last_event_id

            reply_text = f"✅ Added {len(added)} events to your calendar!\n\n" + "\n".join(added)

            account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
            auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
            client = Client(account_sid, auth_token)
            sent_msg = client.messages.create(
                from_="whatsapp:+14155238886",
                to=sender,
                body=reply_text
            )
            if last_event_id:
                message_event_map[sent_msg.sid] = last_event_id

        else:
            event = parse_event(incoming_msg)

            if "error" in event:
                resp.message("That doesn't look like an event. Try sending an invitation or event details!")
            elif not event.get("date"):
                resp.message("Couldn't figure out the date. Can you include the date in your message?")
            else:
                event_id, link = create_calendar_event(event)
                user_last_event[sender] = event_id

                end_display = event.get("end_time", "")
                time_display = f"{event.get('start_time', 'No time')} - {end_display if end_display else '+1hr'}" if event.get("start_time") else "All day"

                event_details_map[event_id] = {
                    "title": event["title"],
                    "date": event["date"],
                    "time_display": time_display,
                    "location": event.get("location")
                }

                reply_text = (
                    f"✅ Added to your calendar!\n"
                    f"*{event['title']}*\n"
                    f"📆 {format_date(event['date'])}\n"
                    f"⏰ {time_display}\n"
                    f"📍 {event.get('location') or 'No location'}\n"
                    f"🔗 {link}\n\n"
                    f"Reply *delete* to remove it."
                )

                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                client = Client(account_sid, auth_token)
                sent_msg = client.messages.create(
                    from_="whatsapp:+14155238886",
                    to=sender,
                    body=reply_text
                )
                message_event_map[sent_msg.sid] = event_id
                print(f"Stored mapping: {sent_msg.sid} -> {event_id}")

    except Exception as e:
        resp.message("Sorry, something went wrong. Try again!")
        print(f"Error: {e}")

    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)