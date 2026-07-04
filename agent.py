import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv

# Patch SSL before any network import — reused from existing LIvekitAIVoice agent.py
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools, get_agent_profile
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":      logger.info(msg)
    elif level == "warning": logger.warning(msg)
    else:                    logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load Supabase settings table into os.environ before worker starts."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value"):
                os.environ[row["key"]] = row["value"]
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Import Google plugin paths ───────────────────────────────────────────────
_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None

try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel (stable path)")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel (beta path)")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


# ── Session factory ──────────────────────────────────────────────────────────

def _build_session(
    tools: list,
    voice: str = None,
    model: str = None,
) -> AgentSession:
    """
    Build an AgentSession configured for Gemini Live realtime (preferred)
    or fallback to pipeline mode (Deepgram STT + Google LLM + Google TTS).
    """
    voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    model = model or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() == "true"
    api_key = os.getenv("GOOGLE_API_KEY", "")

    # Try Gemini Live realtime first (sub-100ms, zero separate STT/TTS)
    if use_realtime and api_key:
        RealtimeModel = _google_realtime or _google_beta_realtime
        if RealtimeModel:
            try:
                realtime_model = RealtimeModel(
                    model=model,
                    voice=voice,
                    api_key=api_key,
                    tools=tools,
                )
                session = AgentSession(
                    vad=silero.VAD.load(),
                    llm=realtime_model,
                )
                logger.info(f"Using Gemini Live realtime: model={model}, voice={voice}")
                return session
            except Exception as exc:
                logger.warning(f"Gemini Live init failed, falling back to pipeline: {exc}")

    # Fallback: pipeline mode (Deepgram STT → Google LLM → Google TTS)
    logger.info("Using pipeline mode (STT + LLM + TTS)")
    stt = None
    if _deepgram_stt:
        stt = _deepgram_stt(model="nova-2", language="en")

    llm_instance = None
    if _google_llm:
        llm_instance = _google_llm(model="gemini-2.0-flash", api_key=api_key)

    tts = None
    if _google_tts:
        tts = _google_tts(voice=voice, api_key=api_key)

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=stt,
        llm=llm_instance,
        tts=tts,
    )
    return session


# ── Agent class ──────────────────────────────────────────────────────────────

class OutboundAgent(Agent):
    """
    AI agent for outbound appointment-booking calls.
    Speaks first on connect, uses Gemini Live for real-time voice.
    """

    def __init__(self, instructions: str, tools: list) -> None:
        super().__init__(
            instructions=instructions,
            tools=tools,
        )


# ── Entrypoint ───────────────────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext):
    """
    Main entrypoint for the LiveKit agent worker.

    For outbound calls (adapted from existing LIvekitAIVoice agent.py):
    1. Parses phone_number and config from job/room metadata
    2. Connects to the room
    3. Optionally loads an agent profile from Supabase
    4. Initiates the SIP call via create_sip_participant
    5. Starts the AI session with Gemini Live
    """
    logger.info(f"Connecting to room: {ctx.room.name}")

    # ── Parse metadata (reused from existing agent.py pattern) ──────────
    phone_number = None
    lead_name = None
    config_dict = {}

    # Check Job Metadata (Legacy/Dispatch)
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
            lead_name = data.get("lead_name")
            config_dict = data
    except Exception:
        pass

    # Check Room Metadata (Dashboard/API) — overrides Job Metadata if present
    try:
        if ctx.room.metadata:
            data = json.loads(ctx.room.metadata)
            if data.get("phone_number"):
                phone_number = data.get("phone_number")
            if data.get("lead_name"):
                lead_name = data.get("lead_name")
            config_dict.update(data)
    except Exception:
        logger.warning("No valid JSON metadata found in Room.")

    await _log("info", f"Call starting — phone={phone_number}, lead={lead_name}")

    # ── Load agent profile if specified ──────────────────────────────────
    profile_id = config_dict.get("agent_profile_id")
    profile = None
    custom_prompt = config_dict.get("system_prompt")
    voice = config_dict.get("voice")
    model = config_dict.get("model")
    enabled_tools_list = []

    if profile_id:
        try:
            profile = await get_agent_profile(profile_id)
            if profile:
                voice = profile.get("voice", voice)
                model = profile.get("model", model)
                custom_prompt = profile.get("system_prompt") or custom_prompt
                try:
                    enabled_tools_list = json.loads(profile.get("enabled_tools", "[]"))
                except Exception:
                    pass
                logger.info(f"Loaded agent profile: {profile.get('name')}")
        except Exception as exc:
            logger.warning(f"Failed to load agent profile {profile_id}: {exc}")

    if not enabled_tools_list:
        try:
            enabled_tools_list = await get_enabled_tools()
        except Exception:
            enabled_tools_list = []

    # ── Build prompt ─────────────────────────────────────────────────────
    system_prompt = build_prompt(
        lead_name=lead_name or config_dict.get("lead_name", "there"),
        business_name=config_dict.get("business_name", "our company"),
        service_type=config_dict.get("service_type", "our service"),
        custom_prompt=custom_prompt,
    )

    # ── Initialize tools ─────────────────────────────────────────────────
    fnc_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tool_methods = fnc_ctx.build_tool_list(enabled_tools_list)

    # ── Build session ────────────────────────────────────────────────────
    session = _build_session(
        tools=tool_methods,
        voice=voice,
        model=model,
    )

    # ── Start session ────────────────────────────────────────────────────
    await session.start(
        room=ctx.room,
        agent=OutboundAgent(
            instructions=system_prompt,
            tools=list(fnc_ctx.function_tools.values()),
        ),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True,
        ),
    )

    # ── Dial out if needed (reused from existing agent.py) ───────────────
    should_dial = False
    if phone_number:
        user_already_here = False
        for p in ctx.room.remote_participants.values():
            if f"sip_{phone_number}" in p.identity or "sip_" in p.identity:
                user_already_here = True
                break

        if not user_already_here:
            should_dial = True
            logger.info("User not in room. Agent will initiate dial-out.")
        else:
            logger.info("User already in room (Dashboard dispatched). Generating greeting.")

    if should_dial:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial out")
            return

        logger.info(f"Initiating outbound SIP call to {phone_number}...")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            logger.info("Call answered! Agent is now listening.")
            await _log("info", f"Call connected to {phone_number}")

            # Speak first — critical for outbound calls
            await session.generate_reply(
                instructions="The call has just connected. Speak immediately — introduce yourself and confirm identity."
            )
        except Exception as e:
            await _log("error", f"Failed to place outbound call to {phone_number}: {e}")
            logger.error(f"Failed to place outbound call: {e}")
            ctx.shutdown()
    else:
        # Inbound or Dashboard-dispatched call — greet immediately
        logger.info("Generating initial greeting...")
        await session.generate_reply(
            instructions="The call is connected. Greet the user immediately."
        )


if __name__ == "__main__":
    # Load DB settings into env before agent starts
    load_db_settings_to_env()
    init_db()

    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
