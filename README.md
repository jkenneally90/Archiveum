# Archiveum

<p align="center">
  <img src="docs/assets/archiveum-logo.png" alt="Archiveum Logo" width="200">
</p>

<p align="center">
  <strong>Your Private AI Archive Companion</strong><br>
  <em>Offline-first. Self-contained. Infinitely customizable.</em>
</p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#installation">Installation</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#documentation">Documentation</a>
</p>

---

## 🌟 What is Archiveum?

**Archiveum** is a fully offline, self-hosted AI companion that brings your personal documents to life through intelligent conversation. Built for privacy-conscious users who refuse to compromise, Archiveum runs entirely on your hardware—no cloud dependencies, no data leaks, no subscriptions.

Whether you're on a high-end Windows 11 workstation or a power-efficient Jetson Orin Nano, Archiveum adapts to your environment while delivering a premium AI experience.

<p align="center">
  <img src="docs/assets/archiveum-demo.gif" alt="Archiveum Demo" width="800">
</p>

---

## ✨ Key Features

### 🔒 Offline-First Architecture
- **Zero cloud dependencies** — Everything runs locally
- **Complete privacy** — Your documents never leave your machine
- **Works without internet** — Perfect for air-gapped environments
- **Local embeddings & LLM inference** via Ollama - Piper for voice synthesis - Whisper for speech-to-text
  - **Windows**: Default STT model is `base.en` (better accuracy for desktop environments)
  - **Linux/Jetson**: Default STT model is `tiny.en` (optimized for lower resource usage)
- RAG for document retrieval - Vector database for semantic search - Web UI for interaction - REST API for integration - CLI for automation - Docker support for containerization - GPU acceleration support - Multi-language support - Multi-modal support (text, audio, video) - Real-time processing - Real-time transcription...

### 📦 Self-Contained Platform
- **Single repository** — Clone and run
- **Integrated voice stack** — STT (Whisper) and TTS (Piper) included
- **Built-in vector database** — No external database setup required
- **Auto-configuration** — Sensible defaults that just work

### 🚀 Guided Installer
- **One-command setup** for both Windows and Linux
- **Smart dependency detection** — Automatically installs what's missing
- **Hardware-optimized presets** — Jetson vs. desktop configurations
- **Systemd service integration** — Auto-boot on startup

```bash
# Ubuntu/Jetson
chmod +x install_archiveum.sh && ./install_archiveum.sh

# Windows (with voice enabled)
PowerShell -ExecutionPolicy Bypass -File .\install_archiveum.ps1 -EnableVoice
```

### 🎭 Persona System
Create distinct AI personalities, each with:

- **Custom LLM models** — Use different models per persona (creative vs. analytical)
- **Unique voice synthesis** — Different Piper voices for each personality
- **Dynamic avatars** — Emotional, contextual, and temporal avatar switching
- **Custom system prompts** — Fully customizable behavior and knowledge domains

**Built-in personas:** Nova (default), Researcher, Storyteller, Gentle Companion

### 🎨 Dynamic Avatar System
The most advanced avatar system in local AI:

- **19 emotional/contextual tags** — Happy, sad, curious, space, technology, morning, evening, and more
- **Auto-switching** — Avatars change based on conversation mood and topics
- **Video support** — Animated MP4/WebM avatars with smooth looping
- **LLM awareness** — Descriptions injected into prompts for natural visual references
- **Click-to-expand** — Full-screen lightbox viewer with video controls

<p align="center">
  <img src="docs/assets/avatar-switching.gif" alt="Dynamic Avatars" width="400">
</p>

### 🧠 Intelligent Memory Layers
Archiveum remembers what matters:

- **Short-term memory** — Last 20 messages for immediate context
- **Long-term memory** — Auto-summarized older conversations
- **Vector retrieval** — Semantic search across your entire document archive
- **Context restoration** — Pick up conversations exactly where you left off

### 🗣️ Voice Integration
Natural spoken interaction:

- **Wake phrases** — "Voice activated" / "Voice deactivated"
- **Voice activity visualization** — Avatar glows red (speaking) or blue (listening)
- **Smart muting** — Mic disabled during TTS to prevent self-hearing
- **Offline speech recognition** — Whisper runs locally
- **High-quality TTS** — Piper voice synthesis

### 📚 The Archive
Your personal knowledge base:

- **Multi-format support** — PDF, DOCX, TXT, Markdown, and more
- **Automatic chunking** — Intelligent document segmentation
- **Semantic indexing** — Meaning-based retrieval, not just keyword matching
- **Category organization** — Organize documents into "shelves"
- **Ingestion monitoring** — Track what worked and what didn't

### 💬 Chat History & CRUD
Complete conversation management:

- **Persistent chat threads** — Every conversation saved automatically
- **Full CRUD operations** — Create, read, update, delete chats
- **Batch operations** — Select and delete multiple conversations
- **JSON export** — Download conversations for backup or sharing
- **Searchable history** — Find past discussions instantly

### ⚡ Smart UX Features
- **Enter to send** — Natural chat interface (Shift+Enter for new line)
- **Real-time status** — Live health monitoring dashboard
- **Responsive design** — Works beautifully on desktop, tablet, and mobile
- **Dark mode** — Automatic and manual theme switching
- **Keyboard shortcuts** — Power-user efficiency

---

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- 8GB+ RAM (16GB+ recommended)
- CUDA-capable GPU (optional but recommended)

### Installation

**Option 1: Guided Installer (Recommended)**

```bash
git clone https://github.com/yourusername/archiveum.git
cd archiveum

# Linux/macOS
chmod +x install_archiveum.sh && ./install_archiveum.sh

# Windows (with voice enabled)
PowerShell -ExecutionPolicy Bypass -File .\install_archiveum.ps1 -EnableVoice
```

**Option 2: Manual Setup**

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Then open `http://localhost:8000` in your browser.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Archiveum Stack                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Web UI     │  │   Voice      │  │   Memory     │      │
│  │  (FastAPI)   │  │   System     │  │   System     │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                  │                  │            │
│  ┌──────┴──────────────────┴──────────────────┴───────┐   │
│  │              Archiveum Assistant                  │   │
│  │         (Conversation Orchestration)               │   │
│  └──────────────────────┬─────────────────────────────┘   │
│                         │                                 │
│  ┌──────────────────────┴─────────────────────────────┐    │
│  │              LLM Core (Ollama)                  │    │
│  │  • Local inference  • Model switching  • Embeddings│   │
│  └──────────────────────┬─────────────────────────────┘   │
│                         │                                 │
│  ┌──────────────────────┴─────────────────────────────┐    │
│  │              Vector Store (Local)                │    │
│  │  • Document chunks  • Semantic search  • Metadata│    │
│  └───────────────────────────────────────────────────┘    │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │    Piper     │  │   Whisper    │  │   Avatars    │      │
│  │    (TTS)     │  │    (STT)     │  │  (Media)     │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Component Breakdown

| Component | Technology | Purpose |
|-----------|------------|---------|
| Web Framework | FastAPI | HTTP API + WebSocket support |
| LLM Engine | Ollama | Local model inference |
| Embeddings | nomic-embed-text | Semantic document vectors |
| Vector DB | Local SQLite + FAISS | Efficient similarity search |
| Speech-to-Text | faster-whisper | Local voice recognition |
| Text-to-Speech | Piper | High-quality voice synthesis |
| Frontend | Vanilla JS + CSS | Zero-dependency UI |

---

## 📖 Documentation

### User Guides
- **[Dynamic Avatar Guide](DYNAMIC_AVATAR_GUIDE.md)** — Master the 19-tag avatar system
- **[Voice Setup Guide](docs/voice-setup.md)** — Configure Piper TTS and Whisper STT
- **[Persona Configuration](docs/personas.md)** — Create custom AI personalities
- **[Troubleshooting](docs/troubleshooting.md)** — Common issues and solutions

### API Documentation
- **REST API** — All endpoints documented at `/docs` when running
- **Health Endpoints**:
  - `GET /health/live` — Liveness probe
  - `GET /health/ready` — Readiness probe
  - `GET /status` — Full system diagnostics

### Configuration
Archiveum uses `archiveum_settings.json` for configuration:

```json
{
  "current_persona_id": "nova",
  "enable_voice": true,
  "voice_model": "en_US-lessac-medium",
  "llm_model": "llama2:7b",
  "embed_model": "nomic-embed-text",
  "voice_silence_timeout_seconds": 2.0
}
```

Override any setting with environment variables: `ARCHIVEUM_ENABLE_VOICE=true`

---

## 🛠️ Development

### Project Structure
```
archiveum/
├── archiveum/           # Main application
│   ├── webapp.py       # FastAPI web interface
│   ├── assistant.py    # Core conversation logic
│   ├── llm.py          # Ollama integration
│   ├── voice.py        # Voice processing
│   └── memory.py       # Memory management
├── data/               # Local data storage
│   ├── avatars/        # Persona avatars
│   ├── chats/          # Conversation history
│   └── index/          # Vector database
├── deploy/             # Deployment configs
├── docs/               # Documentation
├── scripts/            # Utility scripts
├── main.py             # Entry point
└── requirements.txt    # Dependencies
```

### Running Tests
```bash
python scripts/archiveum_self_test.py
```

---

## 🖥️ Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| Windows 11 | ✅ Fully Supported | Native Piper + Ollama |
| Ubuntu 22.04 | ✅ Fully Supported | Tested primary platform |
| Jetson Orin Nano | ✅ Optimized | ARM64, CUDA-accelerated |
| macOS | ⚠️ Experimental | Community support |

---

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

### Quick Contribution Ideas
- Additional voice models
- New persona presets
- UI translations
- Documentation improvements
- Bug reports and feature requests

---

## 📜 License

Archiveum is released under the [MIT License](LICENSE).

---

## 🙏 Acknowledgments

- **Ollama** — For making local LLMs accessible
- **Piper** — For excellent open-source TTS
- **Whisper** — For offline speech recognition
- **FastAPI** — For the wonderful web framework

---

<p align="center">
  <strong>Built with ❤️ for privacy-conscious AI enthusiasts</strong><br>
  <sub>Your data. Your hardware. Your AI.</sub>
</p>
