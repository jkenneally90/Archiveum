from __future__ import annotations

from pathlib import Path

from archiveum.config import ArchiveumPaths, build_paths, load_settings


FACTUAL_CATEGORIES = [
    ("factual/current_reading", "Current Reading"),
    ("factual/analysis", "Analysis"),
    ("factual/summaries", "Summaries"),
    ("factual/reference_notes", "Reference Notes"),
    ("factual/research", "Research"),
]

FICTION_CATEGORIES = [
    ("fiction/prompts/detective", "Detective Prompts"),
    ("fiction/prompts/mystery", "Mystery Prompts"),
    ("fiction/prompts/humor", "Humor Prompts"),
    ("fiction/prompts/fantasy", "Fantasy Prompts"),
    ("fiction/prompts/science_fiction", "Science Fiction Prompts"),
    ("fiction/worldbuilding", "Worldbuilding"),
    ("fiction/characters", "Characters"),
]

PROMPT_TEMPLATES: dict[str, str] = {
    "fiction/prompts/detective/original_detective_story_prompt.txt": (
        "Write an original detective story.\n\n"
        "Core requirements:\n"
        "- Create a fresh mystery with a clear crime, clue trail, and final reveal.\n"
        "- Keep the tone grounded, clever, and suspenseful.\n"
        "- Introduce a memorable investigator with a distinct method.\n"
        "- Make the ending feel earned from the clues already given.\n"
    ),
    "fiction/prompts/mystery/original_mystery_story_prompt.txt": (
        "Write an original mystery story.\n\n"
        "Core requirements:\n"
        "- Build tension through uncertainty, hidden motives, and careful reveals.\n"
        "- Focus on atmosphere and pacing as much as plot.\n"
        "- Give the reader enough clues to speculate without making the answer obvious.\n"
        "- End with a satisfying explanation.\n"
    ),
    "fiction/prompts/humor/original_humor_story_prompt.txt": (
        "Write an original humorous story.\n\n"
        "Core requirements:\n"
        "- Keep the tone playful and character-driven.\n"
        "- Use misunderstandings, reversals, or absurd escalation.\n"
        "- Aim for warmth and wit, not cruelty.\n"
        "- Finish with a strong comedic payoff.\n"
    ),
    "fiction/worldbuilding/setting_notes_template.txt": (
        "Worldbuilding notes template\n\n"
        "Setting name:\n"
        "Time period:\n"
        "Geography:\n"
        "Culture:\n"
        "Power structures:\n"
        "Conflicts:\n"
        "Important locations:\n"
        "Story possibilities:\n"
    ),
}


def default_upload_category_options() -> list[tuple[str, str]]:
    return FACTUAL_CATEGORIES + FICTION_CATEGORIES


def custom_upload_category_options(paths: ArchiveumPaths | None = None) -> list[tuple[str, str]]:
    settings = load_settings(paths or build_paths())
    return [(item["path"], item["label"]) for item in settings.custom_upload_categories]


def upload_category_options(paths: ArchiveumPaths | None = None) -> list[tuple[str, str]]:
    return default_upload_category_options() + custom_upload_category_options(paths)


def ensure_library_structure(uploads_dir: Path, extra_categories: list[tuple[str, str]] | None = None) -> None:
    uploads_dir.mkdir(parents=True, exist_ok=True)

    for relative_path, _label in default_upload_category_options() + (extra_categories or []):
        (uploads_dir / relative_path).mkdir(parents=True, exist_ok=True)

    for relative_path, template in PROMPT_TEMPLATES.items():
        target = uploads_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(template, encoding="utf-8")

