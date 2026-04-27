# AI Voice Agent — LiveKit + Sarvam AI + Vobiz Telephony

A real-time, multilingual voice agent for **outbound sales calls**.

- **Sarvam AI** — STT (Saarika), LLM (Sarvam-30B), TTS (Bulbul). Indic-language native.
- **LiveKit Agents** — orchestration, turn detection, function tools, room management.
- **Vobiz** — Indian SIP trunk that actually rings the prospect's phone.
- Same agent code runs in **console mode** (your laptop mic) and on real **phone calls** — zero changes.

## Pipeline

```
make_call.py              LiveKit Cloud                   Vobiz
    │                          │                             │
    │  CreateRoom(metadata)    │                             │
    ├─────────────────────────►│                             │
    │                          │                             │
    │  AgentDispatch           │                             │
    ├─────────────────────────►│                             │
    │                          │                             │
    │  CreateSIPParticipant    │                             │
    ├─────────────────────────►│   SIP INVITE                │
    │                          ├────────────────────────────►│ ── PSTN ── 📞
    │                          │                             │
    │   ┌──── agent.py joins room ────┐                      │
    │   │  Sarvam STT  → LLM → TTS    │  ◄── audio ─────────│
    │   └─────────────────────────────┘                      │
```

## Files

| File | Purpose |
|---|---|
| `agent.py` | The voice agent — runs as a LiveKit worker or in console mode |
| `prompts.py` | Sales-agent system prompt (multilingual, same-language reply rule) |
| `make_call.py` | Initiates an outbound call: creates room, dispatches agent, dials prospect |
| `outbound_trunk.json` | One-time LiveKit ⇄ Vobiz trunk config |
| `requirements.txt` | Pinned dependencies |
| `.env.example` | Environment template |

---

## Quickstart A — Console mode (no phone, no servers)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install portaudio          # macOS — provides the audio backend
cp .env.example .env             # paste only SARVAM_API_KEY for this mode
python agent.py console
```

Speak through your mic, the agent replies through your speakers.

---

## Quickstart B — Real phone calls (Vobiz + LiveKit)

This is a **one-time setup** then every future call is a single command.

### Step 1 — Create a Vobiz outbound trunk

1. Sign up at https://vobiz.ai and complete KYC (required by Indian telecom regulations).
2. **Trunks → Outbound → Create Trunk**. Buy a phone number (DID).
3. Open the trunk → **Authentication & Linking** → create a credential (username + password).
4. Copy:
   - `SIP Domain` — looks like `xxxxx.sip.vobiz.ai`
   - `Username`, `Password`
   - The phone number you bought (E.164: `+91XXXXXXXXXX`)

### Step 2 — Create a LiveKit Cloud project

1. Sign up at https://cloud.livekit.io.
2. Create a project. From **Settings → Keys**, copy `URL`, `API Key`, `API Secret`.

### Step 3 — Install the LiveKit CLI

```bash
brew install livekit-cli         # macOS
# or download from https://github.com/livekit/livekit-cli/releases

lk cloud auth                    # log in once, picks up your project
```

### Step 4 — Register Vobiz with LiveKit (one-time)

Edit `outbound_trunk.json` and paste your Vobiz `SIP Domain`, `Username`, `Password`, and outbound number.

```bash
lk sip outbound create outbound_trunk.json
```

The CLI prints a `SIPTrunkID` like `ST_xxxxxxxxxxxxxxxx`. Copy it.

### Step 5 — Fill in `.env`

```bash
cp .env.example .env
```

Set:
- `SARVAM_API_KEY`
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
- `OUTBOUND_TRUNK_ID` ← paste the `ST_...` from step 4
- `AGENT_NAME=sales-agent` (default; must match what `agent.py` registers)

### Step 6 — Run the agent worker

In **terminal 1** (long-running):

```bash
python agent.py start
```

You should see logs like `registered worker  agent_name=sales-agent`.

### Step 7 — Place a call

In **terminal 2**:

```bash
python make_call.py +919876543210 --name "Krish" --lang hi-IN
```

The phone rings, the prospect answers, and Maya begins the conversation in their language.

---

## Per-call context (room metadata)

`make_call.py` packs a JSON blob into the room's metadata:

```json
{"name": "Krish", "phone": "+919876543210", "lang_hint": "hi-IN"}
```

`agent.py` reads it on join via `parse_call_context(ctx.room.metadata)` and uses it to:

- Greet the prospect by name in the opening line.
- Seed Bulbul TTS with the right `target_language_code`.
- Tag captured leads with the original phone number.

## Languages supported

`en-IN`, `hi-IN`, `ta-IN`, `te-IN`, `bn-IN`, `mr-IN`, `gu-IN`, `kn-IN`, `ml-IN`, `pa-IN`, `od-IN`.
Saarika auto-detects the spoken language regardless of the `lang_hint`.

## Switching the LLM

In `agent.py`, change `sarvam.LLM(model=...)`:

| Model | Best for |
|---|---|
| `sarvam-30b` (default) | Low-latency conversation |
| `sarvam-30b-16k` | Same speed, longer context |
| `sarvam-105b` | Higher reasoning quality |
| `sarvam-105b-32k` | Highest quality + long context |

## Troubleshooting

- **"403 / authentication failed"** on the SIP leg → Vobiz creds in `outbound_trunk.json` are wrong. Re-run `lk sip outbound update`.
- **Phone rings but agent never speaks** → agent worker isn't running, or `AGENT_NAME` mismatch between `.env` and the worker.
- **`object cannot be found`** when creating SIP participant → `OUTBOUND_TRUNK_ID` in `.env` is stale. Run `lk sip outbound list` to verify.
- **No audio one direction** → check Vobiz codec settings (PCMU/PCMA both enabled is safest).

## Security notes

- Never commit `.env` (only `.env.example`).
- The `VOBIZ_*` creds in `.env.example` are only used to fill `outbound_trunk.json` once; runtime auth is handled by LiveKit using the registered trunk.
- Use IP ACL auth in Vobiz once you deploy to a fixed server — it's stronger than username/password.
