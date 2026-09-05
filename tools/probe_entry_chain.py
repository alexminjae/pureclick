"""Measure the real entry chain on a cold, already-open show, with the saved session.

    python3 tools/probe_entry_chain.py [goodsCode] [playSeq] [out.json]

Runs, from this machine and with the cookies the 예매 창 saved:

    member-info  → signature (stamped with its issue time)
    secure-url   → the queue key (the redirectUrl the gate would navigate to)
    line-up      → the place in line (userSeq / waitingId)
    rank         → myRank / totalRank / oneStopUrl

each timed, then re-sends secure-url with the SAME signature at +2, +5 and +10
minutes to find how long a minted signature stays accepted. It never navigates
and never holds a seat. It does create a waiting entry for the chosen show on
this account — use a cold show, not the one you are about to fight for.
Total ent-waiting-api requests: at most 8 over ~12 minutes.

Default show: 26012391 (임형주 독창회 — open, quiet, has a waiting queue).
"""
import json, re, sys, time, urllib.request, urllib.parse, http.client, ssl

GOODS = sys.argv[1] if len(sys.argv) > 1 else "26012391"   # 임형주 독창회 — open, cold, has a waiting flag
PLAY_SEQ = sys.argv[2] if len(sys.argv) > 2 else "001"
OUT = sys.argv[3] if len(sys.argv) > 3 else "probe_chain.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
jar = json.load(open(__import__("pathlib").Path(__file__).resolve().parent.parent / "mac" / ".nolsniper_cookies.json"))
ctx = ssl.create_default_context()
log = []

def note(**kw):
    kw["t"] = round(time.time(), 3)
    log.append(kw)
    print(json.dumps(kw, ensure_ascii=False)[:600], flush=True)
    json.dump(log, open(OUT, "w"), ensure_ascii=False, indent=1)

def cookies_for(host):
    parts = []
    for c in jar:
        d = (c.get("Domain") or "").lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            parts.append(f"{c['Name']}={c['Value']}")
    return "; ".join(parts)

def call(host, method, path, body=None, headers=None, cookies=True):
    h = {"User-Agent": UA, "Accept": "application/json", "Origin": "https://tickets.interpark.com",
         "Referer": f"https://tickets.interpark.com/goods/{GOODS}"}
    if cookies:
        ck = cookies_for(host)
        if ck: h["Cookie"] = ck
    if body is not None:
        h["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    h.update(headers or {})
    c = http.client.HTTPSConnection(host, timeout=8, context=ctx)
    t0 = time.perf_counter(); c.connect(); t1 = time.perf_counter()
    c.request(method, path, body=body, headers=h); r = c.getresponse(); raw = r.read(); t2 = time.perf_counter()
    try: data = json.loads(raw.decode())
    except Exception: data = raw.decode(errors="replace")[:300]
    setc = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
    c.close()
    return r.status, data, round((t1 - t0) * 1000, 1), round((t2 - t1) * 1000, 1), setc

def member_info():
    st, d, con, req, _ = call("tickets.interpark.com", "GET", f"/api/ticket/v2/reserve-gate/member-info?goodsCode={GOODS}&channelCode=pm")
    sig = d.get("signature", "") if isinstance(d, dict) else ""
    m = re.search(r"\.(\d{10})$", sig)
    note(step="member-info", status=st, connect_ms=con, req_ms=req, issued=int(m.group(1)) if m else None, ok=bool(sig))
    return d if isinstance(d, dict) else None

def secure_url(mi, label):
    body = {"signature": mi["signature"], "secureData": mi["secureData"], "lang": "ko", "passCode": "",
            "from": "NOL", "goodsCode": GOODS, "bizCode": "61776", "playSeq": PLAY_SEQ, "preSales": "N"}
    st, d, con, req, setc = call("ent-waiting-api.interpark.com", "POST", "/waiting/api/secure-url", body)
    url = d.get("redirectUrl") if isinstance(d, dict) else None
    err = d.get("error") if isinstance(d, dict) else d
    key = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("key", [None])[0] if url else None
    note(step=f"secure-url:{label}", status=st, connect_ms=con, req_ms=req, error=err if not url else None,
         redirect_host=urllib.parse.urlparse(url).netloc if url else None, key_len=len(key) if key else 0,
         set_cookie=[s.split(";")[0].split("=")[0] for s in setc], sig_age_s=int(time.time()) - int(mi["signature"].rsplit(".", 1)[-1]))
    return key

def line_up(key, cookies, label):
    st, d, con, req, setc = call("ent-waiting-api.interpark.com", "POST", "/waiting/api/line-up", {"key": key}, cookies=cookies)
    note(step=f"line-up:{label}", status=st, connect_ms=con, req_ms=req, cookies=cookies,
         exist=d.get("exist") if isinstance(d, dict) else None, userSeq=d.get("userSeq") if isinstance(d, dict) else None,
         waitingId=(d.get("waitingId") or "")[:12] + "…" if isinstance(d, dict) and d.get("waitingId") else None,
         error=d.get("error") if isinstance(d, dict) else d, keys=list(d.keys()) if isinstance(d, dict) else None,
         set_cookie=[s.split(";")[0].split("=")[0] for s in setc])
    return d if isinstance(d, dict) else None

def rank(waiting_id, label):
    st, d, con, req, _ = call("ent-waiting-api.interpark.com", "GET", "/waiting/api/rank?waitingId=" + urllib.parse.quote(waiting_id), cookies=False)
    note(step=f"rank:{label}", status=st, connect_ms=con, req_ms=req,
         myRank=d.get("myRank") if isinstance(d, dict) else None, totalRank=d.get("totalRank") if isinstance(d, dict) else None,
         has_oneStopUrl=bool(d.get("oneStopUrl")) if isinstance(d, dict) else None,
         oneStop_host=urllib.parse.urlparse(d.get("oneStopUrl") or "").netloc if isinstance(d, dict) else None,
         sessionId=bool(d.get("sessionId")) if isinstance(d, dict) else None,
         error=d.get("error") if isinstance(d, dict) else d, keys=list(d.keys()) if isinstance(d, dict) else None)
    return d if isinstance(d, dict) else None

mi = member_info()
if not mi: sys.exit("no member-info")
t_fire = time.perf_counter()
key = secure_url(mi, "fresh")
if key:
    lu = line_up(key, True, "with-cookies")
    wid = (lu or {}).get("waitingId")
    if wid:
        rk = rank(wid, "first")
        note(step="chain-total", ms=round((time.perf_counter() - t_fire) * 1000, 1))
        time.sleep(1.0)
        rank(wid, "second")
    # the same key again: what does a second line-up say?
    line_up(key, True, "same-key-again")
# TTL: the same signature, minutes later. UnableReservationTime/other errors vs a signature error tells the window.
for wait_s in (120, 300, 600):
    time.sleep(wait_s - (120 if wait_s > 120 else 0) if wait_s == 120 else wait_s - prev)  # noqa
    prev = wait_s
    secure_url(mi, f"sig-age-{wait_s}s")
note(step="done")
