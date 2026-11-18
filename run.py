#!/usr/bin/env python3
"""Start Langu-AI.   python3 run.py          web app at http://localhost:8787
                   python3 run.py mcp      MCP server on stdio (what Claude Desktop launches)
                   python3 run.py import   rebuild reference data from data/raw
                   python3 run.py login    open Claude Code so you can type /login (needed once for the web chat)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "web"
    if what == "login":
        from dilai.web import find_claude
        exe = find_claude()
        if not exe:
            sys.exit("Claude Code was not found. Install it from https://claude.com/claude-code")
        os.execv(exe, [exe])
    if what == "mcp":
        from dilai import mcp_server as m
    elif what == "import":
        from dilai import importer as m
    else:
        from dilai import web as m
    m.main()
