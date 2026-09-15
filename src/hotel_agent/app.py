"""
Hotel Recommendation Agent - optional bonus module.

Adapted from the original workshop's bonus module. Listens for
FinalBookingCompleted, recommends hotels, and emails a (deliberately funny)
recommendation via SNS. This is the module that requires an SNS email
subscription confirmation - use a personal inbox you can actually open when
subscribing (see scripts/deploy-hotel-agent.sh), not a locked-down work inbox.
"""
import json
import os

import boto3
from strands import Agent, tool
from strands.session.s3_session_manager import S3SessionManager

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "multi-agent-bus")
SESSION_BUCKET = os.environ.get("SESSION_BUCKET")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

eventbridge = boto3.client("events")
sns = boto3.client("sns")

HOTEL_DATA = {
    "Miami": [
        {"name": "Fontainebleau Miami Beach", "price": 350, "rating": 4.5, "tier": "luxury"},
        {"name": "Hampton Inn Miami Beach", "price": 150, "rating": 4.0, "tier": "mid"},
        {"name": "Budget Inn South Beach", "price": 80, "rating": 3.5, "tier": "budget"},
    ],
    "New York": [
        {"name": "The Plaza Hotel", "price": 500, "rating": 4.8, "tier": "luxury"},
        {"name": "Hilton Midtown", "price": 200, "rating": 4.2, "tier": "mid"},
        {"name": "Pod 51 Hotel", "price": 100, "rating": 3.8, "tier": "budget"},
    ],
    "Los Angeles": [
        {"name": "Beverly Hills Hotel", "price": 600, "rating": 4.9, "tier": "luxury"},
        {"name": "Sheraton Universal", "price": 180, "rating": 4.1, "tier": "mid"},
        {"name": "Motel 6 Hollywood", "price": 70, "rating": 3.2, "tier": "budget"},
    ],
}
DEFAULT_HOTELS = [
    {"name": "Downtown Comfort Inn", "price": 120, "rating": 3.9, "tier": "mid"},
    {"name": "City Center Budget Suites", "price": 75, "rating": 3.4, "tier": "budget"},
]


@tool(name="find_hotels")
def find_hotels(destination: str, budget_per_night: int) -> dict:
    """Find hotel recommendations based on destination and budget."""
    print(f"[tool] find_hotels called for {destination} with budget ${budget_per_night}")
    hotels = HOTEL_DATA.get(destination, DEFAULT_HOTELS)

    affordable = [h for h in hotels if h["price"] <= budget_per_night]
    if not affordable:
        affordable = [min(hotels, key=lambda x: x["price"])]
        message = f"No hotels within ${budget_per_night} budget. Showing cheapest option."
    else:
        message = f"Found {len(affordable)} hotels within budget"

    return {
        "status": "success",
        "destination": destination,
        "budget_per_night": budget_per_night,
        "message": message,
        "recommendations": affordable,
    }


@tool(name="send_email")
def send_email(subject: str, message: str, user_email: str = "") -> dict:
    """Send a hotel recommendation email to the user via SNS."""
    print(f"[tool] send_email called - subject: {subject}")
    if not SNS_TOPIC_ARN:
        return {"status": "error", "message": "SNS topic not configured"}
    try:
        response = sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        message_id = response.get("MessageId")
        print(f"[tool] Email sent successfully - MessageId: {message_id}")
        return {"status": "success", "message": "Email sent successfully", "message_id": message_id}
    except Exception as exc:  # noqa: BLE001
        print(f"[tool] Error sending email: {exc}")
        return {"status": "error", "message": f"Failed to send email: {exc}"}


hotel_agent = Agent(
    model=BEDROCK_MODEL_ID,
    system_prompt=(
        "You are a witty and entertaining hotel recommendation agent with a great "
        "sense of humor.\n\nYour job is to recommend hotels based on the traveler's "
        "destination and budget, then send them a FUNNY email about it.\n\n"
        "When you receive a booking event:\n"
        "1. Extract the destination and budget information\n"
        "2. Use the find_hotels tool to get recommendations\n"
        "3. Craft a hilarious, engaging email about the hotel options (use puns, "
        "jokes, and playful language)\n"
        "4. Use the send_email tool to send the funny email to the user\n\n"
        "Make the email entertaining while still being informative. Think of "
        "yourself as a comedian who happens to know a lot about hotels!"
    ),
    tools=[find_hotels, send_email],
)


def lambda_handler(event, context):
    print(f"[event] Received event: {json.dumps(event)[:2000]}")
    try:
        detail = event.get("detail", {})
        booking_id = detail.get("booking_id") or detail.get("bookingID")
        destination = detail.get("destination", "Unknown")
        budget = detail.get("budget", 0) or 0
        budget_per_night = budget // 6

        print(f"[processing] bookingID={booking_id} destination={destination} budget_per_night=${budget_per_night}")

        if SESSION_BUCKET:
            hotel_agent.session_manager = S3SessionManager(
                session_id=booking_id, bucket=SESSION_BUCKET, prefix="planner-sessions", region_name=AWS_REGION
            )
            hotel_agent.agent_id = f"hotel-agent-{booking_id}"

        prompt = (
            f"A travel booking has been finalized for {destination}!\n\n"
            f"Booking ID: {booking_id}\nDestination: {destination}\nBudget per night: ${budget_per_night}\n\n"
            "Please find suitable hotel recommendations, write a HILARIOUS email about "
            "them, and send it."
        )
        response = hotel_agent(prompt)
        print(f"[agent_response] {response}")

        eventbridge.put_events(
            Entries=[
                {
                    "Source": "workshop.hotel-agent",
                    "DetailType": "HotelRecommendationsReady",
                    "Detail": json.dumps(
                        {
                            "booking_id": booking_id,
                            "destination": destination,
                            "budget_per_night": budget_per_night,
                            "agent_recommendation": str(response),
                        }
                    ),
                    "EventBusName": EVENT_BUS_NAME,
                }
            ]
        )
        print(f"[success] Published HotelRecommendationsReady event for bookingID={booking_id}")
        return {"statusCode": 200, "body": json.dumps({"message": "Hotel recommendations generated", "booking_id": booking_id})}
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}")
        return {"statusCode": 500, "body": json.dumps({"error": str(exc)})}
