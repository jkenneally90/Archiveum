# Archiveum

Archiveum is a local archive companion built for both Windows 11 OS, and the Jetson Orin Nano and Ubuntu, with a web UI for file upload, grounded question answering, and optional voice interaction.

## Current status

- Local file ingestion, embeddings, and vector retrieval pipeline
- **Tiered memory system** with short-term (20 recent chats) and long-term (summarized) context
- **Persona system** with per-persona LLM models, voice models, and custom avatars
- **Dynamic Avatar System** — 19-tag emotional/contextual/temporal avatar switching based on conversation
- **Avatar Lightbox Viewer** — Click avatars for full-size view with video playback controls
- **LLM Avatar Awareness** — Persona avatars include descriptions that the LLM can reference in conversation
- **Voice activity visualization** with avatar glow effects (red=speaking, blue=listening)
- **Chat UX improvements** — Press Enter to send, Shift+Enter for new line
- **Public Mode** — Lock-down interface for public/shared deployments with session isolation
- Optional voice mode layered on top of the same archive assistant core
  - **Windows**: Default STT model is `base.en` (better accuracy for desktop environments)
  - **Linux/Jetson**: Default STT model is `tiny.en` (optimized for lower resource usage)
- Admin console for runtime checks and ingestion-error cleanup
- Jetson deployment helper with `systemd` integration

## Run the new app

```bash
python main.py
```

Then open `http://localhost:8000`.

To enable voice input/output alongside the web UI:

```bash
python main.py
```

Set `enable_voice` inside `archiveum_settings.json`, or override any setting with environment variables.

Voice timing defaults are tuned to be a little more forgiving for natural speech:

- `voice_silence_timeout_seconds`: `2.0`
- `voice_post_speech_delay_seconds`: `2.5` (increased to prevent self-hearing)

When voice mode is available, Archiveum can also respond to spoken control phrases:

- `Voice activated` starts voice mode
- `Voice deactivated` stops the current voice conversation loop
- `System shutdown` or `System shut down` asks for confirmation before powering off the current machine

Shutdown confirmation phrases:

- `yes` confirms the shutdown request
- `no` cancels it

## Health endpoints

- `GET /health/live`
- `GET /health/ready`
- `GET /status`

`/status` now includes:

- indexed document count
- indexed chunk count
- recent ingestion errors
- Ollama, Piper, and audio diagnostics

## Jetson deployment

Systemd unit:

- [deploy/archiveum.service](c:/Users/james/Documents/Archiveum/deploy/archiveum.service)

Self-test script:

- `python scripts/archiveum_self_test.py`
- `./install_archiveum.sh`
- `.\install_archiveum.ps1`

Suggested install flow on the Jetson:

```bash
cd ~/Archiveum
chmod +x install_archiveum.sh
./install_archiveum.sh
```

The installer now:

- creates or refreshes `.venv`
- installs Python requirements
- patches `archiveum_settings.json` for the detected project path
- lets you enable or disable voice mode during install
- runs the Archiveum self-test
- optionally installs and starts the `systemd` service

Windows install flow:

```powershell
cd C:\Users\james\Documents\Archiveum
PowerShell -ExecutionPolicy Bypass -File .\install_archiveum.ps1
```

Optional flags:

- `-EnableVoice`
- `-LaunchAfterInstall`
- `-Port 8010`

Windows uninstall and reset:

```powershell
cd C:\Users\james\Documents\Archiveum
PowerShell -ExecutionPolicy Bypass -File .\uninstall_archiveum.ps1
```

Optional flags:

- `-RemovePiper`
- `-RemoveOllama`
- `-KeepUploads`
- `-KeepSettings`
- `-Yes`

Manual flow is still available if you prefer:

```bash
cd ~/Archiveum
python scripts/archiveum_self_test.py
sudo cp deploy/archiveum.service /etc/systemd/system/archiveum.service
sudo systemctl daemon-reload
sudo systemctl enable archiveum.service
sudo systemctl start archiveum.service
sudo systemctl status archiveum.service
```

Jetson uninstall and reset:

```bash
cd ~/Archiveum
chmod +x uninstall_archiveum.sh
./uninstall_archiveum.sh
```

Optional flags:

- `--remove-piper`
- `--remove-ollama`
- `--keep-uploads`
- `--keep-settings`
- `--yes`

## Persona System

Each persona in Archiveum can have its own configuration:

- **Custom LLM model** - Use different Ollama models per persona (e.g., creative models for storytelling)
- **Custom voice model** - Assign different Piper TTS voices to each persona
- **Avatar images & videos** - Upload portrait/landscape images or short video clips (9:16 ratio recommended)
- **System prompt** - Fully customizable personality and behavior

Built-in personas: Nova (default), Researcher, Storyteller, Gentle Companion. Create custom personas via the `/admin/persona` page.

### Dynamic Avatars

The **Dynamic Avatar System** (see `DYNAMIC_AVATAR_GUIDE.md`) provides emotional, contextual, and temporal avatar switching:

- **19 distinct tags** across 3 categories: emotional (happy, sad, curious, etc.), contextual (space, technology, nature, battle), and temporal (morning, afternoon, evening, holiday)
- **Automatic switching** — Avatars change based on detected keywords in user messages
- **Avatar persistence** — Current avatar remains until a new emotion is detected (no revert to default between messages)
- **Upload at `/admin/persona`** — Tag media with emotional keywords and descriptions
- **LLM awareness** — Avatar descriptions are injected into the LLM prompt, allowing natural visual references (e.g., "I love these wildflowers around me!")
- **Debouncing** — Prevents rapid flickering between similar emotional states
- **Click-to-view** — Click any avatar on the home page to open a full-size lightbox with video controls

## Memory System

Archiveum implements a tiered memory system for context retention:

- **Short-term memory**: Last 20 chat messages are retained for immediate context
- **Long-term memory**: Older conversations are summarized into a memory context file
- **Auto-summarization**: Triggered after 20 chats, then every 10 new chats
- **Context restoration**: Recent chats and memory context are automatically loaded on startup and included in LLM prompts

## Avatar & Voice Activity

The home page displays the current persona's avatar in a dedicated right-hand panel:

- **9:16 portrait avatars** - Rectangular portrait-style avatars (upload in Persona settings)
- **Video support** - MP4/WebM video clips with autoplay, loop, and mute for animated avatars
- **Responsive layout** - Avatar stays alongside the chat window on tablets and desktops
- **Voice activity glow** - Avatar glows red when speaking (TTS active), blue when listening
- **Mic muting** - Microphone is muted during TTS playback to prevent the LLM from hearing itself
- **Click-to-expand** - Click any avatar to open a lightbox modal with full-size view and video playback controls
- **Avatar descriptions** — Descriptions you add during upload are shared with the LLM for visual context awareness

## Public Mode

Archiveum includes a **Public Mode** for deploying as a locked, public-facing AI assistant interface:

### Features

- **Fixed Persona**: Pre-configured persona that users cannot change
- **Session Isolation**: Each user gets a unique session with isolated chat history
- **Simplified UI**: Shows only chat interface, voice controls, and Clear Chat button
- **Admin Access**: Hidden entry point for administrators (5-click corner trigger + password)
- **Persistent Configuration**: Mode and settings persist across restarts

### Enabling Public Mode

Set in `archiveum_settings.json`:

```json
{
  "public_mode": true,
  "public_mode_persona_id": "nova",
  "admin_password_hash": "",
  "session_timeout_minutes": 30
}
```

Or set the admin password via the web UI:
1. Go to **Admin** → **Settings**
2. Enter and confirm admin password
3. Save settings

### Switching Modes

**To Admin Mode**: Click the bottom-right corner 5 times, enter admin password

**To Public Mode**: Use `/mode/switch` endpoint with admin password

### Session Isolation

Public Mode automatically:
- Creates unique sessions for each user (stored in secure cookies)
- Isolates chat history per session (no cross-session access)
- Expires sessions after inactivity timeout (default: 30 minutes)
- Clears only current session's history when "Clear Chat" is clicked

### Security

- Session IDs are cryptographically secure (32-byte random tokens)
- Backend enforces strict session boundaries
- Admin password uses PBKDF2-SHA256 with 100k iterations
- Session cookies are HttpOnly and SameSite

## Notes

- Retrieval now uses local embeddings through Ollama and cosine similarity over stored vectors.
- The default embedding model is `nomic-embed-text`; override with `ARCHIVEUM_EMBED_MODEL` if needed.
- Voice mode uses the same assistant core as the web UI, with `faster-whisper` for STT and Piper for TTS.
- On first startup, Archiveum writes `archiveum_settings.json` if it does not already exist.
- The admin console is available at `/admin` for viewing and clearing ingestion errors.
- The uninstallers remove Archiveum's own local footprint by default and only remove shared tools like Ollama or Piper when you opt in.
