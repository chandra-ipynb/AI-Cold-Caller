"""
OutboundAI — FastAPI Backend
REST API endpoints, APScheduler campaign scheduling, LiveKit call dispatch.
Reuses dispatch patterns from existing LIvekitAIVoice make_call.py and dashboard API routes.
"""

import asyncio
import json
import logging
import os
import random
import ssl
import uuid
import certifi
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# SSL fix — reused from existing project
os.environ["SSL_CERT_FILE"] = certifi.where()

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from livekit import api

from db import (
    init_db, get_all_settings, save_settings, get_setting,
    get_all_appointments, cancel_appointment, insert_appointment,
    get_all_calls, get_calls_by_phone, update_call_notes, get_contacts, get_contact_memory,
    get_stats, log_error, get_logs, clear_errors,
    create_campaign, get_all_campaigns, get_campaign, update_campaign_status,
    update_campaign_run_stats, delete_campaign,
    get_all_agent_profiles, get_agent_profile, create_agent_profile,
    update_agent_profile, delete_agent_profile, set_default_agent_profile,
)
from prompts import DEFAULT_SYSTEM_PROMPT

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-server")

# ── APScheduler ──────────────────────────────────────────────────────────────
scheduler = None
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    scheduler = AsyncIOScheduler()
except ImportError:
    logger.warning("APScheduler not installed — campaign scheduling disabled")


# ── LiveKit API client ───────────────────────────────────────────────────────

def _lk_api():
    """Create a LiveKit API client (reused from existing make_call.py pattern)."""
    url = os.getenv("LIVEKIT_URL", "")
    key = os.getenv("LIVEKIT_API_KEY", "")
    secret = os.getenv("LIVEKIT_API_SECRET", "")
    if not (url and key and secret):
        raise HTTPException(500, "LiveKit credentials not configured")
    return api.LiveKitAPI(url=url, api_key=key, api_secret=secret)


# ── Call dispatch (adapted from existing make_call.py + dashboard route.ts) ──

async def dispatch_single_call(
    phone_number: str,
    lead_name: str = None,
    business_name: str = None,
    service_type: str = None,
    system_prompt: str = None,
    agent_profile_id: str = None,
) -> dict:
    """
    Dispatch a single outbound call via LiveKit agent dispatch.
    Adapted from existing make_call.py CreateAgentDispatchRequest pattern.
    """
    phone_number = phone_number.strip()
    if not phone_number.startswith("+"):
        raise HTTPException(400, "Phone number must start with + and country code")

    trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
    if not trunk_id:
        raise HTTPException(500, "OUTBOUND_TRUNK_ID not configured")

    # Room name generation (reused from existing make_call.py)
    room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"

    metadata = json.dumps({
        "phone_number": phone_number,
        "lead_name": lead_name or "",
        "business_name": business_name or "our company",
        "service_type": service_type or "our service",
        "system_prompt": system_prompt or "",
        "agent_profile_id": agent_profile_id or "",
    })

    lk = _lk_api()
    try:
        # Use agent dispatch (from existing make_call.py pattern)
        dispatch_request = api.CreateAgentDispatchRequest(
            agent_name="outbound-caller",
            room=room_name,
            metadata=metadata,
        )
        dispatch = await lk.agent_dispatch.create_dispatch(dispatch_request)

        await log_error("server", f"Call dispatched to {phone_number}", f"room={room_name}", "info")

        return {
            "success": True,
            "room_name": room_name,
            "dispatch_id": dispatch.id if hasattr(dispatch, "id") else str(uuid.uuid4()),
            "phone_number": phone_number,
        }
    except Exception as exc:
        await log_error("server", f"Failed to dispatch call to {phone_number}", str(exc))
        raise HTTPException(500, f"Dispatch failed: {exc}")
    finally:
        await lk.aclose()


# ── Campaign execution ───────────────────────────────────────────────────────

async def run_campaign(campaign_id: str):
    """Execute all calls in a campaign with configurable delay."""
    campaign = await get_campaign(campaign_id)
    if not campaign or campaign["status"] == "paused":
        return

    await update_campaign_status(campaign_id, "running")

    try:
        contacts = json.loads(campaign["contacts_json"])
    except Exception:
        contacts = []

    delay = campaign.get("call_delay_seconds", 3)
    prompt = campaign.get("system_prompt")
    profile_id = campaign.get("agent_profile_id")
    dispatched = 0
    failed = 0

    for contact in contacts:
        phone = contact if isinstance(contact, str) else contact.get("phone", "")
        name = contact.get("name", "") if isinstance(contact, dict) else ""
        if not phone:
            failed += 1
            continue
        try:
            await dispatch_single_call(
                phone_number=phone,
                lead_name=name,
                system_prompt=prompt,
                agent_profile_id=profile_id,
            )
            dispatched += 1
        except Exception as exc:
            failed += 1
            await log_error("campaign", f"Failed to dispatch {phone}: {exc}", "", "warning")

        if delay > 0:
            await asyncio.sleep(delay)

    await update_campaign_run_stats(campaign_id, dispatched, failed)
    await log_error("server", f"Campaign {campaign_id} completed: {dispatched} dispatched, {failed} failed", "", "info")


def schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str):
    """Schedule a campaign with APScheduler."""
    if not scheduler:
        return

    job_id = f"campaign_{campaign_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    if schedule_type == "once":
        # Run immediately
        scheduler.add_job(
            run_campaign, args=[campaign_id], id=job_id,
            trigger="date", run_date=datetime.now(),
        )
    elif schedule_type == "daily":
        hour, minute = (schedule_time or "09:00").split(":")
        scheduler.add_job(
            run_campaign, args=[campaign_id], id=job_id,
            trigger=CronTrigger(hour=int(hour), minute=int(minute)),
        )
    elif schedule_type == "weekdays":
        hour, minute = (schedule_time or "09:00").split(":")
        scheduler.add_job(
            run_campaign, args=[campaign_id], id=job_id,
            trigger=CronTrigger(day_of_week="mon-fri", hour=int(hour), minute=int(minute)),
        )


# ── FastAPI lifecycle ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if scheduler:
        scheduler.start()
        logger.info("APScheduler started")
        # Re-schedule active campaigns
        try:
            campaigns = await get_all_campaigns()
            for c in campaigns:
                if c["status"] == "active" and c["schedule_type"] != "once":
                    schedule_campaign(c["id"], c["schedule_type"], c["schedule_time"])
        except Exception as exc:
            logger.warning(f"Failed to restore campaigns: {exc}")
    yield
    if scheduler:
        scheduler.shutdown()


app = FastAPI(title="OutboundAI", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic models ─────────────────────────────────────────────────────────

class CallRequest(BaseModel):
    phone_number: str
    lead_name: Optional[str] = None
    business_name: Optional[str] = None
    service_type: Optional[str] = None
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class BatchRequest(BaseModel):
    numbers: list
    lead_names: Optional[dict] = None
    business_name: Optional[str] = None
    service_type: Optional[str] = None
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None
    delay_seconds: Optional[int] = 3


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: Optional[str] = "once"
    schedule_time: Optional[str] = "09:00"
    call_delay_seconds: Optional[int] = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AppointmentRequest(BaseModel):
    name: str
    phone: str
    date: str
    time: str
    service: str


class AgentProfileRequest(BaseModel):
    name: str
    voice: Optional[str] = "Aoede"
    model: Optional[str] = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: Optional[str] = "[]"
    is_default: Optional[bool] = False


# ── Dashboard route ──────────────────────────────────────────────────────────

@app.get("/")
async def serve_dashboard():
    """Serve the single-file dashboard."""
    html_path = os.path.join(os.path.dirname(__file__), "ui", "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    return JSONResponse({"message": "OutboundAI API is running. Dashboard not found at ui/index.html."})


# ── Call endpoints ───────────────────────────────────────────────────────────

@app.post("/api/call")
async def api_call(req: CallRequest):
    """Dispatch a single outbound call."""
    result = await dispatch_single_call(
        phone_number=req.phone_number,
        lead_name=req.lead_name,
        business_name=req.business_name,
        service_type=req.service_type,
        system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    return result


@app.post("/api/batch")
async def api_batch(req: BatchRequest):
    """Dispatch batch calls (adapted from existing dashboard queue route.ts)."""
    results = []
    for i, num in enumerate(req.numbers):
        phone = num if isinstance(num, str) else num.get("phone", "")
        name = None
        if isinstance(num, dict):
            name = num.get("name")
        elif req.lead_names and phone in req.lead_names:
            name = req.lead_names[phone]
        try:
            r = await dispatch_single_call(
                phone_number=phone,
                lead_name=name,
                business_name=req.business_name,
                service_type=req.service_type,
                system_prompt=req.system_prompt,
                agent_profile_id=req.agent_profile_id,
            )
            results.append({"phone_number": phone, "status": "dispatched", **r})
        except Exception as exc:
            results.append({"phone_number": phone, "status": "failed", "error": str(exc)})

        if req.delay_seconds and req.delay_seconds > 0 and i < len(req.numbers) - 1:
            await asyncio.sleep(req.delay_seconds)

    return {"success": True, "total": len(req.numbers), "results": results}


@app.post("/api/batch/csv")
async def api_batch_csv(file: UploadFile = File(...)):
    """Upload CSV of contacts and dispatch calls."""
    import csv
    import io
    content = await file.read()
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    results = []
    for row in reader:
        phone = row.get("phone") or row.get("phone_number") or row.get("number") or ""
        name = row.get("name") or row.get("lead_name") or ""
        if not phone.strip():
            continue
        try:
            r = await dispatch_single_call(phone_number=phone.strip(), lead_name=name.strip())
            results.append({"phone_number": phone, "status": "dispatched"})
        except Exception as exc:
            results.append({"phone_number": phone, "status": "failed", "error": str(exc)})
        await asyncio.sleep(2)
    return {"success": True, "total": len(results), "results": results}


# ── Appointment endpoints ────────────────────────────────────────────────────

@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.post("/api/appointments")
async def api_create_appointment(req: AppointmentRequest):
    booking_id = await insert_appointment(req.name, req.phone, req.date, req.time, req.service)
    return {"success": True, "booking_id": booking_id}


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    success = await cancel_appointment(appointment_id)
    if not success:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"success": True}


# ── Call log endpoints ───────────────────────────────────────────────────────

@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.get("/api/calls/{phone}")
async def api_get_calls_by_phone(phone: str):
    return await get_calls_by_phone(phone)


@app.put("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, body: dict):
    notes = body.get("notes", "")
    success = await update_call_notes(call_id, notes)
    if not success:
        raise HTTPException(404, "Call log not found")
    return {"success": True}


# ── CRM / Contacts ──────────────────────────────────────────────────────────

@app.get("/api/contacts")
async def api_get_contacts():
    return await get_contacts()


@app.get("/api/contact/{phone}/memory")
async def api_get_contact_memory(phone: str):
    return await get_contact_memory(phone)


# ── Stats ────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


# ── Campaign endpoints ───────────────────────────────────────────────────────

@app.get("/api/campaigns")
async def api_get_campaigns():
    return await get_all_campaigns()


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    campaign_id = await create_campaign(
        name=req.name,
        contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type,
        schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds,
        system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)
    return {"success": True, "campaign_id": campaign_id}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(run_campaign(campaign_id))
    return {"success": True, "message": "Campaign execution started"}


@app.put("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, body: dict):
    status = body.get("status", "paused")
    success = await update_campaign_status(campaign_id, status)
    if not success:
        raise HTTPException(404, "Campaign not found")
    return {"success": True}


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    # Remove scheduled job
    if scheduler:
        try:
            scheduler.remove_job(f"campaign_{campaign_id}")
        except Exception:
            pass
    success = await delete_campaign(campaign_id)
    if not success:
        raise HTTPException(404, "Campaign not found")
    return {"success": True}


# ── Settings endpoints ───────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(body: dict):
    await save_settings(body)
    return {"success": True}


# ── Prompt endpoint ──────────────────────────────────────────────────────────

@app.get("/api/prompt")
async def api_get_prompt():
    custom = await get_setting("SYSTEM_PROMPT", "")
    return {"prompt": custom if custom else DEFAULT_SYSTEM_PROMPT, "is_custom": bool(custom)}


@app.post("/api/prompt")
async def api_save_prompt(body: dict):
    prompt = body.get("prompt", "")
    from db import set_setting
    await set_setting("SYSTEM_PROMPT", prompt)
    return {"success": True}


# ── Agent Profile endpoints ─────────────────────────────────────────────────

@app.get("/api/agent-profiles")
async def api_get_profiles():
    return await get_all_agent_profiles()


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.post("/api/agent-profiles")
async def api_create_profile(req: AgentProfileRequest):
    profile_id = await create_agent_profile(
        name=req.name, voice=req.voice, model=req.model,
        system_prompt=req.system_prompt, enabled_tools=req.enabled_tools,
        is_default=req.is_default,
    )
    return {"success": True, "profile_id": profile_id}


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_profile(profile_id: str, body: dict):
    success = await update_agent_profile(profile_id, body)
    if not success:
        raise HTTPException(404, "Profile not found")
    return {"success": True}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_profile(profile_id: str):
    success = await delete_agent_profile(profile_id)
    if not success:
        raise HTTPException(404, "Profile not found")
    return {"success": True}


@app.post("/api/agent-profiles/{profile_id}/default")
async def api_set_default_profile(profile_id: str):
    await set_default_agent_profile(profile_id)
    return {"success": True}


# ── Log endpoints ────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def api_get_logs(
    level: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 200,
):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"success": True}


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/api/health")
async def api_health():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "livekit_configured": bool(os.getenv("LIVEKIT_URL")),
        "supabase_configured": bool(os.getenv("SUPABASE_URL")),
        "gemini_configured": bool(os.getenv("GOOGLE_API_KEY")),
    }
