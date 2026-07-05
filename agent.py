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
        # Try GeminiTTS first, fallback to standard TTS
        try:
            _google_tts = _gp.beta.GeminiTTS
            logger.info("Loaded GeminiTTS")
        except AttributeError:
            _google_tts = _gp.TTS
            logger.info("Loaded standard Google TTS")
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
) -> tuple:
    """
    Build an AgentSession configured for Gemini Live realtime (preferred)
    or fallback to pipeline mode (Google LLM + Google TTS, optional Deepgram STT).

    Returns (session, is_realtime) tuple.
    """
    voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    model = model or os.getenv("GEMINI_MODEL", "gemini-2.0-flash-live-preview")
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
                )
                # We do not use TTS for Gemini Realtime anymore, we kickstart it via chat context.
                session = AgentSession(
                    llm=realtime_model,
                    tts=None,
                )
                logger.info(f"Using Gemini Live realtime: model={model}, voice={voice}")
                return session, True
            except Exception as exc:
                logger.warning(f"Gemini Live init failed, falling back to pipeline: {exc}")

    # Fallback: pipeline mode (optional Deepgram STT → Google LLM → Google TTS)
    logger.info("Using pipeline mode (STT + LLM + TTS)")
    stt = None
    if _deepgram_stt and os.getenv("DEEPGRAM_API_KEY"):
        try:
            stt = _deepgram_stt(model="nova-2", language="en")
            logger.info("Pipeline STT: Deepgram nova-2")
        except Exception as exc:
            logger.warning(f"Deepgram STT init failed: {exc}")
    else:
        logger.info("Pipeline STT: skipped (no Deepgram key)")

    llm_instance = None
    if _google_llm:
        llm_instance = _google_llm(model="gemini-2.0-flash", api_key=api_key)
        logger.info("Pipeline LLM: Google gemini-2.0-flash")

    tts = None
    if _google_tts:
        try:
            try:
                tts = _google_tts(voice=voice, api_key=api_key)
            except TypeError:
                tts = _google_tts(voice_name=voice)
            logger.info(f"Pipeline TTS: Google voice={voice}")
        except Exception as exc:
            logger.warning(f"Failed to initialize pipeline TTS: {exc}")

    if not llm_instance:
        raise ValueError("No LLM configured — need either Gemini Realtime or Google LLM with GOOGLE_API_KEY")

    session = AgentSession(
        stt=stt,
        llm=llm_instance,
        tts=tts,
    )
    return session, False


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
    """
    logger.info("=" * 60)
    logger.info("ENTRYPOINT CALLED — new agent job received")
    logger.info(f"  Room: {ctx.room.name}")
    logger.info(f"  Job ID: {ctx.job.id if hasattr(ctx.job, 'id') else 'N/A'}")
    logger.info("=" * 60)

    # ── STEP 1: Connect to room ──────────────────────────────────────
    logger.info("[STEP 1] Connecting to LiveKit room...")
    await ctx.connect()
    logger.info(f"[STEP 1] ✅ Connected to room: {ctx.room.name}")
    logger.info(f"[STEP 1]   Remote participants: {len(ctx.room.remote_participants)}")
    for pid, p in ctx.room.remote_participants.items():
        logger.info(f"[STEP 1]   - Participant: {p.identity} (sid={p.sid})")

    # ── STEP 2: Parse metadata ───────────────────────────────────────
    logger.info("[STEP 2] Parsing metadata...")
    phone_number = None
    lead_name = None
    config_dict = {}

    # Check Job Metadata
    raw_job_meta = getattr(ctx.job, 'metadata', None) or ""
    logger.info(f"[STEP 2]   Job metadata raw: {raw_job_meta[:200] if raw_job_meta else 'EMPTY'}")
    try:
        if raw_job_meta:
            data = json.loads(raw_job_meta)
            phone_number = data.get("phone_number")
            lead_name = data.get("lead_name")
            config_dict = data
            logger.info(f"[STEP 2]   Parsed from job: phone={phone_number}, lead={lead_name}")
    except Exception as exc:
        logger.warning(f"[STEP 2]   Failed to parse job metadata: {exc}")

    # Check Room Metadata
    raw_room_meta = ctx.room.metadata or ""
    logger.info(f"[STEP 2]   Room metadata raw: {raw_room_meta[:200] if raw_room_meta else 'EMPTY'}")
    try:
        if raw_room_meta:
            data = json.loads(raw_room_meta)
            if data.get("phone_number"):
                phone_number = data.get("phone_number")
            if data.get("lead_name"):
                lead_name = data.get("lead_name")
            config_dict.update(data)
            logger.info(f"[STEP 2]   Parsed from room: phone={phone_number}, lead={lead_name}")
    except Exception as exc:
        logger.warning(f"[STEP 2]   Failed to parse room metadata: {exc}")

    logger.info(f"[STEP 2] ✅ Final: phone={phone_number}, lead={lead_name}")
    logger.info(f"[STEP 2]   Full config: {json.dumps(config_dict, default=str)[:500]}")

    if not phone_number:
        logger.error("[STEP 2] ❌ NO PHONE NUMBER FOUND — agent cannot dial out!")
        await _log("error", "No phone_number in metadata — cannot make call")

    # ── STEP 3: Load agent profile ───────────────────────────────────
    logger.info("[STEP 3] Loading agent profile...")
    profile_id = config_dict.get("agent_profile_id")
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
                logger.info(f"[STEP 3] ✅ Loaded profile: {profile.get('name')}, voice={voice}")
        except Exception as exc:
            logger.warning(f"[STEP 3]   Failed to load profile {profile_id}: {exc}")
    else:
        logger.info("[STEP 3]   No profile_id specified, using defaults")

    if not enabled_tools_list:
        try:
            enabled_tools_list = await get_enabled_tools()
        except Exception:
            enabled_tools_list = []

    # ── STEP 4: Build prompt ─────────────────────────────────────────
    logger.info("[STEP 4] Building system prompt...")
    system_prompt = build_prompt(
        lead_name=lead_name or config_dict.get("lead_name", "there"),
        business_name=config_dict.get("business_name", "our company"),
        service_type=config_dict.get("service_type", "our service"),
        custom_prompt=custom_prompt,
    )
    logger.info(f"[STEP 4] ✅ Prompt length: {len(system_prompt)} chars")

    # ── STEP 5: Initialize tools ─────────────────────────────────────
    logger.info("[STEP 5] Initializing tools...")
    fnc_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tool_methods = fnc_ctx.build_tool_list(enabled_tools_list)
    logger.info(f"[STEP 5] ✅ {len(tool_methods)} tools loaded")

    # ── STEP 6: Determine if we need to dial out ─────────────────────
    logger.info("[STEP 6] Checking if SIP dial-out is needed...")
    should_dial = False
    if phone_number:
        user_already_here = False
        for p in ctx.room.remote_participants.values():
            logger.info(f"[STEP 6]   Checking participant: {p.identity}")
            if f"sip_{phone_number}" in p.identity or "sip_" in p.identity:
                user_already_here = True
                break

        if not user_already_here:
            should_dial = True
            logger.info("[STEP 6] → Will dial out (user not in room yet)")
        else:
            logger.info("[STEP 6] → User already in room, will greet directly")
    else:
        logger.warning("[STEP 6] ⚠️ No phone number — skipping dial-out")

    # ── STEP 7: Dial out ─────────────────────────────────────────────
    if should_dial:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
        logger.info(f"[STEP 7] SIP Dial-out starting...")
        logger.info(f"[STEP 7]   Phone: {phone_number}")
        logger.info(f"[STEP 7]   Trunk ID: {trunk_id}")
        logger.info(f"[STEP 7]   Room: {ctx.room.name}")
        logger.info(f"[STEP 7]   SIP Domain: {os.getenv('VOBIZ_SIP_DOMAIN', 'NOT SET')}")

        if not trunk_id:
            logger.error("[STEP 7] ❌ OUTBOUND_TRUNK_ID not set — cannot dial!")
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial out")
            return

        try:
            logger.info("[STEP 7] Calling create_sip_participant...")
            result = await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            logger.info(f"[STEP 7] ✅ SIP call connected! Result: {result}")
            await _log("info", f"Call connected to {phone_number}")
        except Exception as e:
            logger.error(f"[STEP 7] ❌ SIP dial-out FAILED: {e}", exc_info=True)
            await _log("error", f"Failed to place outbound call to {phone_number}: {e}", str(e))
            ctx.shutdown()
            return

    # ── STEP 8: Build AI session ─────────────────────────────────────
    logger.info("[STEP 8] Building AI session (Gemini Live)...")
    is_realtime = False
    try:
        session, is_realtime = _build_session(
            tools=tool_methods,
            voice=voice,
            model=model,
        )
        logger.info(f"[STEP 8] ✅ AI session built successfully (realtime={is_realtime})")
    except Exception as exc:
        logger.error(f"[STEP 8] ❌ Failed to build AI session: {exc}")
        await _log("error", f"AI session build failed: {exc}")
        return

    # ── STEP 9: Start session ────────────────────────────────────────
    logger.info("[STEP 9] Starting agent session in room...")
    try:
        my_agent = OutboundAgent(
            instructions=system_prompt,
            tools=list(fnc_ctx.function_tools.values()),
        )
        
        # Kickstart the LLM so it speaks first in its own voice
        kickstart_msg = f"The call has just connected. Introduce yourself immediately by asking 'Hi, am I speaking with {lead_name}?'. Do not say anything else before that." if lead_name and lead_name != "there" else "The call has connected, please introduce yourself."
        my_agent.chat_ctx.append(text=kickstart_msg, role="user")
        logger.info(f"[STEP 9] Added kickstart message to context: {kickstart_msg}")

        await session.start(
            room=ctx.room,
            agent=my_agent,
            room_input_options=RoomInputOptions(
                close_on_disconnect=True,
            ),
        )
        logger.info("[STEP 9] ✅ Agent session started in room")
    except Exception as exc:
        logger.error(f"[STEP 9] ❌ Failed to start session: {exc}", exc_info=True)
        await _log("error", f"Session start failed: {exc}")
        return

    logger.info("=" * 60)
    logger.info("ENTRYPOINT COMPLETE — agent is now live in room")
    logger.info("=" * 60)


if __name__ == "__main__":
    # Load DB settings into env before agent starts
    load_db_settings_to_env()
    init_db()

    # Resolve LiveKit credentials with fallback variable names
    lk_url = (
        os.getenv("LIVEKIT_URL")
        or os.getenv("LIVEKIT_HTTP_URL", "").replace("https://", "wss://").replace("http://", "ws://")
    )
    lk_key = os.getenv("LIVEKIT_API_KEY") or os.getenv("LIVEKIT_HTTP_API_KEY") or os.getenv("API_KEY", "")
    lk_secret = os.getenv("LIVEKIT_API_SECRET") or os.getenv("LIVEKIT_HTTP_API_SECRET") or os.getenv("API_SECRET_KEY", "")

    if not lk_url or not lk_key or not lk_secret:
        logger.error(
            "Missing LiveKit credentials. Set LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET.\n"
            f"  LIVEKIT_URL={lk_url!r}\n"
            f"  LIVEKIT_API_KEY={lk_key!r}\n"
            f"  LIVEKIT_API_SECRET={'***' if lk_secret else 'MISSING'}"
        )
        exit(1)

    logger.info(f"LiveKit worker connecting to {lk_url} with key={lk_key}")

    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
            ws_url=lk_url,
            api_key=lk_key,
            api_secret=lk_secret,
        )
    )
