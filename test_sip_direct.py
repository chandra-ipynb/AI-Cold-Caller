"""
Direct SIP test — bypasses LiveKit entirely.
Tests Vobiz SIP connectivity, registration, and placing a call.
Uses raw UDP sockets (no external SIP library needed).
"""
import socket
import random
import hashlib
import time
import re
import sys

# Vobiz SIP credentials
SIP_DOMAIN = "cbee90d8.sip.vobiz.ai"
SIP_USER = "voice_agent"
SIP_PASS = "Vobiz@2121995"
SIP_PORT = 5060
FROM_NUMBER = "+918071579399"
TO_NUMBER = sys.argv[1] if len(sys.argv) > 1 else "+917732042430"

LOCAL_IP = "0.0.0.0"
LOCAL_PORT = random.randint(5100, 5999)

def get_local_ip():
    """Get the machine's outbound IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def generate_call_id():
    return f"{random.randint(100000, 999999)}@{get_local_ip()}"

def generate_branch():
    return f"z9hG4bK{random.randint(100000000, 999999999)}"

def generate_tag():
    return f"{random.randint(10000000, 99999999)}"

def md5(s):
    return hashlib.md5(s.encode()).hexdigest()

def compute_digest_response(username, realm, password, method, uri, nonce):
    """Compute SIP digest authentication response."""
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    response = md5(f"{ha1}:{nonce}:{ha2}")
    return response

def send_and_receive(sock, message, server_addr, timeout=5):
    """Send SIP message and receive response."""
    print(f"\n{'='*60}")
    print(f">>> SENDING to {server_addr}")
    print(f"{'='*60}")
    # Print first 3 lines for brevity
    lines = message.strip().split("\r\n")
    for line in lines[:5]:
        print(f"  {line}")
    if len(lines) > 5:
        print(f"  ... ({len(lines)-5} more lines)")
    
    sock.sendto(message.encode(), server_addr)
    
    sock.settimeout(timeout)
    try:
        data, addr = sock.recvfrom(4096)
        response = data.decode(errors='replace')
        print(f"\n{'='*60}")
        print(f"<<< RECEIVED from {addr}")
        print(f"{'='*60}")
        resp_lines = response.strip().split("\r\n")
        for line in resp_lines[:10]:
            print(f"  {line}")
        if len(resp_lines) > 10:
            print(f"  ... ({len(resp_lines)-10} more lines)")
        return response
    except socket.timeout:
        print(f"\n  ⏰ TIMEOUT — no response in {timeout}s")
        return None

def main():
    local_ip = get_local_ip()
    call_id = generate_call_id()
    branch = generate_branch()
    tag = generate_tag()
    
    # Resolve SIP server
    try:
        server_ip = socket.gethostbyname(SIP_DOMAIN)
    except socket.gaierror:
        print(f"❌ Cannot resolve {SIP_DOMAIN}")
        return
    
    server_addr = (server_ip, SIP_PORT)
    
    print(f"🔧 SIP Direct Test")
    print(f"   Local:    {local_ip}:{LOCAL_PORT}")
    print(f"   Server:   {SIP_DOMAIN} ({server_ip}:{SIP_PORT})")
    print(f"   User:     {SIP_USER}")
    print(f"   From:     {FROM_NUMBER}")
    print(f"   To:       {TO_NUMBER}")
    print()
    
    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", LOCAL_PORT))
    
    try:
        # ── TEST 1: SIP OPTIONS (ping) ──────────────────────────────
        print("━" * 60)
        print("TEST 1: SIP OPTIONS (ping to Vobiz)")
        print("━" * 60)
        
        options_msg = (
            f"OPTIONS sip:{SIP_DOMAIN}:{SIP_PORT} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={branch}\r\n"
            f"From: <sip:{SIP_USER}@{SIP_DOMAIN}>;tag={tag}\r\n"
            f"To: <sip:{SIP_DOMAIN}:{SIP_PORT}>\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: 1 OPTIONS\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        
        resp = send_and_receive(sock, options_msg, server_addr)
        if resp:
            status_line = resp.split("\r\n")[0]
            if "200" in status_line:
                print(f"\n  ✅ Vobiz SIP server is ALIVE: {status_line}")
            elif "401" in status_line or "407" in status_line:
                print(f"\n  ✅ Vobiz responded (needs auth): {status_line}")
            else:
                print(f"\n  ⚠️ Vobiz responded: {status_line}")
        else:
            print(f"\n  ❌ No response from Vobiz — SIP server unreachable")
            return
        
        # ── TEST 2: SIP REGISTER ────────────────────────────────────
        print()
        print("━" * 60)
        print("TEST 2: SIP REGISTER (authenticate with Vobiz)")
        print("━" * 60)
        
        branch2 = generate_branch()
        call_id2 = generate_call_id()
        
        register_msg = (
            f"REGISTER sip:{SIP_DOMAIN} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={branch2}\r\n"
            f"From: <sip:{SIP_USER}@{SIP_DOMAIN}>;tag={tag}\r\n"
            f"To: <sip:{SIP_USER}@{SIP_DOMAIN}>\r\n"
            f"Call-ID: {call_id2}\r\n"
            f"CSeq: 1 REGISTER\r\n"
            f"Contact: <sip:{SIP_USER}@{local_ip}:{LOCAL_PORT}>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Expires: 3600\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        
        resp = send_and_receive(sock, register_msg, server_addr)
        if resp and ("401" in resp.split("\r\n")[0] or "407" in resp.split("\r\n")[0]):
            # Extract nonce and realm for digest auth
            nonce_match = re.search(r'nonce="([^"]+)"', resp)
            realm_match = re.search(r'realm="([^"]+)"', resp)
            
            if nonce_match and realm_match:
                nonce = nonce_match.group(1)
                realm = realm_match.group(1)
                print(f"\n  Got challenge: realm={realm}, nonce={nonce[:20]}...")
                
                # Compute digest response
                digest_resp = compute_digest_response(
                    SIP_USER, realm, SIP_PASS, "REGISTER", f"sip:{SIP_DOMAIN}", nonce
                )
                
                branch3 = generate_branch()
                auth_register = (
                    f"REGISTER sip:{SIP_DOMAIN} SIP/2.0\r\n"
                    f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={branch3}\r\n"
                    f"From: <sip:{SIP_USER}@{SIP_DOMAIN}>;tag={tag}\r\n"
                    f"To: <sip:{SIP_USER}@{SIP_DOMAIN}>\r\n"
                    f"Call-ID: {call_id2}\r\n"
                    f"CSeq: 2 REGISTER\r\n"
                    f"Contact: <sip:{SIP_USER}@{local_ip}:{LOCAL_PORT}>\r\n"
                    f"Authorization: Digest username=\"{SIP_USER}\", realm=\"{realm}\", "
                    f"nonce=\"{nonce}\", uri=\"sip:{SIP_DOMAIN}\", "
                    f"response=\"{digest_resp}\", algorithm=MD5\r\n"
                    f"Max-Forwards: 70\r\n"
                    f"Expires: 3600\r\n"
                    f"Content-Length: 0\r\n"
                    f"\r\n"
                )
                
                resp2 = send_and_receive(sock, auth_register, server_addr)
                if resp2:
                    status = resp2.split("\r\n")[0]
                    if "200" in status:
                        print(f"\n  ✅ REGISTERED successfully: {status}")
                    else:
                        print(f"\n  ❌ Registration failed: {status}")
                        return
            else:
                print(f"\n  ❌ Could not extract auth challenge")
                return
        elif resp and "200" in resp.split("\r\n")[0]:
            print(f"\n  ✅ Registered (no auth needed)")
        else:
            print(f"\n  ❌ Registration failed")
            return
        
        # ── TEST 3: SIP INVITE (place call) ─────────────────────────
        print()
        print("━" * 60)
        print(f"TEST 3: SIP INVITE (calling {TO_NUMBER})")
        print("━" * 60)
        
        branch4 = generate_branch()
        call_id3 = generate_call_id()
        tag2 = generate_tag()
        
        # Simple SDP for audio
        sdp_body = (
            f"v=0\r\n"
            f"o=- {random.randint(1000,9999)} 1 IN IP4 {local_ip}\r\n"
            f"s=Test Call\r\n"
            f"c=IN IP4 {local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {LOCAL_PORT + 2} RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=fmtp:101 0-16\r\n"
            f"a=sendrecv\r\n"
        )
        
        invite_msg = (
            f"INVITE sip:{TO_NUMBER}@{SIP_DOMAIN} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={branch4}\r\n"
            f"From: <sip:{FROM_NUMBER}@{SIP_DOMAIN}>;tag={tag2}\r\n"
            f"To: <sip:{TO_NUMBER}@{SIP_DOMAIN}>\r\n"
            f"Call-ID: {call_id3}\r\n"
            f"CSeq: 1 INVITE\r\n"
            f"Contact: <sip:{SIP_USER}@{local_ip}:{LOCAL_PORT}>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {len(sdp_body)}\r\n"
            f"\r\n"
            f"{sdp_body}"
        )
        
        resp = send_and_receive(sock, invite_msg, server_addr, timeout=10)
        if resp:
            status = resp.split("\r\n")[0]
            if "100" in status or "180" in status or "183" in status:
                print(f"\n  ✅ Call is RINGING: {status}")
                print(f"  📞 Check your phone +917732042430!")
                
                # Wait for more responses (180 Ringing, 200 OK)
                print(f"\n  Waiting for answer/further responses...")
                for i in range(5):
                    try:
                        sock.settimeout(5)
                        data, addr = sock.recvfrom(4096)
                        next_resp = data.decode(errors='replace')
                        next_status = next_resp.split("\r\n")[0]
                        print(f"  <<< {next_status}")
                        if "200" in next_status:
                            print(f"\n  ✅ Call ANSWERED!")
                            # Send ACK
                            break
                        elif "486" in next_status or "603" in next_status:
                            print(f"\n  📵 Call REJECTED/BUSY")
                            break
                    except socket.timeout:
                        continue
                        
            elif "401" in status or "407" in status:
                print(f"\n  ⚠️ INVITE needs auth: {status}")
                # Try with auth
                nonce_match = re.search(r'nonce="([^"]+)"', resp)
                realm_match = re.search(r'realm="([^"]+)"', resp)
                if nonce_match and realm_match:
                    nonce = nonce_match.group(1)
                    realm = realm_match.group(1)
                    digest_resp = compute_digest_response(
                        SIP_USER, realm, SIP_PASS, "INVITE",
                        f"sip:{TO_NUMBER}@{SIP_DOMAIN}", nonce
                    )
                    
                    branch5 = generate_branch()
                    # Send ACK for the 401 first
                    ack_msg = (
                        f"ACK sip:{TO_NUMBER}@{SIP_DOMAIN} SIP/2.0\r\n"
                        f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={branch4}\r\n"
                        f"From: <sip:{FROM_NUMBER}@{SIP_DOMAIN}>;tag={tag2}\r\n"
                        f"To: <sip:{TO_NUMBER}@{SIP_DOMAIN}>\r\n"
                        f"Call-ID: {call_id3}\r\n"
                        f"CSeq: 1 ACK\r\n"
                        f"Max-Forwards: 70\r\n"
                        f"Content-Length: 0\r\n"
                        f"\r\n"
                    )
                    sock.sendto(ack_msg.encode(), server_addr)
                    
                    # Re-INVITE with auth
                    auth_invite = (
                        f"INVITE sip:{TO_NUMBER}@{SIP_DOMAIN} SIP/2.0\r\n"
                        f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={branch5}\r\n"
                        f"From: <sip:{FROM_NUMBER}@{SIP_DOMAIN}>;tag={tag2}\r\n"
                        f"To: <sip:{TO_NUMBER}@{SIP_DOMAIN}>\r\n"
                        f"Call-ID: {call_id3}\r\n"
                        f"CSeq: 2 INVITE\r\n"
                        f"Contact: <sip:{SIP_USER}@{local_ip}:{LOCAL_PORT}>\r\n"
                        f"Proxy-Authorization: Digest username=\"{SIP_USER}\", realm=\"{realm}\", "
                        f"nonce=\"{nonce}\", uri=\"sip:{TO_NUMBER}@{SIP_DOMAIN}\", "
                        f"response=\"{digest_resp}\", algorithm=MD5\r\n"
                        f"Max-Forwards: 70\r\n"
                        f"Content-Type: application/sdp\r\n"
                        f"Content-Length: {len(sdp_body)}\r\n"
                        f"\r\n"
                        f"{sdp_body}"
                    )
                    
                    resp2 = send_and_receive(sock, auth_invite, server_addr, timeout=15)
                    if resp2:
                        status2 = resp2.split("\r\n")[0]
                        if "100" in status2 or "180" in status2 or "183" in status2:
                            print(f"\n  ✅ Call is RINGING: {status2}")
                            print(f"  📞 Check your phone {TO_NUMBER}!")
                            for i in range(6):
                                try:
                                    sock.settimeout(5)
                                    data, addr = sock.recvfrom(4096)
                                    next_resp = data.decode(errors='replace')
                                    next_status = next_resp.split("\r\n")[0]
                                    print(f"  <<< {next_status}")
                                except socket.timeout:
                                    continue
                        else:
                            print(f"\n  Result: {status2}")
            else:
                print(f"\n  Response: {status}")
        else:
            print(f"\n  ❌ No response to INVITE — Vobiz didn't respond")
        
        # Send BYE to clean up
        time.sleep(1)
        bye_msg = (
            f"BYE sip:{TO_NUMBER}@{SIP_DOMAIN} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{LOCAL_PORT};branch={generate_branch()}\r\n"
            f"From: <sip:{FROM_NUMBER}@{SIP_DOMAIN}>;tag={tag2}\r\n"
            f"To: <sip:{TO_NUMBER}@{SIP_DOMAIN}>\r\n"
            f"Call-ID: {call_id3}\r\n"
            f"CSeq: 3 BYE\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        sock.sendto(bye_msg.encode(), server_addr)
        print("\n  Sent BYE to clean up")
        
    finally:
        sock.close()
    
    print()
    print("=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    main()
