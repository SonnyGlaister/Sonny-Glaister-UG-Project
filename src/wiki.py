
#!/usr/bin/env python3
"""A minimal local LLM wiki using Ollama and Markdown."""

import argparse
import sqlite3
import sys
from pathlib import Path

import ollama

# ── Configuration ────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources"
WIKI = ROOT / "wiki"
DB_PATH = ROOT / "wiki.db"

MODEL = "qwen3:8b"
OLLAMA_HOST = "http://localhost:11434"

# ── Category and Prompt Constant Definitions ────────────────────────────────────────

CATEGORIES = [
    "activities",
    "groups",
    "locations",
    "misc",
]

METADATATAGS = ["TITLE", "CATEGORY", "LINKED ACTIVITIES", "LINKED GROUPS", "LINKED LOCATIONS"]

PAGEFORMAT = f"""
    The page's format should always take this form where information inside square brackets describes what should occupy that space
    
    TITLE: [A short descriptive title - Take one key element from the file e.g place name, group or activity  - REQUIRED]
    CATEGORY: [The Most applicable Category  - REQUIRED]
    LINKED ACTIVITIES: [a reference to any relevant activities - ignore if category is activity]
    LINKED GROUPS: [A reference to any relevant groups - ignore if category is group]
    LINKED LOCATIONS: [A reference to any relevant locations - ignore if category is location ]

    A new Line
    CONTENT:
    [Exclusively the useful knowledge gained from the ingested file]

    Rules
    - If a field is required, make sure that field shows up in the final page
    - If the category is an activity, the title should be the name of the activity
    - If the category is a group, the title should be the name of the group
    - If the category is a location, the title should be the name of the location

"""

# ── Ollama connection ────────────────────────────────────────

def ask_model(prompt: str, system: str = "") -> str:
    """Send a prompt to Ollama and return its response."""
    client = ollama.Client(host=OLLAMA_HOST)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = client.chat(model=MODEL, messages=messages)
    return response.message.content or ""


# ── Search index ─────────────────────────────────────────────

def connect_db():
    """Open the database and ensure the full-text index exists."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages
        USING fts5(path UNINDEXED, title, body)
    """)
    return conn


def reindex():
    """Rebuild the search index from Markdown files."""
    WIKI.mkdir(parents=True, exist_ok=True)

    with connect_db() as db:
        db.execute("DELETE FROM pages")

        for path in WIKI.rglob("*.md"):
            body = path.read_text(encoding="utf-8")
            title = next(
                (line.lstrip("# ").strip()
                 for line in body.splitlines()
                 if line.startswith("# ")),
                path.stem,
            )
            db.execute(
                "INSERT INTO pages(path, title, body) VALUES (?, ?, ?)",
                (str(path.relative_to(WIKI)), title, body),
            )


def search(query: str, limit: int = 5) -> list[dict]:
    """Find relevant wiki pages using SQLite full-text search."""
    reindex()

    with connect_db() as db:
        # Quote individual terms to avoid FTS syntax errors.
        terms = query.split()
        if not terms:
            return []

        match_query = " OR ".join(
            '"' + term.replace('"', '""') + '"'
            for term in terms
        )

        rows = db.execute("""
            SELECT path, title, body
            FROM pages
            WHERE pages MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (match_query, limit)).fetchall()

    return [
        {"path": path, "title": title, "body": snippet}
        for path, title, snippet in rows
    ]


# ── Wiki operations ──────────────────────────────────────────

def update(title: str, content: str, source: str = "", category: str = "misc") -> Path:
    """Create or replace a wiki page with LLM-generated content."""

    folder = WIKI / category
    folder.mkdir(parents=True, exist_ok=True)



    # Use a simple filename derived from the title.
    slug = "".join(
        c.lower() if c.isalnum() else "-"
        for c in title
    ).strip("-")
    slug = "-".join(part for part in slug.split("-") if part)
    path = folder /  f"{slug or 'untitled'}.md"

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    prompt = f"""
You maintain a concise, factual Markdown knowledge wiki.

Update the page below using the new material.
- Preserve useful existing information.
- Remove repetition.
- Do not invent facts.
- Use clear headings.
- Keep source references when supplied.
- Return only the complete Markdown page.

Page title: {title}

Existing page:
{existing or "(No existing page)"}

New material:
{content}

Source:
{source or "(Not specified)"}
"""

    result = ask_model(
        prompt,
        system="You are a careful knowledge-base editor.",
    )
    path.write_text(result.rstrip() + "\n", encoding="utf-8")
    reindex()
    return path


def ingest(source_path: str) -> Path | None:
    """Read a source document and use Ollama to update the wiki."""
    path = Path(source_path).expanduser()
    inbox = (SOURCES / "inbox").resolve()
    processed = SOURCES / "processed"
    processed.mkdir(parents=True, exist_ok=True)

    if path.is_dir():
        files = sorted(
            p for p in path.iterdir()
            if p.is_file() and p.suffix.lower() in {".txt", ".md"}
        )

        if not files:
            print(f"No .txt or .md files found in {path}")
            return None

        for file in files:
            try:
                ingest(str(file))
            except Exception as exc:
                print(f"Failed to ingest {file.name}: {exc}")

        return None


    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()

    if not path.is_file():
        raise FileNotFoundError(f"Source file not found: {path}")

    if path.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("This starter supports .txt and .md files only.")

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("The source file is empty.")

    prompt = f"""
Analyze the source material and extract its main topic.
Select the CORRECT category from the Approved list
If a piece of data feels like it shouldn't go in any of the specific categories, it should go in misc

APPROVED CATEGORIES: {CATEGORIES}

{PAGEFORMAT}

SOURCE:
{text}

Preserve important facts, names, definitions, and qualifications.
You must NOT invent information, the only information you should add to the fils should come from the markdown
You only know what you are provided
Do not use outside information to aid in your creation of the markdown file


"""

    result = ask_model(
        prompt,
        system="You extract useful, accurate knowledge from documents.",
    )

    first_line, separator, body = result.partition("\n")
    # title = first_line.removeprefix("TITLE:").strip()
    metadata = {}
    for line in result.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)

            # Stripped and upper case for consistency and to stop hallucinations
            key = key.strip().upper()

            if key in METADATATAGS:
                metadata[key] = value.strip()

    title = metadata.get("TITLE", path.stem.title())
    category = metadata.get("CATEGORY", "misc")
    # linkedActivities = metadata.get("LINKED ACTIVITIES", "")
    # linkedGroups = metadata.get("LINKED GROUPS", "")
    # linkedLocations = metadata.get("LINKED LOCATIONS", "")

    if category not in CATEGORIES:
        category = "misc"

    if not separator or not title:
        title = path.stem.replace("_", " ").replace("-", " ").title()
        body = result

    if path.is_relative_to(inbox):
        destination = processed / path.name

        if destination.exists():
            raise FileExistsError(f"Cannot move source: {destination} already exists")
        path.rename(destination)
        source_path = destination
    else:
        source_path = path

    wiki_page = update(title, body, source=str(path), category=str(category))
    return wiki_page


def ask(question: str, limit: int = 5) -> str:
    """Answer a question using relevant wiki pages."""
    results = search(question, limit=limit)

    if not results:
        return "I couldn't find relevant wiki pages. Try ingesting a source first."

    context = "\n\n".join(
        f"## {page['title']}\n"
        f"Source page: {page['path']}\n"
        f"{page['body']}"
        for page in results
    )

    prompt = f"""
Answer the question using the wiki context below.

Rules:
- The context is your sole source of information
- Only use information supported by the context.
- If the context doesn't contain the answer, say so.
- Cite the relevant wiki page paths in your answer.
- Do not pretend to have read source material that isn't included.
- Do not ignore important information simply because one item appears relevant
- Use all relevant items in your response
- Regardless of how little information is given, do not try and add information from the context that isn't asked for
- For example: if asked to name a list of a category, only give the names as the user did not ask for more information than this

Wiki context:
{context}

Question:
{question}
"""

    return ask_model(
        prompt,
        system="You are a careful assistant answering from a local knowledge wiki.",
    )


# ── Command-line interface ───────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Local Ollama LLM wiki")
    commands = parser.add_subparsers(dest="command", required=True)

    p_ingest = commands.add_parser("ingest", help="Ingest a .txt or .md file")
    p_ingest.add_argument("file")

    p_update = commands.add_parser("update", help="Update a wiki page")
    p_update.add_argument("title")
    p_update.add_argument("file", help="Text file containing new material")

    p_search = commands.add_parser("search", help="Search wiki pages")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=5)

    p_ask = commands.add_parser("ask", help="Ask a question about the wiki")
    p_ask.add_argument("question")
    p_ask.add_argument("--limit", type=int, default=5)

    commands.add_parser("reindex", help="Rebuild the search index")

    args = parser.parse_args()

    try:
        if args.command == "ingest":
            page = ingest(args.file)

            if page is not None:
                print(f"Updated wiki page: {page.relative_to(ROOT)}")

        elif args.command == "update":
            content = Path(args.file).read_text(encoding="utf-8")
            page = update(args.title, content, source=args.file)
            print(f"Updated wiki page: {page.relative_to(ROOT)}")

        elif args.command == "search":
            for page in search(args.query, args.limit):
                print(f"\n## {page['title']} ({page['path']})")
                print(page["body"])

        elif args.command == "ask":
            print(ask(args.question, args.limit))

        elif args.command == "reindex":
            reindex()
            print("Search index rebuilt.")

    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Includes connection and model errors from Ollama.
        print(
            f"Operation failed: {exc}\n"
            "Check that LLM Model is running and the model is installed.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
