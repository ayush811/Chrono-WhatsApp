from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import os
import json

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

def parse_event(message):
    prompt = f"""
    Extract calendar event details from this message and return ONLY a JSON object with these fields:
    - title
    - date (YYYY-MM-DD format)
    - time (HH:MM 24hr format, null if not mentioned)
    - location (null if not mentioned)
    - description (any extra details from the message like dress code, what to bring, contact info, vibe etc. null if nothing extra mentioned)

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
    incoming_msg = request.form.get("Body")
    resp = MessagingResponse()

    try:
        event = parse_event(incoming_msg)

        if "error" in event:
            resp.message("That doesn't look like an event. Try sending an invitation or event details!")
        else:
            reply = (
                f"📅 Got it! Here's what I extracted:\n"
                f"*{event['title']}*\n"
                f"📆 {event['date']}\n"
                f"⏰ {event.get('time', 'No time specified')}\n"
                f"📍 {event.get('location', 'No location')}\n"
                f"📝 {event.get('description', '')}"
            )
            resp.message(reply)
    except Exception as e:
        resp.message("Sorry, something went wrong parsing that. Try again!")
        print(f"Error: {e}")

    return str(resp)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)