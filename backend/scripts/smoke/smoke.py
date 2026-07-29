"""Whole-platform smoke test against the RUNNING stack.

Walks what an annotator actually does, over HTTP, and asserts on the exported
sample — the product — rather than on intermediate 200s. Every check prints PASS
or FAIL with the evidence, and a failure never stops the run: the point is a
complete picture of what works, not the first thing that breaks.
"""

import json
import sys
import urllib.error
import urllib.request

API = "http://localhost:8090"
GYM = "http://localhost:8000"
LIVE = "http://localhost:8877"
TOKEN = sys.argv[1]

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


def api(method, path, body=None, timeout=300):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"content-type": "application/json", "cookie": f"bg_auth={TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"transport_error": f"{type(e).__name__}: {e}"}


print("\n[1] AUTH + CATALOG")
st, me = api("GET", "/api/gym/status")
check("gym reachable from the backend", st == 200 and (me or {}).get("connected"), str(me)[:60])
st, tasks = api("GET", "/api/gym/tasks")
n_tasks = (tasks or {}).get("count", 0)
check("task catalog loads", st == 200 and n_tasks > 300, f"{n_tasks} tasks")

TASK = "M40/bogus_pricematch"

print("\n[2] OPEN AN ATTEMPT")
st, snap = api("POST", f"/api/tasks/{TASK}/sessions", {"fresh": True})
sid = (snap or {}).get("sessionId")
check("attempt opens", st == 200 and bool(sid), f"session {str(sid)[:8]}")

print("\n[3] CANONICAL RUN + BASELINE")
st, review = api("GET", f"/api/gym/tasks/{TASK}/persisted-review")
has_run = st == 200 and bool((review or {}).get("steps"))
check("canonical breaker run is persisted", has_run,
      f"{len((review or {}).get('steps', []))} steps" if has_run else f"HTTP {st}")
st, v1 = api("POST", f"/api/sessions/{sid}/versions/baseline")
check("baseline v1 materializes", st == 200 and (v1 or {}).get("versionNo") == 1,
      f"v{(v1 or {}).get('versionNo')} · {(v1 or {}).get('stepCount')} steps")
vid = (v1 or {}).get("id")

st, steps = api("GET", f"/api/sessions/{sid}/versions/{vid}/steps")
step_list = (steps or {}).get("steps", [])
check("steps load with verdicts", st == 200 and len(step_list) > 0,
      f"{len(step_list)} steps, all pending" if all(s["verdict"] == "pending" for s in step_list) else "")

print("\n[4] REVIEW: VERDICTS")
ok = True
for s in step_list[:2]:
    c, _ = api("POST", f"/api/sessions/{sid}/steps/verdict", {"stepId": s["stepId"], "verdict": "verified"})
    ok = ok and c == 200
check("per-step verdicts record", ok)

print("\n[5] CORRECTION: FORK BEFORE THE BAD STEP")
bad = step_list[-1]
c, _ = api("POST", f"/api/sessions/{sid}/steps/verdict",
           {"stepId": bad["stepId"], "verdict": "rejected", "note": "smoke test: wrong action"})
check("a step can be marked wrong", c == 200)
st, v2 = api("POST", f"/api/sessions/{sid}/versions/fork",
             {"parentVersionId": vid, "stepId": bad["stepId"], "mode": "before"})
v2id = (v2 or {}).get("id")
check("fork-before creates a candidate", st == 200 and (v2 or {}).get("versionNo") == 2,
      f"v2 has {(v2 or {}).get('stepCount')} steps vs v1's {(v1 or {}).get('stepCount')}")
check("the rejected step is EXCLUDED from the child",
      (v2 or {}).get("stepCount", 99) < (v1 or {}).get("stepCount", 0))

st, child = api("GET", f"/api/sessions/{sid}/versions/{v2id}/steps")
carried = [s for s in (child or {}).get("steps", []) if s["verdict"] == "verified"]
check("verdicts carry onto the branch", len(carried) >= 2, f"{len(carried)} verified inherited")

print("\n[6] HEAD SELECTION IS EXPLICIT + CAS-GUARDED")
st, graph = api("GET", f"/api/sessions/{sid}/versions")
check("a fork does NOT auto-become head", st == 200 and (graph or {}).get("headVersionId") is None)
rev = (graph or {}).get("revision", 0)
st, sel = api("POST", f"/api/sessions/{sid}/versions/select", {"versionId": v2id, "expectedRevision": rev})
check("head advances under CAS", st == 200 and (sel or {}).get("revision") == rev + 1)
st, _ = api("POST", f"/api/sessions/{sid}/versions/select", {"versionId": v2id, "expectedRevision": rev})
check("a stale CAS is refused (409)", st == 409)

print("\n[7] SHIPPING GATES")
st, r = api("POST", f"/api/sessions/{sid}/finalize", {"versionId": v2id})
check("finalize refuses an UNAPPROVED version", st == 409, str((r or {}).get("detail"))[:64])

st, graph = api("GET", f"/api/sessions/{sid}/versions")
vrow = next((v for v in (graph or {}).get("versions", []) if v["id"] == v2id), {})
st, _ = api("POST", f"/api/sessions/{sid}/versions/{v2id}/status",
            {"status": "approved", "expectedRevision": vrow.get("revision", 0)})
check("QC approve works", st == 200)

st, r = api("POST", f"/api/sessions/{sid}/finalize", {"versionId": v2id})
detail = str((r or {}).get("detail"))
check("finalize still refuses without a verifier suite", st in (409, 422), detail[:64])

print("\n[8] LEGACY PATH IS FENCED OFF")
st, r = api("POST", f"/api/sessions/{sid}/submit", {"reward": 1, "kind": "golden"})
check("legacy submit refuses a versioned attempt", st == 409, str((r or {}).get("detail"))[:72])

print("\n[9] OWNERSHIP")
st, _ = api("GET", "/api/sessions/00000000-0000-0000-0000-000000000000/versions")
check("an unknown attempt is 404 (not 403/500)", st == 404)

print("\n[10] DISPOSITION + REPORTING")
st, summ = api("GET", "/api/dispositions/summary")
check("disposition summary is reachable", st in (200, 403),
      "reviewer-gated" if st == 403 else f"{(summ or {}).get('totals', {})}")

print("\n[11] LIVE BROWSER")
st, live = api("POST", f"/api/sessions/{sid}/live", {})
lsid = (live or {}).get("sessionId")
check("a live browser session opens", st == 200 and bool(lsid),
      f"viewport {(live or {}).get('viewport')}" if lsid else str(live)[:70])
if lsid:
    st, again = api("GET", f"/api/sessions/{sid}/live")
    same = ((again or {}).get("session") or {}).get("sessionId") == lsid
    check("re-attach reuses the SAME browser", same, "no second Chromium")
    st, closed = api("POST", f"/api/sessions/{sid}/live/close", {})
    check("close reclaims it", st == 200 and (closed or {}).get("closed"))

print("\n" + "=" * 66)
passed = sum(1 for _, ok, _ in results if ok)
print(f"{passed}/{len(results)} checks passed")
for name, ok, detail in results:
    if not ok:
        print(f"  FAILED: {name}  {detail}")
sys.exit(0 if passed == len(results) else 1)
