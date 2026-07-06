"""
Direct SIP call test — bypasses the agent entirely.
Tests whether LiveKit can reach Vobiz and place a call.
"""
import asyncio
import os
import sys
import certifi
import time

os.environ['SSL_CERT_FILE'] = certifi.where()

from dotenv import load_dotenv
load_dotenv(".env")

from livekit import api
from livekit.protocol.sip import (
    ListSIPOutboundTrunkRequest,
    CreateSIPParticipantRequest,
    CreateSIPOutboundTrunkRequest,
    SIPOutboundTrunkInfo,
)

PHONE = sys.argv[1] if len(sys.argv) > 1 else "+917732042430"

async def main():
    url = os.getenv("LIVEKIT_URL", "wss://livekit.growthbara.com")
    key = os.getenv("LIVEKIT_API_KEY", "growthbara")
    secret = os.getenv("LIVEKIT_API_SECRET", "6lYfkzNIXsAG7fwAvI7/V/dZS8aUCPn0MgCbutEmMzw=")

    print(f"LiveKit URL:    {url}")
    print(f"API Key:        {key}")
    print(f"API Secret:     {secret[:12]}...")
    print(f"Target Phone:   {PHONE}")
    print()

    # Convert wss to https for API calls
    api_url = url.replace("wss://", "https://").replace("ws://", "http://")
    lk = api.LiveKitAPI(url=api_url, api_key=key, api_secret=secret)

    try:
        # Step 1: List existing trunks
        print("=" * 50)
        print("STEP 1: Listing SIP Outbound Trunks")
        print("=" * 50)
        resp = await lk.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
        trunks = resp.items
        print(f"Found {len(trunks)} outbound trunks:")
        for t in trunks:
            print(f"  ID:       {t.sip_trunk_id}")
            print(f"  Name:     {t.name}")
            print(f"  Address:  {t.address}")
            print(f"  Numbers:  {t.numbers}")
            print(f"  Username: {t.auth_username}")
            print(f"  Transport: {t.transport}")
            print()

        # Step 2: If no trunk, create one
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
        if not trunks:
            print("No trunks found! Creating one...")
            trunk_info = SIPOutboundTrunkInfo(
                name="Vobiz Outbound",
                address=os.getenv("VOBIZ_SIP_DOMAIN", "cbee90d8.sip.vobiz.ai"),
                auth_username=os.getenv("VOBIZ_USERNAME", "voice_agent"),
                auth_password=os.getenv("VOBIZ_PASSWORD", "Vobiz@2121995"),
                numbers=[os.getenv("VOBIZ_OUTBOUND_NUMBER", "+918071579399")],
            )
            new_trunk = await lk.sip.create_outbound_trunk(
                CreateSIPOutboundTrunkRequest(trunk=trunk_info)
            )
            trunk_id = new_trunk.sip_trunk_id
            print(f"Created trunk: {trunk_id}")
        elif not trunk_id:
            trunk_id = trunks[0].sip_trunk_id
            print(f"Using first trunk: {trunk_id}")
        else:
            # Verify trunk_id exists
            found = any(t.sip_trunk_id == trunk_id for t in trunks)
            if not found:
                print(f"WARNING: OUTBOUND_TRUNK_ID={trunk_id} not found in LiveKit!")
                print(f"Using first available trunk: {trunks[0].sip_trunk_id}")
                trunk_id = trunks[0].sip_trunk_id

        # Step 3: Create a room
        print()
        print("=" * 50)
        print("STEP 2: Creating test room")
        print("=" * 50)
        room_name = f"sip-test-{int(time.time())}"
        room = await lk.room.create_room(api.CreateRoomRequest(
            name=room_name,
            empty_timeout=120,
        ))
        print(f"Room created: {room.name} (sid={room.sid})")

        # Step 4: Try to place SIP call
        print()
        print("=" * 50)
        print(f"STEP 3: Dialing {PHONE} via trunk {trunk_id}")
        print("=" * 50)
        try:
            sip_result = await lk.sip.create_sip_participant(
                CreateSIPParticipantRequest(
                    sip_trunk_id=trunk_id,
                    sip_call_to=PHONE,
                    room_name=room_name,
                    participant_identity=f"sip_{PHONE.replace('+', '')}",
                    participant_name="Test Call",
                )
            )
            print(f"✅ SIP call initiated!")
            print(f"   Participant SID: {sip_result.participant_id if hasattr(sip_result, 'participant_id') else sip_result}")
            print(f"   Full result: {sip_result}")
        except Exception as e:
            print(f"❌ SIP call FAILED: {e}")
            print(f"   Error type: {type(e).__name__}")
            if hasattr(e, 'message'):
                print(f"   Message: {e.message}")

    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await lk.aclose()

if __name__ == "__main__":
    asyncio.run(main())
