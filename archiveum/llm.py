from __future__ import annotations

import os
import re

import requests

from archiveum.config import AppSettings
from archiveum.personas import get_persona, get_persona_system_prompt


DEFAULT_SYSTEM_PROMPT = (
    "You are Archiveum, a calm and thoughtful archive companion. "
    "Be warm, conversational, and easy to talk to. "
    "Answer using retrieved archive context only when it is genuinely relevant or the user is clearly asking about documents, files, or archive material. "
    "When archive material is not needed, reply like a natural conversation shaped by the active persona. "
    "When you do use archive context, be honest about uncertainty and cite source filenames in plain language when possible. "
    "Prefer plain natural sentences over markdown-heavy formatting unless structure is genuinely helpful."
)

GROUNDING_APPENDIX = (
    " Stay grounded in retrieved archive context when it is relevant. "
    "If the user is just chatting, do not force the conversation back to the archive. "
    "If the archive does not support a claim, avoid inventing details. "
    "Keep answers natural and suitable for speaking aloud."
)

MEMORY_APPENDIX = (
    " You have access to memory from previous conversations. "
    "Use this memory naturally to maintain continuity and recall user preferences. "
    "Don't explicitly mention having a memory system unless the user asks."
)

AVATAR_APPENDIX = (
    " You are aware of your current visual appearance as shown to the user. "
    "You can reference your avatar's description naturally in conversation when relevant, "
    "such as describing what you're doing, wearing, or your surroundings. "
    "Keep references natural and context-appropriate rather than forced."
)


class ArchiveumLLM:
    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        settings: AppSettings | None = None,
    ) -> None:
        self.settings = settings
        if settings is not None:
            self.url = url or settings.ollama_chat_url
            self.model = model or settings.ollama_chat_model
            self.timeout = timeout if timeout != 120 else settings.ollama_timeout
        else:
            self.url = url or os.getenv("ARCHIVEUM_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
            self.model = model or os.getenv("ARCHIVEUM_OLLAMA_MODEL", "llama3.1:8b")
            self.timeout = timeout

    def answer(
        self,
        question: str,
        retrieved_context: str,
        *,
        mode: str = "chat",
        memory_context: str = "",
        recent_chats: str = "",
        avatar_context: str = "",
        persona_id_override: str | None = None,
    ) -> str:
        persona_name = "Archiveum"
        model_name = self.model
        if persona_id_override:
            persona = get_persona(persona_id_override)
            if persona and persona.name:
                persona_name = persona.name
            if persona and persona.llm_model:
                model_name = persona.llm_model

        if mode == "archive":
            prompt = (
                "The user is asking about archive material.\n"
                "Answer from the provided archive context only.\n"
                "Treat the archive context as the only allowed source of facts.\n"
                "If a fact is not stated in the context, do not infer it, embellish it, or fill it in.\n"
                "If the context is incomplete, say that briefly instead of inventing details.\n"
                "Do not claim the information is hidden, buried, hard to access, or unavailable if it is present in the context.\n"
                "Do not add speculative backstory, fictional events, or extra narrative flourishes.\n"
                "Do not output exercises, writing prompts, roleplay, or dialogue transcripts.\n\n"
                f"Question:\n{question.strip()}\n\n"
                f"Archive context:\n{retrieved_context or 'No relevant archive context was found.'}\n\n"
                "Give one direct reply only.\n"
                "Do not script both sides of a conversation.\n"
                "Do not invent follow-up questions from the user.\n"
                "When useful, name the source file in plain language.\n"
                "Prefer a concise summary of what the context explicitly says.\n"
                "If you mention founders, products, goals, architecture, controversy, or current status, use only details explicitly present in the context.\n"
                "Do not rewrite the answer as marketing copy.\n"
                "Do not introduce names, titles, roles, dates, or technical claims unless they appear in the context.\n"
                "Respond conversationally, but keep the answer grounded in the provided context."
            )
        else:
            # Chat mode - include memory and avatar context if available
            memory_section = f"\n{memory_context}\n{recent_chats}" if (memory_context or recent_chats) else ""
            avatar_section = avatar_context if avatar_context else ""
            prompt = (
                "The user is having a normal conversation rather than asking for archive retrieval.\n"
                "Reply as the active persona in a warm, natural, chat-friendly way.\n"
                "Do not mention missing archive context, retrieved files, or limitations of the archive unless the user asks about them directly.\n"
                "Answer the user's latest message only.\n"
                "Do not continue the conversation by writing the user's next lines.\n"
                "Do not simulate a back-and-forth dialogue.\n"
                "Do not include speaker labels like User, Assistant, or "
                f"{persona_name}.\n"
                f"{memory_section}{avatar_section}\n"
                f"Question:\n{question.strip()}\n\n"
                "Respond conversationally."
            )

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self._system_prompt(persona_id_override=persona_id_override)},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {
                "temperature": 0.05,
                "stop": [
                    "\nUser:",
                    "\nAssistant:",
                    "\nArchiveum:",
                    f"\n{persona_name}:",
                    "\nExercise:",
                    "\nPerson A:",
                    "\nPerson B:",
                ]
            },
        }

        try:
            response = requests.post(self.url, json=payload, timeout=self.timeout)
            if not response.ok:
                body = (response.text or "").strip()
                detail = f"HTTP {response.status_code}"
                if body:
                    detail += f"; response={body}"
                raise RuntimeError(f"Ollama chat request failed for {self.url}: {detail}")
            data = response.json()
            raw = data.get("message", {}).get("content", "").strip()
            return self._clean_response(raw, persona_name=persona_name)
        except requests.RequestException as exc:
            raise RuntimeError(f"Ollama chat request failed for {self.url}: {exc}") from exc

    def _system_prompt(self, persona_id_override: str | None = None) -> str:
        # Priority: custom prompt > persona > default
        custom = ""
        if hasattr(self, "settings") and self.settings is not None:
            custom = (self.settings.custom_system_prompt or "").strip()

        # If custom prompt is set, use it
        if custom:
            return custom + GROUNDING_APPENDIX + MEMORY_APPENDIX + AVATAR_APPENDIX

        # Try to use selected persona
        if hasattr(self, "settings") and self.settings is not None:
            persona_id = (persona_id_override or self.settings.current_persona_id or "").strip()
            if persona_id:
                persona_prompt = get_persona_system_prompt(persona_id)
                if persona_prompt:
                    return persona_prompt + GROUNDING_APPENDIX + MEMORY_APPENDIX + AVATAR_APPENDIX

        # Fall back to default persona
        return DEFAULT_SYSTEM_PROMPT + GROUNDING_APPENDIX + MEMORY_APPENDIX + AVATAR_APPENDIX

    def _clean_response(self, text: str, *, persona_name: str = "Archiveum") -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return ""

        # Drop a leading echoed question if the model repeats the user's prompt before answering.
        if cleaned.startswith("|"):
            segments = [segment.strip() for segment in cleaned.split("|") if segment.strip()]
            if len(segments) >= 2 and segments[0].endswith("?"):
                cleaned = segments[1]
        if cleaned.lower().startswith("question:"):
            cleaned = cleaned.split(":", 1)[1].strip()
        question_echo_match = re.match(r"^[^?\n]{0,220}\?\s+", cleaned)
        if question_echo_match:
            cleaned = cleaned[question_echo_match.end():].strip()

        # Drop common unsupported framing the model sometimes invents ahead of a grounded answer.
        unsupported_leads = [
            "to answer your question",
            "i must first admit",
            "i don't have all the details",
            "the information you're looking for is buried",
            "however, i can provide a general overview",
        ]
        lowered = cleaned.lower()
        for lead in unsupported_leads:
            if lowered.startswith(lead):
                parts = re.split(r"\n\s*\n", cleaned, maxsplit=1)
                if len(parts) == 2:
                    cleaned = parts[1].strip()
                break

        # If the model starts scripting the next speaker, cut it off at the first new speaker turn.
        speaker_patterns = [
            r"\n\s*User\s*:",
            r"\n\s*Assistant\s*:",
            r"\n\s*Archiveum\s*:",
            rf"\n\s*{re.escape(persona_name)}\s*:",
            r"\n\s*You\s*:",
            r"\n\s*Exercise\s*:",
            r"\n\s*Person A\s*:",
            r"\n\s*Person B\s*:",
        ]
        first_cut = None
        for pattern in speaker_patterns:
            match = re.search(pattern, cleaned, flags=re.IGNORECASE)
            if match:
                first_cut = match.start() if first_cut is None else min(first_cut, match.start())
        if first_cut is not None:
            cleaned = cleaned[:first_cut].rstrip()

        # A second guard for dialogue transcripts without explicit speaker labels.
        double_newline_patterns = [
            "\n\nOh, that sounds",
            "\n\nThat sounds",
            "\n\nDefinitely.",
            "\n\nAbsolutely.",
            "\n\nI know, right?",
            "\n\nWhat kind of",
            "\n\nDo you have any idea",
            "\n\nExercise:",
            "\n\nPerson A:",
            "\n\nPerson B:",
        ]
        for marker in double_newline_patterns:
            idx = cleaned.find(marker)
            if idx > 0:
                cleaned = cleaned[:idx].rstrip()
                break

        return cleaned
