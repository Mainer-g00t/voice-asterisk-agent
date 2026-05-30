"""
Example webhook tools server — demonstrates how external tools work with the voice agent.

Each endpoint receives:
  POST /tool-name
  Body: {"tool": "tool_name", "arguments": {...}}

And returns a JSON object that gets passed back to the LLM as the tool result.

Add more endpoints here to expose new capabilities to voice agents.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from loguru import logger

app = FastAPI(title="Voice Agent Tools Server")


@app.post("/get_current_time")
async def get_current_time(request: Request):
    """
    Returns the current date and time, optionally in a given timezone.

    Tool arguments:
      - timezone (string, optional): IANA timezone name, e.g. "America/New_York"
    """
    body = await request.json()
    args = body.get("arguments", {})
    tz_name = args.get("timezone", "UTC").strip() or "UTC"

    logger.info(f"[get_current_time] timezone={tz_name!r}")

    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = timezone.utc
        tz_name = "UTC"

    now = datetime.now(tz)
    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%A, %B %-d, %Y"),
        "time": now.strftime("%-I:%M %p"),
        "timezone": tz_name,
    }


@app.post("/get_weather")
async def get_weather(request: Request):
    """
    Simulated weather tool — returns fake but realistic-looking weather data.
    Replace this with a real weather API call (e.g. Open-Meteo, OpenWeatherMap).

    Tool arguments:
      - location (string): city or location name
      - units    (string, optional): "celsius" or "fahrenheit" (default: celsius)
    """
    body = await request.json()
    args = body.get("arguments", {})
    location = args.get("location", "Unknown").strip()
    units = args.get("units", "celsius").lower()

    logger.info(f"[get_weather] location={location!r} units={units!r}")

    # Simulated response — swap for a real API call
    temp_c = 18
    temp = temp_c if units == "celsius" else round(temp_c * 9/5 + 32)
    unit_label = "°C" if units == "celsius" else "°F"

    return {
        "location": location,
        "temperature": f"{temp}{unit_label}",
        "condition": "Partly cloudy",
        "humidity": "62%",
        "wind": "12 km/h NW",
        "note": "⚠️ Simulated data — connect a real weather API for production use",
    }


@app.post("/echo")
async def echo(request: Request):
    """
    Debug tool — echoes back everything it receives.
    Useful for verifying the webhook pipeline is working end to end.
    """
    body = await request.json()
    logger.info(f"[echo] received: {body}")
    return {
        "echo": body.get("arguments", {}),
        "tool": body.get("tool"),
        "status": "ok",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
