# Telephony

voice-agent has three call modes. Browser calls and simulated calls work immediately after installation. Real phone calls (PSTN) go through **your own Asterisk server and SIP trunk**. They are switched off by default and are **integration-ready, not production-verified**.

> Real calls to Indian numbers are regulated by TRAI and may incur charges from your carrier. Read [COMPLIANCE.md](COMPLIANCE.md) before enabling them. **Never use voice-agent to bypass DND preferences, caller-ID rules, number-series requirements or consent.**

## Contents

- [Call modes](#call-modes)
- [The simulator](#the-simulator)
- [Asterisk ARI adapter](#asterisk-ari-adapter)
- [Example Asterisk configuration](#example-asterisk-configuration)
- [Enabling real calls](#enabling-real-calls)
- [Status and what remains to verify](#status-and-what-remains-to-verify)

## Call modes

| Mode | What happens | Available |
|---|---|---|
| `WEBRTC` | A live voice conversation in the browser: the dashboard voice playground, or a customer "talk to us" link. | Always |
| `SIMULATION` | A text-only simulated call. No network or phone is touched. Dial outcomes and the callee are simulated, and the agent, model, tools and knowledge are real. | Always |
| `PSTN` | A real outbound phone call through Asterisk ARI. Audio runs through the same voice pipeline as browser calls. | Only after [enabling](#enabling-real-calls) |

The mode is stored on every call and campaign, and is shown in the dashboard. Campaigns are created in `SIMULATION` mode by default. Workspace imports always produce simulation drafts. `GET /api/workspaces/{id}/telephony/status` reports which modes are available and which PSTN prerequisites are missing.

## The simulator

`engine/providers/telephony/simulator.py`

**Dial outcomes.** `SimulatedTelephonyProvider.dial()` moves a call through `dialing → ringing` and then decides the outcome from a deterministic roll on a seed. Campaigns use the job's `dedupe_key`, so re-running the same attempt gives the same result. The rates come from the scenario:

| Scenario field | Default | Outcome |
|---|---|---|
| `answer_rate` | 0.75 | `answered`: the conversation runs |
| `busy_rate` | 0.05 | `busy` |
| `fail_rate` | 0.05 | `failed` ("simulated network failure") |
| remainder | 0.15 | `no_answer` |

The rates must each be between 0 and 1, and together must not exceed 1. Unanswered simulated calls are retried by campaigns like real ones.

**Callee personas.** When a call is answered, the callee is either:

* **scripted** (`ScriptedCallee`): fixed lines for one of six personas:

  | Persona | Behaviour |
  |---|---|
  | `interested` | Asks about the offer and cost, and agrees to be contacted |
  | `not_interested` | Politely declines |
  | `callback` | Busy; asks for a callback tomorrow around six |
  | `questions` | Asks what you offer and about support |
  | `wrong_number` | "There's nobody called … here" |
  | `opt_out` | "Please don't call me again. Remove my number from your list." |

* **LLM role-play** (`LLMCallee`): the local model plays a person with the persona's attitude, for up to 6 replies.

Campaigns pick a persona per contact from weighted `persona_weights`. The default is interested 3, questions 2, callback 2, not_interested 2, wrong_number 1, opt_out 1. A simulated conversation runs for at most 12 callee turns. Its duration is estimated from the word count.

**Simulated inbound calls.** `POST /api/workspaces/{id}/telephony/simulate-inbound` (editor) runs an always-answered inbound call from a given number with a chosen persona. It is linked to an existing contact if the number matches one.

**What simulation never does.** It never places a network call. It never adds a real number to the suppression list, creates a lead, or changes a real contact's status. Callback requests from simulations are stored flagged `is_simulated`. See [ARCHITECTURE.md](ARCHITECTURE.md#call-modes).

## Asterisk ARI adapter

`engine/providers/telephony/asterisk_ari.py` (call control) and `engine/providers/telephony/rtp.py` (media).

voice-agent never talks to a carrier directly. **Asterisk** connects to your SIP trunk provider, and voice-agent controls Asterisk through the Asterisk REST Interface (ARI). The adapter contains no provider-specific endpoints or credentials.

### ARI resources used

The adapter uses only documented ARI resources:

| Resource | Used for |
|---|---|
| `GET /ari/asterisk/info` | Health check (reports the Asterisk version) |
| `POST /ari/channels` | Originate an outbound call directly into the Stasis app (`endpoint`, `app`, `appArgs=outbound`, `channelId=va-<uuid>`, `timeout`, optional `callerId`) |
| `POST /ari/channels/{id}/answer` | Answer an inbound channel (adapter support only; see below) |
| `DELETE /ari/channels/{id}` | Hang up |
| `POST /ari/channels/externalMedia` | Create an RTP media channel to and from voice-agent (`format=slin16`, `encapsulation=rtp`, `transport=udp`, `direction=both`) |
| `POST /ari/bridges`, `POST /ari/bridges/{id}/addChannel`, `DELETE /ari/bridges/{id}` | A mixing bridge that joins the phone channel and the media channel |
| WebSocket `/ari/events?app=…&api_key=user:pass` | `StasisStart` and `ChannelDestroyed` events (reconnects with exponential backoff up to 30 s) |

### Outbound call flow

1. The scheduler runs `run_pstn_call` (`backend/app/calls/pstn.py`) for an eligible campaign contact. It checks again that real telephony is enabled and that the workspace's PSTN attestations are complete.
2. The adapter originates `endpoint_template.format(number=…)` (default `PJSIP/{number}@trunk`). The destination must match `^\+?[0-9]{6,16}$`.
3. It waits for `StasisStart` (answered) or `ChannelDestroyed` (not answered). The Q.850 cause is mapped to a status: 17 → busy, 18/19 → no answer, 1/21/34/38 → failed. A ring timeout is treated as no answer and the channel is hung up.
4. When answered, it opens a local UDP RTP socket from the configured port range, creates a mixing bridge and an `externalMedia` channel pointing at that socket, and adds both channels to the bridge. If Asterisk reports `UNICASTRTP_LOCAL_ADDRESS`/`UNICASTRTP_LOCAL_PORT`, voice-agent sends there. Otherwise it replies to the first peer that sends it media (symmetric RTP). Packets from other addresses or SSRCs are dropped.
5. `RtpMedia` exchanges 20 ms packets (320 samples of 16 kHz, 16-bit linear PCM, payload type taken from the incoming stream). It feeds the `VoicePipeline`, so VAD, whisper.cpp, the LLM and Piper work exactly as for browser calls.
6. When either side hangs up, the media channel and bridge are torn down, the call is analysed, and the transcript and outcome are stored.

### Inbound calls

The adapter can handle inbound calls: it answers channels that enter the Stasis app without being originated by voice-agent, attaches media, and calls a registered `on_inbound` handler. **The backend does not currently register an inbound handler**, and it only opens the ARI event socket when it places an outbound call. Real inbound calling is therefore **not wired up** in this version. Use simulated inbound calls to test inbound agents. The dialplan snippet below shows how inbound calls would be routed once the handler exists.

### Configuration fields

Set these under **Settings → Telephony → Asterisk (SIP) connection** (instance admins):

| Field | Default | Notes |
|---|---|---|
| `base_url` | `http://127.0.0.1:8088` | ARI HTTP base. It must resolve to a loopback or private address unless `VA_ALLOW_REMOTE_PROVIDER_HOSTS=true`. |
| `username` / password | — | ARI user. The password is stored encrypted (Fernet, key derived from the install secret) and is never shown again. |
| `app` | `voice-agent` | Stasis application name (letters, digits, `-`, `_`). |
| `endpoint_template` | `PJSIP/{number}@trunk` | Must contain `{number}`. |
| `caller_id` | empty | Caller ID for outbound calls. It must be a number you are registered to use. |
| `rtp_bind_host` | `127.0.0.1` | Local address voice-agent listens on for RTP. |
| `rtp_advertise_host` | `127.0.0.1` | Address Asterisk sends RTP to (`external_host`). |
| `rtp_port_start` / `rtp_port_end` | 40000 / 40100 | UDP ports for media (two per call, allocated round-robin). |
| `byte_order` | `big` | slin16 sample byte order in RTP. RFC 3551 specifies network order (big-endian) for L16, but some deployments differ. |

## Example Asterisk configuration

These are **skeletons with placeholders**, for orientation only. They have not been tested against a live carrier. Your SIP provider's documentation takes precedence. Values in `<ANGLE_BRACKETS>` must be replaced.

**`/etc/asterisk/http.conf`**: ARI runs on Asterisk's built-in HTTP server. Keep it on loopback if Asterisk runs on the same Mac.

```ini
[general]
enabled = yes
bindaddr = 127.0.0.1
bindport = 8088
```

**`/etc/asterisk/ari.conf`**

```ini
[general]
enabled = yes
pretty = no

[<ARI_USERNAME>]
type = user
read_only = no
password = <LONG_RANDOM_ARI_PASSWORD>
password_format = plain
```

**`/etc/asterisk/pjsip.conf`**: a registration-based trunk. The endpoint is named `trunk` to match the default `endpoint_template`.

```ini
[transport-udp]
type = transport
protocol = udp
bind = 0.0.0.0:5060
; Behind NAT, also set:
; external_media_address = <PUBLIC_IP>
; external_signaling_address = <PUBLIC_IP>
; local_net = 192.168.0.0/16

[trunk-auth]
type = auth
auth_type = userpass
username = <SIP_USERNAME>
password = <SIP_PASSWORD>

[trunk]
type = aor
contact = sip:<PROVIDER_SIP_HOST>:5060

[trunk]
type = endpoint
transport = transport-udp
context = from-trunk
disallow = all
allow = alaw,ulaw
aors = trunk
outbound_auth = trunk-auth
from_user = <SIP_USERNAME>
from_domain = <PROVIDER_SIP_HOST>
direct_media = no
rtp_symmetric = yes
force_rport = yes
rewrite_contact = yes
dtmf_mode = rfc4733

[trunk-registration]
type = registration
transport = transport-udp
outbound_auth = trunk-auth
server_uri = sip:<PROVIDER_SIP_HOST>
client_uri = sip:<SIP_USERNAME>@<PROVIDER_SIP_HOST>

[trunk-identify]
type = identify
endpoint = trunk
match = <PROVIDER_SIGNALLING_IP_OR_CIDR>
```

**`/etc/asterisk/extensions.conf`**: outbound calls are originated straight into the Stasis app, so they need no dialplan. Inbound calls from the trunk would be handed to the app like this (see [Inbound calls](#inbound-calls) for the current limitation):

```ini
[from-trunk]
exten => _X.,1,NoOp(Inbound call for ${EXTEN} from ${CALLERID(num)})
 same => n,Stasis(voice-agent,inbound)
 same => n,Hangup()
```

Also set an RTP port range for the SIP side in `rtp.conf` and open it, and the SIP port, only toward your provider. Protect the Asterisk server against toll fraud: use strong credentials, restrict `identify` matches, and do not expose ARI publicly.

## Enabling real calls

All of the following are required. Each step is checked again in code before every call.

1. **Installation switch.** Set `VA_REAL_TELEPHONY_ENABLED=true` in `.env` and restart (`./scripts/stop.sh && ./scripts/start.sh`). Until this is set, saving an enabled Asterisk provider is refused (`pstn_disabled`). Creating a PSTN campaign is refused, and running PSTN campaigns are paused.
2. **Provider.** As an instance admin, open **Settings → Telephony**, choose the Asterisk connection, fill in the fields above, enable it and save. Check that the health status shows `Asterisk <version>`.
3. **Workspace attestations.** As a workspace admin, open **Compliance → Real phone calls** and record:
   * authorisation for real calling (`pstn_authorized`),
   * your telemarketer or entity registration ID,
   * your registered number series (140xx for promotional, 160xx for service/transactional),
   * that automated (A2P) calling has been declared to your provider.
4. **Compliance data.** Import fresh DND scrub results, and make sure consent records exist for the campaign purpose.
5. **Campaign.** Create a campaign with mode `PSTN`. Starting it is refused while any attestation is missing (`pstn_prerequisites`). Every call still goes through the eligibility engine.

Start with a single test contact that is a number you own, `max_concurrent_calls = 1` and `max_attempts = 1`.

## Status and what remains to verify

**Status: integration-ready, not production-verified.** The ARI adapter, RTP codec and PSTN call path are exercised only by automated tests against a **mocked ARI server** (`tests/unit/test_provider_adapters.py`). They have **not** been run against a real Asterisk server or a live carrier from this repository.

Before relying on real calls, someone needs to verify with an actual provider:

* **Credentials and trunk.** SIP registration or IP authentication with your provider, and the right `endpoint_template` for your dialplan and number format (E.164 with `+`, national format, or a required prefix).
* **Caller ID.** That the `caller_id` you set is your registered 140/160-series number and is passed through unchanged. Many carriers override or reject unregistered caller IDs.
* **slin16 byte order.** That audio is clean in both directions. Distorted, very loud noise usually means the byte order is wrong; switch `byte_order` between `big` and `little`.
* **NAT and RTP ports.** The SIP-side RTP range, and the `externalMedia` range between Asterisk and voice-agent (`rtp_bind_host`, `rtp_advertise_host`, `rtp_port_start/end`), when Asterisk and voice-agent are on different hosts. voice-agent's RTP socket locks onto the first peer address and SSRC it hears from and drops packets from anyone else. Even so, bind it to loopback or a private interface and firewall the range.
* **DTMF.** Not handled by voice-agent. Keypad presses are ignored.
* **Hang-up and cause mapping.** That busy, no-answer and failure causes from your carrier map to the expected statuses, and that remote hang-ups end the pipeline promptly.
* **Recording.** The PSTN path does not currently attach a call recorder, so PSTN calls are **not recorded** even when recording is enabled (the recording disclosure is still spoken). Recording phone calls also has its own consent and legal requirements; confirm them before adding it.
* **Inbound calls.** Not wired into the backend (see above).
* **Resource handling under load.** A new ARI adapter, with its own event WebSocket, is created for each PSTN call. Check how connections behave over long campaigns.
* **Latency and quality** over a real phone line (8 kHz carrier audio upsampled to slin16), including VAD thresholds and barge-in behaviour.
* **Charges.** Calls to Indian numbers may incur carrier charges, and reportedly a terminating charge for automated (A2P) calls. Confirm the tariff with your provider.

Contributions that verify the integration with a real provider are welcome. See [CONTRIBUTING.md](../CONTRIBUTING.md).
