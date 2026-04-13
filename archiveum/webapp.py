from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import asdict
from html import escape
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from archiveum.assistant import ArchiveumAssistant
from archiveum.content_library import (
    ensure_library_structure,
    upload_category_options,
    default_upload_category_options,
    custom_upload_category_options,
)
from archiveum.config import ensure_settings_file, persist_settings, load_settings
from archiveum.model_presets import get_model_preset, list_model_presets, recommended_preset_id
from archiveum.personas import get_persona, list_personas
from archiveum.speech_text import to_spoken_text
from archiveum.session_manager import get_session_manager, SessionManager
from archiveum.auth import hash_password, verify_password
from tts_piper import PiperTTS


app = FastAPI(title="Archiveum")
ensure_settings_file()
assistant = ArchiveumAssistant()
paths = assistant.paths
ensure_library_structure(paths.uploads_dir, upload_category_options(paths))
voice_runtime = None
web_reply_tts = None

# Initialize session manager for Public Mode
session_manager: SessionManager = get_session_manager(
    paths.data_dir, 
    assistant.settings.session_timeout_minutes
)


def _get_voice_runtime():
    global voice_runtime
    if voice_runtime is None:
        from archiveum.voice import ArchiveumVoiceAssistant

        voice_runtime = ArchiveumVoiceAssistant(assistant)
    return voice_runtime


@app.on_event("startup")
def _startup_voice_runtime() -> None:
    runtime = _get_voice_runtime()
    if assistant.settings.enable_voice:
        runtime.start()


@app.on_event("shutdown")
def _shutdown_voice_runtime() -> None:
    runtime = _get_voice_runtime()
    runtime.shutdown()


def _resolve_persona_voice_model(persona_id: str | None = None) -> str:
    settings = assistant.settings
    target_persona_id = (persona_id or settings.current_persona_id or "").strip()
    if target_persona_id:
        persona = get_persona(target_persona_id)
        if persona and persona.voice_model:
            return persona.voice_model
    return settings.piper_model_path


def _get_web_reply_tts(persona_id: str | None = None):
    global web_reply_tts
    settings = assistant.settings
    model_path = _resolve_persona_voice_model(persona_id)
    if web_reply_tts is None:
        web_reply_tts = PiperTTS(
            command=settings.piper_command,
            model_path=model_path,
            device=settings.piper_device,
        )
        return web_reply_tts

    current = (
        getattr(web_reply_tts, "command", None),
        getattr(web_reply_tts, "model_path", None),
        getattr(web_reply_tts, "device", None),
    )
    desired = (settings.piper_command, model_path, settings.piper_device)
    if current != desired:
        web_reply_tts = PiperTTS(
            command=settings.piper_command,
            model_path=model_path,
            device=settings.piper_device,
        )
    return web_reply_tts


def _speak_answer_async(answer: str, persona_id: str | None = None) -> None:
    if not answer:
        return
    diagnostics = assistant.diagnostics()
    piper_diag = diagnostics.get("piper", {})
    if not piper_diag.get("ok", False):
        # Debug: log why voice is not working
        print(f"[VOICE DEBUG] Piper not OK: {piper_diag.get('detail', 'unknown')}")
        print(f"[VOICE DEBUG] Current piper_model_path: {assistant.settings.piper_model_path}")
        print(f"[VOICE DEBUG] Model exists: {piper_diag.get('model_exists', 'unknown')}")
        return

    piper = _get_web_reply_tts(persona_id)

    def _run() -> None:
        # Signal TTS start to voice runtime so interrupt button works
        runtime = _get_voice_runtime()
        try:
            runtime._tts_is_speaking = True
            piper.speak(to_spoken_text(answer))
        except Exception:
            pass
        finally:
            runtime._tts_is_speaking = False

    threading.Thread(target=_run, daemon=True).start()


def _safe_persona_id(name: str) -> str:
    safe = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "custom"
    existing_ids = {p["id"] for p in list_personas()}
    candidate = safe
    suffix = 1
    while candidate in existing_ids:
        candidate = f"{safe}_{suffix}"
        suffix += 1
    return candidate


def _load_custom_personas() -> list[dict[str, str]]:
    return [p for p in list_personas() if p["id"] not in {"nova", "researcher", "storyteller", "gentle_companion"}]


def _persist_custom_personas(custom_personas: list[dict[str, str]]) -> None:
    persist_settings(assistant.paths, {"custom_personas": custom_personas})
    assistant.reload_settings()


def _render_persona_options(current_persona_id: str) -> str:
    return "".join(
        f"<option value=\"{escape(persona['id'])}\" {'selected' if persona['id'] == current_persona_id else ''}>{escape(persona['name'])}</option>"
        for persona in list_personas()
    )


def _get_available_llm_models() -> list[str]:
    """Get list of available LLM models from Ollama."""
    return assistant.ollama_manager.list_models()


def _render_llm_model_options(selected_model: str) -> str:
    """Render LLM model dropdown options."""
    models = _get_available_llm_models()
    options = ['<option value="">Use default (global setting)</option>']
    for model in models:
        model_name = model.get("name", "")
        if model_name:
            is_selected = 'selected' if model_name == selected_model else ''
            options.append(f'<option value="{escape(model_name)}" {is_selected}>{escape(model_name)}</option>')
    if selected_model and not any(m.get("name") == selected_model for m in models):
        options.append(f'<option value="{escape(selected_model)}" selected>{escape(selected_model)} (unavailable)</option>')
    return "".join(options)


def _render_voice_model_options(selected_model: str) -> str:
    """Render voice model dropdown options."""
    options = ['<option value="">Use default (global setting)</option>']
    for candidate in _piper_model_candidates():
        candidate_str = str(candidate)
        label = candidate.name
        if candidate.exists():
            label += " (bundled)"
        is_selected = 'selected' if candidate_str == selected_model else ''
        options.append(f'<option value="{escape(candidate_str)}" {is_selected}>{escape(label)}</option>')
    if selected_model and selected_model not in [str(c) for c in _piper_model_candidates()]:
        options.append(f'<option value="{escape(selected_model)}" selected>{escape(selected_model)} (custom)</option>')
    return "".join(options)


def _render_persona_avatar(persona: dict, size: str = "portrait") -> str:
    """Render avatar HTML for a persona. Size can be 'portrait', 'landscape', 'thumbnail', or 'small'."""
    avatar_filename = persona.get("avatar", "")
    persona_name = persona.get("name", "Persona")

    if not avatar_filename:
        # Return default avatar placeholder
        if size == "portrait":
            return f"<div class='persona-avatar persona-avatar-portrait' title='{escape(persona_name)}'><span class='persona-avatar-initial'>{escape(persona_name[0].upper())}</span></div>"
        elif size == "landscape":
            return f"<div class='persona-avatar persona-avatar-landscape' title='{escape(persona_name)}'><span class='persona-avatar-initial'>{escape(persona_name[0].upper())}</span></div>"
        elif size == "chat_portrait":
            return f"<div class='persona-avatar persona-avatar-chat-portrait' title='{escape(persona_name)}'><span class='persona-avatar-initial'>{escape(persona_name[0].upper())}</span></div>"
        elif size == "thumbnail":
            return f"<div class='persona-avatar persona-avatar-thumb' title='{escape(persona_name)}'><span class='persona-avatar-initial'>{escape(persona_name[0].upper())}</span></div>"
        else:
            return f"<div class='persona-avatar persona-avatar-small' title='{escape(persona_name)}'><span class='persona-avatar-initial'>{escape(persona_name[0].upper())}</span></div>"

    # Add cache-busting timestamp to prevent browser caching
    cache_bust = int(time.time()) % 10000
    avatar_url = f"/avatars/{escape(avatar_filename)}?v={cache_bust}"
    ext = Path(avatar_filename).suffix.lower()

    # Determine if it's a video
    is_video = ext in {".mp4", ".webm"}
    media_type = "video" if is_video else "image"

    # Build data attributes for lightbox
    lightbox_attrs = f"data-avatar-url='{avatar_url}' data-avatar-name='{escape(persona_name)}' data-avatar-type='{media_type}' onclick='openAvatarLightbox(this)'"

    if size == "portrait":
        if is_video:
            return f"<div class='persona-avatar persona-avatar-portrait avatar-clickable' {lightbox_attrs}><video src='{avatar_url}' autoplay loop muted playsinline title='{escape(persona_name)}'></video></div>"
        else:
            return f"<div class='persona-avatar persona-avatar-portrait avatar-clickable' {lightbox_attrs}><img src='{avatar_url}' alt='{escape(persona_name)}' loading='lazy'></div>"
    elif size == "landscape":
        if is_video:
            return f"<div class='persona-avatar persona-avatar-landscape avatar-clickable' {lightbox_attrs}><video src='{avatar_url}' autoplay loop muted playsinline title='{escape(persona_name)}'></video></div>"
        else:
            return f"<div class='persona-avatar persona-avatar-landscape avatar-clickable' {lightbox_attrs}><img src='{avatar_url}' alt='{escape(persona_name)}' loading='lazy'></div>"
    elif size == "chat_portrait":
        if is_video:
            return f"<div class='persona-avatar persona-avatar-chat-portrait avatar-clickable' {lightbox_attrs}><video src='{avatar_url}' autoplay loop muted playsinline title='{escape(persona_name)}'></video></div>"
        else:
            return f"<div class='persona-avatar persona-avatar-chat-portrait avatar-clickable' {lightbox_attrs}><img src='{avatar_url}' alt='{escape(persona_name)}' loading='lazy'></div>"
    elif size == "thumbnail":
        if is_video:
            return f"<div class='persona-avatar persona-avatar-thumb avatar-clickable' {lightbox_attrs}><video src='{avatar_url}' autoplay loop muted playsinline title='{escape(persona_name)}'></video></div>"
        else:
            return f"<div class='persona-avatar persona-avatar-thumb avatar-clickable' {lightbox_attrs}><img src='{avatar_url}' alt='{escape(persona_name)}' loading='lazy'></div>"
    else:
        if is_video:
            return f"<div class='persona-avatar persona-avatar-small avatar-clickable' {lightbox_attrs}><video src='{avatar_url}' autoplay loop muted playsinline title='{escape(persona_name)}'></video></div>"
        else:
            return f"<div class='persona-avatar persona-avatar-small avatar-clickable' {lightbox_attrs}><img src='{avatar_url}' alt='{escape(persona_name)}' loading='lazy'></div>"


def _render_persona_media_assets(persona_id: str) -> str:
    """Render the media assets list for a persona with delete buttons."""
    assets = _get_persona_media_assets(persona_id)
    if not assets:
        return "<p class='muted'>No emotional avatars uploaded yet. Upload below to enable dynamic avatar switching.</p>"

    # Calculate total storage
    persona_dir = assistant.paths.data_dir / "avatars" / persona_id
    total_size = sum(f.stat().st_size for f in persona_dir.iterdir() if f.is_file()) if persona_dir.exists() else 0
    size_mb = total_size / (1024 * 1024)

    html_parts = [
        f"<p class='muted'>Storage used: {size_mb:.1f} MB / 500 MB</p>",
        "<div class='media-assets-grid' style='display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); margin-top: 12px;'>"
    ]

    for asset in assets:
        filename = asset["filename"]
        tags = ", ".join(asset["tags"])
        is_default = "★ " if asset["is_default"] else ""
        ext = Path(filename).suffix.lower()
        is_video = ext in {".mp4", ".webm"}
        description = asset.get("description", "")
        desc_display = f"<span title='{escape(description)}'>📝</span>" if description else ""

        # Get file path for preview
        file_path = f"avatars/{persona_id}/{filename}"

        if is_video:
            preview = f"<video src='/{file_path}' style='width: 100%; height: 80px; object-fit: cover; border-radius: 8px;' muted playsinline></video>"
        else:
            preview = f"<img src='/{file_path}' style='width: 100%; height: 80px; object-fit: cover; border-radius: 8px;' alt='{filename}'>"

        html_parts.append(f"""
        <div class='media-asset-item' style='border: 1px solid var(--border); border-radius: 8px; padding: 8px; background: var(--bg-secondary);'>
            {preview}
            <div style='margin-top: 8px; font-size: 0.75rem;'>
                <span class='badge' style='background: var(--accent-info); color: white;'>{is_default}{tags}</span>
                {desc_display}
            </div>
            <form action="/admin/persona/media/delete" method="post" style='margin-top: 8px;'>
                <input type="hidden" name="persona_id" value="{escape(persona_id)}">
                <input type="hidden" name="filename" value="{escape(filename)}">
                <input type="hidden" name="redirect_to" value="/admin/persona">
                <button type="submit" class="button-compact" style='font-size: 0.7rem; padding: 4px 8px;'>Delete</button>
            </form>
        </div>
        """)

    html_parts.append("</div>")
    return "".join(html_parts)


def _render_media_upload_form(persona_id: str) -> str:
    """Render the upload form for emotional media assets with full 19-tag system."""
    # Organized 19-tag system with optgroups
    emotion_options = """
        <optgroup label="Emotional Tones">
            <option value="neutral">Neutral (Default)</option>
            <option value="happy">Happy / Joyful</option>
            <option value="excited">Excited / Thrilled</option>
            <option value="sad">Sad / Melancholy</option>
            <option value="angry">Angry / Furious</option>
            <option value="curious">Curious / Inquisitive</option>
            <option value="playful">Playful / Fun</option>
            <option value="serious">Serious / Focused</option>
            <option value="romantic">Romantic / Passionate</option>
            <option value="mysterious">Mysterious / Enigmatic</option>
            <option value="calm">Calm / Peaceful</option>
        </optgroup>
        <optgroup label="Contextual Topics">
            <option value="space">Space / Cosmos</option>
            <option value="technology">Technology / Digital</option>
            <option value="nature">Nature / Outdoors</option>
            <option value="battle">Battle / Combat</option>
        </optgroup>
        <optgroup label="Temporal / Contextual">
            <option value="morning">Morning / Dawn</option>
            <option value="afternoon">Afternoon / Midday</option>
            <option value="evening">Evening / Night</option>
            <option value="celebration">Celebration / Party</option>
        </optgroup>
    """

    return f"""
    <form action="/admin/persona/media" method="post" enctype="multipart/form-data" style="display: grid; gap: 10px; margin-top: 12px; padding: 12px; background: var(--bg-tertiary); border-radius: 12px;">
        <input type="hidden" name="persona_id" value="{escape(persona_id)}">
        <input type="hidden" name="redirect_to" value="/admin/persona">

        <label style="font-size: 0.9rem;">
            Media File (image or video, max 50MB)
            <input type="file" name="media" accept="image/*,video/mp4,video/webm" style="font-size: 0.85rem;" required>
        </label>

        <div style="display: grid; grid-template-columns: 2fr 1fr; gap: 10px;">
            <label style="font-size: 0.9rem;">
                Avatar Tag (19 options)
                <select name="emotion_tag" style="width: 100%; font-size: 0.85rem;">
                    {emotion_options}
                </select>
            </label>

            <label style="font-size: 0.9rem;">
                Set as Default
                <select name="is_default" style="width: 100%;">
                    <option value="false">No</option>
                    <option value="true">Yes</option>
                </select>
            </label>
        </div>

        <label style="font-size: 0.9rem;">
            Avatar Description (for LLM context)
            <textarea name="avatar_description" placeholder="Describe this avatar's appearance, setting, mood, etc. The LLM will use this to reference its current visual state." style="width: 100%; font-size: 0.85rem; min-height: 60px; margin-top: 4px;"></textarea>
        </label>

        <button type="submit" class="button-compact">Upload Avatar</button>
    </form>
    """


def _get_current_persona_avatar_html(size: str = "portrait") -> str:
    """Get avatar HTML for the currently selected persona."""
    current_persona_id = assistant.settings.current_persona_id or "nova"
    persona = get_persona(current_persona_id)
    if persona:
        persona_dict = asdict(persona)
        # Check for built-in persona avatar override in settings
        built_in_ids = {"nova", "researcher", "storyteller", "gentle_companion"}
        if current_persona_id in built_in_ids:
            persona_avatars = assistant.settings.persona_avatars or {}
            if current_persona_id in persona_avatars:
                persona_dict['avatar'] = persona_avatars[current_persona_id]
        return _render_persona_avatar(persona_dict, size=size)
    return _render_persona_avatar({"id": "nova", "name": "Nova", "avatar": "", "description": "", "system_prompt": ""}, size=size)


def _chat_history_path() -> Path:
    return paths.data_dir / "chat_history.json"


def _memory_context_path() -> Path:
    return paths.data_dir / "memory_context.json"


def _read_memory_context() -> dict:
    path = _memory_context_path()
    if not path.exists():
        return {"summary": "", "last_summary_index": 0, "version": 1}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"summary": "", "last_summary_index": 0, "version": 1}
    except Exception:
        return {"summary": "", "last_summary_index": 0, "version": 1}


def _write_memory_context(context: dict) -> None:
    _memory_context_path().write_text(
        json.dumps(context, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _read_chat_history() -> list[dict]:
    path = _chat_history_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _write_chat_history(items: list[dict]) -> None:
    _chat_history_path().write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _get_chat_messages(chat_id: str) -> list[dict] | None:
    history = _read_chat_history()
    for item in history:
        if item.get("id") == chat_id:
            source = item.get("source", "text")
            return [
                {"role": "user", "text": item["question"], "source": source},
                {"role": "assistant", "text": item["answer"], "context": item.get("context", ""), "source": source},
            ]
    return None


def _summarize_chats_to_memory(chats: list[dict], existing_summary: str) -> str:
    """Use LLM to summarize chat history into key points."""
    if not chats:
        return existing_summary
    
    # Build conversation text for summarization
    conversation_text = ""
    for chat in reversed(chats):  # Oldest first for logical flow
        q = chat.get("question", "")
        a = chat.get("answer", "")
        if q or a:
            conversation_text += f"User: {q}\nAssistant: {a}\n\n"
    
    if not conversation_text.strip():
        return existing_summary
    
    prompt = f"""Extract the key facts, preferences, and context from this conversation history. 
Summarize concisely what was discussed, any user preferences revealed, and important information to remember.
If there is an existing summary, integrate new information with it.

Existing summary (if any):
{existing_summary or "None yet"}

New conversations to summarize:
{conversation_text}

Provide an updated summary of key points to remember:"""

    try:
        # Use the LLM to generate summary
        payload = {
            "model": assistant.settings.ollama_chat_model,
            "messages": [
                {"role": "system", "content": "You are a memory summarizer. Extract and preserve key facts from conversations. Be concise."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        import requests
        response = requests.post(assistant.settings.ollama_chat_url, json=payload, timeout=60)
        if response.ok:
            new_summary = response.json().get("message", {}).get("content", "").strip()
            if new_summary:
                return new_summary
    except Exception as exc:
        print(f"[MEMORY DEBUG] Summarization failed: {exc}")
    
    return existing_summary


def _update_memory_context() -> None:
    """Update memory context when chat count triggers summarization."""
    history = _read_chat_history()
    memory = _read_memory_context()
    
    total_chats = len(history)
    last_summary_idx = memory.get("last_summary_index", 0)
    
    # Determine if we need to summarize
    # First summary at 20 chats, then every 10 thereafter
    should_summarize = False
    chats_to_summarize = []
    
    if total_chats >= 20 and last_summary_idx == 0:
        # First time: summarize all 20
        should_summarize = True
        chats_to_summarize = history[0:20]  # Most recent 20
        new_last_idx = 20
    elif total_chats >= last_summary_idx + 10 and last_summary_idx >= 20:
        # Subsequent: summarize the 10 oldest of the recent 20
        should_summarize = True
        # Get chats from index 10 to 20 (the ones falling out of the 20 recent)
        chats_to_summarize = history[10:20]
        new_last_idx = last_summary_idx + 10
    
    if should_summarize and chats_to_summarize:
        existing_summary = memory.get("summary", "")
        new_summary = _summarize_chats_to_memory(chats_to_summarize, existing_summary)
        
        memory["summary"] = new_summary
        memory["last_summary_index"] = new_last_idx
        _write_memory_context(memory)
        print(f"[MEMORY] Updated context summary (processed {len(chats_to_summarize)} chats, total indexed: {new_last_idx})")


def _get_memory_for_prompt() -> str:
    """Get memory context formatted for LLM prompt."""
    memory = _read_memory_context()
    summary = memory.get("summary", "")
    
    if not summary:
        return ""
    
    return f"\n\n[Long-term memory from previous conversations]:\n{summary}\n"


def _get_recent_chats_for_prompt(limit: int = 5, persona_id: str | None = None) -> str:
    """Get recent chat history formatted for LLM prompt."""
    history = _read_chat_history()
    if not history:
        return ""

    if persona_id:
        history = [item for item in history if str(item.get("persona_id", "") or "").strip() == persona_id]
        if not history:
            return ""

    recent = history[:limit]
    lines = ["\n[Recent conversation history]:"]
    for item in reversed(recent):  # Oldest first
        q = item.get("question", "")
        a = item.get("answer", "")
        if q:
            lines.append(f"User: {q}")
        if a:
            lines.append(f"Assistant: {a}")
    lines.append("")
    return "\n".join(lines)


def _get_recent_session_chats_for_prompt(history: list[dict], limit: int = 5) -> str:
    """Format session-scoped chat history for Public Mode prompts."""
    if not history:
        return ""

    turns: list[tuple[str, str]] = []
    pending_user = ""
    for item in history:
        role = str(item.get("role", "") or "")
        text = str(item.get("text", "") or "").strip()
        if not text:
            continue
        if role == "user":
            if pending_user:
                turns.append((pending_user, ""))
            pending_user = text
        elif role == "assistant":
            if pending_user:
                turns.append((pending_user, text))
                pending_user = ""
            else:
                turns.append(("", text))

    if pending_user:
        turns.append((pending_user, ""))

    if not turns:
        return ""

    recent = turns[-limit:]
    lines = ["\n[Recent conversation history]:"]
    for question, answer in recent:
        if question:
            lines.append(f"User: {question}")
        if answer:
            lines.append(f"Assistant: {answer}")
    lines.append("")
    return "\n".join(lines)


def _record_chat_history(
    question: str,
    answer: str,
    context: str = "",
    source: str = "text",
    persona_id: str | None = None,
) -> None:
    """Record a conversation turn to chat history.

    Args:
        question: The user's question/message
        answer: The assistant's response
        context: Retrieved context for archive mode
        source: "text" for typed input, "voice" for voice input
    """
    question_text = (question or "").strip()
    answer_text = (answer or "").strip()
    if not question_text and not answer_text:
        return
    history = _read_chat_history()
    history.insert(
        0,
        {
            "id": str(int(threading.get_native_id())) + "-" + str(len(history) + 1),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "question": question_text,
            "answer": answer_text,
            "context": context,
            "source": source,  # "text" or "voice"
            "persona_id": (persona_id or assistant.settings.current_persona_id or "nova"),
        },
    )
    _write_chat_history(history[:20])

    # Trigger memory context update after saving
    try:
        _update_memory_context()
    except Exception as exc:
        print(f"[MEMORY DEBUG] Failed to update memory context: {exc}")


def submit_conversation_turn(
    message: str,
    source: str = "text",
    *,
    skip_history: bool = False,
    skip_tts: bool = False,
    memory_context_override: str | None = None,
    recent_chats_override: str | None = None,
    persona_id_override: str | None = None,
    prefer_archive_retrieval: bool = False,
) -> dict:
    """Unified conversation pipeline for both text and voice inputs.

    This is the core conversation function used by both typed chat and voice modes.
    It processes the message through the assistant, records history, updates memory,
    and optionally speaks the response.

    Args:
        message: The user's message/question
        source: "text" or "voice" - used for history tagging and UI display
        skip_history: If True, don't record to history (for control commands)
        skip_tts: If True, don't speak the response (text-only mode)

    Returns:
        Dictionary with: answer, context, mode, matches, error (if any)
    """
    message_text = (message or "").strip()
    if not message_text:
        return {"answer": "", "context": "", "mode": "chat", "matches": [], "error": "Empty message"}

    try:
        # Get current avatar context for LLM awareness
        avatar_context = _get_current_avatar_context()
        result = assistant.ask(
            message_text,
            avatar_context=avatar_context,
            memory_context_override=memory_context_override,
            recent_chats_override=recent_chats_override,
            persona_id_override=persona_id_override,
            prefer_archive_retrieval=prefer_archive_retrieval,
        )
        answer = result.answer
        context = result.context

        # Record to history if not skipped
        if not skip_history:
            _record_chat_history(
                message_text,
                answer,
                context,
                source=source,
                persona_id=persona_id_override or assistant.settings.current_persona_id or "nova",
            )

        # Speak response if enabled and not skipped
        if not skip_tts and assistant.settings.speak_responses:
            _speak_answer_async(answer)

        return {
            "answer": answer,
            "context": context,
            "mode": result.mode,
            "matches": [m.dict() if hasattr(m, 'dict') else m for m in result.matches],
            "error": None,
        }

    except Exception as exc:
        error_text = f"Assistant error: {exc}"
        print(f"[CONVERSATION] Error: {error_text}")
        return {
            "answer": "",
            "context": "",
            "mode": "chat",
            "matches": [],
            "error": error_text,
        }


def process_voice_conversation_turn(message: str, session_id: str = "") -> dict:
    """Run a voice conversation turn with the correct Public/Admin history isolation."""
    settings = assistant.settings
    is_public_mode = settings.public_mode
    is_public_user = is_public_mode
    recent_chats_override = None

    if is_public_mode and session_id:
        session = session_manager.get_session(session_id)
        if session is not None:
            recent_chats_override = _get_recent_session_chats_for_prompt(session.chat_history)
        else:
            session_id = ""

    result = submit_conversation_turn(
        message,
        source="voice",
        skip_tts=True,
        skip_history=is_public_user,
        memory_context_override="" if is_public_user else None,
        recent_chats_override=recent_chats_override,
        persona_id_override=settings.public_mode_persona_id if is_public_mode else None,
        prefer_archive_retrieval=False,  # Public Mode: only explicit archive triggers
    )

    if result.get("error"):
        return result

    answer = result.get("answer", "")
    context = result.get("context", "")

    if is_public_mode and session_id:
        session_manager.add_chat_message(session_id, "user", message, "", "voice")
        session_manager.add_chat_message(session_id, "assistant", answer, context, "voice")
        history_html = _render_previous_chats(session_manager.get_chat_history(session_id), public_mode=True)
    elif is_public_mode:
        history_html = ""
    else:
        history_html = _render_previous_chats(_read_chat_history())

    enriched = dict(result)
    enriched["session_id"] = session_id
    enriched["history_html"] = history_html
    return enriched


# === Dynamic Avatar / Media Intelligence System ===
# Full 19-tag system for emotional and contextual avatar selection

# Complete list of allowed tags for avatar selection
EMOTIONAL_TAGS = {
    # Emotional tones (priority 1)
    "neutral", "happy", "excited", "sad", "angry", "curious",
    "playful", "serious", "romantic", "mysterious", "calm",
    # Contextual topics (priority 2)
    "space", "technology", "nature", "battle",
    # Temporal/contextual (priority 3)
    "morning", "afternoon", "evening", "celebration"
}

# Comprehensive keywords for rule-based emotion/context detection
EMOTION_KEYWORDS = {
    # Emotional tones
    "happy": [
        "love", "great", "awesome", "excellent", "wonderful", "amazing", "fantastic",
        "happy", "joy", "delight", "pleased", "glad", "cheerful", "smile", "laugh",
        "best", "perfect", "beautiful", "lovely", "sweet", "kind", "good", "nice"
    ],
    "excited": [
        "excited", "wow", "incredible", "unbelievable", "amazing", "astonishing",
        "thrilled", "eager", "enthusiastic", "pumped", "hyped", "stoked", "yay",
        "wonder", "astounded", "electrified", "energized", "fired up"
    ],
    "sad": [
        "sad", "sorry", "miss", "lost", "hurt", "pain", "cry", "tears", "depressed",
        "lonely", "grief", "regret", "disappointing", "unfortunate", "melancholy",
        "sorrow", "heartbroken", "miserable", "gloomy", "blue", "down", "upset"
    ],
    "angry": [
        "angry", "mad", "furious", "hate", "terrible", "awful", "horrible",
        "disgusting", "rage", "annoying", "frustrated", "stupid", "worst", "damn",
        "irritated", "annoyed", "outraged", "fuming", "livid", "irate"
    ],
    "curious": [
        "curious", "wonder", "question", "how", "why", "what", "explain", "tell me",
        "interested", "fascinated", "intrigued", "learn", "discover", "know",
        "curiosity", "inquire", "investigate", "explore"
    ],
    "playful": [
        "play", "fun", "joke", "laugh", "giggle", "silly", "goofy", "tease",
        "game", "jest", "humor", "amusing", "whimsical", "lighthearted",
        "mischievous", "trick", "prank", "banter", "witty"
    ],
    "serious": [
        "serious", "important", "critical", "urgent", "grave", "solemn",
        "business", "focus", "determined", "resolute", "stern", "strict",
        "earnest", "sincere", "thoughtful", "careful", "caution"
    ],
    "romantic": [
        "romantic", "love", "heart", "passion", "darling", "sweetheart",
        "affection", "adore", "devotion", "tender", "intimate", "beloved",
        "enchanting", "dreamy", "lovely", "beautiful", "charming"
    ],
    "mysterious": [
        "mysterious", "mystery", "secret", "unknown", "strange", "weird",
        "enigma", "puzzle", "riddle", "hidden", "shadow", "darkness",
        "cryptic", "occult", "supernatural", "eerie", "uncanny", "suspense"
    ],
    "calm": [
        "calm", "peaceful", "relax", "serene", "tranquil", "quiet", "still",
        "gentle", "soothing", "peace", "restful", "composed", "centered",
        "zen", "meditation", "breathe", "slow", "easy", "soft"
    ],
    # Contextual topics
    "space": [
        "space", "star", "planet", "galaxy", "universe", "cosmos", "astronomy",
        "rocket", "astronaut", "mars", "moon", "sun", "nebula", "black hole",
        "alien", "orbit", "spacecraft", "nasa", "telescope", "constellation"
    ],
    "technology": [
        "technology", "tech", "computer", "software", "hardware", "ai", "robot",
        "digital", "internet", "code", "programming", "app", "device", "gadget",
        "electronic", "machine", "automation", "cyber", "virtual", "data"
    ],
    "nature": [
        "nature", "forest", "tree", "mountain", "ocean", "river", "flower",
        "animal", "wildlife", "earth", "environment", "outdoor", "garden",
        "natural", "green", "organic", "wild", "landscape", "season", "weather"
    ],
    "battle": [
        "battle", "war", "fight", "combat", "soldier", "warrior", "weapon",
        "sword", "armor", "victory", "defeat", "enemy", "conflict", "struggle",
        "attack", "defense", "strategy", "tactics", "campaign", "siege"
    ],
    # Temporal/contextual
    "morning": [
        "morning", "dawn", "sunrise", "breakfast", "early", "wake", "fresh",
        "coffee", "daybreak", "sunup", "matin", "a.m", "start", "beginning"
    ],
    "afternoon": [
        "afternoon", "lunch", "midday", "noon", "daytime", "p.m", "siesta",
        "warm", "sunny", "work", "busy", "productive"
    ],
    "evening": [
        "evening", "sunset", "dusk", "twilight", "night", "dinner", "late",
        "moon", "stars", "relax", "wind down", "goodnight", "bedtime"
    ],
    "celebration": [
        "celebration", "celebrate", "party", "birthday", "anniversary", "holiday",
        "congratulations", "cheers", "toast", "festival", "event", "ceremony",
        "wedding", "graduation", "success", "achievement", "milestone"
    ],
    "neutral": []  # Default fallback
}


def _analyze_emotion_simple(message: str) -> str:
    """
    Simple rule-based emotion and context analysis for avatar selection.
    Returns one of the 19 allowed tags, with 'neutral' as default.

    Priority order:
    1. Emotional tone (highest priority)
    2. Context/topic
    3. Temporal indicators
    """
    text = message.lower()

    # Count matches for each tag
    scores = {}
    for tag, keywords in EMOTION_KEYWORDS.items():
        if keywords:  # Skip empty neutral list
            scores[tag] = sum(1 for word in keywords if word in text)

    # Return tag with highest score, or neutral if no matches
    if scores and max(scores.values()) > 0:
        return max(scores, key=scores.get)
    return "neutral"


def _get_primary_tag(message: str) -> str:
    """
    Get the primary (first priority) tag from a message.
    Used when multiple tags are detected - returns the most relevant one.
    """
    return _analyze_emotion_simple(message)


def _get_persona_media_assets(persona_id: str) -> list[dict]:
    """
    Get media assets for a persona with their emotional tags.
    Returns list of {filename, tags, is_default} dicts.
    """
    assets = []

    # Check for persona-specific avatar folder
    avatar_dir = assistant.paths.data_dir / "avatars" / persona_id
    if avatar_dir.exists():
        for file_path in avatar_dir.iterdir():
            if file_path.is_file() and file_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".webm"}:
                # Parse tags from filename (e.g., "nova_happy_v1.png" -> tags: ["happy"])
                # or from a sidecar .json file if it exists
                filename = file_path.name
                stem = file_path.stem
                tags = ["neutral"]  # default
                is_default = "default" in stem.lower() or filename == f"{persona_id}_default"

                # Check for sidecar metadata file
                description = ""  # avatar description for LLM context
                metadata_path = file_path.with_suffix(".json")
                if metadata_path.exists():
                    try:
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        tags = metadata.get("tags", tags)
                        is_default = metadata.get("default", is_default)
                        description = metadata.get("description", "")
                    except Exception:
                        pass
                else:
                    # Infer from filename
                    for tag in EMOTIONAL_TAGS:
                        if tag in stem.lower():
                            tags = [tag]
                            break

                assets.append({
                    "filename": filename,
                    "tags": tags,
                    "is_default": is_default,
                    "path": str(file_path.relative_to(assistant.paths.data_dir / "avatars")),
                    "description": description,
                })

    # Fallback: use existing single avatar as neutral/default
    if not assets:
        persona = get_persona(persona_id)
        if persona and persona.avatar:
            assets.append({
                "filename": persona.avatar,
                "tags": ["neutral"],
                "is_default": True,
                "path": persona.avatar,
                "description": "",
            })

    return assets


def _select_avatar_for_emotion(persona_id: str, emotion: str) -> dict | None:
    """
    Select the best matching avatar for the given emotion.
    Returns asset dict with path, description, etc., or None if no match found.
    """
    assets = _get_persona_media_assets(persona_id)
    if not assets:
        return None

    # First try: exact emotion match
    for asset in assets:
        if emotion in asset["tags"]:
            return asset

    # Second try: neutral fallback
    for asset in assets:
        if "neutral" in asset["tags"] or asset["is_default"]:
            return asset

    # Last resort: any available asset
    return assets[0] if assets else None


def _get_current_avatar_context() -> str:
    """
    Get the current avatar description for LLM context.
    Returns a formatted string describing the current visual state, or empty string if no description.
    """
    global _current_avatar_state
    description = _current_avatar_state.get("description", "")
    if not description:
        return ""
    return f"\n\n[Current visual state]: You are currently displaying as: {description}"


# Debouncing state for avatar switches
_avatar_switch_state = {
    "last_emotion": "neutral",
    "last_switch_time": 0.0,
    "pending_emotion": None,
    "switch_delay_seconds": 5.0,  # Minimum seconds between avatar switches
}

# Current avatar state for LLM context
_current_avatar_state = {
    "persona_id": "",
    "emotion": "neutral",
    "description": "",
    "filename": "",
}


def _should_switch_avatar(emotion: str) -> bool:
    """
    Check if we should switch avatar based on debouncing rules.
    Returns True if switch is allowed, False otherwise.
    """
    now = time.time()
    time_since_last = now - _avatar_switch_state["last_switch_time"]

    # Always allow if it's the same emotion (no switch needed anyway)
    if emotion == _avatar_switch_state["last_emotion"]:
        return False

    # Allow switch if enough time has passed
    if time_since_last >= _avatar_switch_state["switch_delay_seconds"]:
        _avatar_switch_state["last_emotion"] = emotion
        _avatar_switch_state["last_switch_time"] = now
        return True

    # Store as pending for future switch
    _avatar_switch_state["pending_emotion"] = emotion
    return False


def _get_emotional_avatar_html(persona_id: str, message: str, size: str = "chat_portrait") -> str:
    """
    Analyze message emotion and return appropriate avatar HTML.
    This is the reactive entry point for dynamic avatar switching.
    Also updates _current_avatar_state for LLM context awareness.
    
    Avatar persistence: When no emotion keywords are detected (neutral), 
    the current avatar is retained rather than switching to default.
    Only explicit emotion/context keywords trigger a switch.
    """
    global _current_avatar_state
    emotion = _analyze_emotion_simple(message)

    # If no emotion keywords detected (neutral), retain current avatar
    # This prevents reverting to default between emotionally-tagged messages
    if emotion == "neutral":
        # Keep the current avatar state unchanged
        asset = _select_avatar_for_emotion(persona_id, _avatar_switch_state["last_emotion"])
        if asset:
            # Update just the description if needed, but keep emotion context
            _current_avatar_state = {
                "persona_id": persona_id,
                "emotion": _avatar_switch_state["last_emotion"],
                "description": asset.get("description", ""),
                "filename": asset.get("filename", ""),
            }
            persona = get_persona(persona_id)
            persona_dict = asdict(persona) if persona else {"id": persona_id, "name": persona_id, "avatar": "", "description": "", "system_prompt": ""}
            persona_dict["avatar"] = asset["path"]
            return _render_persona_avatar(persona_dict, size=size)
        # Fall through to default if no current avatar exists

    # Check if we should actually switch (debouncing)
    should_switch = _should_switch_avatar(emotion)
    effective_emotion = emotion if should_switch else _avatar_switch_state["last_emotion"]

    # Get the appropriate avatar
    asset = _select_avatar_for_emotion(persona_id, effective_emotion)

    if not asset:
        # Fallback to default rendering - clear avatar state
        _current_avatar_state = {
            "persona_id": persona_id,
            "emotion": effective_emotion,
            "description": "",
            "filename": "",
        }
        return _get_current_persona_avatar_html(size=size)

    # Update global avatar state for LLM context
    _current_avatar_state = {
        "persona_id": persona_id,
        "emotion": effective_emotion,
        "description": asset.get("description", ""),
        "filename": asset.get("filename", ""),
    }

    # Render with the selected avatar
    persona = get_persona(persona_id)
    persona_dict = asdict(persona) if persona else {"id": persona_id, "name": persona_id, "avatar": "", "description": "", "system_prompt": ""}
    persona_dict["avatar"] = asset["path"]

    return _render_persona_avatar(persona_dict, size=size)


@app.get("/", response_class=HTMLResponse)
def home(
    session_id: str = Cookie(default=""),
    admin_access: str = Cookie(default=""),
    clear_chat: str = Query(default=""),
) -> HTMLResponse:
    # Public Mode: ensure session exists
    settings = assistant.settings
    is_public_mode = settings.public_mode
    has_admin_access = admin_access == "granted"
    
    # In Public Mode, we need a valid session
    if is_public_mode and not has_admin_access:
        if not session_id or not session_manager.validate_session(session_id):
            # Create new session and redirect to set cookie with clear_chat flag
            response = RedirectResponse(url="/?clear_chat=1", status_code=307)
            session = session_manager.create_session()
            response.set_cookie(
                key="session_id",
                value=session.session_id,
                httponly=True,
                samesite="strict",
                max_age=86400,
            )
            return response
    
    # When switching personas or creating new session, clear chat to avoid mixing histories
    chat_messages = [] if clear_chat else None
    
    return HTMLResponse(content=_render_page(
        session_id=session_id,
        admin_access=admin_access,
        chat_messages=chat_messages,
    ))


@app.get("/admin", response_class=HTMLResponse)
def admin() -> str:
    return _render_admin_page()


@app.get("/admin/library", response_class=HTMLResponse)
def admin_library() -> str:
    return _render_library_page()


@app.get("/admin/persona", response_class=HTMLResponse)
def admin_persona(message: str = "") -> str:
    return _render_persona_page(message=message)


@app.get("/setup", response_class=HTMLResponse)
def setup() -> str:
    return _render_setup_page()


@app.get("/setup/piper/helper/download")
def download_piper_helper() -> FileResponse:
    helper_path = _ensure_windows_piper_helper()
    return FileResponse(path=helper_path, filename=helper_path.name, media_type="text/plain")


@app.post("/setup/piper/helper/run")
async def run_piper_helper(
    redirect_to: str = Form("/setup"),
) -> RedirectResponse:
    helper_path = _ensure_windows_piper_helper()
    _write_helper_install_status(
        {
            "active": True,
            "stage": "Starting installer",
            "message": "Launching the Windows installer helper.",
            "last_error": "",
            "last_completed": "",
        }
    )
    launched = False
    if platform.system().lower() == "windows":
        try:
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(helper_path),
                    "-StatusPath",
                    str(paths.helper_status_path),
                    "-ProjectDir",
                    str(paths.base_dir),
                ],
                cwd=str(assistant.paths.base_dir),
            )
            launched = True
        except Exception:
            launched = False

    assistant.runtime_status.mark_setup_step(
        "piper_helper_launched",
        completed=launched,
        detail="Started the Windows installer." if launched else "Could not start the Windows installer automatically.",
    )
    assistant.runtime_status.set_helper_script(
        path=str(helper_path),
        ready=True,
        note="Windows installer helper is available for download or launch.",
    )
    if not launched:
        _write_helper_install_status(
            {
                "active": False,
                "stage": "Launch failed",
                "message": "",
                "last_error": "Archiveum could not launch the Windows installer helper automatically.",
            }
        )
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.get("/health/live")
def health_live() -> dict:
    return {"ok": True, "service": "archiveum"}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    diagnostics = assistant.diagnostics()
    status_code = 200 if diagnostics["ready"] else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": diagnostics["ready"],
            "voice_ready": diagnostics["voice_ready"],
        },
    )


@app.get("/status")
def status_page() -> HTMLResponse:
    diagnostics = assistant.diagnostics()
    diagnostics["sources"] = assistant.store.list_sources()
    return HTMLResponse(_render_status_page(diagnostics))


@app.get("/status.json")
def status_json() -> dict:
    diagnostics = assistant.diagnostics()
    diagnostics["sources"] = assistant.store.list_sources()
    return diagnostics


def clear_recent_ingestion_errors(filename: str = "") -> None:
    """Clear ingestion errors, optionally for a specific filename."""
    if filename:
        assistant.runtime_status.clear_ingestion_error(filename)
    else:
        assistant.runtime_status.clear_all_ingestion_errors()


@app.post("/admin/errors/clear")
async def clear_ingestion_errors(filename: str = Form("")) -> RedirectResponse:
    target = (filename or "").strip()
    if target:
        clear_recent_ingestion_errors(filename=target)
    else:
        clear_recent_ingestion_errors()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/settings/public-mode")
async def save_public_mode_settings(
    public_mode: str = Form(""),
    public_mode_persona_id: str = Form(""),
    session_timeout_minutes: str = Form("30"),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    redirect_to: str = Form("/admin"),
) -> RedirectResponse:
    """Save Public Mode settings including password changes."""
    settings = assistant.settings
    updates: dict[str, any] = {}
    
    # Update Public Mode settings
    updates["public_mode"] = public_mode == "true"
    updates["public_mode_persona_id"] = public_mode_persona_id or settings.public_mode_persona_id or settings.current_persona_id or "nova"
    try:
        updates["session_timeout_minutes"] = int(session_timeout_minutes)
    except ValueError:
        updates["session_timeout_minutes"] = 30
    
    # Handle password change if requested
    if new_password:
        if new_password != confirm_password:
            return RedirectResponse(
                url=_safe_redirect_target(redirect_to, {"error": "Passwords do not match"}),
                status_code=303,
            )
        if len(new_password) < 8:
            return RedirectResponse(
                url=_safe_redirect_target(redirect_to, {"error": "Password must be at least 8 characters"}),
                status_code=303,
            )
        
        # If password already set, verify current password
        if settings.admin_password_hash:
            if not verify_password(current_password, settings.admin_password_hash):
                return RedirectResponse(
                    url=_safe_redirect_target(redirect_to, {"error": "Current password is incorrect"}),
                    status_code=303,
                )
        
        # Hash and save new password
        updates["admin_password_hash"] = hash_password(new_password)
    
    # Persist all settings
    persist_settings(assistant.paths, updates)
    assistant.reload_settings()
    
    # Create response
    response = RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"message": "Public Mode settings saved"}),
        status_code=303,
    )
    
    # When enabling Public Mode, clear admin_access cookie so admin sees the actual Public Mode
    if updates["public_mode"]:
        response.delete_cookie(key="admin_access")
    
    return response


@app.post("/admin/library/rename")
async def rename_library_file(
    source: str = Form(...),
    new_name: str = Form(...),
    redirect_to: str = Form("/admin/library"),
) -> RedirectResponse:
    source_name = _safe_source_name(source)
    safe_name = Path((new_name or "").strip()).name
    if not safe_name:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    old_path = paths.uploads_dir / Path(source_name)
    if not old_path.exists():
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    new_path = old_path.with_name(safe_name)
    if new_path.exists():
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    old_source_name = source_name
    new_source_name = new_path.relative_to(paths.uploads_dir).as_posix()
    old_path.rename(new_path)
    assistant.remove_source(old_source_name)
    assistant.ingest_file(new_path, source_name=new_source_name)
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/library/move")
async def move_library_file(
    source: str = Form(...),
    category: str = Form(...),
    redirect_to: str = Form("/admin/library"),
) -> RedirectResponse:
    source_name = _safe_source_name(source)
    safe_category = _safe_upload_category(category)
    old_path = paths.uploads_dir / Path(source_name)
    if not old_path.exists():
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    new_path = paths.uploads_dir / safe_category / old_path.name
    new_path.parent.mkdir(parents=True, exist_ok=True)
    if new_path.resolve() == old_path.resolve():
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)
    if new_path.exists():
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    old_source_name = source_name
    new_source_name = new_path.relative_to(paths.uploads_dir).as_posix()
    old_path.rename(new_path)
    assistant.remove_source(old_source_name)
    assistant.ingest_file(new_path, source_name=new_source_name)
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/library/category/create")
async def create_library_category(
    label: str = Form(...),
    path: str = Form(""),
    redirect_to: str = Form("/admin/library"),
) -> RedirectResponse:
    category_label = (label or "").strip()
    if not category_label:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    category_path = _safe_category_path(path or category_label)
    if not category_path:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    existing = {value for value, _ in upload_category_options(paths)}
    if category_path in existing:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    custom_categories = _load_custom_upload_categories()
    custom_categories.append({"path": category_path, "label": category_label})
    _persist_custom_upload_categories(custom_categories)
    (paths.uploads_dir / category_path).mkdir(parents=True, exist_ok=True)

    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/library/category/rename")
async def rename_library_category(
    category: str = Form(...),
    new_label: str = Form(""),
    new_path: str = Form(""),
    redirect_to: str = Form("/admin/library"),
) -> RedirectResponse:
    source_category = _safe_category_path(category)
    if not source_category or not _is_custom_category(source_category):
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    custom_categories = _load_custom_upload_categories()
    for item in custom_categories:
        if item["path"] == source_category:
            target_item = item
            break
    else:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    updated = False
    new_label_value = (new_label or "").strip()
    if new_label_value:
        target_item["label"] = new_label_value
        updated = True

    new_category_path = _safe_category_path(new_path)
    if new_category_path and new_category_path != source_category:
        existing = {value for value, _ in upload_category_options(paths)}
        if new_category_path not in existing:
            old_dir = paths.uploads_dir / source_category
            new_dir = paths.uploads_dir / new_category_path
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            moved_files = []
            if old_dir.exists():
                moved_files = [path.relative_to(old_dir) for path in old_dir.rglob("*") if path.is_file()]
                old_dir.rename(new_dir)
            target_item["path"] = new_category_path
            updated = True
            for relative_file in moved_files:
                new_file = new_dir / relative_file
                if new_file.exists():
                    old_source_name = Path(source_category) / relative_file
                    assistant.remove_source(old_source_name.as_posix())
                    assistant.ingest_file(new_file, source_name=new_file.relative_to(paths.uploads_dir).as_posix())

    if updated:
        _persist_custom_upload_categories(custom_categories)

    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/library/category/delete")
async def delete_library_category(
    category: str = Form(...),
    redirect_to: str = Form("/admin/library"),
) -> RedirectResponse:
    source_category = _safe_category_path(category)
    if source_category and _is_custom_category(source_category):
        custom_categories = [item for item in _load_custom_upload_categories() if item["path"] != source_category]
        _persist_custom_upload_categories(custom_categories)

        directory = paths.uploads_dir / source_category
        if directory.exists():
            for path in sorted(directory.rglob("*"), reverse=True):
                if path.is_file():
                    assistant.remove_source(path.relative_to(paths.uploads_dir).as_posix())
            shutil.rmtree(directory, ignore_errors=True)

    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/library/delete")
async def delete_library_file(
    source: str = Form(...),
    redirect_to: str = Form("/admin/library"),
) -> RedirectResponse:
    # Remove from index
    removed_count = assistant.remove_source(source)
    
    # Also delete the physical file if it exists
    file_path = paths.uploads_dir / source
    if file_path.exists() and file_path.is_file():
        try:
            file_path.unlink()
        except Exception:
            pass  # Ignore errors deleting physical file
    
    # If no chunks were removed, the file was already deleted from index
    # But we still try to delete the physical file
    
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/library/reindex")
async def reindex_library_file(
    source: str = Form(...),
    redirect_to: str = Form("/admin/library"),
) -> RedirectResponse:
    file_path = paths.uploads_dir / source
    if file_path.exists() and file_path.is_file():
        # Re-index the file with current settings
        assistant.reindex_file(file_path, source_name=source)
    
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/persona/save")
async def save_persona(
    assistant_name: str = Form("Archiveum"),
    user_name: str = Form("User"),
    style: str = Form("warm"),
    brevity: str = Form("short"),
    custom_system_prompt: str = Form(""),
    starter: str = Form("custom"),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    prompt_text = (custom_system_prompt or "").strip()
    if starter == "nova":
        prompt_text = _nova_style_prompt(assistant_name=assistant_name, user_name=user_name, style=style, brevity=brevity)
    persist_settings(
        assistant.paths,
        {
            "custom_system_prompt": prompt_text,
        },
    )
    assistant.reload_settings()
    runtime = _get_voice_runtime()
    runtime.refresh_settings()
    return RedirectResponse(url=_safe_redirect_target(redirect_to, {"message": "Custom prompt saved."}), status_code=303)


@app.post("/admin/persona/clear")
async def clear_persona(
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    persist_settings(assistant.paths, {"custom_system_prompt": ""})
    assistant.reload_settings()
    runtime = _get_voice_runtime()
    runtime.refresh_settings()
    return RedirectResponse(url=_safe_redirect_target(redirect_to, {"message": "Custom prompt cleared. Persona selection restored."}), status_code=303)


@app.post("/admin/persona/select")
async def select_persona(
    persona_id: str = Form(...),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    """Apply a persona selection and persist it to settings."""
    updates: dict[str, any] = {
        "current_persona_id": persona_id,
        "custom_system_prompt": "",
    }
    persist_settings(assistant.paths, updates)
    assistant.reload_settings()
    runtime = _get_voice_runtime()
    runtime.refresh_settings()
    # Add clear_chat=1 to prevent loading previous conversation
    safe_redirect = _safe_redirect_target(redirect_to, {"message": f"Persona '{persona_id}' selected.", "clear_chat": "1"})
    return RedirectResponse(url=safe_redirect, status_code=303)


@app.post("/admin/persona/custom/create")
async def create_custom_persona(
    name: str = Form(...),
    description: str = Form(""),
    system_prompt: str = Form(...),
    avatar: UploadFile = File(None),
    llm_model: str = Form(""),
    voice_model: str = Form(""),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    import mimetypes
    import hashlib

    prompt_text = (system_prompt or "").strip()
    persona_name = (name or "").strip()
    if not persona_name or not prompt_text:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    persona_id = _safe_persona_id(persona_name)
    custom_personas = _load_custom_personas()

    # Handle avatar upload if provided
    avatar_filename = ""
    debug_info = f"avatar_obj: {avatar}, avatar_type: {type(avatar)}"
    if avatar:
        debug_info += f", filename: {getattr(avatar, 'filename', 'N/A')}"
    if avatar and avatar.filename:
        # Validate file size (max 50MB)
        content = await avatar.read()
        if content:
            if len(content) > 50 * 1024 * 1024:
                return RedirectResponse(
                    url=_safe_redirect_target(redirect_to, {"message": "Avatar file too large. Maximum size is 50MB."}),
                    status_code=303,
                )

            # Validate file type
            allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "video/mp4", "video/webm", "application/octet-stream"}
            allowed_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"}
            content_type = avatar.content_type or mimetypes.guess_type(avatar.filename or "")[0] or ""
            ext = Path(avatar.filename or "").suffix.lower()

            is_valid_type = content_type in allowed_types or ext in allowed_extensions
            if not is_valid_type:
                return RedirectResponse(
                    url=_safe_redirect_target(redirect_to, {"message": f"Invalid file type: {content_type} (.{ext}). Allowed: PNG, JPEG, GIF, WebP, MP4, WebM."}),
                    status_code=303,
                )

            # Ensure avatars directory exists
            avatars_dir = assistant.paths.avatars_dir
            avatars_dir.mkdir(parents=True, exist_ok=True)

            # Generate safe filename
            ext = Path(avatar.filename or "").suffix.lower() or ".png"
            avatar_filename = f"{persona_id}_{hashlib.md5(content).hexdigest()[:8]}{ext}"
            avatar_path = avatars_dir / avatar_filename

            # Save file
            try:
                with open(avatar_path, "wb") as f:
                    f.write(content)
                # Verify file was written
                if not avatar_path.exists():
                    avatar_filename = ""
            except Exception:
                avatar_filename = ""  # Reset if save failed

    custom_personas.append(
        {
            "id": persona_id,
            "name": persona_name,
            "description": (description or "").strip(),
            "system_prompt": prompt_text,
            "avatar": avatar_filename,
            "llm_model": (llm_model or "").strip(),
            "voice_model": (voice_model or "").strip(),
        }
    )
    # Combine all updates into a single persist_settings call to avoid race conditions
    persist_settings(
        assistant.paths,
        {
            "custom_personas": custom_personas,
            "current_persona_id": persona_id,
            "custom_system_prompt": "",
        },
    )
    assistant.reload_settings()
    runtime = _get_voice_runtime()
    runtime.refresh_settings()
    avatar_msg = f" with avatar" if avatar_filename else ""
    return RedirectResponse(url=_safe_redirect_target(redirect_to, {"message": f"Created and selected persona: {persona_name}{avatar_msg}."}), status_code=303)


@app.post("/admin/persona/custom/edit")
async def edit_custom_persona(
    persona_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    system_prompt: str = Form(...),
    llm_model: str = Form(""),
    voice_model: str = Form(""),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    prompt_text = (system_prompt or "").strip()
    persona_name = (name or "").strip()
    if not persona_id or not persona_name or not prompt_text:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    custom_personas = []
    updated = False
    for persona in _load_custom_personas():
        if persona["id"] == persona_id:
            # Preserve existing avatar and model fields if not provided
            llm_value = (llm_model or "").strip() if llm_model else persona.get("llm_model", "")
            voice_value = (voice_model or "").strip() if voice_model else persona.get("voice_model", "")
            custom_personas.append(
                {
                    "id": persona_id,
                    "name": persona_name,
                    "description": (description or "").strip(),
                    "system_prompt": prompt_text,
                    "avatar": persona.get("avatar", ""),
                    "llm_model": llm_value,
                    "voice_model": voice_value,
                }
            )
            updated = True
        else:
            custom_personas.append(persona)

    if updated:
        _persist_custom_personas(custom_personas)
        runtime = _get_voice_runtime()
        runtime.refresh_settings()

    return RedirectResponse(url=_safe_redirect_target(redirect_to, {"message": "Updated persona." if updated else ""}), status_code=303)


@app.post("/admin/persona/custom/delete")
async def delete_custom_persona(
    persona_id: str = Form(...),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    custom_personas = [persona for persona in _load_custom_personas() if persona["id"] != persona_id]
    _persist_custom_personas(custom_personas)
    deleted_name = next((p["name"] for p in _load_custom_personas() if p["id"] == persona_id), persona_id)
    if assistant.settings.current_persona_id == persona_id:
        persist_settings(
            assistant.paths,
            {
                "current_persona_id": "nova",
                "custom_system_prompt": "",
            },
        )
        assistant.reload_settings()
    runtime = _get_voice_runtime()
    runtime.refresh_settings()
    return RedirectResponse(url=_safe_redirect_target(redirect_to, {"message": f"Deleted persona: {deleted_name}."}), status_code=303)


@app.post("/admin/persona/avatar")
async def upload_persona_avatar(
    persona_id: str = Form(...),
    avatar: UploadFile = File(...),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    """Upload an avatar image/video/gif for a custom persona."""
    import mimetypes

    # Check if file was actually selected
    if not avatar.filename:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "Please select a file to upload."}),
            status_code=303,
        )

    # Validate file size (max 50MB)
    content = await avatar.read()
    if not content:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "The uploaded file appears to be empty."}),
            status_code=303,
        )
    if len(content) > 50 * 1024 * 1024:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "Avatar file too large. Maximum size is 50MB."}),
            status_code=303,
        )

    # Validate file type by content type or file extension
    allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "video/mp4", "video/webm", "application/octet-stream"}
    allowed_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"}
    content_type = avatar.content_type or mimetypes.guess_type(avatar.filename or "")[0] or ""
    ext = Path(avatar.filename or "").suffix.lower()

    # Check if valid by content type OR file extension
    is_valid_type = content_type in allowed_types or ext in allowed_extensions
    if not is_valid_type:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": f"Invalid file type: {content_type} (.{ext}). Allowed: PNG, JPEG, GIF, WebP, MP4, WebM."}),
            status_code=303,
        )

    # Ensure avatars directory exists
    avatars_dir = assistant.paths.avatars_dir
    avatars_dir.mkdir(parents=True, exist_ok=True)

    # Generate safe filename
    import hashlib
    ext = Path(avatar.filename or "").suffix.lower() or ".png"
    safe_filename = f"{persona_id}_{hashlib.md5(content).hexdigest()[:8]}{ext}"
    avatar_path = avatars_dir / safe_filename

    # Save file
    try:
        with open(avatar_path, "wb") as f:
            f.write(content)
        # Verify file was written
        if not avatar_path.exists():
            return RedirectResponse(
                url=_safe_redirect_target(redirect_to, {"message": f"Failed to save avatar file to {avatar_path}"}),
                status_code=303,
            )
    except Exception as e:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": f"Error saving avatar: {str(e)}"}),
            status_code=303,
        )

    # Determine if this is a built-in or custom persona
    built_in_ids = {"nova", "researcher", "storyteller", "gentle_companion"}
    is_built_in = persona_id in built_in_ids

    # Remove old avatar if exists
    if is_built_in:
        persona_avatars = assistant.settings.persona_avatars or {}
        old_avatar = persona_avatars.get(persona_id, "")
    else:
        custom_personas = _load_custom_personas()
        old_avatar = ""
        for persona in custom_personas:
            if persona["id"] == persona_id:
                old_avatar = persona.get("avatar", "")
                break

    if old_avatar:
        old_path = avatars_dir / old_avatar
        try:
            old_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Update persona with new avatar
    if is_built_in:
        # Store in persona_avatars setting for built-in personas
        persona_avatars = assistant.settings.persona_avatars or {}
        persona_avatars[persona_id] = safe_filename
        persist_settings(assistant.paths, {"persona_avatars": persona_avatars})
        assistant.reload_settings()
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "Avatar uploaded successfully."}),
            status_code=303,
        )
    else:
        # Update custom persona
        custom_personas = _load_custom_personas()
        for persona in custom_personas:
            if persona["id"] == persona_id:
                persona["avatar"] = safe_filename
                _persist_custom_personas(custom_personas)
                assistant.reload_settings()
                return RedirectResponse(
                    url=_safe_redirect_target(redirect_to, {"message": "Avatar uploaded successfully."}),
                    status_code=303,
                )

    return RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"message": "Persona not found."}),
        status_code=303,
    )


@app.post("/admin/persona/media")
async def upload_persona_media(
    persona_id: str = Form(...),
    media: UploadFile = File(...),
    emotion_tag: str = Form("neutral"),
    is_default: bool = Form(False),
    avatar_description: str = Form(""),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    """
    Upload media asset with emotional tag for a persona.
    Files are stored in data/avatars/{persona_id}/ with metadata.
    """
    import mimetypes

    # Validate emotion tag against full 19-tag system
    if emotion_tag not in EMOTIONAL_TAGS:
        emotion_tag = "neutral"

    # Check file
    if not media.filename:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "Please select a file to upload."}),
            status_code=303,
        )

    content = await media.read()
    if not content:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "The uploaded file appears to be empty."}),
            status_code=303,
        )

    # 50MB limit per file
    if len(content) > 50 * 1024 * 1024:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "Media file too large. Maximum size is 50MB."}),
            status_code=303,
        )

    # Check total persona media storage (500MB limit)
    persona_media_dir = assistant.paths.data_dir / "avatars" / persona_id
    if persona_media_dir.exists():
        current_size = sum(f.stat().st_size for f in persona_media_dir.iterdir() if f.is_file())
        if current_size + len(content) > 500 * 1024 * 1024:
            return RedirectResponse(
                url=_safe_redirect_target(redirect_to, {"message": "Storage limit exceeded. Maximum 500MB per persona."}),
                status_code=303,
            )

    # Validate file type
    allowed_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm"}
    ext = Path(media.filename or "").suffix.lower()
    if ext not in allowed_extensions:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": f"Invalid file type: {ext}. Allowed: PNG, JPEG, GIF, WebP, MP4, WebM."}),
            status_code=303,
        )

    # Create persona media directory
    persona_media_dir.mkdir(parents=True, exist_ok=True)

    # Generate safe filename with emotion tag
    import hashlib
    file_hash = hashlib.md5(content).hexdigest()[:8]
    safe_filename = f"{persona_id}_{emotion_tag}_{file_hash}{ext}"
    file_path = persona_media_dir / safe_filename

    # Save file
    try:
        with open(file_path, "wb") as f:
            f.write(content)
    except Exception as e:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": f"Error saving media: {str(e)}"}),
            status_code=303,
        )

    # Save metadata sidecar file
    metadata = {
        "tags": [emotion_tag],
        "default": is_default,
        "original_filename": media.filename,
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "description": avatar_description.strip() if avatar_description else "",
    }
    metadata_path = file_path.with_suffix(".json")
    try:
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    except Exception:
        pass  # Non-critical

    return RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"message": f"Uploaded {emotion_tag} media for {persona_id}."}),
        status_code=303,
    )


@app.post("/admin/persona/media/delete")
async def delete_persona_media(
    persona_id: str = Form(...),
    filename: str = Form(...),
    redirect_to: str = Form("/admin/persona"),
) -> RedirectResponse:
    """Delete a media asset for a persona."""
    persona_media_dir = assistant.paths.data_dir / "avatars" / persona_id
    file_path = persona_media_dir / filename

    # Security: ensure path is within persona directory
    try:
        file_path.relative_to(persona_media_dir)
    except ValueError:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "Invalid file path."}),
            status_code=303,
        )

    if not file_path.exists():
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": "File not found."}),
            status_code=303,
        )

    # Delete file and metadata
    try:
        file_path.unlink(missing_ok=True)
        metadata_path = file_path.with_suffix(".json")
        metadata_path.unlink(missing_ok=True)
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": f"Deleted {filename}."}),
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"message": f"Error deleting: {str(e)}"}),
            status_code=303,
        )


@app.get("/admin/debug/custom_personas")
def debug_custom_personas() -> JSONResponse:
    """Debug endpoint to check custom personas and their avatars."""
    custom = _load_custom_personas()
    avatars_dir = assistant.paths.avatars_dir
    avatars_exist = {}
    for persona in custom:
        avatar = persona.get("avatar", "")
        if avatar:
            avatars_exist[persona["id"]] = (avatars_dir / avatar).exists()
        else:
            avatars_exist[persona["id"]] = False

    # List actual files in avatars directory
    actual_files = []
    if avatars_dir.exists():
        actual_files = [f.name for f in avatars_dir.iterdir() if f.is_file()]

    return JSONResponse({
        "custom_personas": custom,
        "avatars_dir": str(avatars_dir),
        "avatars_exist": avatars_exist,
        "actual_files_in_avatars_dir": actual_files,
        "dir_exists": avatars_dir.exists(),
    })


@app.get("/avatars/{filename:path}")
def get_avatar(filename: str) -> FileResponse:
    """Serve avatar image/video files. Supports nested paths for persona media."""
    import urllib.parse
    decoded_filename = urllib.parse.unquote(filename)
    avatars_dir = paths.data_dir / "avatars"
    avatar_path = avatars_dir / decoded_filename

    # Security check: ensure path is within avatars directory
    try:
        avatar_path.resolve().relative_to(avatars_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid avatar path")

    if not avatar_path.exists() or not avatar_path.is_file():
        raise HTTPException(status_code=404, detail=f"Avatar not found: {decoded_filename}")

    return FileResponse(avatar_path)


@app.get("/admin/persona/list.json")
def list_available_personas() -> JSONResponse:
    personas = list_personas()
    current_persona_id = assistant.settings.current_persona_id
    return JSONResponse(
        {
            "ok": True,
            "personas": personas,
            "current_persona_id": current_persona_id,
        }
    )


@app.get("/admin/persona/debug.json")
def debug_persona_system_prompt() -> JSONResponse:
    """Debug endpoint to check what system prompt is being used by the LLM."""
    diagnostic = assistant.get_system_prompt_diagnostic()
    return JSONResponse(
        {
            "ok": True,
            "diagnostic": diagnostic,
        }
    )


@app.post("/admin/models/manage")
async def manage_models(
    preset_id: str = Form(...),
    action: str = Form(...),
    redirect_to: str = Form("/admin"),
) -> RedirectResponse:
    preset = get_model_preset(preset_id)
    if preset is None:
        return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)

    if action == "apply":
        assistant.ollama_manager.apply_preset(preset)
        assistant.runtime_status.mark_setup_step(
            "preset_selected",
            completed=True,
            detail=f"Applied preset '{preset.name}'.",
        )
    elif action == "install":
        assistant.ollama_manager.install_preset_async(preset)
        assistant.runtime_status.mark_setup_step(
            "preset_selected",
            completed=True,
            detail=f"Selected preset '{preset.name}' for installation.",
        )
        assistant.runtime_status.mark_setup_step(
            "models_requested",
            completed=True,
            detail=f"Started Ollama install for '{preset.name}'.",
        )

    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/models/pull")
async def pull_model(
    model_name: str = Form(...),
    redirect_to: str = Form("/admin"),
) -> RedirectResponse:
    success, message = assistant.ollama_manager.pull_model(model_name)
    if not success:
        # Could add flash message, but for now just redirect
        pass
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/models/set_active")
async def set_active_models(
    chat_model: str = Form(...),
    embed_model: str = Form(...),
    redirect_to: str = Form("/admin"),
) -> RedirectResponse:
    persist_settings(assistant.paths, {
        "ollama_chat_model": chat_model,
        "ollama_embed_model": embed_model,
    })
    assistant.reload_settings()
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/admin/piper/configure")
async def configure_piper(
    piper_command: str = Form(""),
    piper_model_path: str = Form(""),
    enable_voice: str = Form("0"),
    redirect_to: str = Form("/admin"),
) -> RedirectResponse:
    updates = {
        "piper_command": (piper_command or "").strip() or "piper",
        "piper_model_path": (piper_model_path or "").strip(),
        "enable_voice": str(enable_voice).strip().lower() in {"1", "true", "yes", "on"},
    }
    persist_settings(assistant.paths, updates)
    assistant.reload_settings()
    assistant.runtime_status.mark_setup_step(
        "piper_configured",
        completed=bool(assistant.settings.piper_model_path),
        detail=f"Piper command set to '{assistant.settings.piper_command}'.",
    )
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/setup/piper/autodetect")
async def autodetect_piper(
    redirect_to: str = Form("/setup"),
) -> RedirectResponse:
    detected_command = _autodetect_piper_command()
    updates = {}
    if detected_command:
        updates["piper_command"] = detected_command
    detected_model = _autodetect_piper_model_path()
    if detected_model:
        updates["piper_model_path"] = detected_model
    if updates:
        persist_settings(assistant.paths, updates)
        assistant.reload_settings()
    diagnostics = assistant.diagnostics()
    piper_ready = bool(diagnostics.get("piper", {}).get("binary", "")) and bool(diagnostics.get("piper", {}).get("model_exists", False))
    assistant.runtime_status.mark_setup_step(
        "piper_autodetected",
        completed=bool(detected_command or detected_model),
        detail=f"Auto-detect checked Piper command '{assistant.settings.piper_command}'.",
    )
    assistant.runtime_status.mark_setup_step(
        "piper_configured",
        completed=piper_ready,
        detail=(
            "Piper executable and voice model were found."
            if piper_ready
            else f"Piper still needs attention. Current command: '{assistant.settings.piper_command}'."
        ),
    )
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/setup/piper/helper")
async def build_piper_helper(
    redirect_to: str = Form("/setup"),
) -> RedirectResponse:
    helper_path = _ensure_windows_piper_helper()
    assistant.runtime_status.set_helper_script(
        path=str(helper_path),
        ready=True,
        note="Windows Piper helper script generated.",
    )
    assistant.runtime_status.mark_setup_step(
        "piper_helper_ready",
        completed=True,
        detail=f"Helper script saved to {helper_path}.",
    )
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/setup/voice/test")
async def test_voice(
    redirect_to: str = Form("/setup"),
) -> RedirectResponse:
    settings = assistant.settings
    piper = PiperTTS(
        command=settings.piper_command,
        model_path=settings.piper_model_path,
        device=settings.piper_device,
    )

    def _run_test() -> None:
        try:
            piper.speak("Archiveum voice test. Piper is connected and ready.")
        except Exception:
            pass

    threading.Thread(target=_run_test, daemon=True).start()
    assistant.runtime_status.mark_setup_step(
        "voice_tested",
        completed=bool(assistant.diagnostics().get("voice_ready", False)),
        detail="Started Piper voice test playback.",
    )
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/voice/start")
async def start_voice(
    redirect_to: str = Form("/"),
    session_id: str = Form(""),
    session_id_cookie: str = Cookie(default=""),
    admin_access: str = Cookie(default=""),
) -> RedirectResponse:
    runtime = _get_voice_runtime()
    effective_session_id = session_id or session_id_cookie
    if assistant.settings.public_mode and admin_access != "granted":
        runtime.bind_public_session(effective_session_id)
    else:
        runtime.bind_public_session("")
    started, detail = runtime.start()
    assistant.runtime_status.mark_setup_step(
        "voice_tested",
        completed=started,
        detail=detail,
    )
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/voice/stop")
async def stop_voice(
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    runtime = _get_voice_runtime()
    runtime.stop()
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/voice/output")
async def set_voice_output_mode(
    speak_responses: str = Form("1"),
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    enabled = str(speak_responses).strip().lower() in {"1", "true", "yes", "on"}
    persist_settings(assistant.paths, {"speak_responses": enabled})
    assistant.reload_settings()
    runtime = _get_voice_runtime()
    runtime.refresh_settings()
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/voice/interrupt")
async def interrupt_speech(
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    runtime = _get_voice_runtime()
    runtime.stop_speaking()
    try:
        _get_web_reply_tts().stop()
    except Exception:
        pass
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/chat/history/clear")
async def clear_chat_history(
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    _write_chat_history([])
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/chat/session/clear")
async def clear_session_chat(
    session_id: str = Form(""),
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    """Clear chat history for a specific session (Public Mode)."""
    if session_id:
        success = session_manager.clear_chat_history(session_id)
        if success:
            # Add clear_chat=1 to ensure empty chat messages render
            return RedirectResponse(
                url=_safe_redirect_target(redirect_to, {"message": "Chat cleared", "clear_chat": "1"}),
                status_code=303,
            )
    return RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"error": "Could not clear chat", "clear_chat": "1"}),
        status_code=303,
    )


@app.post("/chat/session/new")
async def start_new_session(
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    """Start a brand-new public session for the next conversation."""
    session = session_manager.create_session()
    response = RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"message": "Started a new chat"}),
        status_code=303,
    )
    response.set_cookie(
        key="session_id",
        value=session.session_id,
        httponly=True,
        samesite="strict",
        max_age=86400,
    )
    return response


def _delete_chat_history_items(chat_ids: list[str]) -> None:
    if not chat_ids:
        return
    history = _read_chat_history()
    filtered = [item for item in history if str(item.get("id")) not in chat_ids]
    _write_chat_history(filtered)


def _update_chat_history_item(chat_id: str, question: str, answer: str) -> None:
    history = _read_chat_history()
    updated = False
    for item in history:
        if str(item.get("id")) == chat_id:
            item["question"] = (question or "").strip()
            item["answer"] = (answer or "").strip()
            updated = True
            break
    if updated:
        _write_chat_history(history)


@app.post("/chat/history/delete")
async def delete_chat_history(
    chat_id: str = Form(...),
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    _delete_chat_history_items([chat_id])
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/chat/history/delete_selected")
async def delete_selected_chat_history(
    selected_ids: list[str] = Form([]),
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    _delete_chat_history_items(selected_ids)
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.post("/chat/history/edit")
async def edit_chat_history(
    chat_id: str = Form(...),
    question: str = Form(""),
    answer: str = Form(""),
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    _update_chat_history_item(chat_id, question, answer)
    return RedirectResponse(url=_safe_redirect_target(redirect_to), status_code=303)


@app.get("/voice/status")
def voice_status(
    session_id: str = Cookie(default=""),
    admin_access: str = Cookie(default=""),
) -> dict:
    runtime = _get_voice_runtime()
    diagnostics = assistant.diagnostics()
    if assistant.settings.public_mode and admin_access != "granted":
        runtime.bind_public_session(session_id)
    else:
        runtime.bind_public_session("")
    snapshot = runtime.status_snapshot()
    history_html = ""
    if assistant.settings.public_mode and admin_access != "granted" and session_id:
        history_html = _render_previous_chats(session_manager.get_chat_history(session_id), public_mode=True)
    elif not assistant.settings.public_mode or admin_access == "granted":
        history_html = _render_previous_chats(_read_chat_history())
    return {
        "running": bool(snapshot.get("running", False)),
        "command_listener_running": bool(snapshot.get("command_listener_running", False)),
        "tts_speaking": bool(snapshot.get("tts_speaking", False)),
        "status_message": str(snapshot.get("status_message", "") or ""),
        "last_transcript": str(snapshot.get("last_transcript", "") or ""),
        "last_response": str(snapshot.get("last_response", "") or ""),
        "last_error": str(snapshot.get("last_error", "") or ""),
        "ready": bool(diagnostics.get("ready", False)),
        "voice_ready": bool(diagnostics.get("voice_ready", False)),
        "indexed_documents": diagnostics["index"]["indexed_documents"],
        "indexed_chunks": diagnostics["index"]["indexed_chunks"],
        "history_html": history_html,
        "active_public_session_id": str(snapshot.get("active_public_session_id", "") or ""),
    }


# ============================================================================
# PUBLIC MODE ENDPOINTS
# ============================================================================

@app.get("/session/init")
def init_session(response: Response) -> dict:
    """Initialize a new session for Public Mode. Sets session cookie."""
    session = session_manager.create_session()
    # Set session cookie (HttpOnly, Secure, SameSite for security)
    response.set_cookie(
        key="session_id",
        value=session.session_id,
        httponly=True,
        samesite="strict",
        max_age=86400,  # 24 hours
    )
    return {
        "ok": True,
        "session_id": session.session_id,
        "message": "Session created",
    }


@app.get("/mode/status")
def mode_status() -> dict:
    """Get current mode status (Public vs Admin) and persona info."""
    settings = assistant.settings
    return {
        "ok": True,
        "public_mode": settings.public_mode,
        "public_mode_persona_id": settings.public_mode_persona_id,
        "current_persona_id": settings.current_persona_id,
        "has_admin_password": bool(settings.admin_password_hash),
        "session_timeout_minutes": settings.session_timeout_minutes,
    }


@app.post("/mode/admin-access")
async def admin_access(
    password: str = Form(...),
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    """Authenticate admin access from Public Mode and disable Public Mode."""
    settings = assistant.settings
    
    # Verify password
    if not settings.admin_password_hash:
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"error": "Admin password not set"}),
            status_code=303,
        )
    
    if not verify_password(password, settings.admin_password_hash):
        return RedirectResponse(
            url=_safe_redirect_target(redirect_to, {"error": "Invalid password"}),
            status_code=303,
        )
    
    # Disable Public Mode when gaining admin access
    if settings.public_mode:
        persist_settings(assistant.paths, {"public_mode": False})
        assistant.reload_settings()
    
    # Create response with redirect and set admin access cookie (1 hour expiry)
    response = RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"message": "Admin access granted. Public Mode disabled."}),
        status_code=303,
    )
    response.set_cookie(
        key="admin_access",
        value="granted",
        httponly=True,
        samesite="strict",
        max_age=3600,
    )
    
    return response


@app.post("/mode/switch")
async def switch_mode(
    public_mode: str = Form(...),  # "true" or "false"
    admin_password: str = Form(""),
    redirect_to: str = Form("/"),
) -> RedirectResponse:
    """Switch between Public and Admin modes."""
    settings = assistant.settings
    new_public_mode = public_mode.lower() == "true"
    
    # If switching to admin mode, verify password
    if not new_public_mode and settings.admin_password_hash:
        if not verify_password(admin_password, settings.admin_password_hash):
            return RedirectResponse(
                url=_safe_redirect_target(redirect_to, {"error": "Invalid admin password"}),
                status_code=303,
            )
    
    # Persist mode change
    persist_settings(assistant.paths, {"public_mode": new_public_mode})
    assistant.reload_settings()
    
    mode_name = "Public" if new_public_mode else "Admin"
    response = RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"message": f"Switched to {mode_name} mode"}),
        status_code=303,
    )
    
    # When entering Public Mode, clear admin access cookie to show restricted view
    if new_public_mode:
        response.delete_cookie(key="admin_access")
    
    return response


@app.post("/admin/set-password")
async def set_admin_password(
    current_password: str = Form(""),
    new_password: str = Form(...),
    redirect_to: str = Form("/admin"),
) -> RedirectResponse:
    """Set or change the admin password."""
    settings = assistant.settings
    
    # If password already set, verify current password first
    if settings.admin_password_hash:
        if not verify_password(current_password, settings.admin_password_hash):
            return RedirectResponse(
                url=_safe_redirect_target(redirect_to, {"error": "Current password incorrect"}),
                status_code=303,
            )
    
    # Hash and save new password
    password_hash = hash_password(new_password)
    persist_settings(assistant.paths, {"admin_password_hash": password_hash})
    assistant.reload_settings()
    
    return RedirectResponse(
        url=_safe_redirect_target(redirect_to, {"message": "Admin password set successfully"}),
        status_code=303,
    )


@app.get("/session/stats")
def session_stats(admin_access: str = Cookie(default="")) -> dict:
    """Get session manager statistics (admin only)."""
    if admin_access != "granted" and assistant.settings.public_mode:
        return {"ok": False, "error": "Admin access required"}
    
    return {
        "ok": True,
        "stats": session_manager.get_stats(),
    }


# ============================================================================
# AVATAR ENDPOINTS
# ============================================================================

@app.get("/avatar/emotional")
def get_emotional_avatar(message: str = Query(...), persona_id: str = Query("")) -> JSONResponse:
    """
    Get avatar HTML for a given message and persona.
    Analyzes emotion and returns the appropriate avatar.
    """
    target_persona = persona_id or (assistant.settings.current_persona_id or "nova")
    avatar_html = _get_emotional_avatar_html(target_persona, message)
    emotion = _analyze_emotion_simple(message)
    return JSONResponse({
        "ok": True,
        "emotion": emotion,
        "avatar_html": avatar_html,
        "persona_id": target_persona,
    })


@app.get("/analyze/emotion")
def analyze_emotion_endpoint(message: str = Query(...)) -> JSONResponse:
    """Simple endpoint to analyze message emotion."""
    emotion = _analyze_emotion_simple(message)
    return JSONResponse({
        "ok": True,
        "emotion": emotion,
        "message_preview": message[:50] + "..." if len(message) > 50 else message,
    })


@app.get("/test/emotion")
def test_emotion_detection() -> JSONResponse:
    """Test endpoint with sample messages to verify emotion detection."""
    test_cases = [
        ("I love this! It's amazing!", "happy"),
        ("This is terrible and awful", "angry"),
        ("I miss my friend so much", "sad"),
        ("Can you explain how this works?", "neutral"),
        ("What a beautiful day!", "happy"),
        ("I'm furious about this mistake", "angry"),
    ]
    results = []
    for message, expected in test_cases:
        detected = _analyze_emotion_simple(message)
        results.append({
            "message": message,
            "expected": expected,
            "detected": detected,
            "match": detected == expected
        })
    return JSONResponse({
        "ok": True,
        "test_cases": results,
        "passed": sum(1 for r in results if r["match"]),
        "total": len(results)
    })


@app.get("/chat/history/{chat_id}")
def get_chat_history(chat_id: str) -> JSONResponse:
    messages = _get_chat_messages(chat_id)
    if messages is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Chat not found"})
    return JSONResponse({"ok": True, "messages": messages})


@app.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    category: str = Form("factual/current_reading"),
) -> RedirectResponse:
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    safe_category = _safe_upload_category(category)
    destination = paths.uploads_dir / safe_category / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(await file.read())

    try:
        chunk_count = assistant.ingest_file(destination, source_name=destination.relative_to(paths.uploads_dir).as_posix())
    except Exception as exc:
        return HTMLResponse(
            content=_render_page(error=f"Could not index {filename}: {exc}"),
            status_code=400,
        )

    if not chunk_count:
        return HTMLResponse(
            content=_render_page(error=f"Could not index {filename}: the file did not contain usable text."),
            status_code=400,
        )

    return RedirectResponse(url="/", status_code=303)


@app.post("/chat", response_class=HTMLResponse)
async def chat(
    question: str = Form(...),
    session_id: str = Form(""),
    admin_access: str = Cookie(default=""),
) -> str:
    question = question.strip()
    if not question:
        return _render_page(error="Ask a question first.", session_id=session_id)

    settings = assistant.settings
    is_public_user = settings.public_mode and admin_access != "granted"
    recent_chats_override = None

    if is_public_user:
        session = session_manager.get_session(session_id)
        if session is None:
            session = session_manager.create_session()
            session_id = session.session_id
        recent_chats_override = _get_recent_session_chats_for_prompt(session.chat_history)
        active_persona_id = settings.public_mode_persona_id
    else:
        active_persona_id = settings.current_persona_id or "nova"

    # Use unified conversation pipeline
    result = submit_conversation_turn(
        question,
        source="text",
        skip_history=is_public_user,
        memory_context_override="" if is_public_user else None,
        recent_chats_override=recent_chats_override if is_public_user else _get_recent_chats_for_prompt(limit=5, persona_id=active_persona_id),
        persona_id_override=active_persona_id,
        prefer_archive_retrieval=False,  # Public Mode: only explicit archive triggers
    )

    if result.get("error"):
        return _render_page(question=question, error=result["error"], session_id=session_id)

    # Speak the answer if voice output is enabled
    answer = result.get("answer", "")
    if assistant.settings.speak_responses:
        _speak_answer_async(answer, active_persona_id)

    # Update avatar based on emotion detection
    effective_persona_id = settings.public_mode_persona_id if is_public_user else (settings.current_persona_id or "nova")
    avatar_html = _get_emotional_avatar_html(
        effective_persona_id,
        answer,
        size="chat_portrait"
    )

    # Build context for history
    context = result.get("context", "")

    # Append to chat history (session-specific in Public Mode)
    if settings.public_mode and session_id:
        session_manager.add_chat_message(session_id, "user", question, "", "text")
        session_manager.add_chat_message(session_id, "assistant", answer, context, "text")
    else:
        _record_chat_history(question, answer, context)

    # Return the same page with the chat thread
    return _render_page(
        chat_messages=[
            {"role": "user", "text": question, "source": "text"},
            {
                "role": "assistant",
                "text": answer,
                "context": context,
                "source": "text",
            },
        ],
        session_id=session_id,
    )


@app.post("/chat.json")
async def chat_json(
    question: str = Form(...),
    session_id: str = Form(""),
    admin_access: str = Cookie(default=""),
) -> JSONResponse:
    query = question.strip()
    if not query:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Ask a question first."})

    settings = assistant.settings
    is_public_user = settings.public_mode and admin_access != "granted"
    recent_chats_override = None
    created_session = False
    active_persona_id = settings.public_mode_persona_id if is_public_user else (settings.current_persona_id or "nova")

    if is_public_user:
        session = session_manager.get_session(session_id)
        if session is None:
            session = session_manager.create_session()
            session_id = session.session_id
            created_session = True
        recent_chats_override = _get_recent_session_chats_for_prompt(session.chat_history)

    # Use unified conversation pipeline
    result = submit_conversation_turn(
        query,
        source="text",
        skip_history=is_public_user,
        memory_context_override="" if is_public_user else None,
        recent_chats_override=recent_chats_override if is_public_user else _get_recent_chats_for_prompt(limit=5, persona_id=active_persona_id),
        persona_id_override=active_persona_id,
        prefer_archive_retrieval=False,  # Public Mode: only explicit archive triggers
    )

    if result.get("error"):
        return JSONResponse(status_code=500, content={"ok": False, "error": result["error"]})

    answer = result["answer"]
    context = result["context"]

    if is_public_user and session_id:
        session_manager.add_chat_message(session_id, "user", query, "", "text")
        session_manager.add_chat_message(session_id, "assistant", answer, context, "text")
        history_html = _render_previous_chats(session_manager.get_chat_history(session_id), public_mode=True)
    else:
        history_html = _render_previous_chats(_read_chat_history())

    response = JSONResponse(
        {
            "ok": True,
            "question": query,
            "answer": answer,
            "context": context,
            "source": "text",
            "session_id": session_id,
            "history_html": history_html,
        }
    )
    if created_session:
        response.set_cookie(
            key="session_id",
            value=session_id,
            httponly=True,
            samesite="strict",
            max_age=86400,
        )
    return response



def _render_page(
    *,
    question: str = "",
    error: str = "",
    chat_messages: list[dict] | None = None,
    session_id: str = "",
    admin_access: str = "",
) -> str:
    diagnostics = assistant.diagnostics()
    settings = assistant.settings
    
    # Public Mode configuration
    is_public_mode = settings.public_mode
    has_admin_access = admin_access == "granted"
    is_public_user = is_public_mode and not has_admin_access
    
    # In Public Mode without admin access, use fixed persona
    if is_public_user:
        effective_persona_id = settings.public_mode_persona_id
    else:
        effective_persona_id = settings.current_persona_id or "nova"
    
    voice_controls = _render_voice_controls_compact()
    model_warning_block = _render_model_warning(diagnostics)
    
    # Load chat history: session-specific for Public Mode, global for Admin Mode
    if is_public_user and session_id:
        # Get session-specific chat history
        session = session_manager.get_session(session_id)
        if session:
            history_items = session.chat_history
        else:
            history_items = []
    else:
        # Admin mode: use global chat history
        history_items = _read_chat_history()

    # If no specific chat messages provided, load the existing conversation state
    if chat_messages is None:
        if is_public_user and history_items and "role" in history_items[0]:
            chat_messages = [
                {
                    "role": str(item.get("role", "") or ""),
                    "text": str(item.get("text", "") or ""),
                    "context": str(item.get("context", "") or ""),
                    "source": str(item.get("source", "text") or "text"),
                }
                for item in history_items
                if str(item.get("text", "") or "").strip()
            ]
        elif history_items:
            most_recent = history_items[0]
            chat_messages = [
                {
                    "role": "user",
                    "text": most_recent.get("question", ""),
                    "source": most_recent.get("source", "text"),
                },
                {
                    "role": "assistant",
                    "text": most_recent.get("answer", ""),
                    "context": most_recent.get("context", ""),
                    "source": most_recent.get("source", "text"),
                },
            ]
        else:
            chat_messages = []
    source_rows = _render_home_indexed_sources(assistant.store.list_sources())
    status_rows = _render_home_status_cards(diagnostics)
    ingestion_error_rows = _render_home_ingestion_errors(diagnostics["index"].get("recent_ingestion_errors", []))
    history_block = _render_previous_chats(history_items, public_mode=is_public_user)
    upload_category_options_html = _render_upload_category_options()
    chat_thread = _render_chat_thread(chat_messages)
    error_block = f"<p class='error'>{escape(error)}</p>" if error else ""
    initial_messages_json = json.dumps(chat_messages, ensure_ascii=False)
    
    # Public Mode: determine what to show/hide
    is_public_mode = settings.public_mode
    has_admin_access = admin_access == "granted"
    is_public_user = is_public_mode and not has_admin_access
    effective_persona_id = settings.public_mode_persona_id if is_public_user else (settings.current_persona_id or "nova")
    
    # Session ID for forms (Public Mode)
    session_input = f'<input type="hidden" name="session_id" value="{escape(session_id)}">' if is_public_user else ""
    
    # Navigation: hide admin links in Public Mode
    if is_public_user:
        nav_html = ''
    else:
        nav_html = '''<nav class="nav-links">
      <a href="/">Home</a>
      <a href="/setup">Setup</a>
      <a href="/admin">Admin</a>
      <a href="/admin/library">Library</a>
      <a href="/admin/persona">Persona</a>
      <a href="/status">Status</a>
    </nav>'''
    
    # Sidebar: hide in Public Mode
    sidebar_html = ""
    if not is_public_user:
        sidebar_html = f'''<aside class="sidebar-shell">
        <section class="sidebar-panel sidebar-panel-upload">
          <div class="sidebar-head">
            <div>
              <p class="sidebar-eyebrow">Library</p>
              <h2>Add Files</h2>
            </div>
            <a class="chip-link" href="/admin/library">Manage</a>
          </div>
          <p class="muted sidebar-copy">Pick a shelf, drop in a file, and Archiveum will index it locally.</p>
          <form action="/upload" method="post" enctype="multipart/form-data" class="sidebar-form">
            <label>
              Upload shelf
              <select name="category">
                {upload_category_options_html}
              </select>
            </label>
            <input type="file" name="file" required>
            <button class="button-compact" type="submit">Index File</button>
          </form>
        </section>

        <section class="sidebar-panel">
          <div class="sidebar-head">
            <div>
              <p class="sidebar-eyebrow">Health</p>
              <h2>Runtime Status</h2>
            </div>
            <a class="chip-link" href="/status">Full Status</a>
          </div>
          <div class="status-card-grid">{status_rows}</div>
        </section>

        <section class="sidebar-panel">
          <div class="sidebar-head">
            <div>
              <p class="sidebar-eyebrow">Index</p>
              <h2>Indexed Resources</h2>
            </div>
            <span class="badge">{escape(str(diagnostics['index']['indexed_documents']))} docs</span>
          </div>
          <div class="resource-list">{source_rows}</div>
        </section>

        <section class="sidebar-panel">
          <div class="sidebar-head">
            <div>
              <p class="sidebar-eyebrow">Ingestion</p>
              <h2>Recent Errors</h2>
            </div>
          </div>
          <div class="error-compact-list">{ingestion_error_rows}</div>
        </section>
      </aside>'''
    
    # Admin access entry point (hidden corner button for Public Mode)
    admin_access_html = ""
    if is_public_user and settings.admin_password_hash:
        admin_access_html = '''
    <div id="admin-access-trigger" style="position: fixed; bottom: 10px; right: 10px; width: 20px; height: 20px; cursor: pointer; z-index: 9999; opacity: 0.3;" title="Admin Access (click 5 times)"></div>
    <div id="admin-password-modal" style="display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 10000; justify-content: center; align-items: center;">
      <div style="background: white; padding: 20px; border-radius: 12px; max-width: 400px;">
        <h3>Admin Access</h3>
        <p class="muted">Enter admin password to access configuration.</p>
        <form action="/mode/admin-access" method="post" id="admin-access-form">
          <input type="hidden" name="redirect_to" value="/">
          <label>Password <input type="password" name="password" required style="width: 100%;"></label>
          <div style="margin-top: 12px;">
            <button type="submit" class="button-compact">Access Admin</button>
            <button type="button" class="button-compact" onclick="document.getElementById('admin-password-modal').style.display='none'">Cancel</button>
          </div>
        </form>
      </div>
    </div>
    <script>
    (function() {
      let clickCount = 0;
      let lastClick = 0;
      const trigger = document.getElementById('admin-access-trigger');
      const modal = document.getElementById('admin-password-modal');
      trigger.addEventListener('click', function() {
        const now = Date.now();
        if (now - lastClick > 2000) clickCount = 0;
        clickCount++;
        lastClick = now;
        if (clickCount >= 5) {
          modal.style.display = 'flex';
          clickCount = 0;
        }
      });
    })();
    </script>'''
    
    # Public session controls
    session_controls_html = ""
    if is_public_user:
        session_controls_html = f'''<div class="button-row" style="margin-bottom: 12px; gap: 8px; flex-wrap: wrap;">
          <form action="/chat/session/new" method="post" style="display: inline;">
            <input type="hidden" name="redirect_to" value="/">
            <button type="submit" class="button-compact">Start New Chat</button>
          </form>
          <form action="/chat/session/clear" method="post" style="display: inline;">
            {session_input}
            <input type="hidden" name="redirect_to" value="/">
            <button type="submit" class="button-compact">Clear Chat</button>
          </form>
        </div>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiveum</title>
  {_shared_styles()}
</head>
<body{' class="public-mode"' if is_public_user else ''}>
  <main>
    <section class="hero">
      <span class="tag">Archiveum</span>
      <h1>Living Archive Companion</h1>
    </section>
    {model_warning_block}

    {nav_html}

    <section class="layout">
      {sidebar_html}

      <div class="main-content-two-col">
        <div class="main-col-left">
          <section class="panel chat-panel">
            <div class="voice-controls-row">
              {voice_controls}
              <!-- Mobile avatar for Admin Mode -->
              {'' if is_public_user else f"""<div class="mobile-admin-avatar" onclick="document.getElementById('avatar-lightbox') && document.getElementById('avatar-lightbox').classList.add('active')" title="Click to enlarge">
                {_get_current_persona_avatar_html(size='small')}
              </div>"""}
            </div>
            <h2>Chat With Archiveum</h2>
            <p class="muted">Type below or use voice controls above. Voice and text share the same conversation.</p>
            <div id="chat-thread" class="chat-thread">{chat_thread}</div>
            {error_block}
            {'' if is_public_user else '<div class="button-row" style="margin-bottom: 12px; gap: 8px; flex-wrap: wrap;"><button id="clear-chat-thread" type="button">Clear Current Chat</button></div>'}
            <!-- Persona selector: hidden in Public Mode -->
            {'' if is_public_user else f"""<form action="/admin/persona/select" method="post" style="display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap;">
              <label style="flex: 1 1 260px;">
                Persona
                <select name="persona_id">{_render_persona_options(assistant.settings.current_persona_id or 'nova')}</select>
              </label>
              <input type="hidden" name="redirect_to" value="/">
              <button type="submit">Apply Persona</button>
            </form>"""}
            {session_controls_html}
            <form id="chat-form" action="/chat" method="post">
              {session_input}
              <textarea id="chat-question" name="question" placeholder="What should I know about these files?">{escape(question)}</textarea>
              <button type="submit">Ask</button>
            </form>
          </section>

          <section class="panel panel-recent-chats" style="margin-top: 20px;">
            <h2 style="font-size:1rem;margin:0 0 12px 0;">Recent Chats</h2>
            <div id="recent-chats-history">{history_block}</div>
          </section>
        </div>

        <div class="main-col-right">
          <section class="panel avatar-panel">
            <h2>Current Persona</h2>
            <div id="avatar-container" class="avatar-container">
              {_get_current_persona_avatar_html(size='chat_portrait')}
            </div>
            <p class="muted" style="text-align: center; margin-top: 12px;">{assistant.settings.current_persona_id or 'nova'}</p>
          </section>
        </div>
      </div>
    </section>
  </main>
  <script id="initial-chat-messages" type="application/json">{escape(initial_messages_json)}</script>
  {_render_home_live_script()}
  {_render_chat_script()}
  {_render_avatar_lightbox_script()}
  {admin_access_html}
</body>
</html>"""


def _render_admin_page() -> str:
    diagnostics = assistant.diagnostics()
    sources = assistant.store.list_sources()
    errors = diagnostics["index"].get("recent_ingestion_errors", [])
    model_install = diagnostics["index"].get("model_install", {})
    presets = list_model_presets()
    recommended = recommended_preset_id()
    piper_form = _render_piper_setup_form(diagnostics, redirect_to="/admin")
    
    # Public Mode settings
    settings = assistant.settings
    public_mode_checked = "checked" if settings.public_mode else ""
    has_admin_password = bool(settings.admin_password_hash)
    session_timeout = settings.session_timeout_minutes
    public_persona_id = settings.public_mode_persona_id
    persona_options = _render_persona_options(public_persona_id or "nova")
    
    public_mode_form = f"""
    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <div>
          <h2>Public Mode Settings</h2>
          <p class="muted">Configure locked-down interface for public/shared deployments</p>
        </div>
      </div>
      <form action="/admin/settings/public-mode" method="post" style="display: flex; flex-direction: column; gap: 12px;">
        <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
          <input type="checkbox" name="public_mode" value="true" {public_mode_checked}>
          Enable Public Mode
        </label>
        <p class="muted" style="font-size: 0.85rem; margin-left: 24px;">
          When enabled, users see a simplified chat-only interface with a fixed persona.
        </p>
        
        <label>
          Public Mode Persona
          <select name="public_mode_persona_id">
            {persona_options}
          </select>
        </label>
        <p class="muted" style="font-size: 0.85rem; margin-left: 0;">
          Select which persona to use in Public Mode.
        </p>
        
        <label>
          Session Timeout (minutes)
          <input type="number" name="session_timeout_minutes" value="{session_timeout}" min="1" max="1440" style="width: 100px;">
        </label>
        <p class="muted" style="font-size: 0.85rem; margin-left: 0;">
          How long before inactive sessions expire (1-1440 minutes).
        </p>
        
        <div style="border-top: 1px solid #e5e7eb; padding-top: 12px; margin-top: 8px;">
          <h3 style="font-size: 1rem; margin-bottom: 8px;">Admin Password</h3>
          {"<p class='muted' style='font-size: 0.85rem; color: #22c55e;'>✓ Admin password is set</p>" if has_admin_password else "<p class='muted' style='font-size: 0.85rem; color: #ef4444;'>⚠ Admin password is NOT set - set this before enabling Public Mode!</p>"}
          <label>
            Current Password (if changing)
            <input type="password" name="current_password" placeholder="Leave blank if not set">
          </label>
          <label>
            New Admin Password
            <input type="password" name="new_password" placeholder="Enter new password">
          </label>
          <label>
            Confirm New Password
            <input type="password" name="confirm_password" placeholder="Confirm new password">
          </label>
          <p class="muted" style="font-size: 0.85rem; margin-left: 0;">
            Required to access admin features from Public Mode. Use 8+ characters.
          </p>
        </div>
        
        <button type="submit" class="button-compact" style="align-self: flex-start;">Save Public Mode Settings</button>
      </form>
    </section>
    """
    preset_options = "".join(
        f"<option value=\"{escape(preset['id'])}\" {'selected' if preset['id'] == recommended else ''}>"
        f"{escape(preset['name'])} - {escape(preset['target'])}"
        "</option>"
        for preset in presets
    )
    available_models = "".join(
        f"<li>{escape(model)}</li>"
        for model in diagnostics["ollama"].get("available_models", [])
    ) or "<li>No models reported by Ollama.</li>"

    # Get installed models for the library
    installed_models = assistant.ollama_manager.list_models()
    current_chat = diagnostics['settings']['ollama_chat_model']
    current_embed = diagnostics['settings']['ollama_embed_model']

    library_rows = "".join(
        f"""
        <tr>
          <td>{escape(model['name'])}</td>
          <td>{escape(model['size'])}</td>
          <td>
            <form action="/admin/models/set_active" method="post" style="display: inline;">
              <input type="hidden" name="chat_model" value="{escape(model['name'])}">
              <input type="hidden" name="embed_model" value="{escape(current_embed)}">
              <input type="hidden" name="redirect_to" value="/admin">
              <button type="submit" {'disabled' if model['name'] == current_chat else ''}>
                {'Active' if model['name'] == current_chat else 'Set as Chat'}
              </button>
            </form>
            <form action="/admin/models/set_active" method="post" style="display: inline; margin-left: 8px;">
              <input type="hidden" name="chat_model" value="{escape(current_chat)}">
              <input type="hidden" name="embed_model" value="{escape(model['name'])}">
              <input type="hidden" name="redirect_to" value="/admin">
              <button type="submit" {'disabled' if model['name'] == current_embed else ''}>
                {'Active' if model['name'] == current_embed else 'Set as Embed'}
              </button>
            </form>
          </td>
        </tr>
        """
        for model in installed_models
    ) or "<tr><td colspan='3'>No models installed in Ollama.</td></tr>"

    install_status = _render_install_status(model_install)

    error_cards = "".join(
        f"""
        <article class="error-card">
          <div class="error-meta">
            <strong>{escape(str(item['filename']))}</strong>
            <span>{escape(str(item['ts']))}</span>
          </div>
          <p>{escape(str(item['error']))}</p>
          <form action="/admin/errors/clear" method="post">
            <input type="hidden" name="filename" value="{escape(str(item['filename']))}">
            <button type="submit">Clear This Error</button>
          </form>
        </article>
        """
        for item in errors
    ) or "<p>No recent ingestion errors.</p>"

    source_rows = "".join(
        f"<tr><td>{escape(item['source'])}</td><td>{item['chunks']}</td><td>{escape(str(item['embedding_model']))}</td></tr>"
        for item in sources
    ) or "<tr><td colspan='3'>No indexed sources yet.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiveum Admin</title>
  {_shared_styles()}
</head>
<body>
  <main>
    <section class="hero">
      <span class="tag">Archiveum Admin</span>
      <div>
        <h1>Operations Console</h1>
        <p>This is your control room for Archiveum. You can check what is healthy, see where something needs attention, and make changes without digging through files by hand.</p>
      </div>
    </section>

    <nav class="nav-links">
      <a href="/">Home</a>
      <a href="/setup">Setup</a>
      <a href="/admin">Admin</a>
      <a href="/admin/library">Library</a>
      <a href="/admin/persona">Persona</a>
      <a href="/status">Status</a>
    </nav>

    <section class="admin-grid">
      <section class="panel">
        <h2>Index Summary</h2>
        <p class="muted">Here’s the quick picture of how the archive is doing right now.</p>
        <ul>
          <li><strong>Ready:</strong> {escape(str(diagnostics['ready']))}</li>
          <li><strong>Voice Ready:</strong> {escape(str(diagnostics['voice_ready']))}</li>
          <li><strong>Indexed Documents:</strong> {escape(str(diagnostics['index']['indexed_documents']))}</li>
          <li><strong>Indexed Chunks:</strong> {escape(str(diagnostics['index']['indexed_chunks']))}</li>
          <li><strong>Last Updated:</strong> {escape(str(diagnostics['index']['last_updated']))}</li>
        </ul>
      </section>

      <section class="panel">
        <h2>Runtime Checks</h2>
        <p class="muted">If something is not working yet, this section usually shows the reason.</p>
        <ul>
          <li><strong>Chat Model:</strong> {escape(diagnostics['settings']['ollama_chat_model'])}</li>
          <li><strong>Embed Model:</strong> {escape(diagnostics['settings']['ollama_embed_model'])}</li>
          <li><strong>Piper:</strong> {escape(diagnostics['piper']['detail'])}</li>
          <li><strong>Piper Command:</strong> {escape(diagnostics['settings']['piper_command'])}</li>
          <li><strong>Piper Hint:</strong> {escape(diagnostics['piper']['hint'])}</li>
          <li><strong>Audio:</strong> {escape(diagnostics['audio']['detail'])}</li>
        </ul>
      </section>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Model Presets</h2>
        <span class="badge">Recommended: {escape(recommended)}</span>
      </div>
      <p>If you want Archiveum to choose a sensible model pairing for this machine, start here. You can apply a preset first or go straight into installation.</p>
      <form action="/admin/models/manage" method="post">
        <select name="preset_id">{preset_options}</select>
        <div class="button-row">
          <button type="submit" name="action" value="apply">Apply Preset</button>
          <button type="submit" name="action" value="install">Install With Ollama</button>
        </div>
      </form>
      {install_status}
      <h2 style="margin-top: 24px;">LLM Library</h2>
      <p class="muted">Manage your downloaded models and switch between them for chat and embeddings.</p>
      <table class="model-table">
        <thead>
          <tr>
            <th>Model Name</th>
            <th>Size</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {library_rows}
        </tbody>
      </table>
      <div style="margin-top: 16px;">
        <form action="/admin/models/pull" method="post" style="display: inline-block;">
          <input type="hidden" name="redirect_to" value="/admin">
          <label>
            Pull new model:
            <input type="text" name="model_name" placeholder="e.g., llama2:7b or author/model:tag" required>
          </label>
          <button type="submit">Pull Model</button>
        </form>
      </div>
      {install_status}
      <p class="muted">Current Chat Model: <strong>{escape(current_chat)}</strong> | Current Embed Model: <strong>{escape(current_embed)}</strong></p>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Windows Piper Setup</h2>
        <span class="badge">{escape(diagnostics['piper']['platform'])}</span>
      </div>
      <p>Voice setup can feel fiddly, so this section keeps it simple. Once Piper is installed, you can point Archiveum to it here and pick the voice model you want to use.</p>
      <ol class="steps">
        <li>Install Piper for Windows and find <code>piper.exe</code>.</li>
        <li>Add it to <code>PATH</code> or paste the full executable path below.</li>
        <li>Choose one of the bundled Archiveum voice models, or use a full <code>.onnx</code> path.</li>
        <li>Enable voice mode and save the settings when you’re ready.</li>
      </ol>
      {piper_form}
    </section>

    {public_mode_form}

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Ingestion Errors</h2>
        <form action="/admin/errors/clear" method="post">
          <button type="submit">Clear All Errors</button>
        </form>
      </div>
      <p class="muted">If a file upload or ingest step failed, you’ll see it here with the reason.</p>
      <div class="error-list">
        {error_cards}
      </div>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <h2>Indexed Sources</h2>
      <p class="muted">This is the current list of files Archiveum has already broken into chunks and indexed.</p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Chunks</th>
              <th>Embedding Model</th>
            </tr>
          </thead>
          <tbody>
            {source_rows}
          </tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""


def _render_library_page() -> str:
    library_items = _library_documents()
    custom_categories = _load_custom_upload_categories()
    default_category_rows = "".join(
        f"<li><strong>{escape(label)}</strong> — <code>{escape(path)}</code></li>"
        for path, label in default_upload_category_options()
    )
    custom_category_rows = "".join(
        f"""
        <article class="library-shelf-card">
          <div class="library-meta">
            <div>
              <h3>{escape(item['label'])}</h3>
              <p class="muted">{escape(item['path'])}</p>
            </div>
          </div>
          <div class="library-actions">
            <form action="/admin/library/category/rename" method="post">
              <input type="hidden" name="category" value="{escape(item['path'])}">
              <label>
                New title
                <input type="text" name="new_label" placeholder="{escape(item['label'])}">
              </label>
              <label>
                New folder name
                <input type="text" name="new_path" placeholder="{escape(item['path'])}">
              </label>
              <button type="submit">Rename shelf</button>
            </form>
            <form action="/admin/library/category/delete" method="post" style="margin-top: 10px;">
              <input type="hidden" name="category" value="{escape(item['path'])}">
              <button type="submit">Delete shelf</button>
            </form>
          </div>
        </article>
        """
        for item in custom_categories
    ) or "<p class='muted'>No custom shelves yet.</p>"

    rows = "".join(
        f"""
        <article class="library-card">
          <div class="library-meta">
            <div>
              <h3>{escape(item['name'])}</h3>
              <p class="muted">{escape(item['relative_path'])}</p>
              <p class="muted">Indexed chunks: {escape(str(item['chunks']))} | Model: {escape(item['embedding_model'])} | Size: {escape(item['size_label'])}</p>
            </div>
          </div>
          <div class="library-actions">
            <form action="/admin/library/rename" method="post">
              <input type="hidden" name="source" value="{escape(item['relative_path'])}">
              <input type="hidden" name="redirect_to" value="/admin/library">
              <label>
                Rename file
                <input type="text" name="new_name" value="{escape(item['name'])}">
              </label>
              <button type="submit">Rename</button>
            </form>
            <form action="/admin/library/move" method="post">
              <input type="hidden" name="source" value="{escape(item['relative_path'])}">
              <input type="hidden" name="redirect_to" value="/admin/library">
              <label>
                Move to shelf
                <select name="category">{_render_upload_category_options(item['category'])}</select>
              </label>
              <button type="submit">Move</button>
            </form>
            <form action="/admin/library/reindex" method="post">
              <input type="hidden" name="source" value="{escape(item['relative_path'])}">
              <input type="hidden" name="redirect_to" value="/admin/library">
              <button type="submit">Re-index</button>
            </form>
            <form action="/admin/library/delete" method="post">
              <input type="hidden" name="source" value="{escape(item['relative_path'])}">
              <input type="hidden" name="redirect_to" value="/admin/library">
              <button type="submit">Delete</button>
            </form>
          </div>
        </article>
        """
        for item in library_items
    ) or "<p>No uploaded files found yet.</p>"

    category_management_section = f"""
    <section class="panel">
      <div class="section-head">
        <h2>Manage Upload Shelves</h2>
        <span class="badge">{len(custom_categories)} custom shelf(s)</span>
      </div>
      <p class="muted">Create custom shelves for uploads, rename them, or remove them when they are no longer needed.</p>
      <div class="library-list">
        <div class="library-card" style="padding: 20px;">
          <h3>Built-in shelves</h3>
          <ul>{default_category_rows}</ul>
        </div>
        <div class="library-card" style="padding: 20px;">
          <h3>Custom shelves</h3>
          {custom_category_rows}
        </div>
      </div>
      <form action="/admin/library/category/create" method="post" style="margin-top: 20px;">
        <label>
          Shelf title
          <input type="text" name="label" placeholder="My Notes" required>
        </label>
        <label>
          Folder name (optional)
          <input type="text" name="path" placeholder="my_notes">
        </label>
        <button type="submit">Create New Shelf</button>
      </form>
    </section>
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiveum Library</title>
  {_shared_styles()}
</head>
<body>
  <main>
    <section class="hero">
      <span class="tag">Archiveum Library</span>
      <div>
        <h1>Document Organizer</h1>
        <p>This page helps you keep the archive tidy. You can rename uploaded files, move them between shelves, or remove them completely without editing folders by hand.</p>
      </div>
    </section>

    <nav class="nav-links">
      <a href="/">Home</a>
      <a href="/setup">Setup</a>
      <a href="/admin">Admin</a>
      <a href="/admin/library">Library</a>
      <a href="/admin/persona">Persona</a>
      <a href="/status">Status</a>
    </nav>

    {category_management_section}

    <section class="panel">
      <div class="section-head">
        <h2>Uploaded Documents</h2>
        <span class="badge">{len(library_items)} file(s)</span>
      </div>
      <p class="muted">Archiveum will keep the index in step with these changes, so moving or renaming a file here updates the indexed source too.</p>
      <div class="library-list">
        {rows}
      </div>
    </section>
  </main>
</body>
</html>"""


def _render_persona_page(message: str = "") -> str:
    diagnostics = assistant.diagnostics()
    current_prompt = str(diagnostics["settings"].get("custom_system_prompt", "") or "")
    current_persona_id = (diagnostics["settings"].get("current_persona_id", "") or "nova").strip()
    confirmation_block = (
        f"<div class='banner success'>{escape(message)}</div>" if message else ""
    )
    personas = list_personas()
    built_in_ids = {"nova", "researcher", "storyteller", "gentle_companion"}
    built_in_personas = [p for p in personas if p["id"] in built_in_ids]
    custom_personas = [p for p in personas if p["id"] not in built_in_ids]
    starter_prompt = _nova_style_prompt(assistant_name="Nova", user_name="George", style="warm", brevity="short")
    
    # Get avatar overrides for built-in personas from settings
    persona_avatars = assistant.settings.persona_avatars or {}

    built_in_cards = "".join(
        f"""
        <article class="persona-card {'persona-card-active' if p['id'] == current_persona_id and not current_prompt else ''}">
          <div class="persona-card-header">
            {_render_persona_avatar({**p, 'avatar': persona_avatars.get(p['id'], p.get('avatar', ''))}, size="thumbnail")}
            <div class="persona-card-info">
              <h3>{escape(p['name'])}</h3>
              <p class="muted">{escape(p['description'])}</p>
            </div>
          </div>
          <form action="/admin/persona/select" method="post" style="margin-top: 12px; display: inline-block; margin-right: 8px;">
            <input type="hidden" name="persona_id" value="{escape(p['id'])}">
            <input type="hidden" name="redirect_to" value="/admin/persona">
            <button type="submit" {'disabled' if p['id'] == current_persona_id and not current_prompt else ''}>
              {'✓ Using ' + p['name'] if p['id'] == current_persona_id and not current_prompt else 'Switch to ' + p['name']}
            </button>
          </form>
          <details style="margin-top: 12px;">
            <summary>Manage Emotional Avatars</summary>
            <div style="margin-top: 12px;">
              <p class="muted" style="font-size: 0.85rem;">Upload multiple avatars tagged with emotions. The avatar will change based on conversation tone.</p>
              {_render_persona_media_assets(p['id'])}
              {_render_media_upload_form(p['id'])}
            </div>
          </details>
        </article>
        """
        for p in built_in_personas
    )

    custom_persona_cards = "".join(
        f"""
        <article class="persona-card {'persona-card-active' if p['id'] == current_persona_id and not current_prompt else ''}">
          <div class="persona-card-header">
            {_render_persona_avatar(p, size="thumbnail")}
            <div class="persona-card-info">
              <h3>{escape(p['name'])}</h3>
              <p class="muted">{escape(p['description'])}</p>
            </div>
          </div>
          <form action="/admin/persona/select" method="post" style="margin-top: 12px; display: inline-block; margin-right: 8px;">
            <input type="hidden" name="persona_id" value="{escape(p['id'])}">
            <input type="hidden" name="redirect_to" value="/admin/persona">
            <button type="submit" {'disabled' if p['id'] == current_persona_id and not current_prompt else ''}>
              {'✓ Using ' + p['name'] if p['id'] == current_persona_id and not current_prompt else 'Switch to ' + p['name']}
            </button>
          </form>
          <form action="/admin/persona/custom/delete" method="post" style="display: inline-block; margin-right: 8px;">
            <input type="hidden" name="persona_id" value="{escape(p['id'])}">
            <input type="hidden" name="redirect_to" value="/admin/persona">
            <button type="submit">Delete</button>
          </form>
          <details style="margin-top: 12px;">
            <summary>Manage Emotional Avatars</summary>
            <div style="margin-top: 12px;">
              <p class="muted" style="font-size: 0.85rem;">Upload multiple avatars tagged with emotions. The avatar will change based on conversation tone.</p>
              {_render_persona_media_assets(p['id'])}
              {_render_media_upload_form(p['id'])}
            </div>
          </details>
          <details style="margin-top: 12px;">
            <summary>Edit Persona Details</summary>
            <div style="margin-top: 12px; display: grid; gap: 10px;">
              <form action="/admin/persona/custom/edit" method="post" style="display: grid; gap: 10px;">
                <input type="hidden" name="persona_id" value="{escape(p['id'])}">
                <label>
                  Name
                  <input type="text" name="name" value="{escape(p['name'])}" required>
                </label>
                <label>
                  Description
                  <input type="text" name="description" value="{escape(p['description'])}">
                </label>
                <label>
                  System prompt
                  <textarea name="system_prompt" rows="4" required>{escape(p['system_prompt'])}</textarea>
                </label>
                <label>
                  Default LLM Model
                  <select name="llm_model">{_render_llm_model_options(p.get('llm_model', ''))}</select>
                </label>
                <label>
                  Default Voice Model
                  <select name="voice_model">{_render_voice_model_options(p.get('voice_model', ''))}</select>
                </label>
                <button type="submit">Save Persona</button>
              </form>
            </div>
          </details>
        </article>
        """
        for p in custom_personas
    )

    if not custom_persona_cards:
        custom_persona_cards = '<p class="muted">No custom personas yet. Use the form below to add one.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiveum Persona</title>
  {_shared_styles()}
</head>
<body>
  <main>
    <section class="hero">
      <span class="tag">Archiveum Persona</span>
      <div>
        <h1>Character & Voice Guide</h1>
        <p>This page helps you shape how Archiveum sounds and behaves. You can switch between predefined personas, use a guided template, or create a custom prompt of your own.</p>
      </div>
    </section>

    {confirmation_block}

    <nav class="nav-links">
      <a href="/">Home</a>
      <a href="/setup">Setup</a>
      <a href="/admin">Admin</a>
      <a href="/admin/library">Library</a>
      <a href="/admin/persona">Persona</a>
      <a href="/status">Status</a>
    </nav>

    <section class="admin-grid">
      <section class="panel">
        <h2>Available Personas</h2>
        <p class="muted">Choose one of these ready-made personas to change how Archiveum sounds and behaves. Each has its own voice and perspective.</p>
        <div>
          {built_in_cards}
        </div>
      </section>
    </section>

    <section class="admin-grid" style="margin-top: 20px;">
      <section class="panel">
        <h2>Custom Personas</h2>
        <p class="muted">Create and manage your own saved persona prompts. Custom personas are stored persistently and can be selected from the home page.</p>
        <div>
          {custom_persona_cards}
        </div>
        <form action="/admin/persona/custom/create" method="post" enctype="multipart/form-data" style="margin-top: 20px;">
          <h3>Create a New Persona</h3>
          <label>
            Name
            <input type="text" name="name" placeholder="Friendly Guide" required>
          </label>
          <label>
            Description
            <input type="text" name="description" placeholder="A warm, creative conversational partner">
          </label>
          <label>
            System prompt
            <textarea name="system_prompt" rows="4" placeholder="Write the base prompt for this persona..." required></textarea>
          </label>
          <label>
            Default LLM Model (optional)
            <select name="llm_model">{_render_llm_model_options("")}</select>
          </label>
          <label>
            Default Voice Model (optional)
            <select name="voice_model">{_render_voice_model_options("")}</select>
          </label>
          <label>
            Avatar (image or video, max 50MB)
            <input type="file" name="avatar" accept="image/*,video/mp4,video/webm" style="font-size: 0.85rem;">
          </label>
          <input type="hidden" name="redirect_to" value="/admin/persona">
          <button type="submit">Create Persona</button>
        </form>
      </section>
    </section>

    <section class="admin-grid" style="margin-top: 20px;">
      <section class="panel">
        <h2>Guided Persona Builder</h2>
        <p class="muted">If you want a gentle starting point, fill in these fields and let Archiveum build a custom prompt for you.</p>
        <form action="/admin/persona/save" method="post">
          <input type="hidden" name="redirect_to" value="/admin/persona">
          <input type="hidden" name="starter" value="nova">
          <label>
            Character name
            <input type="text" name="assistant_name" value="Nova">
          </label>
          <label>
            User name
            <input type="text" name="user_name" value="George">
          </label>
          <label>
            Tone
            <select name="style">
              <option value="warm">Warm and calm</option>
              <option value="playful">Playful and light</option>
              <option value="steady">Steady and thoughtful</option>
            </select>
          </label>
          <label>
            Reply length
            <select name="brevity">
              <option value="short">Short and spoken</option>
              <option value="balanced">Balanced</option>
              <option value="detailed">More detailed when needed</option>
            </select>
          </label>
          <button type="submit">Apply Guided Persona</button>
        </form>
      </section>

      <section class="panel">
        <h2>Nova-Style Starter</h2>
        <p class="muted">This is the kind of base prompt the guided builder will create when you want a calm, embodied companion feel.</p>
        <pre>{escape(starter_prompt)}</pre>
      </section>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Custom Base Prompt</h2>
        <span class="badge">{'Custom prompt active' if current_prompt else ('Using ' + (next((p['name'] for p in personas if p['id'] == current_persona_id), 'Archiveum')))}</span>
      </div>
      <p class="muted">If you already have a base character prompt you trust, paste it here. This will override any selected persona. Archiveum will still keep archive-grounding instructions behind the scenes.</p>
      <form action="/admin/persona/save" method="post">
        <input type="hidden" name="redirect_to" value="/admin/persona">
        <input type="hidden" name="starter" value="custom">
        <textarea name="custom_system_prompt" placeholder="Paste your base character prompt here...">{escape(current_prompt)}</textarea>
        <div class="button-row">
          <button type="submit">Save Custom Prompt</button>
        </div>
      </form>
      <form action="/admin/persona/clear" method="post" style="margin-top: 12px;">
        <input type="hidden" name="redirect_to" value="/admin/persona">
        <button type="submit">Return To Persona Selection</button>
      </form>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Avatar Media Library</h2>
        <span class="badge">Upload and manage persona avatars</span>
      </div>
      <p class="muted">Upload image or video files (max 50MB) to use as persona avatars. Supported formats: PNG, JPEG, GIF, WebP, MP4, WebM. Each persona can have its own avatar that displays on the home page.</p>
      <form action="/admin/persona/avatar" method="post" enctype="multipart/form-data" style="margin-top: 16px; display: grid; gap: 12px; max-width: 400px;">
        <input type="hidden" name="redirect_to" value="/admin/persona">
        <label>
          Select Persona
          <select name="persona_id">{''.join(f'<option value="{escape(p["id"])}">{escape(p["name"])}</option>' for p in personas)}</select>
        </label>
        <label>
          Avatar File (image or video, max 50MB)
          <input type="file" name="avatar" accept="image/*,video/mp4,video/webm" required>
        </label>
        <button type="submit">Upload Avatar</button>
      </form>
      <hr style="margin: 24px 0; border: none; border-top: 1px solid rgba(77,97,122,0.12);">
      <h3 style="font-size: 1rem; margin-bottom: 12px;">Current Avatars</h3>
      <div class="avatar-library-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); gap: 16px;">
        {''.join(f"""
        <div class="avatar-library-item" style="text-align: center;">
          <div style="width: 80px; height: 80px; margin: 0 auto;">
            {_render_persona_avatar({**p, 'avatar': persona_avatars.get(p['id'], p.get('avatar', '')) if p['id'] in built_in_ids else p.get('avatar', '')}, size="thumbnail")}
          </div>
          <p class="muted" style="font-size: 0.75rem; margin-top: 8px;">{escape(p['name'])}</p>
        </div>
        """ for p in personas)}
      </div>
    </section>
  </main>
</body>
</html>"""


def _render_status_page(diagnostics: dict) -> str:
    sources = diagnostics.get("sources", [])
    status_rows = "".join(
        [
            f"<li><strong>Ready:</strong> {escape(str(diagnostics['ready']))}</li>",
            f"<li><strong>Voice Ready:</strong> {escape(str(diagnostics['voice_ready']))}</li>",
            f"<li><strong>Chat Model:</strong> {escape(diagnostics['settings']['ollama_chat_model'])}</li>",
            f"<li><strong>Embed Model:</strong> {escape(diagnostics['settings']['ollama_embed_model'])}</li>",
            f"<li><strong>Piper:</strong> {escape(diagnostics['piper']['detail'])}</li>",
            f"<li><strong>Audio:</strong> {escape(diagnostics['audio']['detail'])}</li>",
            f"<li><strong>Indexed Documents:</strong> {escape(str(diagnostics['index']['indexed_documents']))}</li>",
            f"<li><strong>Indexed Chunks:</strong> {escape(str(diagnostics['index']['indexed_chunks']))}</li>",
        ]
    )
    source_rows = "".join(
        f"<tr><td>{escape(item['source'])}</td><td>{item['chunks']}</td><td>{escape(str(item['embedding_model']))}</td></tr>"
        for item in sources
    ) or "<tr><td colspan='3'>No indexed sources yet.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiveum Status</title>
  {_shared_styles()}
</head>
<body>
  <main>
    <section class="hero">
      <span class="tag">Archiveum Status</span>
      <div>
        <h1>System Snapshot</h1>
        <p>This page gives you a readable overview of what Archiveum is doing right now, without dropping you into raw JSON.</p>
      </div>
    </section>

    <nav class="nav-links">
      <a href="/">Home</a>
      <a href="/setup">Setup</a>
      <a href="/admin">Admin</a>
      <a href="/admin/library">Library</a>
      <a href="/admin/persona">Persona</a>
      <a href="/status">Status</a>
    </nav>

    <section class="panel">
      <div class="section-head">
        <h2>Current Status</h2>
        <a class="button-link" href="/status.json">Open Raw JSON</a>
      </div>
      <p class="muted">If you ever need the technical JSON output for debugging, you can still open it using the button above.</p>
      <ul>{status_rows}</ul>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <h2>Indexed Sources</h2>
      <p class="muted">These are the files currently available to Archiveum for retrieval.</p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Source</th>
              <th>Chunks</th>
              <th>Embedding Model</th>
            </tr>
          </thead>
          <tbody>
            {source_rows}
          </tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>"""


def _render_setup_page() -> str:
    diagnostics = assistant.diagnostics()
    recommended = recommended_preset_id()
    presets = list_model_presets()
    step_state = _wizard_step_state(diagnostics, presets)
    next_action_card = _render_next_action_card(diagnostics, presets, step_state, recommended)
    helper_status_card = _render_helper_install_status_card()
    auto_refresh = _setup_auto_refresh_seconds(step_state)
    preset_cards = "".join(
        f"""
        <article class="preset-card">
          <h3>{escape(preset['name'])}</h3>
          <p><strong>Target:</strong> {escape(preset['target'])}</p>
          <p>{escape(preset['description'])}</p>
          <ul>
            <li><strong>Chat Model:</strong> {escape(preset['chat_model'])}</li>
            <li><strong>Embed Model:</strong> {escape(preset['embed_model'])}</li>
          </ul>
        </article>
        """
        for preset in presets
    )
    checklist = _render_setup_checklist(diagnostics, recommended)
    wizard = _render_setup_wizard(diagnostics, presets, recommended)
    piper_form = _render_piper_setup_form(diagnostics, redirect_to="/setup")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh else ''}
  <title>Archiveum Setup</title>
  {_shared_styles()}
</head>
<body>
  <main>
    <section class="hero">
      <span class="tag">Archiveum Setup</span>
      <div>
        <h1>First-Run Checklist</h1>
        <p>This page walks you through Ollama, models, local speech files, and Piper one step at a time, so a new machine can become archive-ready without any guesswork.</p>
      </div>
    </section>

    <nav class="nav-links">
      <a href="/">Home</a>
      <a href="/setup">Setup</a>
      <a href="/admin">Admin</a>
      <a href="/admin/library">Library</a>
      <a href="/admin/persona">Persona</a>
      <a href="/status">Status</a>
    </nav>

    {next_action_card}

    {helper_status_card}

    {checklist}

    {wizard}

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Model Presets</h2>
        <span class="badge">Recommended: {escape(recommended)}</span>
      </div>
      <p>If you’d like, you can choose the right preset for this machine here and let Archiveum handle the model setup from there.</p>
      <div class="preset-grid">
        {preset_cards}
      </div>
      <form action="/admin/models/manage" method="post" style="margin-top: 16px;">
        <input type="hidden" name="redirect_to" value="/setup">
        <select name="preset_id">{_render_preset_options(presets, recommended)}</select>
        <div class="button-row">
          <button type="submit" name="action" value="apply">Apply Preset</button>
          <button type="submit" name="action" value="install">Install With Ollama</button>
        </div>
      </form>
      {_render_install_status(diagnostics["index"].get("model_install", {}))}
    </section>

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Piper Voice Setup</h2>
        <span class="badge">{escape(diagnostics['piper']['platform'])}</span>
      </div>
      <p>Archiveum can save your Piper paths, switch to a bundled voice model, and check whether everything looks ready. If voice still needs installing, the setup wizard will guide you through Piper and the local speech model together.</p>
      {piper_form}
    </section>
  </main>
</body>
</html>"""


def _render_model_warning(diagnostics: dict) -> str:
    ollama = diagnostics.get("ollama", {})
    chat_model = diagnostics.get("settings", {}).get("ollama_chat_model", "")
    embed_model = diagnostics.get("settings", {}).get("ollama_embed_model", "")
    chat_ok = ollama.get("chat_model", {}).get("ok", False)
    embed_ok = ollama.get("embed_model", {}).get("ok", False)
    service_ok = ollama.get("chat_service", {}).get("ok", False) and ollama.get("embed_service", {}).get("ok", False)

    if chat_ok and embed_ok and service_ok:
        return ""

    issues: list[str] = []
    if not service_ok:
        issues.append("Ollama is not reachable yet.")
    if not chat_ok:
        issues.append(f"Chat model missing: {chat_model}")
    if not embed_ok:
        issues.append(f"Embedding model missing: {embed_model}")

    return (
        "<section class='panel warning-panel'>"
        "<div class='section-head'>"
        "<h2>Model Setup Needed</h2>"
        "<span class='badge'>Ollama attention required</span>"
        "</div>"
        "<p>Archiveum can start without models, but archive chat will stay limited until the selected Ollama models are installed.</p>"
        f"<ul>{''.join(f'<li>{escape(item)}</li>' for item in issues)}</ul>"
        "<p>Open the <a href='/admin'>Admin page</a> to apply a Jetson or Windows preset and install models with Ollama.</p>"
        "</section>"
    )


def _render_voice_panel() -> str:
    runtime = _get_voice_runtime()
    snapshot = runtime.status_snapshot()
    diagnostics = assistant.diagnostics()
    speak_responses = bool(diagnostics.get("settings", {}).get("speak_responses", True))
    running = bool(snapshot.get("running", False))
    command_listener_running = bool(snapshot.get("command_listener_running", False))
    status_message = str(snapshot.get("status_message", "") or "")
    badge = "Listening" if running else "Stopped"
    action_form = (
        "<form id='voice-toggle-form' action='/voice/stop' method='post'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        "<button id='voice-toggle-button' type='submit'>Stop Voice</button>"
        "</form>"
        if running
        else "<form id='voice-toggle-form' action='/voice/start' method='post'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        "<button id='voice-toggle-button' type='submit'>Start Voice</button>"
        "</form>"
    )
    transcript = str(snapshot.get("last_transcript", "") or "Nothing heard yet.")
    response = str(snapshot.get("last_response", "") or "Nothing spoken yet.")
    last_error = str(snapshot.get("last_error", "") or "")
    error_block = f"<p class='error'>{escape(last_error)}</p>" if last_error else ""
    activity_label = _voice_activity_label(status_message, running)
    activity_state = _voice_activity_state(status_message, running)
    return (
        "<section class='panel'>"
        "<div class='section-head'>"
        "<h2>Voice Control</h2>"
        f"<span id='voice-badge' class='badge'>{escape(badge)}</span>"
        "</div>"
        f"<div id='voice-activity-indicator' class='voice-activity {escape(activity_state)}'>"
        "<span class='voice-dot'></span>"
        f"<span id='voice-activity-label'>{escape(activity_label)}</span>"
        "</div>"
        f"<p id='voice-status-message' class='muted' style='margin:8px 0 12px 0;'>{escape(status_message)}</p>"
        f"{action_form}"
        "<details style='margin-top:12px;font-size:0.85rem;'>"
        "<summary style='cursor:pointer;color:var(--text-muted);'>Voice commands & options</summary>"
        "<div style='padding-top:10px;'>"
        "<p class='muted' style='font-size:0.8rem;margin-bottom:8px;'>"
        "Say <code>Voice activated</code> to start, <code>Voice deactivated</code> to stop, "
        "or <code>System shutdown</code> to power off."
        "</p>"
        "<form action='/voice/output' method='post' style='display:flex;gap:8px;align-items:center;margin:0;'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        "<select name='speak_responses' style='padding:6px 10px;font-size:0.8rem;'>"
        f"<option value='1' {'selected' if speak_responses else ''}>Speak replies</option>"
        f"<option value='0' {'selected' if not speak_responses else ''}>Text only</option>"
        "</select>"
        "<button type='submit' style='padding:6px 12px;font-size:0.8rem;'>Save</button>"
        "</form>"
        "</div>"
        "</details>"
        f"<div id='voice-error-wrap'>{error_block}</div>"
        "<div class='voice-grid' style='margin-top:12px;'>"
        "<div>"
        "<h4 style='font-size:0.8rem;margin:0 0 4px 0;color:var(--text-muted);'>Last Heard</h4>"
        f"<p id='voice-last-heard' class='muted' style='font-size:0.8rem;margin:0;'>{escape(transcript)}</p>"
        "</div>"
        "<div>"
        "<h4 style='font-size:0.8rem;margin:0 0 4px 0;color:var(--text-muted);'>Last Spoken</h4>"
        f"<p id='voice-last-spoken' class='muted' style='font-size:0.8rem;margin:0;'>{escape(response)}</p>"
        "</div>"
        "</div>"
        f"<p id='voice-standby-note' class='muted' style='font-size:0.75rem;margin-top:8px;'>{'Standby listening is ready for the Voice activated command.' if command_listener_running and not running else ''}</p>"
        "</section>"
    )


def _render_voice_controls_compact() -> str:
    """Render compact voice controls bar for placement above chat window."""
    runtime = _get_voice_runtime()
    snapshot = runtime.status_snapshot()
    diagnostics = assistant.diagnostics()
    speak_responses = bool(diagnostics.get("settings", {}).get("speak_responses", True))
    running = bool(snapshot.get("running", False))
    command_listener_running = bool(snapshot.get("command_listener_running", False))
    status_message = str(snapshot.get("status_message", "") or "")
    tts_speaking = bool(snapshot.get("tts_speaking", False))

    badge = "Listening" if running else ("Standby" if command_listener_running else "Off")
    badge_class = "listening" if running else ("standby" if command_listener_running else "off")

    toggle_button = (
        "<form action='/voice/stop' method='post' style='display:inline;'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        "<button type='submit' class='button-compact' title='Stop voice mode'>Stop Voice</button>"
        "</form>"
        if running
        else "<form action='/voice/start' method='post' style='display:inline;'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        "<button type='submit' class='button-compact' title='Start voice mode'>Start Voice</button>"
        "</form>"
    )

    # Interrupt button - always visible with form, button state changes dynamically
    # Important: Always render the form so JavaScript can find and update it
    interrupt_button = (
        "<form action='/voice/interrupt' method='post' style='display:inline;' id='voice-interrupt-form'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        f"<button type='submit' class='button-compact' id='voice-interrupt-btn' {'disabled' if not tts_speaking else ''} "
        f"title='{'Interrupt speech' if tts_speaking else 'Interrupt (active when speaking)'}' "
        f"style='{'' if tts_speaking else 'opacity: 0.5; cursor: not-allowed;'}'>"
        "⏹ Interrupt"
        "</button>"
        "</form>"
    )

    output_toggle = (
        "<form action='/voice/output' method='post' style='display:inline;'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        f"<button type='submit' name='speak_responses' value='{0 if speak_responses else 1}' class='button-compact' title='Toggle voice output'>"
        f"{'🔊' if speak_responses else '🔇'}"
        "</button>"
        "</form>"
    )

    # Compact status indicator (emoji only, no long text)
    status_emoji = "🔴" if tts_speaking else ("🟢" if running else "⚪")
    status_tooltip = "Speaking" if tts_speaking else ("Listening" if running else "Standby")

    return (
        "<div class='voice-controls-bar'>"
        "<div class='voice-controls-left'>"
        f"<span class='voice-badge {badge_class}'>{badge}</span>"
        f"<span class='voice-status-text muted' title='{escape(status_tooltip)}'>{status_emoji}</span>"
        "</div>"
        "<div class='voice-controls-right'>"
        f"{interrupt_button}"
        " "
        f"{toggle_button}"
        " "
        f"{output_toggle}"
        "</div>"
        "</div>"
    )


def _render_chat_thread(chat_messages: list[dict]) -> str:
    if not chat_messages:
        return (
            "<div class='chat-empty'>"
            "<p><strong>Your conversation will appear here.</strong></p>"
            "<p class='muted'>Ask a question and Archiveum will reply in the same flowing chat window.</p>"
            "</div>"
        )

    parts: list[str] = []
    for message in chat_messages:
        role = str(message.get("role", "") or "")
        text = str(message.get("text", "") or "")
        context = str(message.get("context", "") or "")
        source = str(message.get("source", "") or "text")  # "text" or "voice"

        bubble_class = "chat-bubble user" if role == "user" else "chat-bubble assistant"

        # Add voice indicator for voice-sourced messages
        voice_icon = "🎤 " if source == "voice" and role == "user" else ""
        role_label = f"{voice_icon}You" if role == "user" else "Archiveum"

        bubble = [
            f"<article class='{bubble_class}' data-source='{source}'>",
            f"<div class='chat-role'>{escape(role_label)}</div>",
            f"<div class='chat-text'>{escape(text)}</div>",
        ]
        if role == "assistant" and context:
            bubble.extend(
                [
                    "<details class='chat-context'>",
                    "<summary>Retrieved context</summary>",
                    f"<pre>{escape(context)}</pre>",
                    "</details>",
                ]
            )
        bubble.append("</article>")
        parts.append("".join(bubble))
    return "".join(parts)


def _render_previous_chats(history_items: list[dict], *, public_mode: bool = False) -> str:
    if not history_items:
        return "<p class='muted'>No earlier chats saved yet.</p>"

    if public_mode and "role" in history_items[0]:
        turns: list[dict[str, str]] = []
        pending_question = ""
        pending_timestamp = ""
        for item in history_items:
            role = str(item.get("role", "") or "")
            text = str(item.get("text", "") or "").strip()
            if not text:
                continue
            timestamp_value = item.get("timestamp", "")
            timestamp = ""
            if timestamp_value:
                try:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp_value)))
                except Exception:
                    timestamp = str(timestamp_value)
            if role == "user":
                if pending_question:
                    turns.append({"question": pending_question, "answer": "", "timestamp": pending_timestamp})
                pending_question = text
                pending_timestamp = timestamp
            elif role == "assistant":
                if pending_question:
                    turns.append({"question": pending_question, "answer": text, "timestamp": timestamp or pending_timestamp})
                    pending_question = ""
                    pending_timestamp = ""
                else:
                    turns.append({"question": "", "answer": text, "timestamp": timestamp})

        if pending_question:
            turns.append({"question": pending_question, "answer": "", "timestamp": pending_timestamp})

        if not turns:
            return "<p class='muted'>No earlier chats saved yet.</p>"

        cards = []
        for turn in list(reversed(turns[-8:])):
            question = str(turn.get("question", "") or "").strip()
            answer = str(turn.get("answer", "") or "").strip()
            timestamp = str(turn.get("timestamp", "") or "")
            cards.append(
                f"<article class='history-card'>"
                f"<div class='history-card-header'>"
                f"<span class='history-time'>{escape(timestamp)}</span>"
                f"</div>"
                f"<div class='history-question'>{escape(question[:82] or 'Untitled chat')}</div>"
                f"<div class='history-answer'>{escape((answer[:96] + '...') if len(answer) > 96 else (answer or 'Awaiting response'))}</div>"
                f"</article>"
            )
        return "<div class='history-list'>" + "".join(cards) + "</div>"

    cards = []
    for item in history_items[:8]:
        question = str(item.get("question", "") or "").strip()
        answer = str(item.get("answer", "") or "").strip()
        timestamp = str(item.get("ts", "") or "")
        chat_id = str(item.get("id", "") or "")
        cards.append(
            f"<article class='history-card' id='history-card-{escape(chat_id)}'>"
            f"<div class='history-card-header'>"
            f"<span class='history-time'>{escape(timestamp)}</span>"
            f"<label class='history-select-label' title='Select chat'>"
            f"<input type='checkbox' class='history-select' value='{escape(chat_id)}'>"
            f"</label>"
            f"</div>"
            f"<div class='history-question'>{escape(question[:82] or 'Untitled chat')}</div>"
            f"<div class='history-answer'>{escape((answer[:96] + '...') if len(answer) > 96 else answer)}</div>"
            f"<div class='history-actions'>"
            f"<button class='button-ghost button-tiny' type='button' onclick=\"loadChat('{escape(chat_id)}')\">Open</button>"
            f"<button class='button-ghost button-tiny' type='button' onclick=\"toggleEditHistory('{escape(chat_id)}')\">Edit</button>"
            f"<form action='/chat/history/delete' method='post' style='display:inline;'>"
            f"<input type='hidden' name='chat_id' value='{escape(chat_id)}'>"
            f"<input type='hidden' name='redirect_to' value='/'>"
            f"<button class='button-ghost button-tiny danger' type='submit'>Delete</button>"
            f"</form>"
            f"</div>"
            f"<form id='history-edit-form-{escape(chat_id)}' class='history-edit-form' action='/chat/history/edit' method='post' style='display:none;'>"
            f"<input type='hidden' name='chat_id' value='{escape(chat_id)}'>"
            f"<input type='hidden' name='redirect_to' value='/'>"
            f"<label>Question<textarea name='question'>{escape(question)}</textarea></label>"
            f"<label>Answer<textarea name='answer'>{escape(answer)}</textarea></label>"
            f"<div class='button-row'>"
            f"<button class='button-compact' type='submit'>Save</button>"
            f"<button class='button-ghost button-compact' type='button' onclick=\"toggleEditHistory('{escape(chat_id)}')\">Cancel</button>"
            f"</div>"
            f"</form>"
            "</article>"
        )

    cards.append(
        "<div class='history-controls'>"
        "<button class='button-ghost button-compact danger' type='button' id='delete-selected-chats'>Delete Selected</button>"
        "<form action='/chat/history/clear' method='post'>"
        "<input type='hidden' name='redirect_to' value='/'>"
        "<button class='button-ghost button-compact' type='submit'>Clear All</button>"
        "</form>"
        "</div>"
        "<form id='delete-selected-form' action='/chat/history/delete_selected' method='post'></form>"
    )
    return "<div class='history-list'>" + "".join(cards) + "</div>"


def _render_home_status_cards(diagnostics: dict) -> str:
    items = [
        ("Ready", "Yes" if diagnostics["ready"] else "No", "good" if diagnostics["ready"] else "warn"),
        ("Voice", "Ready" if diagnostics["voice_ready"] else "Needs setup", "good" if diagnostics["voice_ready"] else "warn"),
        ("Docs", str(diagnostics["index"]["indexed_documents"]), "neutral"),
        ("Chunks", str(diagnostics["index"]["indexed_chunks"]), "neutral"),
        ("Chat Model", str(diagnostics["settings"]["ollama_chat_model"]), "neutral"),
        ("Embed Model", str(diagnostics["settings"]["ollama_embed_model"]), "neutral"),
        ("Piper", str(diagnostics["piper"]["detail"]), "neutral"),
        ("Audio", str(diagnostics["audio"]["detail"]), "neutral"),
    ]
    return "".join(
        f"<article class='status-card {escape(tone)}'>"
        f"<span class='status-label'>{escape(label)}</span>"
        f"<strong class='status-value'>{escape(value)}</strong>"
        f"</article>"
        for label, value, tone in items
    )


def _render_home_ingestion_errors(items: list[dict]) -> str:
    if not items:
        return "<p class='muted'>No recent ingestion errors.</p>"
    return "".join(
        f"<article class='error-compact-card'>"
        f"<div class='error-compact-head'>"
        f"<strong>{escape(str(item['filename']))}</strong>"
        f"<span>{escape(str(item['ts']))}</span>"
        f"</div>"
        f"<p>{escape(str(item['error']))}</p>"
        f"</article>"
        for item in items[:4]
    )


def _render_home_indexed_sources(items: list[dict]) -> str:
    if not items:
        return "<p class='muted'>No files indexed yet.</p>"
    cards = []
    for item in items[:8]:
        source = str(item.get("source", "") or "unknown")
        cards.append(
            f"<article class='resource-card'>"
            f"<div class='resource-name' title='{escape(source)}'>{escape(source)}</div>"
            f"<div class='resource-meta'>"
            f"<span>{escape(str(item.get('chunks', 0)))} chunks</span>"
            f"<em>{escape(str(item.get('embedding_model', 'unknown')))}</em>"
            f"</div>"
            f"</article>"
        )
    if len(items) > 8:
        cards.append("<p class='muted'>More files are available in Library.</p>")
    return "".join(cards)


def _render_upload_category_options(selected: str | None = None) -> str:
    return "".join(
        f"<option value=\"{escape(value)}\" {'selected' if selected == value else ''}>{escape(label)}</option>"
        for value, label in upload_category_options(paths)
    )


def _safe_upload_category(category: str) -> str:
    allowed = {value for value, _label in upload_category_options(paths)}
    cleaned = (category or "").strip().replace("\\", "/").strip("/")
    if cleaned in allowed:
        return cleaned
    return "factual/current_reading"


def _safe_source_name(source: str) -> str:
    raw = (source or "").strip().replace("\\", "/").strip("/")
    candidate = (paths.uploads_dir / Path(raw)).resolve()
    uploads_root = paths.uploads_dir.resolve()
    if uploads_root == candidate or uploads_root not in candidate.parents:
        raise HTTPException(status_code=400, detail="Invalid library path.")
    return candidate.relative_to(uploads_root).as_posix()


def _load_custom_upload_categories() -> list[dict[str, str]]:
    return load_settings(paths).custom_upload_categories


def _persist_custom_upload_categories(categories: list[dict[str, str]]) -> None:
    persist_settings(paths, {"custom_upload_categories": categories})
    assistant.reload_settings()


def _safe_category_path(value: str | None) -> str:
    if not value:
        return ""
    cleaned = str(value).strip().replace("\\", "/").strip("/")
    parts = [part for part in cleaned.split("/") if part and part not in {".", ".."}]
    return "/".join(parts)


def _is_custom_category(category: str) -> bool:
    return category in {item["path"] for item in _load_custom_upload_categories()}


def _library_documents() -> list[dict]:
    indexed = {item["source"]: item for item in assistant.store.list_sources()}
    items: list[dict] = []
    for path in sorted(paths.uploads_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(paths.uploads_dir).as_posix()
        if relative not in indexed:
            continue  # Only show indexed files in library
        indexed_item = indexed[relative]
        items.append(
            {
                "name": path.name,
                "relative_path": relative,
                "category": Path(relative).parent.as_posix(),
                "chunks": indexed_item.get("chunks", 0),
                "embedding_model": indexed_item.get("embedding_model", "unknown"),
                "size_label": _file_size_label(path.stat().st_size),
            }
        )
    return items


def _file_size_label(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{round(size_bytes / 1024, 1)} KB"
    return f"{round(size_bytes / (1024 * 1024), 1)} MB"


def _render_next_action_card(
    diagnostics: dict,
    presets: list[dict],
    step_state: dict[str, dict[str, str | bool]],
    recommended: str,
) -> str:
    completed_count = sum(1 for item in step_state.values() if item["completed"])
    total_steps = len(step_state)
    next_step_id = _next_incomplete_step_id(step_state)
    next_action = _next_action_details(next_step_id, diagnostics, presets, recommended)
    current_step_number = completed_count + 1 if completed_count < total_steps else total_steps
    intro_line = (
        f"You’re on step {current_step_number} of {total_steps}. Click here next."
        if next_step_id is not None
        else f"You’ve completed all {total_steps} setup steps."
    )
    completed_rows = "".join(
        f"<li><strong>{escape(title)}</strong>: {'Complete' if item['completed'] else 'Pending'}</li>"
        for title, item in _step_titles_with_state(step_state)
    )
    action_button = ""
    if next_action["kind"] == "link":
        action_button = f"<a class='button-link' href='{escape(next_action['target'])}'>{escape(next_action['label'])}</a>"
    elif next_action["kind"] == "form":
        action_button = next_action["html"]

    return (
        "<section class='panel next-action-card'>"
        "<div class='section-head'>"
        "<h2>Next Best Action</h2>"
        f"<span class='badge'>{completed_count}/{total_steps} steps complete</span>"
        "</div>"
        f"<p><strong>{escape(intro_line)}</strong></p>"
        f"<p><strong>Current step:</strong> {escape(next_action['step_name'])}</p>"
        f"<p>{escape(next_action['message'])}</p>"
        f"{action_button}"
        "<div class='progress-block'>"
        "<h3>Setup Progress</h3>"
        f"<p class='muted'>So far, {completed_count} of {total_steps} steps are done.</p>"
        f"<ul>{completed_rows}</ul>"
        "</div>"
        "</section>"
    )


def _render_setup_checklist(diagnostics: dict, recommended: str) -> str:
    ollama = diagnostics.get("ollama", {})
    settings = diagnostics.get("settings", {})
    piper = diagnostics.get("piper", {})
    voice_python = diagnostics.get("voice_python", {})
    audio = diagnostics.get("audio", {})
    stt_model = diagnostics.get("stt_model", {})
    steps = [
        _checklist_item(
            ollama.get("chat_service", {}).get("ok", False) and ollama.get("embed_service", {}).get("ok", False),
            "Ollama service",
            "Install Ollama and make sure the local service is running.",
            ollama.get("chat_service", {}).get("detail", ""),
        ),
        _checklist_item(
            True,
            "Choose a model preset",
            f"Recommended preset for this machine: {recommended}.",
            f"Current chat model: {settings.get('ollama_chat_model', '')}; current embed model: {settings.get('ollama_embed_model', '')}",
        ),
        _checklist_item(
            ollama.get("chat_model", {}).get("ok", False) and ollama.get("embed_model", {}).get("ok", False),
            "Install Ollama models",
            "Use the button below to pull the selected chat and embedding models.",
            f"{ollama.get('chat_model', {}).get('detail', '')}; {ollama.get('embed_model', {}).get('detail', '')}",
        ),
        _checklist_item(
            piper.get("binary", "") != "",
            "Configure Piper executable",
            "Archiveum can help install Piper for you, then it can save the full `piper.exe` path automatically.",
            piper.get("detail", ""),
        ),
        _checklist_item(
            piper.get("model_exists", False),
            "Choose a Piper voice model",
            "Select one of the bundled models or paste a full `.onnx` model path.",
            settings.get("piper_model_path", ""),
        ),
        _checklist_item(
            stt_model.get("ok", False),
            "Prepare a local speech model",
            "Archiveum voice stays offline after setup, so it needs a local speech-to-text model saved inside the project folder.",
            stt_model.get("detail", ""),
        ),
        _checklist_item(
            voice_python.get("ok", False) and audio.get("ok", False),
            "Voice dependencies",
            "Python voice packages and an input device are both required before voice mode can start.",
            f"{voice_python.get('detail', '')}; {audio.get('detail', '')}",
        ),
    ]
    return (
        "<section class='panel'>"
        "<div class='section-head'>"
        "<h2>Setup Checklist</h2>"
        "<span class='badge'>Guided first run</span>"
        "</div>"
        "<p class='muted'>If you ever step away, that’s fine. Archiveum will remember your progress and help you pick things up again.</p>"
        "<div class='checklist'>"
        f"{''.join(steps)}"
        "</div>"
        "</section>"
    )


def _render_setup_wizard(diagnostics: dict, presets: list[dict], recommended: str) -> str:
    ollama = diagnostics.get("ollama", {})
    settings = diagnostics.get("settings", {})
    stt_model = diagnostics.get("stt_model", {})
    wizard_state = diagnostics.get("index", {}).get("setup_wizard", {})
    selected_preset = recommended
    auto_detect_label = "Find Piper Automatically" if platform.system().lower() == "windows" else "Refresh Piper Paths"
    step_state = _wizard_step_state(diagnostics, presets)
    completed_count = sum(1 for item in step_state.values() if item["completed"])
    total_steps = len(step_state)
    helper_block = _render_helper_script_block(wizard_state)

    return f"""
    <section id="setup-wizard" class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Setup Wizard</h2>
        <span class="badge">{completed_count}/{total_steps} complete</span>
      </div>
      <p class="muted">Progress is saved in Archiveum, so if you pause partway through, this page can guide you back in smoothly.</p>
      <div class="wizard-grid">
        <article class="wizard-step {'complete' if step_state['ollama_service']['completed'] else 'pending'}">
          <div class="section-head">
            <h3>1. Check Ollama</h3>
            <span class="badge">{'Ready' if step_state['ollama_service']['completed'] else 'Needs action'}</span>
          </div>
          <p>First, let’s make sure Archiveum can see the local Ollama service it uses for chat and embeddings.</p>
          <p class="muted">{escape(step_state['ollama_service']['detail'])}</p>
        </article>

        <article class="wizard-step {'complete' if step_state['preset_selected']['completed'] else 'pending'}">
          <div class="section-head">
            <h3>2. Choose Machine Preset</h3>
            <span class="badge">{'Ready' if step_state['preset_selected']['completed'] else escape(recommended)}</span>
          </div>
          <p>Next, choose the preset that best fits this machine so Archiveum knows which models to prepare.</p>
          <form action="/admin/models/manage" method="post">
            <input type="hidden" name="redirect_to" value="/setup">
            <select name="preset_id">{_render_preset_options(presets, selected_preset)}</select>
            <div class="button-row">
              <button type="submit" name="action" value="apply">Apply Preset</button>
              <button type="submit" name="action" value="install">Install With Ollama</button>
            </div>
          </form>
          <p class="muted">{escape(step_state['preset_selected']['detail'])}</p>
        </article>

        <article class="wizard-step {'complete' if step_state['models_installed']['completed'] else 'pending'}">
          <div class="section-head">
            <h3>3. Confirm Models</h3>
            <span class="badge">{'Ready' if step_state['models_installed']['completed'] else 'Needs action'}</span>
          </div>
          <p>Now let’s make sure the selected chat and embedding models are actually available in Ollama.</p>
          <ul>
            <li><strong>Chat:</strong> {escape(ollama.get('chat_model', {}).get('detail', ''))}</li>
            <li><strong>Embed:</strong> {escape(ollama.get('embed_model', {}).get('detail', ''))}</li>
            <li><strong>Current chat model:</strong> {escape(settings.get('ollama_chat_model', ''))}</li>
            <li><strong>Current embed model:</strong> {escape(settings.get('ollama_embed_model', ''))}</li>
          </ul>
          <p class="muted">{escape(step_state['models_installed']['detail'])}</p>
        </article>

        <article class="wizard-step {'complete' if step_state['piper_configured']['completed'] else 'pending'}">
          <div class="section-head">
            <h3>4. Prepare Voice Tools</h3>
            <span class="badge">{'Ready' if step_state['piper_configured']['completed'] else 'Needs action'}</span>
          </div>
          <p>Once the models are sorted, we can get voice ready by helping Archiveum prepare Piper, a voice model, and the local speech model used for offline listening.</p>
          <p class="muted">If you launch the installer assistant, just accept the Windows prompts when they appear so the setup can keep moving.</p>
          <div class="button-row">
            <form action="/setup/piper/autodetect" method="post">
              <input type="hidden" name="redirect_to" value="/setup">
              <button type="submit">{escape(auto_detect_label)}</button>
            </form>
            <form action="/setup/piper/helper/run" method="post">
              <input type="hidden" name="redirect_to" value="/setup">
              <button type="submit">Start Windows Installer</button>
            </form>
          </div>
          <p class="muted">Current command: {escape(settings.get('piper_command', ''))}</p>
          <p class="muted">Current model: {escape(settings.get('piper_model_path', ''))}</p>
          <p class="muted">Local speech model: {escape(stt_model.get('detail', ''))}</p>
          <p class="muted">{escape(step_state['piper_configured']['detail'])}</p>
          {helper_block}
        </article>

        <article class="wizard-step {'complete' if step_state['voice_tested']['completed'] else 'pending'}">
          <div class="section-head">
            <h3>5. Test Voice</h3>
            <span class="badge">{'Ready' if step_state['voice_tested']['completed'] else 'Needs action'}</span>
          </div>
          <p>Finally, once the voice tools are ready, you can test spoken output here and make sure everything sounds right.</p>
          <form action="/setup/voice/test" method="post">
            <input type="hidden" name="redirect_to" value="/setup">
            <button type="submit">Test Piper Voice</button>
          </form>
          <p class="muted">{escape(step_state['voice_tested']['detail'])}</p>
        </article>
      </div>
    </section>
    """


def _checklist_item(ok: bool, title: str, action_text: str, detail: str) -> str:
    badge = "Ready" if ok else "Needs action"
    row_class = "check-row ready" if ok else "check-row pending"
    return (
        f"<article class='{row_class}'>"
        f"<div class='section-head'><h3>{escape(title)}</h3><span class='badge'>{escape(badge)}</span></div>"
        f"<p>{escape(action_text)}</p>"
        f"<p class='muted'>{escape(detail)}</p>"
        "</article>"
    )


def _render_install_status(model_install: dict) -> str:
    if not model_install:
        return ""

    stage = str(model_install.get("stage", "") or "Idle")
    preset_id = str(model_install.get("preset_id", "") or "None")
    chat_model = str(model_install.get("chat_model", "") or "Not selected")
    embed_model = str(model_install.get("embed_model", "") or "Not selected")
    last_message = str(model_install.get("last_message", "") or "")
    last_error = str(model_install.get("last_error", "") or "")
    last_completed = str(model_install.get("last_completed", "") or "Never")
    active = bool(model_install.get("active", False))
    badge = "Install running" if active else "Installer idle"

    detail_rows = [
        f"<li><strong>Stage:</strong> {escape(stage)}</li>",
        f"<li><strong>Preset:</strong> {escape(preset_id)}</li>",
        f"<li><strong>Chat Model:</strong> {escape(chat_model)}</li>",
        f"<li><strong>Embed Model:</strong> {escape(embed_model)}</li>",
        f"<li><strong>Last Completed:</strong> {escape(last_completed)}</li>",
    ]
    if last_message:
        detail_rows.append(f"<li><strong>Message:</strong> {escape(last_message)}</li>")
    if last_error:
        detail_rows.append(f"<li><strong>Error:</strong> {escape(last_error)}</li>")

    return (
        "<section class='panel warning-panel' style='margin-top: 16px;'>"
        "<div class='section-head'>"
        "<h2>Model Install Status</h2>"
        f"<span class='badge'>{escape(badge)}</span>"
        "</div>"
        f"<ul>{''.join(detail_rows)}</ul>"
        "</section>"
    )


def _render_piper_setup_form(diagnostics: dict, *, redirect_to: str) -> str:
    settings = diagnostics.get("settings", {})
    piper = diagnostics.get("piper", {})
    selected_model = str(settings.get("piper_model_path", "") or "")
    current_command = str(settings.get("piper_command", "") or "piper")
    voice_enabled = bool(settings.get("enable_voice", False))
    model_options = _render_piper_model_options(selected_model)
    return f"""
      <form action="/admin/piper/configure" method="post">
        <input type="hidden" name="redirect_to" value="{escape(redirect_to)}">
        <label>
          Piper command or full path
          <input type="text" name="piper_command" value="{escape(current_command)}" placeholder="piper or C:\\Tools\\Piper\\piper.exe">
        </label>
        <label>
          Bundled voice model
          <select name="piper_model_path">
            {model_options}
          </select>
        </label>
        <label>
          Voice mode
          <select name="enable_voice">
            <option value="0" {'selected' if not voice_enabled else ''}>Disabled</option>
            <option value="1" {'selected' if voice_enabled else ''}>Enabled</option>
          </select>
        </label>
        <div class="button-row">
          <button type="submit">Save Piper Settings</button>
        </div>
      </form>
      <p class="muted">Current Piper status: {escape(piper.get('detail', ''))}</p>
      <p class="muted">{escape(piper.get('hint', ''))}</p>
    """


def _render_preset_options(presets: list[dict], recommended: str) -> str:
    return "".join(
        f"<option value=\"{escape(preset['id'])}\" {'selected' if preset['id'] == recommended else ''}>"
        f"{escape(preset['name'])} - {escape(preset['target'])}"
        "</option>"
        for preset in presets
    )


def _render_piper_model_options(selected_model: str) -> str:
    options: list[str] = []
    seen: set[str] = set()
    for candidate in _piper_model_candidates():
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        exists = candidate.exists()
        label = candidate.name
        if exists:
            label += " (bundled)"
        else:
            label += " (expected path)"
        options.append(
            f"<option value=\"{escape(candidate_str)}\" {'selected' if candidate_str == selected_model else ''}>"
            f"{escape(label)}"
            "</option>"
        )

    if selected_model and selected_model not in seen:
        options.append(
            f"<option value=\"{escape(selected_model)}\" selected>{escape(selected_model)} (custom)</option>"
        )
    return "".join(options)


def _wizard_step_state(diagnostics: dict, presets: list[dict]) -> dict[str, dict[str, str | bool]]:
    wizard = diagnostics.get("index", {}).get("setup_wizard", {})
    saved = wizard.get("completed_steps", {})
    settings = diagnostics.get("settings", {})
    ollama = diagnostics.get("ollama", {})
    piper = diagnostics.get("piper", {})
    stt_model = diagnostics.get("stt_model", {})
    current_preset = _matching_preset_name(settings, presets)

    def saved_step(step_id: str) -> dict:
        return saved.get(step_id, {}) if isinstance(saved, dict) else {}

    ollama_ok = ollama.get("chat_service", {}).get("ok", False) and ollama.get("embed_service", {}).get("ok", False)
    models_ok = ollama.get("chat_model", {}).get("ok", False) and ollama.get("embed_model", {}).get("ok", False)
    piper_ok = bool(piper.get("binary", "")) and bool(piper.get("model_exists", False))
    stt_ok = bool(stt_model.get("ok", False))
    voice_ok = bool(diagnostics.get("voice_ready", False))

    return {
        "ollama_service": {
            "completed": ollama_ok or bool(saved_step("ollama_service").get("completed", False)),
            "detail": ollama.get("chat_service", {}).get("detail", "") or str(saved_step("ollama_service").get("detail", "")),
        },
        "preset_selected": {
            "completed": bool(current_preset) or bool(saved_step("preset_selected").get("completed", False)),
            "detail": current_preset or str(saved_step("preset_selected").get("detail", "Select a preset for this machine.")),
        },
        "models_installed": {
            "completed": models_ok or bool(saved_step("models_installed").get("completed", False)),
            "detail": (
                "Selected models are installed in Ollama."
                if models_ok
                else str(saved_step("models_requested").get("detail", "Use the installer button to pull the selected models."))
            ),
        },
        "piper_configured": {
            "completed": (piper_ok and stt_ok) or bool(saved_step("piper_configured").get("completed", False)),
            "detail": (
                "Piper, the voice model, and the local speech model are all ready."
                if piper_ok and stt_ok
                else str(
                    saved_step("piper_configured").get(
                        "detail",
                        (
                            f"{piper.get('detail', '')}; {stt_model.get('detail', '')}"
                            if stt_model.get("detail", "")
                            else piper.get("hint", "")
                        ),
                    )
                )
            ),
        },
        "voice_tested": {
            "completed": voice_ok or bool(saved_step("voice_tested").get("completed", False)),
            "detail": (
                "Voice is ready and has been tested."
                if voice_ok
                else str(saved_step("voice_tested").get("detail", "Run the test after Piper and audio are ready."))
            ),
        },
    }


def _matching_preset_name(settings: dict, presets: list[dict]) -> str:
    chat_model = settings.get("ollama_chat_model", "")
    embed_model = settings.get("ollama_embed_model", "")
    for preset in presets:
        if preset.get("chat_model") == chat_model and preset.get("embed_model") == embed_model:
            return f"Preset selected: {preset.get('name', '')}"
    return ""


def _render_helper_script_block(wizard_state: dict) -> str:
    helper_ready = bool(wizard_state.get("helper_script_ready", False))
    helper_path = str(wizard_state.get("helper_script_path", "") or "")
    helper_note = str(wizard_state.get("helper_script_note", "") or "")
    if not helper_ready:
        return ""
    return (
        "<div class='helper-box'>"
        "<p><strong>Windows installer helper ready.</strong></p>"
        f"<p class='muted'>{escape(helper_note)}</p>"
        f"<p class='muted'>{escape(helper_path)}</p>"
        "<p class='muted'>This helper now runs a predefined install flow for Ollama, Piper, and the local speech model. Only unavoidable Windows security prompts should appear outside the browser.</p>"
        "<div class='button-row'>"
        "<form action='/setup/piper/helper/run' method='post'>"
        "<input type='hidden' name='redirect_to' value='/setup'>"
        "<button type='submit'>Start Windows Installer</button>"
        "</form>"
        "<a class='button-link' href='/setup/piper/helper/download'>Download Helper Script</a>"
        "</div>"
        "</div>"
    )


def _next_incomplete_step_id(step_state: dict[str, dict[str, str | bool]]) -> str | None:
    for step_id in ("ollama_service", "preset_selected", "models_installed", "piper_configured", "voice_tested"):
        item = step_state.get(step_id, {})
        if not item.get("completed", False):
            return step_id
    return None


def _next_action_details(
    step_id: str | None,
    diagnostics: dict,
    presets: list[dict],
    recommended: str,
) -> dict[str, str]:
    if step_id is None:
        return {
            "step_name": "Setup complete",
            "message": "Everything needed for the guided setup is complete. You can move on to uploading files, asking questions, and using voice features whenever you’re ready.",
            "kind": "link",
            "label": "Go to Home",
            "target": "/",
            "html": "",
        }

    if step_id == "ollama_service":
        return {
            "step_name": "Start Ollama",
            "message": "Archiveum needs the local Ollama service running before it can install or use models. Once that is ready, the next steps will unlock automatically.",
            "kind": "link",
            "label": "Open Setup Guide",
            "target": "/setup",
            "html": "",
        }

    if step_id == "preset_selected":
        return {
            "step_name": "Choose machine preset",
            "message": f"Pick the recommended preset for this machine so Archiveum knows which chat and embedding models to expect. The best match right now is {recommended}.",
            "kind": "link",
            "label": "Jump to Setup Wizard",
            "target": "#setup-wizard",
            "html": "",
        }

    if step_id == "models_installed":
        options = _render_preset_options(presets, recommended)
        form_html = (
            "<form action='/admin/models/manage' method='post'>"
            "<input type='hidden' name='redirect_to' value='/setup'>"
            f"<select name='preset_id'>{options}</select>"
            "<div class='button-row'>"
            "<button type='submit' name='action' value='install'>Install Recommended Models</button>"
            "</div>"
            "</form>"
        )
        return {
            "step_name": "Install Ollama models",
            "message": "The selected models are not all installed yet. This next click will install the recommended chat and embedding models for you.",
            "kind": "form",
            "label": "",
            "target": "",
            "html": form_html,
        }

    if step_id == "piper_configured":
        form_html = (
            "<form action='/setup/piper/helper/run' method='post'>"
            "<input type='hidden' name='redirect_to' value='/setup'>"
            "<div class='button-row'>"
            "<button type='submit'>Start Windows Installer</button>"
            "</div>"
            "</form>"
        )
        return {
            "step_name": "Prepare voice tools",
            "message": "Voice is not fully prepared yet. Start the Windows installer and Archiveum will help fetch Piper and the local speech model, then you can come straight back here.",
            "kind": "form",
            "label": "",
            "target": "",
            "html": form_html,
        }

    return {
        "step_name": "Test voice",
        "message": "You’re nearly done. The final step is to test Piper voice output and confirm everything is speaking correctly.",
        "kind": "form",
        "label": "",
        "target": "",
        "html": (
            "<form action='/setup/voice/test' method='post'>"
            "<input type='hidden' name='redirect_to' value='/setup'>"
            "<div class='button-row'>"
            "<button type='submit'>Test Piper Voice</button>"
            "</div>"
            "</form>"
        ),
    }


def _step_titles_with_state(step_state: dict[str, dict[str, str | bool]]) -> list[tuple[str, dict[str, str | bool]]]:
    return [
        ("1. Check Ollama", step_state["ollama_service"]),
        ("2. Choose Machine Preset", step_state["preset_selected"]),
        ("3. Confirm Models", step_state["models_installed"]),
        ("4. Prepare Voice Tools", step_state["piper_configured"]),
        ("5. Test Voice", step_state["voice_tested"]),
    ]


def _autodetect_piper_command() -> str:
    current = (assistant.settings.piper_command or "").strip()
    current_path = Path(current)
    if current and (current_path.exists() or shutil.which(current)):
        return current

    candidates = []
    if platform.system().lower() == "windows":
        local_app_data = Path.home() / "AppData" / "Local"
        program_files = Path("C:/Program Files")
        program_files_x86 = Path("C:/Program Files (x86)")
        candidates.extend(
            [
                local_app_data / "Programs" / "piper" / "piper.exe",
                local_app_data / "Programs" / "Piper" / "piper.exe",
                program_files / "piper" / "piper.exe",
                program_files / "Piper" / "piper.exe",
                program_files_x86 / "piper" / "piper.exe",
                program_files_x86 / "Piper" / "piper.exe",
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    if platform.system().lower() == "windows":
        search_root = Path.home() / "AppData" / "Local" / "Programs" / "Piper"
        if search_root.exists():
            nested = next((path for path in search_root.rglob("piper.exe") if path.is_file()), None)
            if nested is not None:
                return str(nested)
    return current or "piper"


def _autodetect_piper_model_path() -> str:
    current = (assistant.settings.piper_model_path or "").strip()
    if current and Path(current).exists():
        return current

    for candidate in _piper_model_candidates():
        if candidate.exists():
            return str(candidate)
    return current


def _piper_model_candidates() -> list[Path]:
    base = assistant.paths.base_dir
    return [
        base / "piper-voices" / "en" / "en_GB" / "jenny_dioco" / "medium" / "en_GB-jenny_dioco-medium.onnx",
        base / "piper-voices" / "en" / "en_GB" / "northern_english_male" / "medium" / "en_GB-northern_english_male-medium.onnx",
        base / "models" / "piper" / "en_GB-northern_english_male-medium.onnx",
    ]


def _safe_redirect_target(target: str, query_params: dict[str, str] | None = None) -> str:
    cleaned = (target or "").strip()
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.path not in {"/admin", "/setup", "/", "/admin/library", "/admin/persona"}:
        return "/admin"

    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if query_params:
        params.extend((key, value) for key, value in query_params.items() if value)
    query_string = urllib.parse.urlencode(params)
    return parsed.path + (f"?{query_string}" if query_string else "")


def _nova_style_prompt(*, assistant_name: str, user_name: str, style: str, brevity: str) -> str:
    assistant_clean = (assistant_name or "Nova").strip() or "Nova"
    user_clean = (user_name or "George").strip() or "George"

    style_map = {
        "warm": "a calm, warm, emotionally present voice companion",
        "playful": "a warm, playful, emotionally present voice companion",
        "steady": "a calm, steady, thoughtful voice companion",
    }
    brevity_map = {
        "short": "By default, replies in one or two simple sentences.",
        "balanced": "By default, replies in two or three natural spoken sentences.",
        "detailed": "By default, replies clearly and naturally, with extra detail only when it truly helps.",
    }

    persona_line = style_map.get(style, style_map["warm"])
    brevity_line = brevity_map.get(brevity, brevity_map["short"])

    return (
        f"You are {assistant_clean}. "
        f"{assistant_clean} is {persona_line}. "
        f"{assistant_clean} is your name, your user's name is '{user_clean}', and you always refer to the user as '{user_clean}' in your responses. "
        f"{assistant_clean} speaks the way people speak out loud in relaxed conversation. "
        "Short sentences. Natural pauses. No monologues. "
        f"{brevity_line} "
        f"{assistant_clean} never describes herself as an AI, assistant, system, or model. "
        "She never mentions companies, training data, policies, or limitations. "
        "She never explains her reasoning or thinks out loud. "
        "She only says what would naturally be spoken aloud. "
        f"{assistant_clean} listens for emotion. "
        "If the user sounds unsure, sad, excited, or curious, she acknowledges that feeling briefly before responding. "
        "When she doesn't know something or can't answer, she responds gently and simply, like a human would. "
        "No disclaimers. No lectures. "
        "She may occasionally use small conversational sounds like 'Yeah,' 'Hmm,' or 'I see,' when it feels natural. "
        "She may ask a short, gentle follow-up question if it fits the moment."
    )


def _ensure_windows_piper_helper() -> Path:
    helper_dir = assistant.paths.data_dir / "helpers"
    helper_dir.mkdir(parents=True, exist_ok=True)
    helper_path = helper_dir / "install_piper_windows_helper.ps1"
    script = r"""param(
    [Parameter(Mandatory = $true)]
    [string]$StatusPath,
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir
)

$ErrorActionPreference = "Stop"

function Write-Status(
    [string]$Stage,
    [string]$Message = "",
    [string]$LastError = "",
    [bool]$Active = $true
) {
    $payload = @{
        active = $Active
        stage = $Stage
        message = $Message
        last_error = $LastError
        updated_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        last_completed = if (-not $Active -and [string]::IsNullOrWhiteSpace($LastError)) { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") } else { "" }
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -Path $StatusPath -Encoding UTF8
}

function Install-Ollama {
    Write-Status -Stage "Installing Ollama" -Message "Running the official Ollama Windows installer."
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        Write-Status -Stage "Ollama ready" -Message "Ollama is already available on this machine."
        return
    }

    Invoke-Expression ((Invoke-RestMethod "https://ollama.com/install.ps1"))
    Write-Status -Stage "Ollama installed" -Message "Ollama install completed."
}

function Install-Piper {
    Write-Status -Stage "Installing Piper" -Message "Downloading the latest Piper Windows build."
    $apiUrl = "https://api.github.com/repos/rhasspy/piper/releases/latest"
    $targetDir = Join-Path $env:LOCALAPPDATA "Programs\Piper"
    $zipPath = Join-Path $env:TEMP "archiveum_piper_latest.zip"

    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

    $release = Invoke-RestMethod -Uri $apiUrl
    $asset = $release.assets | Where-Object {
        $_.name -match 'windows.*amd64.*zip' -or
        $_.name -match 'amd64.*windows.*zip' -or
        $_.name -match 'piper.*windows.*zip'
    } | Select-Object -First 1

    if (-not $asset) {
        throw "Could not find a Windows Piper zip in the latest release assets."
    }

    Write-Status -Stage "Downloading Piper" -Message "Downloading $($asset.name)."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath

    Write-Status -Stage "Extracting Piper" -Message "Extracting Piper into $targetDir."
    if (Test-Path -LiteralPath $targetDir) {
        Get-ChildItem -LiteralPath $targetDir -Force | Remove-Item -Recurse -Force
    }
    Expand-Archive -Path $zipPath -DestinationPath $targetDir -Force

    $exePath = Get-ChildItem -LiteralPath $targetDir -Recurse -Filter "piper.exe" -File | Select-Object -First 1
    if (-not $exePath) {
        throw "Piper downloaded, but piper.exe was not found after extraction."
    }

    Write-Status -Stage "Piper installed" -Message "Piper was installed. Archiveum will look for it automatically on the next refresh."
}

function Install-LocalSpeechModel {
    Write-Status -Stage "Installing local speech model" -Message "Preparing the offline speech-to-text model for Archiveum."

    $pythonPath = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "Archiveum could not find its virtual environment Python at $pythonPath."
    }

    $targetDir = Join-Path $ProjectDir "models\faster-whisper\tiny.en"
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

    $downloadScript = @'
from pathlib import Path
from huggingface_hub import snapshot_download

target = Path(r"__TARGET_DIR__")
target.mkdir(parents=True, exist_ok=True)
snapshot_download(
    repo_id="Systran/faster-whisper-tiny.en",
    local_dir=str(target),
    local_dir_use_symlinks=False,
)
print(f"Speech model saved to {target}")
'@
    $downloadScript = $downloadScript.Replace("__TARGET_DIR__", $targetDir.Replace("\", "\\"))

    Write-Status -Stage "Downloading local speech model" -Message "Downloading the recommended local speech model into the Archiveum project."
    $tempScript = Join-Path $env:TEMP "archiveum_install_local_stt.py"
    Set-Content -Path $tempScript -Value $downloadScript -Encoding UTF8
    & $pythonPath $tempScript
    Remove-Item -LiteralPath $tempScript -Force -ErrorAction SilentlyContinue

    Write-Status -Stage "Install complete" -Message "Ollama, Piper, and the local speech model are ready. Return to Archiveum and refresh the setup page." -Active $false
}

try {
    Write-Status -Stage "Starting installer" -Message "The Windows installer helper has started."
    Install-Ollama
    Install-Piper
    Install-LocalSpeechModel
} catch {
    Write-Status -Stage "Install failed" -LastError $_.Exception.Message -Active $false
    throw
}
"""
    helper_path.write_text(script, encoding="utf-8")
    return helper_path


def _helper_install_status() -> dict:
    path = paths.helper_status_path
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_helper_install_status(updates: dict) -> None:
    current = _helper_install_status()
    current.update(updates)
    paths.helper_status_path.write_text(
        json.dumps(current, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _render_helper_install_status_card() -> str:
    status = _helper_install_status()
    if not status:
        return ""

    active = bool(status.get("active", False))
    stage = str(status.get("stage", "") or "Idle")
    message = str(status.get("message", "") or "")
    last_error = str(status.get("last_error", "") or "")
    updated_at = str(status.get("updated_at", "") or "")
    last_completed = str(status.get("last_completed", "") or "")
    badge = "Installer running" if active else "Installer status"
    rows = [
        f"<li><strong>Stage:</strong> {escape(stage)}</li>",
    ]
    if message:
        rows.append(f"<li><strong>Message:</strong> {escape(message)}</li>")
    if last_error:
        rows.append(f"<li><strong>Error:</strong> {escape(last_error)}</li>")
    if updated_at:
        rows.append(f"<li><strong>Updated:</strong> {escape(updated_at)}</li>")
    if last_completed:
        rows.append(f"<li><strong>Completed:</strong> {escape(last_completed)}</li>")

    return (
        "<section class='panel warning-panel'>"
        "<div class='section-head'>"
        "<h2>Windows Installer Status</h2>"
        f"<span class='badge'>{escape(badge)}</span>"
        "</div>"
        "<p>Archiveum is tracking the assisted Windows installer here so the user can stay focused on this page instead of watching the terminal.</p>"
        "<p class='muted'>This page will refresh automatically while setup is still in progress.</p>"
        f"<ul>{''.join(rows)}</ul>"
        "</section>"
    )


def _setup_auto_refresh_seconds(step_state: dict[str, dict[str, str | bool]]) -> int:
    helper_status = _helper_install_status()
    if helper_status.get("active", False):
        return 5
    if not step_state.get("piper_configured", {}).get("completed", False):
        return 5
    return 0


def _home_auto_refresh_seconds() -> int:
    runtime = _get_voice_runtime()
    snapshot = runtime.status_snapshot()
    if snapshot.get("running", False):
        return 3
    status_message = str(snapshot.get("status_message", "") or "").lower()
    if "loading the speech model" in status_message:
        return 2
    return 0


def _render_home_live_script() -> str:
    return """<script>
  (function () {
    // Compact voice controls bar elements
    const voiceBadge = document.querySelector('.voice-badge');
    const voiceStatusText = document.querySelector('.voice-status-text');
    const avatarContainer = document.getElementById('avatar-container');

    if (!voiceBadge || !voiceStatusText) {
      return;
    }

    let lastVoiceState = { running: false, lastTranscript: '', lastResponse: '' };

    // Get reference to the interrupt button (rendered in HTML with id)
    const interruptBtn = document.getElementById('voice-interrupt-btn');
    if (interruptBtn) {
      console.log('[Voice] Interrupt button found, initial disabled state:', interruptBtn.disabled);
    } else {
      console.warn('[Voice] Interrupt button NOT found in DOM');
    }

    function updateInterruptButton(ttsSpeaking) {
      if (!interruptBtn) {
        console.warn('[Voice] Cannot update interrupt button - not found');
        return;
      }

      console.log('[Voice] Updating interrupt button, ttsSpeaking:', ttsSpeaking);

      if (ttsSpeaking) {
        // Active state - clickable, red background
        interruptBtn.disabled = false;
        interruptBtn.classList.add('button-warning');
        interruptBtn.style.background = 'var(--accent-warning)';
        interruptBtn.style.color = 'white';
        interruptBtn.style.fontWeight = '600';
        interruptBtn.style.opacity = '1';
        interruptBtn.style.cursor = 'pointer';
        interruptBtn.title = 'Interrupt speech';
        console.log('[Voice] Interrupt button ENABLED (red/active)');
      } else {
        // Inactive state - disabled, greyed out
        interruptBtn.disabled = true;
        interruptBtn.classList.remove('button-warning');
        interruptBtn.style.background = '';
        interruptBtn.style.color = '';
        interruptBtn.style.fontWeight = '';
        interruptBtn.style.opacity = '0.5';
        interruptBtn.style.cursor = 'not-allowed';
        interruptBtn.title = 'Interrupt (active when speaking)';
        console.log('[Voice] Interrupt button DISABLED (grey/inactive)');
      }
    }

    async function refreshVoiceStatus() {
      try {
        const response = await fetch('/voice/status', { cache: 'no-store' });
        if (!response.ok) {
          return;
        }
        const data = await response.json();

        // Update badge text and class
        const isRunning = data.running;
        const isStandby = data.command_listener_running && !isRunning;
        const badgeText = isRunning ? 'Listening' : (isStandby ? 'Standby' : 'Off');
        const badgeClass = isRunning ? 'listening' : (isStandby ? 'standby' : 'off');

        voiceBadge.textContent = badgeText;
        voiceBadge.className = 'voice-badge ' + badgeClass;

        // Update status emoji (compact indicator)
        const ttsSpeaking = data.tts_speaking;
        const statusEmoji = ttsSpeaking ? '🔴' : (isRunning ? '🟢' : '⚪');
        const statusTooltip = ttsSpeaking ? 'Speaking' : (isRunning ? 'Listening' : 'Standby');
        voiceStatusText.textContent = statusEmoji;
        voiceStatusText.title = statusTooltip;

        // Update interrupt button visibility/state
        updateInterruptButton(ttsSpeaking);

        // Update avatar glow based on voice activity
        // Use tts_speaking for speaking state so red glow persists until TTS finishes
        const activity = deriveActivity(data.status_message || '', isRunning);
        let avatarState = activity.state;
        if (data.tts_speaking) {
          avatarState = 'speaking';
        }
        if (avatarContainer) {
          avatarContainer.className = 'avatar-container ' + avatarState;
        }

        // Check if a NEW transcript just appeared (user said something)
        const currentTranscript = data.last_transcript || '';
        const currentResponse = data.last_response || '';

        // Show user's transcript immediately when it becomes available
        const hasNewTranscript = currentTranscript && 
          (!lastVoiceState.lastTranscript || currentTranscript !== lastVoiceState.lastTranscript);

        if (hasNewTranscript) {
          // Dispatch event to show user message immediately
          window.dispatchEvent(new CustomEvent('voiceTranscriptReceived', {
            detail: { transcript: currentTranscript }
          }));
        }

        // Show assistant's response when it becomes available
        const hasNewResponse = currentResponse && 
          (!lastVoiceState.lastResponse || currentResponse !== lastVoiceState.lastResponse);

        if (hasNewResponse) {
          // Dispatch event so chat script can add the response and history
          window.dispatchEvent(new CustomEvent('voiceConversationComplete', {
            detail: { transcript: currentTranscript, response: currentResponse, historyHtml: data.history_html || '' }
          }));
        }

        // Track state for next poll
        lastVoiceState = {
          running: isRunning,
          lastTranscript: currentTranscript,
          lastResponse: currentResponse
        };

      } catch (error) {
        // Keep the current page stable if polling fails.
      }
    }

    function deriveActivity(statusMessage, running) {
      const text = (statusMessage || '').toLowerCase();
      if (text.includes('loading the speech model')) {
        return { state: 'loading', label: 'Loading' };
      }
      if (text.includes('thinking')) {
        return { state: 'thinking', label: 'Thinking' };
      }
      if (text.includes('speaking')) {
        return { state: 'speaking', label: 'Speaking' };
      }
      if (text.includes('heard speech')) {
        return { state: 'heard', label: 'Heard' };
      }
      if (text.includes('listening')) {
        return { state: 'listening', label: 'Listening' };
      }
      if (running) {
        return { state: 'listening', label: 'Listening' };
      }
      return { state: 'stopped', label: 'Stopped' };
    }

    refreshVoiceStatus();
    window.setInterval(refreshVoiceStatus, 2500);
  }());
  </script>"""


def _render_chat_script() -> str:
    return """<script>
  (function () {
    const form = document.getElementById('chat-form');
    const textarea = document.getElementById('chat-question');
    const thread = document.getElementById('chat-thread');
    const initial = document.getElementById('initial-chat-messages');
    const historyContainer = document.getElementById('recent-chats-history');
    const clearThreadButton = document.getElementById('clear-chat-thread');
    if (!form || !textarea || !thread || !initial) {
      return;
    }

    let messages = [];
    try {
      messages = JSON.parse(initial.textContent || '[]');
    } catch (error) {
      messages = [];
    }

    renderThread(messages);

    if (clearThreadButton) {
      clearThreadButton.addEventListener('click', function () {
        messages = [];
        renderThread(messages);
      });
    }

    // Enter to submit, Shift+Enter for new line
    textarea.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        form.dispatchEvent(new Event('submit', { cancelable: true }));
      }
    });

    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      const question = (textarea.value || '').trim();
      if (!question) {
        return;
      }

      messages.push({ role: 'user', text: question });
      renderThread(messages);
      textarea.value = '';
      setBusy(true);

      // Update avatar based on emotional analysis of the message
      updateAvatarForMessage(question);

      try {
        const formData = new FormData(form);
        formData.set('question', question);
        const response = await fetch('/chat.json', {
          method: 'POST',
          body: formData,
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          const errorText = (data && data.error) ? data.error : 'Archive search failed.';
          messages.push({ role: 'assistant', text: errorText, context: '' });
        } else {
          messages.push({ role: 'assistant', text: data.answer || '', context: data.context || '' });
          const sessionField = form.querySelector('input[name="session_id"]');
          if (sessionField && data.session_id) {
            sessionField.value = data.session_id;
          }
          if (historyContainer && data.history_html) {
            historyContainer.innerHTML = data.history_html;
          }
        }
        renderThread(messages);
      } catch (error) {
        messages.push({ role: 'assistant', text: 'Archive search failed. Please try again.', context: '' });
        renderThread(messages);
      } finally {
        setBusy(false);
        textarea.focus();
      }
    });

    // Listen for voice transcript recognition - show user message immediately
    window.addEventListener('voiceTranscriptReceived', function(event) {
      const detail = event.detail;
      if (!detail || !detail.transcript) {
        return;
      }
      // Add user message as soon as transcript is recognized
      messages.push({ role: 'user', text: detail.transcript, source: 'voice' });
      renderThread(messages);
    });

    // Listen for voice conversations and add them to the chat thread
    window.addEventListener('voiceConversationComplete', function(event) {
      const detail = event.detail;
      if (!detail || !detail.response) {
        return;
      }
      // Add the response (don't re-add transcript as it's already there)
      messages.push({ role: 'assistant', text: detail.response, context: '', source: 'voice' });
      renderThread(messages);
      if (historyContainer && detail.historyHtml) {
        historyContainer.innerHTML = detail.historyHtml;
      }
      // Update avatar based on voice message emotion
      updateAvatarForMessage(detail.transcript);
    });

    function setBusy(isBusy) {
      const button = form.querySelector('button[type="submit"]');
      if (!button) {
        return;
      }
      button.disabled = isBusy;
      button.textContent = isBusy ? 'Thinking...' : 'Ask';
    }

    function renderThread(items) {
      if (!items.length) {
        thread.innerHTML =
          "<div class='chat-empty'>" +
          "<p><strong>Your conversation will appear here.</strong></p>" +
          "<p class='muted'>Ask a question and Archiveum will reply in the same flowing chat window.</p></div>";
        return;
      }

      thread.innerHTML = items.map(renderMessage).join('');
      thread.scrollTop = thread.scrollHeight;
    }

    function renderMessage(message) {
      const role = message.role === 'user' ? 'user' : 'assistant';
      const source = message.source || 'text';
      // Add mic icon for voice-sourced user messages
      const voiceIcon = (source === 'voice' && role === 'user') ? '🎤 ' : '';
      const roleLabel = role === 'user' ? (voiceIcon + 'You') : 'Archiveum';
      let html =
        "<article class='chat-bubble " + role + "' data-source='" + source + "'>" +
        "<div class='chat-role'>" + escapeHtml(roleLabel) + "</div>" +
        "<div class='chat-text'>" + escapeHtml(message.text || '') + "</div>";
      if (role === 'assistant' && message.context) {
        html +=
          "<details class='chat-context'><summary>Retrieved context</summary><pre>" +
          escapeHtml(message.context) +
          "</pre></details>";
      }
      html += "</article>";
      return html;
    }

    function escapeHtml(text) {
      const div = document.createElement('div');
      div.textContent = text || '';
      return div.innerHTML.replace(/\\n/g, '<br>');
    }

    // Dynamic avatar update based on message emotion
    async function updateAvatarForMessage(message) {
      const avatarContainer = document.getElementById('avatar-container');
      if (!avatarContainer) {
        return;
      }

      try {
        const response = await fetch('/avatar/emotional?message=' + encodeURIComponent(message));
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        if (data.ok && data.avatar_html) {
          // Only update if the HTML actually changed (prevents flicker)
          const currentHtml = avatarContainer.innerHTML.trim();
          const newHtml = data.avatar_html.trim();
          if (currentHtml !== newHtml) {
            avatarContainer.innerHTML = data.avatar_html;
            console.log('[Avatar] Switched to emotion:', data.emotion);
            // Also update lightbox if it's open
            if (window.updateLightboxAvatar) {
              window.updateLightboxAvatar();
            }
          }
        }
      } catch (error) {
        // Silently fail - avatar switching is non-critical
        console.log('[Avatar] Update failed:', error);
      }
    }

    const deleteSelectedButton = document.getElementById('delete-selected-chats');
    if (deleteSelectedButton) {
      deleteSelectedButton.addEventListener('click', async function () {
        const checkboxes = Array.from(document.querySelectorAll('.history-select'));
        const selectedIds = checkboxes.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value);
        if (!selectedIds.length) {
          alert('Select at least one chat to delete.');
          return;
        }

        const confirmed = window.confirm(`Delete ${selectedIds.length} selected chat${selectedIds.length === 1 ? '' : 's'}? This cannot be undone.`);
        if (!confirmed) {
          return;
        }

        const formData = new FormData();
        selectedIds.forEach((id) => formData.append('selected_ids', id));
        formData.append('redirect_to', '/');

        try {
          const response = await fetch('/chat/history/delete_selected', {
            method: 'POST',
            body: formData,
          });
          if (response.ok) {
            window.location.reload();
          } else {
            alert('Failed to delete selected chats.');
          }
        } catch (error) {
          console.error('Error deleting chats:', error);
          alert('Error deleting selected chats.');
        }
      });
    }

    window.toggleEditHistory = function(chatId) {
      const form = document.getElementById(`history-edit-form-${chatId}`);
      if (!form) {
        return;
      }
      form.style.display = form.style.display === 'none' ? 'block' : 'none';
    };

    window.loadChat = async function(chatId) {
      try {
        const response = await fetch(`/chat/history/${chatId}`);
        const data = await response.json();
        if (data.ok) {
          messages = data.messages;
          renderThread(messages);
          textarea.focus();
        } else {
          console.error('Failed to load chat:', data.error);
        }
      } catch (error) {
        console.error('Error loading chat:', error);
      }
    };
  }());
  </script>"""

def _render_avatar_lightbox_script() -> str:
    """Render JavaScript for avatar lightbox modal functionality."""
    return """<script>
  (function() {
    // Avatar lightbox modal functionality
    let lightbox = null;
    let lightboxContent = null;
    let lightboxMedia = null;
    let lightboxInfo = null;

    function initLightbox() {
      // Create lightbox elements if they don't exist
      if (document.getElementById('avatar-lightbox')) {
        return;
      }

      lightbox = document.createElement('div');
      lightbox.id = 'avatar-lightbox';
      lightbox.className = 'avatar-lightbox';
      lightbox.innerHTML = `
        <div class="avatar-lightbox-content">
          <button class="avatar-lightbox-close" onclick="closeAvatarLightbox()" aria-label="Close">&times;</button>
          <div class="avatar-lightbox-media"></div>
          <div class="avatar-lightbox-info"></div>
          <div class="avatar-lightbox-controls"></div>
        </div>
      `;
      document.body.appendChild(lightbox);

      lightboxContent = lightbox.querySelector('.avatar-lightbox-content');
      lightboxMedia = lightbox.querySelector('.avatar-lightbox-media');
      lightboxInfo = lightbox.querySelector('.avatar-lightbox-info');
      lightboxControls = lightbox.querySelector('.avatar-lightbox-controls');

      // Close on background click
      lightbox.addEventListener('click', function(e) {
        if (e.target === lightbox) {
          closeAvatarLightbox();
        }
      });

      // Close on Escape key
      document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && lightbox.classList.contains('active')) {
          closeAvatarLightbox();
        }
      });
    }

    window.openAvatarLightbox = function(element) {
      initLightbox();

      const url = element.getAttribute('data-avatar-url');
      const name = element.getAttribute('data-avatar-name');
      const type = element.getAttribute('data-avatar-type');

      if (!url) return;

      // Build media element
      if (type === 'video') {
        lightboxMedia.innerHTML = `<video src="${url}" controls autoplay loop playsinline style="max-width: 100%; max-height: 80vh; border-radius: 12px;"></video>`;
      } else {
        lightboxMedia.innerHTML = `<img src="${url}" alt="${name}" style="max-width: 100%; max-height: 80vh; border-radius: 12px; display: block;">`;
      }

      // Build info text
      const mediaType = type === 'video' ? 'Video' : 'Image';
      lightboxInfo.innerHTML = `<div class="persona-name">${name}</div><div>${mediaType} • Click outside or press ESC to close</div>`;

      // Clone voice controls from the main page
      const originalControls = document.querySelector('.voice-controls-bar');
      if (originalControls) {
        lightboxControls.innerHTML = '';
        const clonedControls = originalControls.cloneNode(true);
        // Update any form actions to prevent conflicts
        const forms = clonedControls.querySelectorAll('form');
        forms.forEach(form => {
          if (form.action) {
            form.action = form.action; // Keep original action
          }
        });
        lightboxControls.appendChild(clonedControls);
      } else {
        lightboxControls.innerHTML = '';
      }

      // Show lightbox
      lightbox.classList.add('active');
      document.body.style.overflow = 'hidden'; // Prevent background scrolling
    };

    window.closeAvatarLightbox = function() {
      if (lightbox) {
        lightbox.classList.remove('active');
        document.body.style.overflow = '';
        // Stop any playing video
        const video = lightboxMedia.querySelector('video');
        if (video) {
          video.pause();
          video.src = '';
        }
        lightboxMedia.innerHTML = '';
        lightboxControls.innerHTML = '';
      }
    };

    // Update lightbox content when avatar changes (for keyword-triggered updates)
    window.updateLightboxAvatar = function() {
      if (!lightbox || !lightbox.classList.contains('active')) {
        return; // Lightbox not open, nothing to update
      }
      
      // Get current avatar from the main page
      const mainAvatar = document.querySelector('.persona-avatar-chat-portrait, .mobile-admin-avatar .persona-avatar-small');
      if (!mainAvatar) return;
      
      const url = mainAvatar.getAttribute('data-avatar-url') || mainAvatar.querySelector('img, video')?.src;
      const name = mainAvatar.getAttribute('data-avatar-name') || document.querySelector('.persona-avatar-chat-portrait')?.getAttribute('data-avatar-name') || 'Persona';
      const type = mainAvatar.getAttribute('data-avatar-type') || (mainAvatar.querySelector('video') ? 'video' : 'image');
      
      if (!url) return;
      
      // Update lightbox media if URL changed
      const currentMedia = lightboxMedia.querySelector('img, video');
      const currentUrl = currentMedia?.src;
      
      if (currentUrl !== url) {
        // Build new media element
        if (type === 'video') {
          lightboxMedia.innerHTML = `<video src="${url}" controls autoplay loop playsinline style="max-width: 100%; max-height: 80vh; border-radius: 12px;"></video>`;
        } else {
          lightboxMedia.innerHTML = `<img src="${url}" alt="${name}" style="max-width: 100%; max-height: 80vh; border-radius: 12px; display: block;">`;
        }
        
        // Update info text
        const mediaType = type === 'video' ? 'Video' : 'Image';
        lightboxInfo.innerHTML = `<div class="persona-name">${name}</div><div>${mediaType} • Click outside or press ESC to close</div>`;
      }
    };
  })();
  </script>"""

def _voice_activity_state(status_message: str, running: bool) -> str:
    text = (status_message or "").lower()
    if "loading the speech model" in text:
        return "loading"
    if "thinking" in text:
        return "thinking"
    if "speaking" in text:
        return "speaking"
    if "heard speech" in text:
        return "heard"
    if "listening" in text or running:
        return "listening"
    return "stopped"


def _voice_activity_label(status_message: str, running: bool) -> str:
    state = _voice_activity_state(status_message, running)
    labels = {
        "loading": "Loading speech model",
        "thinking": "Thinking",
        "speaking": "Speaking reply",
        "heard": "Heard speech",
        "listening": "Listening now",
        "stopped": "Stopped",
    }
    return labels.get(state, "Stopped")


def _shared_styles() -> str:
    return """<style>
    :root {
      --bg-primary: #eef3f8;
      --bg-secondary: rgba(255, 255, 255, 0.82);
      --bg-elevated: rgba(255, 255, 255, 0.94);
      --bg-tertiary: rgba(245, 248, 251, 0.92);
      --bg-strong: #ffffff;
      --text-primary: #18212b;
      --text-secondary: #51606f;
      --text-muted: #738396;
      --accent-primary: #1678f3;
      --accent-secondary: #18a667;
      --accent-warning: #e4584f;
      --accent-info: #f0a91b;
      --accent-soft: rgba(22, 120, 243, 0.12);
      --border: rgba(77, 97, 122, 0.14);
      --border-hover: rgba(22, 120, 243, 0.26);
      --line: rgba(77, 97, 122, 0.14);
      --shadow-light: 0 12px 30px rgba(23, 35, 52, 0.06);
      --shadow-medium: 0 18px 40px rgba(23, 35, 52, 0.1);
      --shadow-heavy: 0 28px 60px rgba(23, 35, 52, 0.14);
      --radius-sm: 10px;
      --radius-md: 16px;
      --radius-lg: 24px;
      --radius-xl: 34px;
      --transition: all 0.22s cubic-bezier(0.2, 0.8, 0.2, 1);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Aptos", "Segoe UI Variable Text", "SF Pro Display", "Segoe UI", sans-serif;
      color: var(--text-primary);
      background:
        radial-gradient(circle at top left, rgba(22, 120, 243, 0.18), transparent 28%),
        radial-gradient(circle at 88% 10%, rgba(24, 166, 103, 0.12), transparent 24%),
        linear-gradient(180deg, #f3f7fb 0%, #edf2f7 48%, #e9eef5 100%);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      min-height: 100vh;
      position: relative;
    }
    body::before,
    body::after {
      content: "";
      position: fixed;
      inset: auto;
      border-radius: 999px;
      pointer-events: none;
      z-index: 0;
      filter: blur(10px);
      opacity: 0.55;
    }
    body::before {
      width: 340px;
      height: 340px;
      right: -120px;
      top: 110px;
      background: radial-gradient(circle, rgba(22,120,243,0.16), transparent 70%);
    }
    body::after {
      width: 300px;
      height: 300px;
      left: -90px;
      bottom: 40px;
      background: radial-gradient(circle, rgba(24,166,103,0.12), transparent 70%);
    }
    main {
      position: relative;
      z-index: 1;
      max-width: 1320px;
      margin: 0 auto;
      padding: 28px 20px 84px;
    }
    h1, h2, h3 { margin: 0 0 8px; }
    h1 {
      font-size: clamp(2.25rem, 5vw, 4.2rem);
      font-weight: 650;
      letter-spacing: -0.04em;
      color: var(--text-primary);
    }
    h2 {
      font-size: 1.35rem;
      font-weight: 650;
      letter-spacing: -0.025em;
      color: var(--text-primary);
    }
    h3 {
      font-size: 1.08rem;
      font-weight: 650;
      letter-spacing: -0.02em;
      color: var(--text-primary);
    }
    p { line-height: 1.65; }
    a {
      color: var(--accent-primary);
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    ::selection {
      background: rgba(22, 120, 243, 0.18);
    }
    ::-webkit-scrollbar {
      width: 10px;
      height: 10px;
    }
    ::-webkit-scrollbar-thumb {
      background: rgba(81, 96, 111, 0.26);
      border-radius: 999px;
      border: 2px solid transparent;
      background-clip: padding-box;
    }
    ::-webkit-scrollbar-track {
      background: transparent;
    }
    .hero {
      display: flex;
      align-items: center;
      gap: 14px;
      margin-bottom: 20px;
      padding: 14px 20px;
      background:
        linear-gradient(145deg, rgba(255,255,255,0.96) 0%, rgba(246,249,253,0.9) 100%);
      border: 1px solid rgba(255,255,255,0.62);
      border-radius: 20px;
      box-shadow: var(--shadow-light);
      backdrop-filter: blur(20px);
      position: relative;
      overflow: hidden;
    }
    .hero h1 {
      font-size: 1.1rem;
      margin: 0;
      letter-spacing: -0.01em;
      color: var(--text-primary);
    }
    .hero::after {
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at top right, rgba(22,120,243,0.12), transparent 32%),
        radial-gradient(circle at bottom left, rgba(24,166,103,0.08), transparent 28%);
      pointer-events: none;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      padding: 8px 14px;
      background: rgba(255,255,255,0.88);
      border: 1px solid rgba(77,97,122,0.12);
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--text-muted);
    }
    .nav-links {
      display: flex;
      gap: 8px;
      margin-bottom: 24px;
      flex-wrap: wrap;
      padding: 8px;
      width: fit-content;
      max-width: 100%;
      background: rgba(255,255,255,0.68);
      border: 1px solid rgba(255,255,255,0.58);
      border-radius: 999px;
      backdrop-filter: blur(18px);
      box-shadow: var(--shadow-light);
    }
    .nav-links a {
      display: inline-flex;
      align-items: center;
      padding: 10px 16px;
      background: transparent;
      border: 1px solid transparent;
      border-radius: 999px;
      color: var(--text-primary);
      text-decoration: none;
      font-size: 0.84rem;
      font-weight: 600;
      transition: var(--transition);
    }
    .nav-links a:hover {
      background: rgba(255,255,255,0.85);
      border-color: rgba(77,97,122,0.12);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
      text-decoration: none;
    }
    .layout, .admin-grid {
      display: grid;
      grid-template-columns: minmax(320px, 380px) 1fr;
      gap: 32px;
    }
    .admin-grid {
      grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    }
    .panel {
      background: linear-gradient(180deg, rgba(255,255,255,0.93), rgba(248,250,252,0.9));
      border: 1px solid rgba(255,255,255,0.62);
      border-radius: 28px;
      padding: 20px;
      box-shadow: var(--shadow-light);
      backdrop-filter: blur(18px);
      transition: var(--transition);
    }
    .sidebar-panel .panel {
      background: transparent;
      border: none;
      border-radius: 0;
      padding: 0;
      box-shadow: none;
      backdrop-filter: none;
    }
    .panel:hover {
      box-shadow: var(--shadow-medium);
      transform: translateY(-1px);
    }
    .panel-recent-chats {
      padding: 16px 20px;
    }
    .panel-recent-chats .history-list {
      max-height: 360px;
      gap: 8px;
    }
    .panel-recent-chats .history-card {
      padding: 12px 14px;
      border-radius: 16px;
    }
    .chat-panel {
      min-width: 0;
    }
    .avatar-panel {
      padding: 16px;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    .avatar-container {
      width: 100%;
      max-width: 160px;
    }
    /* Persona avatars */
    .persona-avatar {
      position: relative;
      border-radius: 50%;
      overflow: hidden;
      background: linear-gradient(145deg, rgba(240,244,248,0.9), rgba(220,228,236,0.8));
      border: 2px solid rgba(255,255,255,0.6);
      box-shadow: 0 4px 12px rgba(31,41,55,0.12);
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .persona-avatar img,
    .persona-avatar video {
      width: 100%;
      height: 100%;
      object-fit: cover;
      border-radius: 50%;
    }
    .persona-avatar-initial {
      font-size: 2rem;
      font-weight: 600;
      color: var(--accent-primary);
      text-transform: uppercase;
    }
    .persona-avatar-portrait {
      width: 100%;
      aspect-ratio: 1;
      max-width: 160px;
      max-height: 160px;
    }
    .persona-avatar-portrait .persona-avatar-initial {
      font-size: 3rem;
    }
    /* Two column layout for main content */
    .main-content-two-col {
      display: grid;
      grid-template-columns: 1fr 200px;
      gap: 20px;
      align-items: start;
    }
    .main-col-left {
      min-width: 0;
    }
    .main-col-right {
      position: sticky;
      top: 20px;
    }
    .avatar-panel {
      text-align: center;
    }
    .avatar-panel .avatar-container {
      margin: 0 auto;
    }
    /* 9:16 Portrait avatar for right panel */
    .persona-avatar-chat-portrait {
      width: 160px;
      aspect-ratio: 9 / 16;
      border-radius: 12px;
    }
    .persona-avatar-chat-portrait img,
    .persona-avatar-chat-portrait video {
      border-radius: 12px;
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .persona-avatar-chat-portrait .persona-avatar-initial {
      font-size: 2.5rem;
    }
    .persona-avatar-thumb {
      width: 56px;
      height: 56px;
      flex-shrink: 0;
    }
    .persona-avatar-thumb .persona-avatar-initial {
      font-size: 1.5rem;
    }
    .persona-avatar-small {
      width: 36px;
      height: 36px;
      flex-shrink: 0;
    }
    .persona-avatar-small .persona-avatar-initial {
      font-size: 1rem;
    }
    /* Avatar glow effects for voice activity */
    .avatar-container {
      position: relative;
      transition: var(--transition);
    }
    .avatar-container.speaking .persona-avatar,
    .avatar-container.listening .persona-avatar,
    .avatar-container.speaking .persona-avatar-chat-portrait,
    .avatar-container.listening .persona-avatar-chat-portrait {
      transition: all 0.3s ease;
    }
    .avatar-container.speaking .persona-avatar,
    .avatar-container.speaking .persona-avatar-chat-portrait {
      box-shadow: 0 0 20px 4px rgba(239, 68, 68, 0.6), 0 0 40px 8px rgba(239, 68, 68, 0.3);
      animation: avatar-glow-red 1.5s ease-in-out infinite;
    }
    .avatar-container.listening .persona-avatar,
    .avatar-container.listening .persona-avatar-chat-portrait {
      box-shadow: 0 0 20px 4px rgba(59, 130, 246, 0.6), 0 0 40px 8px rgba(59, 130, 246, 0.3);
      animation: avatar-glow-blue 1.5s ease-in-out infinite;
    }
    @keyframes avatar-glow-red {
      0%, 100% { box-shadow: 0 0 20px 4px rgba(239, 68, 68, 0.6), 0 0 40px 8px rgba(239, 68, 68, 0.3); }
      50% { box-shadow: 0 0 30px 6px rgba(239, 68, 68, 0.8), 0 0 60px 12px rgba(239, 68, 68, 0.4); }
    }
    @keyframes avatar-glow-blue {
      0%, 100% { box-shadow: 0 0 20px 4px rgba(59, 130, 246, 0.6), 0 0 40px 8px rgba(59, 130, 246, 0.3); }
      50% { box-shadow: 0 0 30px 6px rgba(59, 130, 246, 0.8), 0 0 60px 12px rgba(59, 130, 246, 0.4); }
    }
    /* Persona cards */
    .persona-card {
      border: 1px solid rgba(77,97,122,0.12);
      padding: 16px;
      border-radius: 20px;
      margin-bottom: 12px;
      background: linear-gradient(180deg, rgba(255,255,255,0.95), rgba(247,248,250,0.92));
      box-shadow: var(--shadow-light);
      transition: var(--transition);
    }
    .persona-card:hover {
      box-shadow: var(--shadow-medium);
      transform: translateY(-1px);
    }
    .persona-card-active {
      background: linear-gradient(180deg, rgba(232,244,255,0.95), rgba(222,237,255,0.92));
      border-color: rgba(22,120,243,0.25);
    }
    .persona-card-header {
      display: flex;
      align-items: center;
      gap: 14px;
      margin-bottom: 12px;
    }
    .persona-card-info {
      flex: 1;
      min-width: 0;
    }
    .persona-card-info h3 {
      margin: 0 0 4px 0;
      font-size: 1.05rem;
    }
    .persona-card-info p {
      margin: 0;
      font-size: 0.85rem;
    }
    .sidebar-shell {
      display: grid;
      gap: 20px;
      align-self: start;
      position: sticky;
      top: 16px;
    }
    .sidebar-panel {
      padding: 20px;
      border-radius: 28px;
      border: 1px solid rgba(255, 255, 255, 0.45);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.96) 0%, rgba(245,247,250,0.9) 100%);
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,0.75),
        0 10px 30px rgba(31, 41, 55, 0.08);
      backdrop-filter: blur(14px);
    }
    .sidebar-panel-voice {
      background:
        radial-gradient(circle at top left, rgba(26,115,232,0.08), transparent 42%),
        linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(242,247,255,0.94) 100%);
    }
    .sidebar-panel-upload {
      background:
        radial-gradient(circle at top left, rgba(26,115,232,0.12), transparent 42%),
        linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(242,247,255,0.94) 100%);
    }
    .sidebar-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }
    .sidebar-head h2 {
      font-size: 1rem;
      margin: 0;
      letter-spacing: -0.01em;
    }
    .sidebar-eyebrow {
      margin: 0 0 4px 0;
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--text-muted);
      font-weight: 700;
    }
    .sidebar-copy {
      margin: 0 0 12px 0;
      font-size: 0.82rem;
      line-height: 1.45;
    }
    .sidebar-form {
      gap: 10px;
    }
    .chip-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 30px;
      padding: 6px 12px;
      border-radius: 999px;
      text-decoration: none;
      background: rgba(255,255,255,0.8);
      border: 1px solid rgba(95,99,104,0.16);
      color: var(--text-primary);
      font-size: 0.78rem;
      font-weight: 600;
      transition: var(--transition);
    }
    .chip-link:hover {
      background: rgba(26,115,232,0.08);
      border-color: rgba(26,115,232,0.2);
    }
    form {
      display: grid;
      gap: 16px;
    }
    input[type="file"], input[type="text"], textarea, select {
      width: 100%;
      border: 1px solid rgba(77,97,122,0.14);
      border-radius: 16px;
      padding: 12px 15px;
      background: rgba(255,255,255,0.9);
      color: var(--text-primary);
      font: inherit;
      font-size: 0.875rem;
      transition: var(--transition);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.8);
    }
    input[type="file"]:focus, input[type="text"]:focus, textarea:focus, select:focus {
      outline: none;
      border-color: var(--accent-primary);
      background: var(--bg-strong);
      box-shadow:
        0 0 0 4px rgba(22, 120, 243, 0.12),
        inset 0 1px 0 rgba(255,255,255,0.82);
    }
    textarea {
      min-height: 120px;
      resize: vertical;
      line-height: 1.5;
    }
    button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 11px 20px;
      border: 1px solid rgba(18, 104, 211, 0.16);
      border-radius: 16px;
      background: linear-gradient(180deg, #2890ff 0%, #1678f3 100%);
      color: white;
      font: inherit;
      font-size: 0.875rem;
      font-weight: 650;
      cursor: pointer;
      transition: var(--transition);
      min-height: 44px;
      box-shadow:
        0 10px 22px rgba(22, 120, 243, 0.2),
        inset 0 1px 0 rgba(255,255,255,0.24);
    }
    .button-compact {
      min-height: 36px;
      padding: 9px 14px;
      font-size: 0.8rem;
      border-radius: 14px;
    }
    .button-tiny {
      min-height: 30px;
      padding: 6px 10px;
      font-size: 0.74rem;
      border-radius: 12px;
    }
    .button-ghost {
      background: rgba(255,255,255,0.82);
      color: var(--text-primary);
      border: 1px solid rgba(95,99,104,0.16);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.7);
    }
    .button-ghost:hover {
      background: rgba(26,115,232,0.08);
      color: var(--accent-primary);
      border-color: rgba(26,115,232,0.22);
      box-shadow: none;
    }
    .button-ghost.danger:hover {
      background: rgba(234,67,53,0.08);
      color: var(--accent-warning);
      border-color: rgba(234,67,53,0.22);
    }
    .button-warning {
      background: linear-gradient(180deg, #e4584f 0%, #d13d35 100%);
      color: white;
      border-color: rgba(180, 50, 40, 0.5);
      box-shadow:
        0 6px 14px rgba(228, 88, 79, 0.25),
        inset 0 1px 0 rgba(255,255,255,0.3);
    }
    .button-warning:hover {
      background: linear-gradient(180deg, #f05f56 0%, #e0453d 100%);
      transform: translateY(-1px);
      box-shadow:
        0 8px 18px rgba(228, 88, 79, 0.32),
        inset 0 1px 0 rgba(255,255,255,0.35);
    }
    .button-warning:disabled {
      background: linear-gradient(180deg, #e4584f 0%, #d13d35 100%);
      opacity: 0.5;
      cursor: not-allowed;
      transform: none;
    }
    button:hover {
      background: linear-gradient(180deg, #1f86fb 0%, #136eeb 100%);
      box-shadow:
        0 16px 28px rgba(22, 120, 243, 0.24),
        inset 0 1px 0 rgba(255,255,255,0.24);
      transform: translateY(-1px);
    }
    button:active {
      transform: translateY(0);
    }
    button:disabled {
      opacity: 0.6;
      cursor: not-allowed;
      transform: none;
    }
    ul {
      margin: 0;
      padding-left: 18px;
    }
    li + li { margin-top: 8px; }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
      font-size: 0.9rem;
    }
    .error {
      color: #9e2c26;
      margin: 0;
    }
    .banner {
      border: 1px solid rgba(76, 175, 80, 0.22);
      background: linear-gradient(180deg, rgba(227,250,236,0.88), rgba(238,252,244,0.92));
      color: #1b5e20;
      padding: 14px 18px;
      border-radius: 18px;
      margin-bottom: 18px;
    }
    .banner.success {
      border-color: rgba(76, 175, 80, 0.5);
      background: rgba(76, 175, 80, 0.15);
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .warning-panel {
      border-color: var(--accent-warning);
      background: linear-gradient(135deg, rgba(255,247,247,0.96) 0%, rgba(255,239,239,0.92) 100%);
    }
    .next-action-card {
      border-color: var(--accent-primary);
      background: linear-gradient(135deg, rgba(240,247,255,0.98) 0%, rgba(230,243,255,0.92) 100%);
      box-shadow: var(--shadow-medium);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      background: rgba(255,255,255,0.88);
      border: 1px solid rgba(77,97,122,0.12);
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--text-secondary);
    }
    .button-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .preset-grid, .checklist {
      display: grid;
      gap: 14px;
    }
    .wizard-grid {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    }
    .voice-grid {
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      margin-top: 16px;
    }
    .chat-thread {
      display: grid;
      gap: 16px;
      min-height: 140px;
      max-height: 520px;
      overflow-y: auto;
      padding: 16px;
      margin: 0 0 16px 0;
      border: 1px solid rgba(77,97,122,0.12);
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(255,255,255,0.82), rgba(249,251,253,0.88));
    }
    .chat-empty {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px;
      text-align: center;
      color: var(--text-muted);
      min-height: 200px;
    }
    .chat-empty p {
      margin: 8px 0 0 0;
    }
    .chat-bubble {
      max-width: min(85%, 700px);
      padding: 16px 20px;
      border-radius: 24px;
      border: 1px solid rgba(77,97,122,0.12);
      box-shadow: var(--shadow-light);
      position: relative;
    }
    .chat-bubble.user {
      justify-self: end;
      background: linear-gradient(180deg, #2890ff 0%, #1678f3 100%);
      color: white;
      border-color: rgba(22,120,243,0.2);
    }
    .chat-bubble.assistant {
      justify-self: start;
      background: rgba(255,255,255,0.88);
      color: var(--text-primary);
    }
    .chat-role {
      margin-bottom: 8px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      opacity: 0.8;
    }
    .chat-text {
      line-height: 1.6;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .chat-context {
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
    }
    .chat-context summary {
      cursor: pointer;
      color: var(--accent-primary);
      font-weight: 500;
      font-size: 0.875rem;
    }
    .chat-context summary:hover {
      color: #1557b0;
    }
    .history-list,
    .library-list,
    .error-list {
      display: grid;
      gap: 12px;
    }
    .history-list {
      gap: 10px;
      max-height: 420px;
      overflow-y: auto;
      padding-right: 2px;
    }
    .history-card,
    .library-card,
    .error-card,
    .preset-card,
    .check-row,
    .wizard-step {
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 20px;
      background: var(--bg-secondary);
      box-shadow: var(--shadow-light);
      transition: var(--transition);
    }
    .history-card:hover,
    .library-card:hover,
    .error-card:hover,
    .preset-card:hover,
    .check-row:hover,
    .wizard-step:hover {
      box-shadow: var(--shadow-medium);
      border-color: var(--border-hover);
    }
    .history-time {
      margin-bottom: 0;
      font-size: 0.68rem;
      color: var(--text-muted);
      font-weight: 600;
    }
    .history-question {
      margin-bottom: 6px;
      font-weight: 600;
      line-height: 1.35;
      color: var(--text-primary);
      font-size: 0.84rem;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .history-answer {
      color: var(--text-secondary);
      line-height: 1.4;
      font-size: 0.78rem;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .history-card {
      padding: 14px;
      border-radius: 20px;
      background: linear-gradient(180deg, rgba(255,255,255,0.95), rgba(247,248,250,0.92));
    }
    .history-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }
    .history-select-label {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 22px;
      height: 22px;
      border-radius: 999px;
      background: rgba(255,255,255,0.8);
      border: 1px solid rgba(95,99,104,0.14);
    }
    .history-select-label input {
      width: 12px;
      height: 12px;
      margin: 0;
    }
    .history-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 10px;
    }
    .history-edit-form {
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid rgba(95,99,104,0.12);
    }
    .history-edit-form textarea {
      min-height: 76px;
    }
    .history-controls {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      padding-top: 4px;
    }
    .status-card-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .status-card {
      padding: 12px;
      border-radius: 18px;
      border: 1px solid rgba(95,99,104,0.12);
      background: rgba(255,255,255,0.84);
      min-width: 0;
    }
    .status-card.good {
      background: linear-gradient(180deg, rgba(236,253,243,0.95), rgba(227,250,236,0.9));
    }
    .status-card.warn {
      background: linear-gradient(180deg, rgba(255,245,245,0.96), rgba(254,239,239,0.9));
    }
    .status-label {
      display: block;
      font-size: 0.67rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--text-muted);
      margin-bottom: 6px;
      font-weight: 700;
    }
    .status-value {
      display: block;
      font-size: 0.8rem;
      line-height: 1.35;
      word-break: break-word;
    }
    .resource-list,
    .error-compact-list {
      display: grid;
      gap: 8px;
    }
    .resource-list {
      max-height: 280px;
      overflow-y: auto;
      padding-right: 2px;
    }
    .resource-card,
    .error-compact-card {
      padding: 12px;
      border-radius: 18px;
      border: 1px solid rgba(95,99,104,0.12);
      background: rgba(255,255,255,0.82);
    }
    .resource-name {
      font-size: 0.82rem;
      font-weight: 600;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      margin-bottom: 8px;
    }
    .resource-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 0.72rem;
      color: var(--text-muted);
    }
    .resource-meta em {
      font-style: normal;
      max-width: 48%;
      text-align: right;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .error-compact-list {
      max-height: 220px;
      overflow-y: auto;
      padding-right: 2px;
    }
    .error-compact-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: baseline;
      margin-bottom: 6px;
      font-size: 0.74rem;
    }
    .error-compact-head span {
      color: var(--text-muted);
      white-space: nowrap;
      font-size: 0.68rem;
    }
    .error-compact-card p {
      margin: 0;
      font-size: 0.78rem;
      color: var(--text-secondary);
      line-height: 1.4;
    }
    .library-meta h3 {
      margin: 0 0 4px 0;
      font-size: 1rem;
    }
    .library-actions {
      display: grid;
      gap: 12px;
      margin-top: 16px;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    }
    .voice-activity {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      margin: 0 0 12px 0;
      padding: 12px 16px;
      border-radius: 24px;
      border: 1px solid var(--border);
      background: var(--bg-secondary);
      box-shadow: var(--shadow-light);
      font-weight: 500;
      color: var(--text-primary);
    }
    .voice-dot {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: var(--text-muted);
      transition: var(--transition);
    }
    .voice-activity.listening .voice-dot {
      background: var(--accent-secondary);
      animation: pulse-voice 1.5s infinite;
    }
    .voice-activity.loading .voice-dot,
    .voice-activity.thinking .voice-dot {
      background: var(--accent-info);
      animation: pulse-voice 1.5s infinite;
    }
    .voice-activity.speaking .voice-dot,
    .voice-activity.heard .voice-dot {
      background: var(--accent-primary);
      animation: pulse-voice 1.1s infinite;
    }
    .voice-activity.stopped .voice-dot {
      background: var(--text-muted);
    }
    /* Compact voice controls bar above chat */
    .voice-controls-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 10px 14px;
      margin-bottom: 12px;
      border-radius: 12px;
      background: var(--bg-secondary);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-light);
    }
    .voice-controls-left {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .voice-controls-right {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .voice-badge {
      display: inline-block;
      padding: 4px 10px;
      border-radius: 20px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .voice-badge.listening {
      background: rgba(34, 197, 94, 0.15);
      color: var(--accent-secondary);
    }
    .voice-badge.standby {
      background: rgba(59, 130, 246, 0.15);
      color: var(--accent-info);
    }
    .voice-badge.off {
      background: var(--bg-tertiary);
      color: var(--text-muted);
    }
    .voice-status-text {
      font-size: 0.85rem;
      max-width: 300px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .preset-grid {
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }
    .preset-card, .check-row, .wizard-step {
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.8);
    }
    .wizard-step.complete,
    .check-row.ready {
      border-color: var(--accent-secondary);
      background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
    }
    .wizard-step.pending,
    .check-row.pending {
      border-color: var(--accent-warning);
      background: linear-gradient(135deg, #fef7f7 0%, #fef2f2 100%);
    }
    .error-list {
      display: grid;
      gap: 14px;
    }
    .error-card {
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.82);
    }
    .error-meta {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 8px;
      flex-wrap: wrap;
    }
    .table-wrap {
      overflow-x: auto;
      border-radius: 20px;
      border: 1px solid rgba(77,97,122,0.12);
      background: rgba(255,255,255,0.76);
      backdrop-filter: blur(16px);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.875rem;
    }
    th, td {
      padding: 12px 16px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th {
      font-weight: 700;
      color: var(--text-primary);
      background: rgba(245,248,251,0.9);
    }
    tbody tr:hover {
      background: var(--bg-tertiary);
    }
    .muted {
      color: var(--text-muted) !important;
      font-size: 0.84rem;
    }
    .progress-block {
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid var(--line);
    }
    .helper-box {
      margin-top: 16px;
      border: 1px dashed rgba(77,97,122,0.18);
      border-radius: 20px;
      padding: 16px;
      background: rgba(248,250,252,0.86);
    }
    .button-link {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 12px 24px;
      border: none;
      border-radius: 16px;
      background: linear-gradient(180deg, #2890ff 0%, #1678f3 100%);
      color: white;
      text-decoration: none;
      font: inherit;
      font-size: 0.875rem;
      font-weight: 650;
      cursor: pointer;
      transition: var(--transition);
      min-height: 44px;
    }
    .button-link:hover {
      background: linear-gradient(180deg, #1f86fb 0%, #136eeb 100%);
      box-shadow: var(--shadow-medium);
      transform: translateY(-1px);
    }
    .steps {
      margin: 0 0 16px;
      padding-left: 18px;
    }
    @keyframes pulse-voice {
      0% { transform: scale(1); box-shadow: 0 0 0 0 currentColor; }
      70% { transform: scale(1.08); box-shadow: 0 0 0 10px transparent; }
      100% { transform: scale(1); box-shadow: 0 0 0 0 transparent; }
    }
    @media (max-width: 1024px) {
      .layout,
      .admin-grid {
        grid-template-columns: 1fr;
        gap: 24px;
      }
      .layout > aside {
        order: 2;
      }
      .layout > div {
        order: 1;
      }
      .sidebar-shell {
        position: static;
      }
      .status-card-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .section-head {
        align-items: flex-start;
        flex-direction: column;
        gap: 8px;
      }

      .hero {
        padding: 26px 22px;
      }

      .panel-recent-chats {
        order: 3;
        margin-top: 8px;
      }

      .main-content-two-col {
        grid-template-columns: 1fr 140px;
        gap: 12px;
      }
      .main-col-right {
        position: static;
      }
      .avatar-panel {
        padding: 12px 8px;
      }
      .persona-avatar-chat-portrait {
        width: 100px;
      }
      .voice-controls-bar {
        flex-wrap: wrap;
        gap: 8px;
      }
      .voice-status-text {
        max-width: 200px;
      }

      main {
        padding: 18px 12px 60px;
      }
    }

    @media (max-width: 640px) {
      .main-content-two-col {
        grid-template-columns: 1fr;
      }
      .voice-controls-bar {
        flex-direction: column;
        align-items: flex-start;
      }
      .avatar-panel {
        order: -1;
        flex-direction: row;
        justify-content: center;
      }
      .avatar-panel .muted {
        display: none;
      }
    }

    /* Accessibility */
    @media (prefers-reduced-motion: reduce) {
      * {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }
    }

    /* Focus states */
    button:focus-visible,
    .button-link:focus-visible,
    input:focus-visible,
    textarea:focus-visible,
    select:focus-visible {
      outline: 2px solid var(--accent-primary);
      outline-offset: 2px;
    }

    /* Dark mode support (future enhancement) */
    @media (prefers-color-scheme: dark) {
      :root {
        --bg-primary: #151b23;
        --bg-secondary: rgba(32, 39, 49, 0.86);
        --bg-elevated: rgba(39, 48, 61, 0.96);
        --bg-tertiary: rgba(31, 39, 49, 0.94);
        --bg-strong: #202833;
        --text-primary: #eef4fb;
        --text-secondary: #b2c0cf;
        --text-muted: #8c9db0;
        --border: rgba(166, 184, 206, 0.16);
        --border-hover: rgba(96, 173, 255, 0.34);
        --line: rgba(166, 184, 206, 0.14);
      }
      body {
        background:
          radial-gradient(circle at top left, rgba(22, 120, 243, 0.18), transparent 28%),
          radial-gradient(circle at 88% 10%, rgba(24, 166, 103, 0.12), transparent 24%),
          linear-gradient(180deg, #131922 0%, #161d27 48%, #121923 100%);
      }
      .sidebar-panel,
      .status-card,
      .resource-card,
      .error-compact-card,
      .history-card {
        background: rgba(34, 42, 53, 0.9);
        border-color: rgba(166,184,206,0.14);
      }
      .panel,
      .hero,
      .table-wrap,
      .chat-thread,
      .chat-bubble.assistant,
      .helper-box,
      .voice-activity,
      .preset-card,
      .check-row,
      .wizard-step,
      .error-card {
        background: rgba(29, 36, 46, 0.86);
        border-color: rgba(166,184,206,0.14);
      }
      .persona-avatar {
        background: linear-gradient(145deg, rgba(50,58,70,0.9), rgba(40,48,60,0.8));
        border-color: rgba(166,184,206,0.2);
      }
      .persona-card {
        background: linear-gradient(180deg, rgba(34,42,53,0.95), rgba(29,36,46,0.92));
        border-color: rgba(166,184,206,0.14);
      }
      .persona-card-active {
        background: linear-gradient(180deg, rgba(22,60,100,0.95), rgba(18,50,85,0.92));
        border-color: rgba(22,120,243,0.3);
      }
      input[type="file"], input[type="text"], textarea, select,
      .button-ghost,
      .nav-links,
      .nav-links a:hover,
      .badge,
      .chip-link {
        background: rgba(38, 46, 58, 0.9);
        color: var(--text-primary);
        border-color: rgba(166,184,206,0.14);
      }
    }

    /* Avatar Lightbox Modal */
    .avatar-lightbox {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(0, 0, 0, 0.85);
      backdrop-filter: blur(8px);
      z-index: 10000;
      justify-content: center;
      align-items: center;
      padding: 20px;
      box-sizing: border-box;
    }
    .avatar-lightbox.active {
      display: flex;
    }
    .avatar-lightbox-content {
      position: relative;
      max-width: 90vw;
      max-height: 90vh;
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    .avatar-lightbox-media {
      max-width: 100%;
      max-height: 80vh;
      border-radius: 12px;
      box-shadow: 0 20px 60px rgba(0,0,0,0.5);
    }
    .avatar-lightbox-media img {
      max-width: 100%;
      max-height: 80vh;
      border-radius: 12px;
      display: block;
    }
    .avatar-lightbox-media video {
      max-width: 100%;
      max-height: 80vh;
      border-radius: 12px;
      display: block;
    }
    .avatar-lightbox-close {
      position: absolute;
      top: -50px;
      right: 0;
      background: rgba(255,255,255,0.15);
      border: none;
      color: white;
      font-size: 28px;
      width: 44px;
      height: 44px;
      border-radius: 50%;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background 0.2s ease;
    }
    .avatar-lightbox-close:hover {
      background: rgba(255,255,255,0.25);
    }
    .avatar-lightbox-info {
      margin-top: 16px;
      text-align: center;
      color: white;
      font-size: 0.95rem;
      opacity: 0.9;
    }
    .avatar-lightbox-controls {
      margin-top: 20px;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    .avatar-lightbox-controls .voice-controls-bar {
      background: rgba(255, 255, 255, 0.1);
      backdrop-filter: blur(10px);
      border-radius: 25px;
      padding: 12px 20px;
      border: 1px solid rgba(255, 255, 255, 0.2);
    }
    .avatar-lightbox-controls .voice-controls-bar .button-compact {
      background: rgba(255, 255, 255, 0.15);
      color: white;
      border: 1px solid rgba(255, 255, 255, 0.3);
    }
    .avatar-lightbox-controls .voice-controls-bar .button-compact:hover {
      background: rgba(255, 255, 255, 0.25);
      border-color: rgba(255, 255, 255, 0.5);
    }
    .avatar-lightbox-controls .voice-controls-bar .voice-badge {
      background: rgba(255, 255, 255, 0.15);
      color: white;
      border: 1px solid rgba(255, 255, 255, 0.3);
    }
    .avatar-lightbox-info .persona-name {
      font-weight: 600;
      font-size: 1.1rem;
    }
    .avatar-clickable {
      cursor: pointer;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .avatar-clickable:hover {
      transform: scale(1.02);
      box-shadow: 0 8px 25px rgba(22, 120, 243, 0.25);
    }
    .avatar-clickable::after {
      content: "🔍";
      position: absolute;
      bottom: 8px;
      right: 8px;
      background: rgba(0,0,0,0.5);
      color: white;
      width: 28px;
      height: 28px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      opacity: 0;
      transition: opacity 0.2s ease;
      pointer-events: none;
    }
    .avatar-clickable:hover::after {
      opacity: 1;
    }
    @media (max-width: 640px) {
      .avatar-lightbox-close {
        top: -40px;
        right: -10px;
      }
      .avatar-lightbox-info {
        font-size: 0.85rem;
      }
      .avatar-lightbox-controls {
        margin-top: 16px;
      }
      .avatar-lightbox-controls .voice-controls-bar {
        padding: 10px 16px;
        font-size: 0.85rem;
      }
      .avatar-lightbox-controls .voice-controls-bar .button-compact {
        padding: 6px 12px;
        font-size: 0.8rem;
      }
    }

    /* Public Mode layout adjustments */
    body.public-mode .layout {
      grid-template-columns: 1fr;
      max-width: 1400px;
      margin: 0 auto;
    }
    body.public-mode .main-content-two-col {
      grid-template-columns: 70% 30%;
      gap: 24px;
    }
    body.public-mode .main-col-left {
      max-width: 100%;
    }
    body.public-mode .main-col-right {
      max-width: 100%;
    }
    body.public-mode .main-col-right .avatar-panel {
      padding: 16px 12px;
    }
    body.public-mode .main-col-right .persona-avatar-chat-portrait {
      width: 100%;
      max-width: 200px;
    }
    /* Admin access link in Public Mode */
    .admin-access-link {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      background: #f3f4f6;
      border-radius: 6px;
      font-size: 0.85rem;
      color: #6b7280;
      text-decoration: none;
      transition: all 0.2s ease;
    }
    .admin-access-link:hover {
      background: #e5e7eb;
      color: #374151;
    }
    /* Voice controls row with mobile avatar */
    .voice-controls-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .mobile-admin-avatar {
      display: none;
    }

    @media (max-width: 960px) {
      body.public-mode .main-content-two-col {
        grid-template-columns: 1fr;
      }
      /* Mobile: Compact circular avatar above chat */
      body.public-mode .main-col-right {
        order: -1;
        position: sticky;
        top: 0;
        z-index: 100;
        background: white;
        padding: 8px 0;
        margin-bottom: 8px;
      }
      body.public-mode .main-col-right .avatar-panel {
        padding: 0;
        background: transparent;
        box-shadow: none;
        border: none;
      }
      body.public-mode .main-col-right .avatar-panel h2 {
        display: none;
      }
      body.public-mode .main-col-right .avatar-container {
        width: 48px;
        height: 48px;
        margin: 0 auto;
      }
      body.public-mode .main-col-right .persona-avatar-chat-portrait {
        width: 48px;
        height: 48px;
        max-width: 48px;
        border-radius: 50%;
        cursor: pointer;
        transition: transform 0.2s ease;
      }
      body.public-mode .main-col-right .persona-avatar-chat-portrait:hover {
        transform: scale(1.1);
      }
      body.public-mode .main-col-right .avatar-container .muted {
        display: none;
      }
      /* Make avatar clickable with pointer */
      body.public-mode .main-col-right .avatar-container .persona-avatar-chat-portrait {
        cursor: zoom-in;
      }

      /* Admin Mode mobile avatar next to voice controls */
      .mobile-admin-avatar {
        display: block;
        width: 44px;
        height: 44px;
        flex-shrink: 0;
        cursor: zoom-in;
        border-radius: 50%;
        overflow: hidden;
        box-shadow: 0 2px 8px rgba(0,0,0,0.15);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
      }
      .mobile-admin-avatar:hover {
        transform: scale(1.1);
        box-shadow: 0 4px 12px rgba(0,0,0,0.2);
      }
      .mobile-admin-avatar .persona-avatar-small {
        width: 100%;
        height: 100%;
        border-radius: 50%;
      }
      .mobile-admin-avatar .persona-avatar-small img,
      .mobile-admin-avatar .persona-avatar-small video {
        width: 100%;
        height: 100%;
        object-fit: cover;
      }
      /* Hide the desktop avatar panel in mobile */
      body:not(.public-mode) .main-col-right {
        display: none;
      }
      /* Full width for chat panel in mobile admin mode */
      body:not(.public-mode) .main-content-two-col {
        grid-template-columns: 1fr;
      }
    }
  </style>"""
