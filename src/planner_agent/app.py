"""
Planner Agent - coordinates the booking workflow and makes the final call.

Orchestration mode (Step Functions Task invoke, "action" key) is stateless:
Step Functions itself holds the workflow state and hands the Planner exactly
the data it needs on each call (extract / analyze_and_decide / finalize_booking).

Choreography mode (EventBridge) is NOT stateless: WeatherAnalysisCompleted and
FlightSearchCompleted arrive as two independent Lambda invocations, so the
Planner persists partial results to S3 (keyed by bookingID) and only decides
once both have arrived. This is the "distributed state" behavior described in
the workshop's choreography module. Note: EventBridge delivery is at-least-once,
so a duplicate delivery of the second event after cleanup could in principle
re-trigger a decision - acceptable for a workshop, worth knowing in production.
"""
import json
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from strands import Agent, tool
from strands.session.s3_session_manager import S3SessionManager

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "multi-agent-bus")
SESSION_BUCKET = os.environ.get("SESSION_BUCKET")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

eventbridge = boto3.client("events")
s3 = boto3.client("s3")

_last = {}


# --------------------------------------------------------------------------
# S3-backed state for the choreography pattern (see module docstring)
# --------------------------------------------------------------------------
def _state_key(booking_id, name):
    return f"state/{booking_id}/{name}.json"


def _save_state(booking_id, name, data):
    s3.put_object(Bucket=SESSION_BUCKET, Key=_state_key(booking_id, name), Body=json.dumps(data).encode())


def _load_state(booking_id, name):
    try:
        obj = s3.get_object(Bucket=SESSION_BUCKET, Key=_state_key(booking_id, name))
        return json.loads(obj["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def _delete_state(booking_id, name):
    s3.delete_object(Bucket=SESSION_BUCKET, Key=_state_key(booking_id, name))


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def _traveler_count(travelers):
    if isinstance(travelers, dict):
        return max(1, int(travelers.get("adults", 1)) + int(travelers.get("children", 0)))
    try:
        return max(1, int(travelers))
    except (TypeError, ValueError):
        return 1


@tool(name="extract_travel_details")
def extract_travel_details(
    origin: str, destination: str, travel_dates: dict, travelers, budget: float, airline_preference: str = ""
) -> dict:
    """Validate and normalize a raw travel request into trip details."""
    start = travel_dates.get("start") or travel_dates.get("departure") if isinstance(travel_dates, dict) else None
    end = travel_dates.get("end") or travel_dates.get("return") if isinstance(travel_dates, dict) else None
    result = {
        "origin": origin,
        "destination": destination,
        "travel_dates": {"start": start, "end": end},
        "travelers": _traveler_count(travelers),
        "budget": budget,
        "airline_preference": airline_preference,
        "confidence": 0.95 if origin and destination and start else 0.5,
    }
    _last["extracted"] = result
    return result


@tool(name="analyze_and_decide")
def analyze_and_decide(weather_data: dict, flight_data: dict, budget: float) -> dict:
    """Decide whether to auto-approve a booking or escalate for human review."""
    risk = (weather_data or {}).get("risk_level", "LOW")
    within_budget = (flight_data or {}).get("within_budget", False)
    reasons = []
    if risk == "HIGH":
        reasons.append(f"weather risk is {risk}")
    if not within_budget:
        reasons.append("no flight option fits the budget")

    if risk == "HIGH" or not within_budget:
        decision = "needs_human_review"
        booking_status = "pending_review"
    else:
        decision = "booked"
        booking_status = "confirmed"
        if risk == "MEDIUM":
            reasons.append(f"weather risk is {risk} but within acceptable range")

    result = {
        "decision": decision,
        "decision_reason": "; ".join(reasons) if reasons else "low weather risk and flight within budget",
        "booking_status": booking_status,
        "budget": budget,
    }
    _last["decision"] = result
    return result


@tool(name="finalize_booking")
def finalize_booking(human_approval: dict) -> dict:
    """Finalize a booking after a human reviewer has approved or rejected it."""
    approved = (human_approval or {}).get("decision") == "approved"
    result = {
        "booking_status": "confirmed" if approved else "rejected",
        "reviewer": (human_approval or {}).get("reviewer"),
        "notes": (human_approval or {}).get("notes"),
    }
    _last["finalized"] = result
    return result


def _confirmation_code(booking_id):
    return f"CONF-{abs(hash(booking_id)) % 1000000:06d}"


def _build_agent():
    return Agent(
        model=BEDROCK_MODEL_ID,
        system_prompt=(
            "You are a cautious travel coordinator who prioritizes traveler safety "
            "and budget compliance. You extract trip requirements, evaluate options "
            "from other agents, and decide whether to auto-approve bookings or route "
            "them for human review based on risk factors. Use the tool that matches "
            "the requested action. Be concise."
        ),
        tools=[extract_travel_details, analyze_and_decide, finalize_booking],
    )


def _agent_for_booking(booking_id):
    agent = _build_agent()
    if SESSION_BUCKET:
        agent.session_manager = S3SessionManager(
            session_id=booking_id, bucket=SESSION_BUCKET, prefix="planner-sessions", region_name=AWS_REGION
        )
        agent.agent_id = f"planner-agent-{booking_id}"
    return agent


def _publish(detail_type, detail):
    eventbridge.put_events(
        Entries=[
            {
                "Source": "workshop.planner-agent",
                "DetailType": detail_type,
                "Detail": json.dumps(detail),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )


# --------------------------------------------------------------------------
# Orchestration mode
# --------------------------------------------------------------------------
def _handle_orchestration(event, booking_id):
    action = event["action"]
    agent = _agent_for_booking(booking_id)
    _last.clear()

    if action == "extract":
        prompt = (
            f"Booking {booking_id}: extract and validate travel details from this "
            f"request:\n{json.dumps({k: event.get(k) for k in ('origin', 'destination', 'travel_dates', 'travelers', 'budget', 'airline_preference', 'interests')})}"
        )
        response = agent(prompt)
        print(f"[agent_response] bookingID={booking_id} {response}")
        extracted = _last.get("extracted", {})
        return {
            "statusCode": 200,
            "extractedData": extracted,
            "confidence": extracted.get("confidence", 0.5),
            "ready_for_coordination": True,
        }

    if action == "analyze_and_decide":
        prompt = (
            f"Booking {booking_id}, budget ${event.get('budget')}: analyze this "
            f"weather data:\n{json.dumps(event.get('weather_data', {}))}\n"
            f"and this flight data:\n{json.dumps(event.get('flight_data', {}))}\n"
            f"then decide whether to auto-approve or escalate this booking."
        )
        response = agent(prompt)
        print(f"[agent_response] bookingID={booking_id} {response}")
        decision = _last.get("decision", {})
        booking_confirmation = _confirmation_code(booking_id) if decision.get("decision") == "booked" else None
        return {
            "statusCode": 200,
            "decision": decision.get("decision", "needs_human_review"),
            "decision_reason": decision.get("decision_reason", ""),
            "booking_status": decision.get("booking_status", "pending_review"),
            "booking_confirmation": booking_confirmation,
            "message": "Booking auto-approved" if booking_confirmation else "Escalated for human review",
        }

    if action == "finalize_booking":
        prompt = (
            f"Booking {booking_id}: finalize the booking based on this human "
            f"reviewer decision:\n{json.dumps(event.get('human_approval', {}))}"
        )
        response = agent(prompt)
        print(f"[agent_response] bookingID={booking_id} {response}")
        finalized = _last.get("finalized", {})
        confirmed = finalized.get("booking_status") == "confirmed"
        return {
            "statusCode": 200,
            "booking_status": finalized.get("booking_status", "rejected"),
            "booking_confirmation": _confirmation_code(booking_id) if confirmed else None,
            "message": "Booking confirmed after human review" if confirmed else "Booking rejected by reviewer",
        }

    return {"statusCode": 400, "message": f"unknown action: {action}"}


# --------------------------------------------------------------------------
# Choreography mode
# --------------------------------------------------------------------------
def _handle_travel_request_submitted(booking_id, detail):
    agent = _agent_for_booking(booking_id)
    _last.clear()
    prompt = (
        f"Booking {booking_id}: extract and validate travel details from this "
        f"request:\n{json.dumps({k: detail.get(k) for k in ('origin', 'destination', 'travel_dates', 'travelers', 'budget', 'airline_preference', 'interests')})}"
    )
    response = agent(prompt)
    print(f"[agent_response] bookingID={booking_id} {response}")
    extracted = _last.get("extracted", detail)

    request_context = {
        "bookingID": booking_id,
        "userId": detail.get("userId"),
        "origin": extracted.get("origin", detail.get("origin")),
        "destination": extracted.get("destination", detail.get("destination")),
        "travel_dates": extracted.get("travel_dates", detail.get("travel_dates")),
        "travelers": extracted.get("travelers", detail.get("travelers")),
        "budget": extracted.get("budget", detail.get("budget")),
        "airline_preference": extracted.get("airline_preference", detail.get("airline_preference", "")),
        "interests": detail.get("interests", []),
    }
    _save_state(booking_id, "request", request_context)
    _publish("DatesFinalized", request_context)
    print(f"[action] bookingID={booking_id} emitted_event=DatesFinalized")


def _handle_partial_result(booking_id, name, detail):
    _save_state(booking_id, name, detail)
    other = "flight" if name == "weather" else "weather"
    other_data = _load_state(booking_id, other)
    if other_data is None:
        print(f"[event] bookingID={booking_id} waiting on {other} result before deciding")
        return

    weather_data = detail if name == "weather" else other_data
    flight_data = detail if name == "flight" else other_data
    request_context = _load_state(booking_id, "request") or {}

    agent = _agent_for_booking(booking_id)
    _last.clear()
    prompt = (
        f"Booking {booking_id}, budget ${request_context.get('budget')}: analyze "
        f"this weather data:\n{json.dumps(weather_data)}\n"
        f"and this flight data:\n{json.dumps(flight_data)}\n"
        f"then decide whether to auto-approve or escalate this booking."
    )
    response = agent(prompt)
    print(f"[agent_response] bookingID={booking_id} {response}")
    decision = _last.get("decision", {})

    if decision.get("decision") == "booked":
        confirmation = _confirmation_code(booking_id)
        _publish(
            "BookingFinalized",
            {
                "bookingID": booking_id,
                "booking_confirmation": confirmation,
                "decision_reason": decision.get("decision_reason"),
                **request_context,
            },
        )
        print(f"[action] bookingID={booking_id} emitted_event=BookingFinalized")
        # also emit the terminal event the optional Hotel Agent listens for, so
        # that bonus module works whether a booking was auto-approved or went
        # through human review (see _handle_human_approval_decision below).
        _publish(
            "FinalBookingCompleted",
            {
                "booking_id": booking_id,
                "bookingID": booking_id,
                "booking_status": "confirmed",
                "booking_confirmation": confirmation,
                "selected_flight": (flight_data.get("flight_options") or [None])[0],
                **request_context,
            },
        )
        print(f"[action] bookingID={booking_id} emitted_event=FinalBookingCompleted")
        _delete_state(booking_id, "request")
    else:
        pending = {
            **request_context,
            "weather_conditions": weather_data,
            "flight_options": flight_data.get("flight_options", []),
            "review_reason": decision.get("decision_reason", "risk assessment failed"),
        }
        _save_state(booking_id, "pending_review", pending)
        _publish(
            "HumanReviewRequired",
            {
                "bookingID": booking_id,
                "review_reason": decision.get("decision_reason", "risk assessment failed"),
                "route": f"{request_context.get('origin')} -> {request_context.get('destination')}",
                "weather_conditions": weather_data,
                "flight_options": flight_data.get("flight_options", []),
                "recommendation": weather_data.get("recommendation", ""),
            },
        )
        print(f"[action] bookingID={booking_id} emitted_event=HumanReviewRequired")

    for key in ("weather", "flight"):
        _delete_state(booking_id, key)


def _handle_human_approval_decision(booking_id, detail):
    pending = _load_state(booking_id, "pending_review") or {}
    agent = _agent_for_booking(booking_id)
    _last.clear()
    prompt = f"Booking {booking_id}: finalize the booking based on this human reviewer decision:\n{json.dumps(detail)}"
    response = agent(prompt)
    print(f"[agent_response] bookingID={booking_id} {response}")
    finalized = _last.get("finalized", {})
    confirmed = finalized.get("booking_status") == "confirmed"

    _publish(
        "FinalBookingCompleted",
        {
            "booking_id": booking_id,
            "bookingID": booking_id,
            "booking_status": finalized.get("booking_status", "rejected"),
            "booking_confirmation": _confirmation_code(booking_id) if confirmed else None,
            "origin": pending.get("origin"),
            "destination": pending.get("destination"),
            "travel_dates": pending.get("travel_dates"),
            "budget": pending.get("budget"),
            "selected_flight": (pending.get("flight_options") or [None])[0],
            "reviewer": detail.get("reviewer"),
            "timestamp": detail.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        },
    )
    print(f"[action] bookingID={booking_id} emitted_event=FinalBookingCompleted")
    _delete_state(booking_id, "pending_review")
    _delete_state(booking_id, "request")


def lambda_handler(event, context):
    print(f"[event] received: {json.dumps(event)[:2000]}")

    if "action" in event:
        return _handle_orchestration(event, event.get("bookingID"))

    detail_type = event.get("detail-type")
    detail = event.get("detail", {})
    booking_id = detail.get("bookingID") or detail.get("booking_id")
    print(f"[event] bookingID={booking_id} planner agent received {detail_type}")

    try:
        if detail_type == "TravelRequestSubmitted":
            _handle_travel_request_submitted(booking_id, detail)
        elif detail_type == "WeatherAnalysisCompleted":
            _handle_partial_result(booking_id, "weather", detail)
        elif detail_type == "FlightSearchCompleted":
            _handle_partial_result(booking_id, "flight", detail)
        elif detail_type == "HumanApprovalDecision":
            _handle_human_approval_decision(booking_id, detail)
        else:
            print(f"[event] bookingID={booking_id} ignoring unrecognized detail-type={detail_type}")
        return {"statusCode": 200, "bookingID": booking_id}
    except Exception as exc:  # noqa: BLE001
        print(f"[error] bookingID={booking_id} {exc}")
        return {"statusCode": 500, "bookingID": booking_id, "error": str(exc)}
