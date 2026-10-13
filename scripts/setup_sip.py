"""Set up this project's private, local SIP PBX without a paid trunk."""

import ipaddress
import json
import secrets
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
env = dotenv_values(ROOT / ".env")
host = str(ipaddress.IPv4Address(env.get("LAN_IP", "127.0.0.1")))
private = Path.home() / ".cache/voice-desk/private/sip"
private.mkdir(parents=True, exist_ok=True)
private.chmod(0o700)
metadata_file = private / "metadata.json"
metadata = (
    json.loads(metadata_file.read_text())
    if metadata_file.exists()
    else {
        "host": host,
        "port": 15060,
        "transport": "udp",
        "extensions": {"male": "7001", "female": "7002"},
        "accounts": [
            {"username": "1001", "password": secrets.token_urlsafe(24)},
            {"username": "1002", "password": secrets.token_urlsafe(24)},
        ],
    }
)
metadata["host"] = host
key = env.get("CHANNEL_API_KEY", "")
if len(key) < 16:
    raise SystemExit("Configure CHANNEL_API_KEY before SIP setup")
files = {
    "asterisk.conf": "[directories]\nastetcdir => /etc/asterisk\nastmoddir => /usr/lib/asterisk/modules\nastvarlibdir => /var/lib/asterisk\nastdbdir => /var/lib/asterisk\nastkeydir => /var/lib/asterisk\nastdatadir => /usr/share/asterisk\nastagidir => /usr/share/asterisk/agi-bin\nastspooldir => /var/spool/asterisk\nastrundir => /var/run/asterisk\nastlogdir => /var/log/asterisk\n[options]\nverbose = 0\ndebug = 0\n",
    "modules.conf": "[modules]\nautoload=yes\nnoload=chan_iax2.so\nnoload=chan_sip.so\nnoload=res_hep.so\n",
    "logger.conf": "[logfiles]\nconsole => warning,error\n",
    "rtp.conf": "[general]\nrtpstart=20000\nrtpend=20020\n",
    "extensions.conf": f"""[assistants]
exten => 7001,1,Set(VOICE_PROFILE=male)
 same => n,Goto(voice,s,1)
exten => 7002,1,Set(VOICE_PROFILE=female)
 same => n,Goto(voice,s,1)
exten => 1002,1,Dial(PJSIP/1002,30)
 same => n,Hangup()
[voice]
exten => s,1,Answer(300)
 same => n,Set(CURLOPT(conntimeout)=3)
 same => n,Set(CURLOPT(httptimeout)=8)
 same => n,Set(CURLOPT(httpheader)=Host: 127.0.0.1)
 same => n,Set(CURLOPT(httpheader)=Content-Type: application/x-www-form-urlencoded)
 same => n,Set(CURLOPT(httpheader)=Authorization: Bearer {key})
 same => n,Set(REGISTRATION=${{CURL(http://host.docker.internal:7860/api/v1/transport-calls,source=sip&profile=${{VOICE_PROFILE}})}})
 same => n,Set(VOICE_UUID=${{JSON_DECODE(REGISTRATION,call_id)}})
 same => n,GotoIf($["${{VOICE_UUID}}" = ""]?unavailable)
 same => n,AudioSocket(${{VOICE_UUID}},host.docker.internal:9019)
 same => n,Hangup()
 same => n(unavailable),Hangup(17)
""",
}
pjsip = f"""[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_media_address={host}
external_signaling_address={host}
external_signaling_port=15060
local_net=172.16.0.0/12
[transport-tcp]
type=transport
protocol=tcp
bind=0.0.0.0:5060
external_media_address={host}
external_signaling_address={host}
external_signaling_port=15060
local_net=172.16.0.0/12
"""
for account in metadata["accounts"]:
    user = account["username"]
    password = account["password"]
    pjsip += f"""[{user}]
type=endpoint
context=assistants
disallow=all
allow=ulaw,alaw,g722
auth={user}-auth
aors={user}-aor
direct_media=no
media_address={host}
rtp_symmetric=yes
rewrite_contact=yes
force_rport=yes
dtmf_mode=rfc4733
[{user}-auth]
type=auth
auth_type=userpass
username={user}
password={password}
[{user}-aor]
type=aor
max_contacts=3
remove_existing=yes
"""
files["pjsip.conf"] = pjsip
for name, text in files.items():
    p = private / name
    p.write_text(text)
    p.chmod(0o600)
metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
metadata_file.chmod(0o600)
compose = {
    "name": "voice-desk-sip",
    "services": {
        "pbx": {
            "image": "voice-desk-asterisk:22.11.0",
            "restart": "unless-stopped",
            "ports": [
                "15060:5060/udp",
                "15060:5060/tcp",
                "20000-20020:20000-20020/udp",
            ],
            "volumes": [str(private) + ":/etc/asterisk:ro"],
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": [
                "/var/run/asterisk",
                "/var/log/asterisk",
                "/var/spool/asterisk",
                "/var/lib/asterisk",
                "/tmp",
            ],
        }
    },
}
compose_file = ROOT / ".runtime/compose-sip.yaml"
compose_file.write_text(json.dumps(compose, indent=2) + "\n")
subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"], cwd=ROOT, check=True)
print("Local SIP configured. Credentials available only to the administrator in the application.")
