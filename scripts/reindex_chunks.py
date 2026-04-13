#!/usr/bin/env python3
"""
Re-embed all chunks in the store with the current embedding model.
This fixes embedding space mismatches when the embedding model changes.
"""
import json
import sys
from pathlib import Path

# Add parent to path so we can import archiveum modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from archiveum.config import build_paths, load_settings
from archiveum.embeddings import ArchiveumEmbeddings
from archiveum.store import ArchiveStore


def reindex_chunks():
    """Re-embed all chunks with the current embedding model."""
    paths = build_paths(Path(__file__).parent.parent)
    settings = load_settings(paths)
    embeddings = ArchiveumEmbeddings(settings=settings)
    store = ArchiveStore(paths.chunks_path)

    chunks = store._chunks
    if not chunks:
        print("No chunks found to reindex.")
        return

    print(f"Found {len(chunks)} chunks to reindex")
    print(f"Using embedding model: {embeddings.model}")
    print(f"URL: {embeddings.url}")
    print()

    # Group chunks by text to batch embed (avoid embedding the same text twice)
    unique_texts = {}
    for chunk in chunks:
        text = chunk.get("text", "")
        if text and text not in unique_texts:
            unique_texts[text] = []
        if text:
            unique_texts[text].append(chunk)

    print(f"Found {len(unique_texts)} unique text chunks")
    print("Embedding chunks...")

    # Embed all unique texts in batches
    all_texts = list(unique_texts.keys())
    batch_size = 32
    embeddings_map = {}

    for i in range(0, len(all_texts), batch_size):
        batch = all_texts[i : i + batch_size]
        print(f"  Batch {i // batch_size + 1}: embedding {len(batch)} texts...", end="", flush=True)

        try:
            batch_embeddings = embeddings.embed_texts(batch)
            for text, vector in zip(batch, batch_embeddings):
                embeddings_map[text] = vector
            print(" done")
        except Exception as e:
            print(f" ERROR: {e}")
            return False

    print()
    print("Updating chunks with new embeddings...")

    # Update all chunks with new embeddings
    for chunk in chunks:
        text = chunk.get("text", "")
        if text in embeddings_map:
            chunk["embedding"] = embeddings_map[text]
            chunk["embedding_model"] = embeddings.model

    # Save updated chunks
    store._chunks = chunks
    store._save()

    print(f"✓ Successfully reindexed {len(chunks)} chunks")
    print(f"  Embedding model: {embeddings.model}")
    print(f"  Saved to: {paths.chunks_path}")
    return True


if __name__ == "__main__":
    success = reindex_chunks()
    sys.exit(0 if success else 1)
