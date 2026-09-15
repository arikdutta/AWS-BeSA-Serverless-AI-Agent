"""
Weather Agent - analyzes destination weather and travel risk.

Dual-mode handler:
  - Orchestration (Step Functions Task invoke): event has an "action" key,
    returns a flat dict consumed directly via ResultSelector in the ASL.
  - Choreography (EventBridge target): event has "detail-type": "DatesFinalized",
    publishes a "WeatherAnalysisCompleted" event back onto the bus.
"""
import json
import os
from datetime import datetime

import boto3
from strands import Agent, tool
from strands.session.s3_session_manager import S3SessionManager

EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "multi-agent-bus")
SESSION_BUCKET = os.environ.get("SESSION_BUCKET")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

eventbridge = boto3.client("events")

# Dummy data: coastal destinations get storm risk during Atlantic hurricane
# season (June-November) so the same test scenarios that hit the real
# workshop's "high risk" path also hit this one.
HURRICANE_MONTHS = {6, 7, 8, 9, 10, 11}
COASTAL_STORM_CITIES = {"miami": "thunderstorms", "new orleans": "storms", "houston": "storms"}

# captured tool outputs for this invocation (see module docstring in hotel
# agent / planner agent for why we build the outbound payload from these
# instead of parsing the agent's free-text response)
_last = {}


def _month_of(date_str):
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str, fmt).month
        except ValueError:
            continue
    return datetime.utcnow().month


def _first_date(travel_dates):
    if not isinstance(travel_dates, dict):
        return str(travel_dates)
    return travel_dates.get("start") or travel_dates.get("departure") or ""


@tool(name="get_weather_forecast")
def get_weather_forecast(destination: str, travel_date: str) -> dict:
    """Get a weather forecast for a destination on a given date (dummy data)."""
    city = destination.split(",")[0].strip().lower()
    month = _month_of(travel_date)
    if city in COASTAL_STORM_CITIES and month in HURRICANE_MONTHS:
        forecast = {"temperature": 84, "conditions": COASTAL_STORM_CITIES[city], "storm_probability": 0.7}
    else:
        # deterministic pseudo-variety so different cities aren't identical
        seed = sum(ord(c) for c in city) + month
        forecast = {
            "temperature": 55 + (seed % 35),
            "conditions": ["clear", "partly cloudy", "light rain"][seed % 3],
            "storm_probability": round((seed % 10) / 100, 2),
        }
    _last["forecast"] = forecast
    return forecast


@tool(name="analyze_travel_risk")
def analyze_travel_risk(forecast: dict) -> dict:
    """Assess travel risk level from a weather forecast."""
    storm_prob = forecast.get("storm_probability", 0)
    temp = forecast.get("temperature", 70)
    if storm_prob >= 0.5 or temp >= 100 or temp <= 20:
        risk = "HIGH"
    elif storm_prob >= 0.2 or temp >= 90 or temp <= 35:
        risk = "MEDIUM"
    else:
        risk = "LOW"
    result = {"risk_level": risk, "conditions": forecast.get("conditions"), "temperature": temp}
    _last["risk"] = result
    return result


@tool(name="generate_recommendations")
def generate_recommendations(risk_level: str, conditions: str) -> str:
    """Write a short travel recommendation given a risk level and conditions."""
    if risk_level == "HIGH":
        text = f"High risk: {conditions} expected. Consider alternative dates or travel insurance."
    elif risk_level == "MEDIUM":
        text = f"Moderate risk: {conditions} possible. Monitor forecasts closer to departure."
    else:
        text = f"Low risk: {conditions} expected. Good conditions for travel."
    _last["recommendation"] = text
    return text


def _build_agent():
    kwargs = {
        "model": BEDROCK_MODEL_ID,
        "system_prompt": (
            "You are a meteorologist supporting a travel booking system. Given a "
            "destination and travel date, call get_weather_forecast, then "
            "analyze_travel_risk, then generate_recommendations, in that order. "
            "Be concise."
        ),
        "tools": [get_weather_forecast, analyze_travel_risk, generate_recommendations],
    }
    return Agent(**kwargs)


def _run_weather_analysis(booking_id, destination, travel_dates):
    _last.clear()
    agent = _build_agent()
    if SESSION_BUCKET:
        agent.session_manager = S3SessionManager(
            session_id=booking_id, bucket=SESSION_BUCKET, prefix="weather-sessions", region_name=AWS_REGION
        )
        agent.agent_id = f"weather-agent-{booking_id}"

    travel_date = _first_date(travel_dates)
    prompt = (
        f"Booking {booking_id}: analyze travel weather risk for a trip to "
        f"{destination} starting {travel_date}."
    )
    response = agent(prompt)
    print(f"[agent_response] bookingID={booking_id} {response}")

    forecast = _last.get("forecast", {})
    risk = _last.get("risk", {})
    return {
        "weather_analysis": str(response),
        "risk_level": risk.get("risk_level", "LOW"),
        "conditions": risk.get("conditions", forecast.get("conditions", "unknown")),
        "temperature": risk.get("temperature", forecast.get("temperature")),
        "recommendation": _last.get("recommendation", ""),
    }


def _publish(detail_type, detail):
    eventbridge.put_events(
        Entries=[
            {
                "Source": "workshop.weather-agent",
                "DetailType": detail_type,
                "Detail": json.dumps(detail),
                "EventBusName": EVENT_BUS_NAME,
            }
        ]
    )


def lambda_handler(event, context):
    print(f"[event] received: {json.dumps(event)[:2000]}")

    if "action" in event:
        booking_id = event.get("bookingID")
        result = _run_weather_analysis(booking_id, event.get("destination"), event.get("travel_dates"))
        return {"statusCode": 200, **result}

    detail = event.get("detail", {})
    booking_id = detail.get("bookingID") or detail.get("booking_id")
    try:
        result = _run_weather_analysis(booking_id, detail.get("destination"), detail.get("travel_dates"))
        _publish("WeatherAnalysisCompleted", {"bookingID": booking_id, **result})
        print(f"[action] bookingID={booking_id} emitted_event=WeatherAnalysisCompleted")
        return {"statusCode": 200, "bookingID": booking_id}
    except Exception as exc:  # noqa: BLE001 - surface all failures as a completed-with-error event
        print(f"[error] bookingID={booking_id} {exc}")
        _publish("WeatherAnalysisCompleted", {"bookingID": booking_id, "risk_level": "HIGH", "error": str(exc)})
        return {"statusCode": 500, "bookingID": booking_id, "error": str(exc)}
