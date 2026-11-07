"""`notes`: our own MCP server (mcp 2.3.0 `MCPServer`, formerly FastMCP), ADR-008/032.

Tools: create_note, search_notes, delete_note. Storage: one SQLite file (--db). Notes live
outside any task, which is why AgentOps treats create_note as `external` and delete_note as
`destructive` (proposal-only for specialists); search_notes is overridden to `read` in
config/mcp_servers.toml.

Run:
  python -I -m mcp_servers.notes.server --db /data/notes.db                       # stdio
  python -I -m mcp_servers.notes.server --transport streamable-http \\
      --host 0.0.0.0 --port 8765 --allowed-host mcp-notes:8765 --db /data/notes.db
DNS-rebinding protection is ON for HTTP (the SDK default is off): only --allowed-host values
are accepted in the Host header.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

MAX_TITLE = 200
MAX_BODY = 10_000


def build_server(db_path: Path) -> MCPServer:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    server = MCPServer(
        name="agentops-notes",
        instructions="A small notes store: create, search and delete short text notes.",
    )

    @server.tool(description="Create a note. Returns the new note's id.")
    def create_note(
        title: str = Field(max_length=MAX_TITLE), body: str = Field(max_length=MAX_BODY)
    ) -> str:
        cur = conn.execute("INSERT INTO notes (title, body) VALUES (?, ?)", (title, body))
        conn.commit()
        return f"created note {cur.lastrowid}"

    @server.tool(description="Search notes by a word or phrase in the title or body.")
    def search_notes(
        query: str = Field(min_length=1, max_length=200), limit: int = Field(default=5, ge=1, le=20)
    ) -> str:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT id, title, body FROM notes WHERE title LIKE ? OR body LIKE ? "
            "ORDER BY id LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return "\n".join(f"[{i}] {t}: {b[:300]}" for i, t, b in rows) or "no matching notes"

    @server.tool(description="Delete a note by id.")
    def delete_note(note_id: int = Field(ge=1)) -> str:
        cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
        return f"deleted {cur.rowcount} note(s)"

    return server


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--allowed-host", action="append", default=[])
    a = p.parse_args()
    server = build_server(a.db)
    if a.transport == "stdio":
        server.run("stdio")
        return
    allowed = a.allowed_host or [f"127.0.0.1:{a.port}", f"localhost:{a.port}"]
    server.run(
        "streamable-http",
        host=a.host,
        port=a.port,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed
        ),
    )


if __name__ == "__main__":
    main()
