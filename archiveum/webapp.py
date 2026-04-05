from __future__ import annotations

from html import escape
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from archiveum.assistant import ArchiveumAssistant
from archiveum.config import ensure_settings_file


app = FastAPI(title="Archiveum")
ensure_settings_file()
assistant = ArchiveumAssistant()
paths = assistant.paths


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return _render_page()


@app.get("/admin", response_class=HTMLResponse)
def admin() -> str:
    return _render_admin_page()


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
def status() -> dict:
    diagnostics = assistant.diagnostics()
    diagnostics["sources"] = assistant.store.list_sources()
    return diagnostics


@app.post("/admin/errors/clear")
async def clear_ingestion_errors(filename: str = Form("")) -> RedirectResponse:
    target = (filename or "").strip()
    if target:
        assistant.runtime_status.clear_ingestion_error(target)
    else:
        assistant.runtime_status.clear_all_ingestion_errors()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/upload")
async def upload_document(file: UploadFile = File(...)) -> RedirectResponse:
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    destination = paths.uploads_dir / filename
    destination.write_bytes(await file.read())

    chunk_count = assistant.ingest_file(destination)
    if not chunk_count:
        raise HTTPException(status_code=400, detail="The uploaded file did not contain usable text.")

    return RedirectResponse(url="/", status_code=303)


@app.post("/chat", response_class=HTMLResponse)
async def chat(question: str = Form(...)) -> str:
    question = question.strip()
    if not question:
        return _render_page(error="Ask a question first.")

    try:
        result = assistant.ask(question)
        answer = result.answer
        context = result.context
    except Exception as exc:
        return _render_page(question=question, error=f"Archive search failed: {exc}")

    return _render_page(question=question, answer=answer, context=context)


def _render_page(
    *,
    question: str = "",
    answer: str = "",
    context: str = "",
    error: str = "",
) -> str:
    diagnostics = assistant.diagnostics()
    source_rows = "".join(
        f"<li><strong>{escape(item['source'])}</strong> <span>{item['chunks']} chunks</span> <em>{escape(str(item['embedding_model']))}</em></li>"
        for item in assistant.store.list_sources()
    ) or "<li>No files indexed yet.</li>"
    status_items = [
        f"<li><strong>Ready:</strong> {escape(str(diagnostics['ready']))}</li>",
        f"<li><strong>Voice Ready:</strong> {escape(str(diagnostics['voice_ready']))}</li>",
        f"<li><strong>Indexed Documents:</strong> {escape(str(diagnostics['index']['indexed_documents']))}</li>",
        f"<li><strong>Indexed Chunks:</strong> {escape(str(diagnostics['index']['indexed_chunks']))}</li>",
        f"<li><strong>Chat Model:</strong> {escape(diagnostics['settings']['ollama_chat_model'])}</li>",
        f"<li><strong>Embed Model:</strong> {escape(diagnostics['settings']['ollama_embed_model'])}</li>",
        f"<li><strong>Piper:</strong> {escape(diagnostics['piper']['detail'])}</li>",
        f"<li><strong>Audio:</strong> {escape(diagnostics['audio']['detail'])}</li>",
    ]
    status_rows = "".join(status_items)
    ingestion_error_rows = "".join(
        f"<li><strong>{escape(str(item['filename']))}</strong> <span>{escape(str(item['ts']))}</span><br>{escape(str(item['error']))}</li>"
        for item in diagnostics["index"].get("recent_ingestion_errors", [])
    ) or "<li>No recent ingestion errors.</li>"

    answer_block = ""
    if answer:
        answer_block = (
            "<section class='panel'>"
            "<h2>Answer</h2>"
            f"<p>{escape(answer)}</p>"
            "</section>"
        )

    context_block = ""
    if context:
        context_block = (
            "<section class='panel'>"
            "<h2>Retrieved Context</h2>"
            f"<pre>{escape(context)}</pre>"
            "</section>"
        )

    error_block = f"<p class='error'>{escape(error)}</p>" if error else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiveum</title>
  {_shared_styles()}
</head>
<body>
  <main>
    <section class="hero">
      <span class="tag">Archiveum</span>
      <div>
        <h1>Living Archive Companion</h1>
        <p>Upload notes, PDFs, and text files, then ask grounded questions against your local archive.</p>
        <p>Retrieval runs on local embeddings with vector similarity search through Ollama.</p>
      </div>
    </section>

    <nav class="nav-links">
      <a href="/">Home</a>
      <a href="/admin">Admin</a>
      <a href="/status">Status JSON</a>
    </nav>

    <section class="layout">
      <aside class="panel">
        <h2>Add Files</h2>
        <form action="/upload" method="post" enctype="multipart/form-data">
          <input type="file" name="file" required>
          <button type="submit">Index File</button>
        </form>
        <h2 style="margin-top: 24px;">Runtime Status</h2>
        <ul>{status_rows}</ul>
        <h2 style="margin-top: 24px;">Recent Ingestion Errors</h2>
        <ul>{ingestion_error_rows}</ul>
        <h2 style="margin-top: 24px;">Indexed Sources</h2>
        <ul>{source_rows}</ul>
      </aside>

      <div style="display: grid; gap: 20px;">
        <section class="panel">
          <h2>Ask Archiveum</h2>
          {error_block}
          <form action="/chat" method="post">
            <textarea name="question" placeholder="What should I know about these files?">{escape(question)}</textarea>
            <button type="submit">Ask</button>
          </form>
        </section>
        {answer_block}
        {context_block}
      </div>
    </section>
  </main>
</body>
</html>"""


def _render_admin_page() -> str:
    diagnostics = assistant.diagnostics()
    sources = assistant.store.list_sources()
    errors = diagnostics["index"].get("recent_ingestion_errors", [])

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
        <p>Review runtime health, recent ingestion failures, and the current indexed archive.</p>
      </div>
    </section>

    <nav class="nav-links">
      <a href="/">Home</a>
      <a href="/admin">Admin</a>
      <a href="/status">Status JSON</a>
    </nav>

    <section class="admin-grid">
      <section class="panel">
        <h2>Index Summary</h2>
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
        <ul>
          <li><strong>Chat Model:</strong> {escape(diagnostics['settings']['ollama_chat_model'])}</li>
          <li><strong>Embed Model:</strong> {escape(diagnostics['settings']['ollama_embed_model'])}</li>
          <li><strong>Piper:</strong> {escape(diagnostics['piper']['detail'])}</li>
          <li><strong>Audio:</strong> {escape(diagnostics['audio']['detail'])}</li>
        </ul>
      </section>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <div class="section-head">
        <h2>Ingestion Errors</h2>
        <form action="/admin/errors/clear" method="post">
          <button type="submit">Clear All Errors</button>
        </form>
      </div>
      <div class="error-list">
        {error_cards}
      </div>
    </section>

    <section class="panel" style="margin-top: 20px;">
      <h2>Indexed Sources</h2>
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


def _shared_styles() -> str:
    return """<style>
    :root {
      --bg: #f4efe5;
      --paper: #fffaf0;
      --ink: #1f2933;
      --accent: #9f4f2f;
      --accent-soft: #d8a98d;
      --line: #d9c6b5;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(159, 79, 47, 0.10), transparent 32%),
        linear-gradient(180deg, #f6f1e8 0%, var(--bg) 100%);
    }
    main {
      max-width: 1100px;
      margin: 0 auto;
      padding: 32px 20px 56px;
    }
    h1, h2 { margin: 0 0 12px; }
    h1 {
      font-size: clamp(2.4rem, 5vw, 4.2rem);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    p { line-height: 1.6; }
    .hero {
      display: grid;
      gap: 18px;
      margin-bottom: 24px;
    }
    .tag {
      display: inline-block;
      width: fit-content;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255, 250, 240, 0.85);
      font-size: 0.9rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .nav-links {
      display: flex;
      gap: 10px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }
    .nav-links a {
      color: var(--ink);
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 12px;
      background: rgba(255, 250, 240, 0.85);
    }
    .layout, .admin-grid {
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 20px;
    }
    .admin-grid {
      grid-template-columns: 1fr 1fr;
    }
    .panel {
      background: rgba(255, 250, 240, 0.95);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 20px;
      box-shadow: 0 12px 30px rgba(31, 41, 51, 0.08);
    }
    form {
      display: grid;
      gap: 12px;
    }
    input[type="file"], input[type="text"], textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }
    textarea {
      min-height: 140px;
      resize: vertical;
    }
    button {
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      background: var(--accent);
      color: #fffaf0;
      font: inherit;
      cursor: pointer;
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
      font-family: "Courier New", monospace;
      font-size: 0.95rem;
    }
    .error {
      color: #8b1e1e;
      margin: 0;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .error-list {
      display: grid;
      gap: 14px;
    }
    .error-card {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.7);
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
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }
    @media (max-width: 860px) {
      .layout, .admin-grid {
        grid-template-columns: 1fr;
      }
      .section-head {
        align-items: flex-start;
        flex-direction: column;
      }
    }
  </style>"""
