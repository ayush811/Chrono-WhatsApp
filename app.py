from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    # Read the incoming message
    incoming_msg = request.form.get("Body")
    print(f"Received: {incoming_msg}")

    # Send a reply back
    resp = MessagingResponse()
    resp.message(f"Got your message: {incoming_msg}")
    return str(resp)

if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))