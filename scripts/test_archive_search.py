#!/usr/bin/env python3
"""
Test script to verify archive search is working after re-indexing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from archiveum.config import build_paths, load_settings
from archiveum.embeddings import ArchiveumEmbeddings
from archiveum.store import ArchiveStore
from archiveum.assistant import ArchiveumAssistant


def test_archive_search():
    """Test that archive search returns relevant results."""
    paths = build_paths(Path(__file__).parent.parent)
    settings = load_settings(paths)
    
    # Initialize components
    embeddings = ArchiveumEmbeddings(settings=settings)
    store = ArchiveStore(paths.chunks_path)
    assistant = ArchiveumAssistant(settings=settings)
    
    test_queries = [
        "Where is george's office?",  # Should trigger archive (explicit "where is")
        "Can you tell me about the company profile?",  # Should trigger archive (informational)
        "Who are the directors?",  # Should trigger archive (explicit "who are the")
        "What's on floor 3?",  # Should trigger archive (informational)
        "Have you ever been to alice's office?",  # Should be chat (conversational)
        "Why not?",  # Should be chat (follow-up)
        "Can you take me there?",  # Should be chat (conversational)
        "What days do you have free?",  # Should be chat (conversational)
        "Where do you go on holiday?",  # Should be chat (conversational)
    ]
    
    print("=" * 70)
    print("ARCHIVE SEARCH TEST")
    print("=" * 70)
    print(f"Embedding Model: {embeddings.model}")
    print(f"Store Stats: {store.stats()}")
    print()
    
    for query in test_queries:
        print(f"Query: {query}")
        print("-" * 70)
        
        # Test with assistant (uses same search logic)
        result = assistant.ask(
            query,
            prefer_archive_retrieval=True,  # Public Mode behavior
            avatar_context="",
        )
        
        print(f"Mode: {result.mode}")
        print(f"Matches: {len(result.matches)}")
        
        if result.matches:
            for i, match in enumerate(result.matches, 1):
                score = match.get("score", 0)
                source = match.get("source", "unknown")
                text_preview = match.get("text", "")[:80].replace("\n", " ")
                print(f"  [{i}] {source} (score: {score})")
                print(f"      {text_preview}...")
            print()
            print(f"Answer: {result.answer[:200]}...")
        else:
            print("  [No matches found]")
            print(f"Answer: {result.answer}")
        
        print()


if __name__ == "__main__":
    test_archive_search()
