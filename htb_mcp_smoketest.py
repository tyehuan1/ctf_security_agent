#!/usr/bin/env python3
"""HTB CTF MCP — connection smoke test.

Proves three things BEFORE game day, without needing an MCP-enabled event:
  1. the endpoint + streamable-HTTP transport work,
  2. your personal MCP token authenticates (Authorization: Bearer <token>),
  3. harvests the EXACT tool identifiers + input schemas the wiring step needs
     (the HTB help page lists display names like "Get My Teams", not the string
     IDs your client must call).

A successful `initialize` + `list_tools` is the pass condition — that alone means
auth and transport are good. Calling a read-only "teams" tool is a bonus and is
only attempted when it takes no required arguments (so we never guess args).

The account-level read tools (Get My Teams, List CTF Events, Retrieve CTF Details)
do not depend on an event's MCP toggle, so this works right now.

Setup:
    pip install mcp
    export MCP_TOKEN=...        # or put MCP_TOKEN=... in a .env next to this file
    python htb_mcp_smoketest.py

This is a standalone diagnostic — it is NOT part of the solver and holds no flag.
The token is read from the environment/.env and never printed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ENDPOINT = "https://mcp.hackthebox.ai/v1/ctf/mcp/"


def _load_token() -> str:
    """MCP_TOKEN from the environment, falling back to a .env beside this script.
    Minimal .env parse so there's no hard dependency on python-dotenv."""
    token = os.environ.get("MCP_TOKEN")
    if token:
        return token.strip()
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("MCP_TOKEN=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("MCP_TOKEN not set. Put it in the environment or a .env next to this script.")


def _required_params(input_schema: dict | None) -> list[str]:
    if not isinstance(input_schema, dict):
        return []
    req = input_schema.get("required")
    return list(req) if isinstance(req, list) else []


async def main() -> int:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    except ImportError:
        sys.exit("The MCP SDK is missing. Install it with:  pip install mcp")

    token = _load_token()
    headers = {"Authorization": f"Bearer {token}"}

    print(f"Connecting to {ENDPOINT} over streamable HTTP ...")
    # mcp>=2 takes an httpx client (built with our auth header) rather than headers=.
    http_client = create_mcp_http_client(headers=headers)
    try:
        async with streamable_http_client(ENDPOINT, http_client=http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("  initialize OK")

                result = await session.list_tools()
                tools = result.tools
                print(f"  list_tools OK — {len(tools)} tools exposed\n")
                print("=" * 68)
                print("AUTH + TRANSPORT CONFIRMED. Exact tool identifiers below —")
                print("map these to your HTBClient methods when wiring htb_mcp.py.")
                print("=" * 68)
                for t in tools:
                    req = _required_params(getattr(t, "inputSchema", None))
                    req_str = f"  required args: {', '.join(req)}" if req else "  (no required args)"
                    desc = (t.description or "").strip().replace("\n", " ")
                    if len(desc) > 90:
                        desc = desc[:87] + "..."
                    print(f"\n- {t.name}{req_str}")
                    if desc:
                        print(f"    {desc}")

                # Bonus: call a no-arg read-only "teams" tool if one exists.
                teams = [
                    t for t in tools
                    if "team" in t.name.lower() and not _required_params(getattr(t, "inputSchema", None))
                ]
                if teams:
                    name = teams[0].name
                    print(f"\nCalling read-only tool '{name}' as a live check ...")
                    try:
                        call = await session.call_tool(name, {})
                        for block in getattr(call, "content", []) or []:
                            text = getattr(block, "text", None)
                            if text:
                                print("  response:", text[:500])
                        print("  call OK — your token can read your account.")
                    except Exception as exc:  # noqa: BLE001 - diagnostic, report and continue
                        print(f"  call raised: {exc!r} (list_tools already proved auth; this is non-fatal)")
                else:
                    print("\nNo no-arg 'teams' tool found to call — list_tools already proves auth.")
        print("\nSMOKE TEST PASSED.")
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level diagnostic
        print(f"\nSMOKE TEST FAILED: {type(exc).__name__}: {exc}")
        print("If this is an auth error, re-check the MCP_TOKEN (regenerate under "
              "Profile Settings > MCP Access). If it's a transport/connection error, "
              "confirm the endpoint and that streamable HTTP is reachable.")
        return 1
    finally:
        await http_client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
