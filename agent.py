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

from db import init_db, log_error, get_enabled_tools, get_agent_profile, save_transcription
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
                # Add a Google TTS for instant first greeting via session.say()
                # After the greeting, Gemini Realtime handles all audio natively
                tts_for_greeting = None
                if _google_tts:
                    for tts_kwargs in [
                        {"voice_name": "en-IN-Standard-A"},  # Indian English female
                        {"voice_name": "en-US-Standard-H"},  # fallback US English
                        {},                                   # bare defaults
                    ]:
                        try:
                            tts_for_greeting = _google_tts(**tts_kwargs)
                            logger.info(f"TTS for greeting initialized: {tts_kwargs}")
                            break
                        except Exception:
                            continue
                session = AgentSession(
                    llm=realtime_model,
                    tts=tts_for_greeting,
                )
                logger.info(f"Using Gemini Live realtime: model={model}, voice={voice}, tts={'yes' if tts_for_greeting else 'no'}")
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

    # ── STEP 5: Initialize tools ───────────────────────────────────
    logger.info("[STEP 5] Initializing tools...")
    fnc_ctx = AppointmentTools(ctx, phone_number, lead_name)
    tool_methods = fnc_ctx.build_tool_list(enabled_tools_list)
    logger.info(f"[STEP 5] ✅ {len(tool_methods)} tools loaded")

    # Pre-fetch contact history so Gemini has it in the prompt (no tool call needed)
    contact_context = ""
    if phone_number:
        try:
            history = await fnc_ctx.lookup_contact(phone=phone_number)
            if history and "No history" not in history:
                contact_context = f"\n\n━━━ CONTACT HISTORY ━━━\n{history}"
                logger.info(f"[STEP 5] Pre-fetched contact history ({len(history)} chars)")
        except Exception as exc:
            logger.warning(f"[STEP 5] Could not pre-fetch contact history: {exc}")

    # Append contact history to the system prompt
    if contact_context:
        system_prompt += contact_context

    # ── STEP 6: Build AI session ─────────────────────────────────────
    # Build BEFORE dialing so the session is warm when the call connects
    logger.info("[STEP 6] Building AI session (Gemini Live)...")
    is_realtime = False
    try:
        session, is_realtime = _build_session(
            tools=tool_methods,
            voice=voice,
            model=model,
        )
        logger.info(f"[STEP 6] ✅ AI session built (realtime={is_realtime})")
    except Exception as exc:
        logger.error(f"[STEP 6] ❌ Failed to build AI session: {exc}")
        await _log("error", f"AI session build failed: {exc}")
        return

    # ── STEP 7: Start session (before dial — warm up Gemini) ─────────
    logger.info("[STEP 7] Starting agent session in room...")
    try:
        # For Gemini Realtime, pass NO tools to prevent blocking function calls
        realtime_tools = []
        if is_realtime:
            logger.info("[STEP 7] Realtime mode: NO tools (pure conversation)")
        else:
            realtime_tools = list(fnc_ctx.function_tools.values())

        my_agent = OutboundAgent(
            instructions=system_prompt,
            tools=realtime_tools,
        )
        await session.start(
            room=ctx.room,
            agent=my_agent,
            room_input_options=RoomInputOptions(
                close_on_disconnect=True,
            ),
        )
        logger.info("[STEP 7] ✅ Agent session started — Gemini is warm")
    except Exception as exc:
        logger.error(f"[STEP 7] ❌ Failed to start session: {exc}", exc_info=True)
        await _log("error", f"Session start failed: {exc}")
        return

    # ── STEP 8: Dial out ─────────────────────────────────────────────
    should_dial = False
    if phone_number:
        user_already_here = False
        for p in ctx.room.remote_participants.values():
            if f"sip_{phone_number}" in p.identity or "sip_" in p.identity:
                user_already_here = True
                break
        if not user_already_here:
            should_dial = True

    if should_dial:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
        logger.info(f"[STEP 8] Dialing {phone_number} via trunk {trunk_id}...")

        if not trunk_id:
            logger.error("[STEP 8] ❌ OUTBOUND_TRUNK_ID not set")
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot dial out")
            return

        try:
            result = await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            logger.info(f"[STEP 8] ✅ Call connected")
            await _log("info", f"Call connected to {phone_number}")
        except Exception as e:
            logger.error(f"[STEP 8] ❌ Dial failed: {e}", exc_info=True)
            await _log("error", f"Failed to place outbound call to {phone_number}: {e}", str(e))
            ctx.shutdown()
            return

    # ── STEP 9: Instant greeting via TTS (< 1 second) ────────────────
    try:
        greeting = f"Hi, am I speaking with {lead_name}?" if lead_name and lead_name != "there" else "Hi there! Do you have a moment?"
        logger.info(f"[STEP 9] Sending instant greeting: {greeting}")
        await session.say(greeting, allow_interruptions=True)
        logger.info("[STEP 9] ✅ Greeting sent")
    except Exception as exc:
        logger.warning(f"[STEP 9] session.say() failed, trying generate_reply: {exc}")
        try:
            await session.generate_reply(
                instructions=f"Say: Hi, am I speaking with {lead_name}?"
            )
        except Exception as exc2:
            logger.warning(f"[STEP 9] generate_reply also failed: {exc2}")

    logger.info("=" * 60)
    logger.info("ENTRYPOINT COMPLETE — agent is now live in room")
    logger.info("=" * 60)

    # ── Transcription capture ────────────────────────────────────────
    import time as _time
    from datetime import datetime
    call_start_time = _time.time()
    transcript_log = []

    # Capture the greeting we already sent
    if lead_name and lead_name != "there":
        transcript_log.append({
            "role": "assistant",
            "text": f"Hi, am I speaking with {lead_name}?",
            "timestamp": datetime.now().isoformat(),
        })

    @session.on("user_speech_committed")
    def _on_user_speech(msg):
        text = ""
        try:
            text = msg.content if hasattr(msg, 'content') else str(msg)
        except Exception:
            text = str(msg)
        if text:
            transcript_log.append({
                "role": "user",
                "text": text,
                "timestamp": datetime.now().isoformat(),
            })
            logger.info(f"[TRANSCRIPT] User: {text[:100]}")

    @session.on("agent_speech_committed")
    def _on_agent_speech(msg):
        text = ""
        try:
            text = msg.content if hasattr(msg, 'content') else str(msg)
        except Exception:
            text = str(msg)
        if text:
            transcript_log.append({
                "role": "assistant",
                "text": text,
                "timestamp": datetime.now().isoformat(),
            })
            logger.info(f"[TRANSCRIPT] Agent: {text[:100]}")

    # Save transcript when room disconnects
    @ctx.room.on("disconnected")
    async def _on_disconnect():
        duration = int(_time.time() - call_start_time)
        if not transcript_log:
            logger.info("[TRANSCRIPT] No transcript to save (empty)")
            return
        try:
            call_id = ctx.job.id if hasattr(ctx.job, 'id') else ctx.room.name
            tid = await save_transcription(
                call_id=call_id,
                room_name=ctx.room.name,
                phone_number=phone_number,
                lead_name=lead_name,
                transcript=transcript_log,
                duration_seconds=duration,
            )
            logger.info(f"[TRANSCRIPT] ✅ Saved {len(transcript_log)} messages (id={tid}, duration={duration}s)")
        except Exception as exc:
            logger.error(f"[TRANSCRIPT] ❌ Failed to save: {exc}")


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
