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
    token_b64 = os.environ.get("GOOGLE_TOKEN")
    print(f"Token b64 length: {len(token_b64) if token_b64 else 'MISSING'}")
    token_bytes = base64.b64decode(token_b64)
    creds = pickle.loads(token_bytes)
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
            end_dt = (start_obj + timedelta(hours=1)).isoformat()
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

def find_event_on_calendar(title=None, date_str=None):
    service = get_calendar_service()
    kwargs = {"calendarId": "primary", "maxResults": 5, "singleEvents": True, "orderBy": "startTime"}
    if date_str:
        kwargs["timeMin"] = f"{date_str}T00:00:00Z"
        kwargs["timeMax"] = f"{date_str}T23:59:59Z"
    if title:
        kwargs["q"] = title
    results = service.events().list(**kwargs).execute()
    items = results.get("items", [])
    # Skip birthday and other special event types that can't be modified/deleted
    items = [e for e in items if e.get("eventType", "default") == "default"]
    return items[0] if items else None

def detect_intent(message):
    today = date.today().strftime("%Y-%m-%d")
    prompt = f"""
    Today's date is {today}.

    Classify this message into one of these intents and return ONLY a JSON object:

    1. "add" — user wants to add a new calendar event
    2. "delete" — user wants to delete/cancel/remove an existing event
    3. "update" — user wants to change/reschedule/rename an existing event
    4. "none" — not calendar related

    For "delete" and "update", also extract:
    - search_title: the event name or keyword to search for (null if not mentioned)
    - search_date: the date to search on in YYYY-MM-DD format (resolve "today", "tomorrow", "friday" etc using today's date. null if not mentioned)

    For "update", also extract what needs to change:
    - new_title: new name if being renamed (null if not)
    - new_date: new date in YYYY-MM-DD if being rescheduled (null if not)
    - new_start_time: new start time in HH:MM 24hr if being changed (null if not)
    - new_end_time: new end time in HH:MM 24hr if being changed (null if not)
    - new_location: new location if being changed (null if not)

    Examples:
    "delete today's meeting" → {{"intent": "delete", "search_title": "meeting", "search_date": "{today}"}}
    "cancel the party on friday" → {{"intent": "delete", "search_title": "party", "search_date": "<friday's date>"}}
    "move tomorrow's dentist to 3pm" → {{"intent": "update", "search_title": "dentist", "search_date": "<tomorrow>", "new_start_time": "15:00"}}
    "rename saturday's event to game night" → {{"intent": "update", "search_title": null, "search_date": "<saturday>", "new_title": "game night"}}
    "party at mine saturday 9pm" → {{"intent": "add"}}
    "what's up" → {{"intent": "none"}}

    Message: {message}
    """
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    response = requests.post(GEMINI_URL, json=body)
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Intent parse error: {e}, raw: {raw}")
        return {"intent": "none"}

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

    If this message contains ANY time-based activity, appointment, reminder, or plan — even a personal one — extract it as an event. Only return {{"error": "not an event"}} if the message has absolutely no time or date reference at all.

    Message: {message}
    """
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    response = requests.post(GEMINI_URL, json=body)
    print(f"Gemini status: {response.status_code}")
    print(f"Gemini response: {response.text}")
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}, raw: {raw}")
        return {"error": "parse failed"}

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
    body = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime_type, "data": image_base64}}]}]}
    response = requests.post(GEMINI_URL, json=body)
    print(f"Gemini image status: {response.status_code}")
    print(f"Gemini image response: {response.text}")
    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}, raw: {raw}")
        return [{"error": "parse failed"}]

def format_event_block(event, link, time_display):
    return (
        f"*{event['title']}*\n"
        f"📆 {format_date(event['date'])}\n"
        f"⏰ {time_display}\n"
        f"📍 {event.get('location') or 'No location'}\n"
        f"🔗 {link}"
    )

@app.route("/webhook", methods=["POST"])
def webhook():
    print(f"ALL PARAMS: {request.form}")
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From")
    resp = MessagingResponse()
    num_media = int(request.form.get("NumMedia", 0))

    # Image (with or without caption)
    if num_media > 0:
        image_url = request.form.get("MediaUrl0")
        caption = incoming_msg.strip()
        try:
            events = parse_events_from_image(image_url)
            if not events or "error" in events[0]:
                resp.message("That image doesn't seem to have any event details!")
                return str(resp)

            # If caption exists, filter events based on it
            if caption:
                titles = [e.get("title", "") for e in events]
                filter_prompt = f"""
                I have these events extracted from an image: {json.dumps(titles)}
                The user said: "{caption}"
                Return ONLY a JSON array of the event titles the user wants to keep, based on what they said.
                If they want all events, return all titles.
                Example: ["Ramen Bowl", "Chicken and Waffles"]
                """
                body = {"contents": [{"parts": [{"text": filter_prompt}]}]}
                filter_response = requests.post(GEMINI_URL, json=body)
                raw = filter_response.json()["candidates"][0]["content"]["parts"][0]["text"]
                raw = raw.strip().replace("```json", "").replace("```", "").strip()
                try:
                    keep_titles = json.loads(raw)
                    keep_titles_lower = [t.lower() for t in keep_titles]
                    events = [e for e in events if e.get("title", "").lower() in keep_titles_lower]
                except json.JSONDecodeError:
                    pass

            blocks = []
            last_event_id = None
            for event in events:
                if not event.get("date"):
                    continue
                event_id, link = create_calendar_event(event)
                last_event_id = event_id
                end_display = event.get("end_time", "")
                time_display = f"{event.get('start_time')} - {end_display}" if event.get("start_time") and end_display else f"{event.get('start_time')} +1hr" if event.get("start_time") else "All day"
                event_details_map[event_id] = {"title": event["title"], "date": event["date"], "time_display": time_display, "location": event.get("location")}
                blocks.append(format_event_block(event, link, time_display))

            user_last_event[sender] = last_event_id
            divider = "\n➖➖➖➖➖➖➖➖\n"
            reply_text = f"✅ Added {len(blocks)} event{'s' if len(blocks) > 1 else ''} to your calendar!\n\n" + divider.join(blocks)

            account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
            auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
            client = Client(account_sid, auth_token)
            sent_msg = client.messages.create(from_="whatsapp:+14155238886", to=sender, body=reply_text)
            if last_event_id:
                message_event_map[sent_msg.sid] = last_event_id

        except Exception as e:
            resp.message("Sorry, something went wrong. Try again!")
            print(f"Error: {e}")
        return str(resp)

    # Check if this is a reply to a bot message (for quick delete)
    original_msg_sid = request.form.get("OriginalRepliedMessageSid")

    # Text messages — detect intent first
    try:
        intent_data = detect_intent(incoming_msg)
        intent = intent_data.get("intent", "none")
        print(f"Intent: {intent_data}")

        # DELETE
        if intent == "delete":
            # If replying to a bot message, try to use the mapped event ID first
            event_id = None
            if original_msg_sid and original_msg_sid in message_event_map:
                event_id = message_event_map[original_msg_sid]
                print(f"Found event from reply mapping: {event_id}")
            elif sender in user_last_event and not intent_data.get("search_title") and not intent_data.get("search_date"):
                event_id = user_last_event[sender]
                print(f"Found event from last event: {event_id}")

            if event_id:
                try:
                    service = get_calendar_service()
                    cal_event = service.events().get(calendarId="primary", eventId=event_id).execute()
                    title = cal_event.get("summary", "Event")
                    raw_date = cal_event["start"].get("date") or cal_event["start"].get("dateTime", "")[:10]
                    delete_calendar_event(event_id)
                    resp.message(f"🗑️ Deleted *{title}* ({format_date(raw_date)}) from your calendar!")
                except Exception as e:
                    print(f"Error deleting mapped event: {e}")
                    resp.message("That event may have already been deleted or can't be found.")
            else:
                cal_event = find_event_on_calendar(
                    title=intent_data.get("search_title"),
                    date_str=intent_data.get("search_date")
                )
                if cal_event:
                    event_id = cal_event["id"]
                    title = cal_event.get("summary", "Event")
                    raw_date = cal_event["start"].get("date") or cal_event["start"].get("dateTime", "")[:10]
                    delete_calendar_event(event_id)
                    resp.message(f"🗑️ Deleted *{title}* ({format_date(raw_date)}) from your calendar!")
                else:
                    resp.message("Couldn't find that event in your calendar. Can you be more specific?")

        # UPDATE
        elif intent == "update":
            cal_event = find_event_on_calendar(
                title=intent_data.get("search_title"),
                date_str=intent_data.get("search_date")
            )
            if cal_event:
                service = get_calendar_service()
                event_id = cal_event["id"]
                updated = cal_event.copy()

                if intent_data.get("new_title"):
                    updated["summary"] = intent_data["new_title"]
                if intent_data.get("new_location"):
                    updated["location"] = intent_data["new_location"]
                if intent_data.get("new_start_time") or intent_data.get("new_date"):
                    new_date = intent_data.get("new_date") or (cal_event["start"].get("date") or cal_event["start"].get("dateTime", "")[:10])
                    new_start = intent_data.get("new_start_time") or cal_event["start"].get("dateTime", "T00:00")[11:16]
                    new_end = intent_data.get("new_end_time") or cal_event["end"].get("dateTime", "T01:00")[11:16]
                    updated["start"] = {"dateTime": f"{new_date}T{new_start}:00", "timeZone": TIMEZONE}
                    updated["end"] = {"dateTime": f"{new_date}T{new_end}:00", "timeZone": TIMEZONE}

                service.events().update(calendarId="primary", eventId=event_id, body=updated).execute()
                title = updated.get("summary", cal_event.get("summary", "Event"))
                raw_date = updated["start"].get("date") or updated["start"].get("dateTime", "")[:10]
                resp.message(f"✏️ Updated! *{title}* is now on {format_date(raw_date)}.")
            else:
                resp.message("Couldn't find that event. Can you be more specific?")

        # ADD
        elif intent == "add":
            event = parse_event(incoming_msg)
            if "error" in event:
                resp.message("That doesn't look like an event. Try sending an invitation or event details!")
            elif not event.get("date"):
                resp.message("Couldn't figure out the date. Can you include it?")
            else:
                event_id, link = create_calendar_event(event)
                user_last_event[sender] = event_id
                end_display = event.get("end_time", "")
                time_display = f"{event.get('start_time', 'No time')} - {end_display if end_display else '+1hr'}" if event.get("start_time") else "All day"
                event_details_map[event_id] = {"title": event["title"], "date": event["date"], "time_display": time_display, "location": event.get("location")}
                reply_text = (
                    f"✅ Added to your calendar!\n\n"
                    f"{format_event_block(event, link, time_display)}\n\n"
                    f"Reply *delete* to remove it."
                )
                account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                client = Client(account_sid, auth_token)
                sent_msg = client.messages.create(from_="whatsapp:+14155238886", to=sender, body=reply_text)
                message_event_map[sent_msg.sid] = event_id
                print(f"Stored mapping: {sent_msg.sid} -> {event_id}")

        # NONE
        else:
            resp.message("I only handle calendar events! Send me an invite, a flyer, or say something like 'delete today's meeting' or 'move Friday's party to 10pm'.")

    except Exception as e:
        resp.message("Sorry, something went wrong. Try again!")
        print(f"Error: {e}")

    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)