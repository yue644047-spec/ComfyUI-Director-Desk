import json, os, sys, urllib.request, urllib.error

BASE = "https://modao.cc/agent-py/ai/mcp"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_mcp_last_result.json")

def load_env():
    env = {}
    p = os.path.join(HERE, ".env")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

TOKEN = load_env().get("MODOA_TOKEN") or ""

def rpc(payload, session_id=None, timeout=150):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "modao-token": TOKEN,
        "User-Agent": "dsh-mcp-client/2.1",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(BASE, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
            raw = resp.read()
            text = raw.decode("utf-8", "replace")
            ctype = resp.headers.get("Content-Type", "") or ""
            msg = None
            if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:")):
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        p = line[5:].strip()
                        if p and p != "[DONE]":
                            msg = json.loads(p)
            elif text.strip():
                msg = json.loads(text)
            return sid, msg, resp.status
    except urllib.error.HTTPError as e:
        return None, {"http_error": e.code, "body": e.read().decode("utf-8", "replace")[:500]}, e.code

def text_of(msg):
    out = []
    for c in (msg or {}).get("result", {}).get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            out.append(c.get("text") or "")
    return "\n".join(out)

def main():
    mode = sys.argv[1]
    sid, init, _st = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "dsh-mcp-client", "version": "2.1"}}}, timeout=60)
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=sid, timeout=30)
    if mode == "list":
        _s, res, _st = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session_id=sid, timeout=60)
        tools = ((res or {}).get("result") or {}).get("tools") or []
        print("TOOLS: " + ", ".join(t.get("name") for t in tools))
    else:
        call = json.load(open(sys.argv[2], encoding="utf-8"))
        _s, res, _st = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": call["tool"], "arguments": call.get("arguments", {})}},
                           session_id=sid, timeout=call.get("timeout", 150))
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        t = text_of(res)
        err = bool((res or {}).get("result", {}).get("isError"))
        print("SAVED " + OUT + " | isError=" + str(err) + " | text_len=" + str(len(t)))
        print(t[:400])

main()
