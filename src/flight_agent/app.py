"""
Flight Manager Agent - searches and evaluates flight options.

Dual-mode handler: orchestration ("action" key, returns a flat dict) or
choreography (EventBridge "DatesFinalized" -> publishes "FlightSearchCompleted").
"""
import json
import os

import boto3
from strands import Agent, tool
from strands.session.s3_session_manager import S3SessionManager

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "multi-agent-bus")
SESSION_BUCKET = os.environ.get("SESSION_BUCKET")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

eventbridge = boto3.client("events")

AIRLINES = ["American", "United", "Delta", "Southwest"]

_last = {}


def _traveler_count(travelers):
    if isinstance(travelers, dict):
        return max(1, int(travelers.get("adults", 1)) + int(travelers.get("children", 0)))
    try:
        return max(1, int(travelers))
    except (TypeError, ValueError):
        return 1


@tool(name="search_flights")
def search_flights(origin: str, destination: str, travelers: int, airline_preference: str = "") -> dict:
    """Search available flights for a route (dummy data, deterministic per route)."""
    seed = sum(ord(c) for c in f"{origin}{destination}")
    base_price = 150 + (seed % 350)
    options = []
    for i, airline in enumerate(AIRLINES):
        price_per_person = base_price + (i * 35) - (40 if airline == airline_preference else 0)
        options.append(
            {
                "airline": airline,
                "price_per_person": max(price_per_person, 60),
                "stops": 0 if i == 0 else (1 if i < 3 else 2),
                "duration_hours": 2.5 + i * 1.1,
            }
        )
    for opt in options:
        opt["total_price"] = round(opt["price_per_person"] * travelers, 2)
    result = {"origin": origin, "destination": destination, "options": options}
    _last["search"] = result
    return result


@tool(name="evaluate_options")
def evaluate_options(options: list, budget: float, airline_preference: str = "") -> dict:
    """Rank flight options by preference match and price, filtered by budget."""
    within_budget_options = [o for o in options if o["total_price"] <= budget]
    pool = within_budget_options or options
    preferred = [o for o in pool if o.get("airline") == airline_preference]
    best = min(preferred or pool, key=lambda o: o["total_price"])
    result = {
        "best_option": best,
        "within_budget": best["total_price"] <= budget,
        "flights_found": len(options),
        "options_within_budget": len(within_budget_options),
    }
    _last["evaluation"] = result
    return result


@tool(name="check_availability")
def check_availability(best_option: dict) -> dict:
    """Check seat availability for the selected flight (dummy data)."""
    seed = sum(ord(c) for c in best_option.get("airline", "")) + int(best_option.get("total_price", 0))
    seats = 1 + (seed % 9)
    result = {"available": True, "seats_remaining": seats}
    _last["availability"] = result
    return result


def _build_agent():
    return Agent(
        model=BEDROCK_MODEL_ID,
        system_prompt=(
            "You are a flight booking specialist. Given a route, traveler count, "
            "budget, and airline preference, call search_flights, then "
            "evaluate_options, then check_availability, in that order. Balance "
            "cost, convenience, and airline preference. Be concise."
        ),
        tools=[search_flights, evaluate_options, check_availability],
    )


def _run_flight_search(booking_id, origin, destination, travelers, budget, airline_preference):
    _last.clear()
    agent = _build_agent()
    if SESSION_BUCKET:
        agent.session_manager = S3SessionManager(
            session_id=booking_id, bucket=SESSION_BUCKET, prefix="flight-sessions", region_name=AWS_REGION
        )
        agent.agent_id = f"flight-agent-{booking_id}"

    count = _traveler_count(travelers)
    prompt = (
        f"Booking {booking_id}: find and evaluate flights from {origin} to {destination} "
        f"for {count} traveler(s), budget ${budget}, preferred airline {airline_preference or 'none'}."
    )
    response = agent(prompt)
    print(f"[agent_response] bookingID={booking_id} {response}")

    search = _last.get("search", {})
    evaluation = _last.get("evaluation", {})
    return {
        "flight_evaluation": str(response),
        "flights_found": evaluation.get("flights_found", len(search.get("options", []))),
        "flight_options": search.get("options", []),
        "best_option": evaluation.get("best_option"),
        "within_budget": evaluation.get("within_budget", False),
        "availability": _last.get("availability", {}),
    }


def _publish(detail_type, detail):
    eventbridge.put_events(
        Entries=[
            {
                "Source": "workshop.flight-manager-agent",
                "DetailType": detail_type,
                "Detail": json.dumps(detail),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )


def lambda_handler(event, context):
    print(f"[event] received: {json.dumps(event)[:2000]}")

    if "action" in event:
        result = _run_flight_search(
            event.get("bookingID"),
            event.get("origin"),
            event.get("destination"),
            event.get("travelers"),
            event.get("budget"),
            event.get("airline_preference", ""),
        )
        return {"statusCode": 200, **result}

    detail = event.get("detail", {})
    booking_id = detail.get("bookingID") or detail.get("booking_id")
    try:
        result = _run_flight_search(
            booking_id,
            detail.get("origin"),
            detail.get("destination"),
            detail.get("travelers"),
            detail.get("budget"),
            detail.get("airline_preference", ""),
        )
        _publish("FlightSearchCompleted", {"bookingID": booking_id, **result})
        print(f"[action] bookingID={booking_id} emitted_event=FlightSearchCompleted")
        return {"statusCode": 200, "bookingID": booking_id}
    except Exception as exc:  # noqa: BLE001
        print(f"[error] bookingID={booking_id} {exc}")
        _publish("FlightSearchCompleted", {"bookingID": booking_id, "within_budget": False, "error": str(exc)})
        return {"statusCode": 500, "bookingID": booking_id, "error": str(exc)}
