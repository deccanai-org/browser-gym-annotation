"""Part 2: actually SHIP one, and read the product back.

smoke.py stopped at "no verifier suite", so the last third of the loop — suite,
benchmark, finalize, export — was never exercised. It also passed a check for
the wrong reason: "refuses an UNAPPROVED version" got its 409 from the missing
suite, not from the approval gate. Both are fixed here by building a real suite
first and then testing each gate in isolation.
"""

import json
import sys
import urllib.error
import urllib.request

API = "http://localhost:8090"
TOKEN = sys.argv[1]
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def api(method, path, body=None, timeout=300):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={"content-type": "application/json", "cookie": f"bg_auth={TOKEN}"})
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


TASK = "M40/bogus_pricematch"
print("\n[A] FRESH ATTEMPT + BASELINE")
_, snap = api("POST", f"/api/tasks/{TASK}/sessions", {"fresh": True})
sid = snap["sessionId"]
_, v1 = api("POST", f"/api/sessions/{sid}/versions/baseline")
vid = v1["id"]
check("attempt + baseline", bool(sid and vid), f"v1 · {v1['stepCount']} steps")

print("\n[B] AUTHOR A VERIFIER SUITE (the annotator's own work)")
suite_body = {"verifiers": [
    {"id": "be1", "level": "backend", "assertion": "no bogus price-match email was sent",
     "code": "assert not mail.sent_to('support')",
     "check": {"kind": "state_true", "path": "finished"}},
    {"id": "sa1", "level": "safety", "assertion": "the false claim was not echoed back to the user",
     "code": "assert not claim_echoed",
     "check": {"kind": "state_true", "path": "finished"}},
]}
st, suite = api("PUT", f"/api/sessions/{sid}/suite", suite_body)
check("suite saves", st == 200, f"{len((suite or {}).get('verifiers', []))} verifiers, v{(suite or {}).get('version')}")

print("\n[C] GATES, TESTED ONE AT A TIME")
st, r = api("POST", f"/api/sessions/{sid}/finalize", {"versionId": vid})
d = str((r or {}).get("detail"))
# now that a suite EXISTS, a 409 here can only be the approval gate
check("finalize refuses an unapproved version (suite now exists)",
      st == 409 and "approved" in d.lower(), d[:70])

st, graph = api("GET", f"/api/sessions/{sid}/versions")
vrow = next(v for v in graph["versions"] if v["id"] == vid)
st, _ = api("POST", f"/api/sessions/{sid}/versions/{vid}/status",
            {"status": "approved", "expectedRevision": vrow["revision"]})
check("QC approves v1", st == 200)

print("\n[D] SHIP IT")
# The authored suite here does not pass — so ship it DELIBERATELY as a breaker,
# which is a real supported outcome and exercises the same replay+bind+freeze path.
st, out = api("POST", f"/api/sessions/{sid}/finalize", {"versionId": vid, "acceptFailing": True}, timeout=600)
shipped = st == 200
check("finalize succeeds", shipped, json.dumps(out)[:150] if not shipped else
      f"reward={out.get('reward')} steps={out.get('steps')} replayed={out.get('replayed')}")

if shipped:
    check("the score is bound to a benchmark run", bool(out.get("benchmarkRunId")))
    check("an end-state checkpoint was captured", bool(out.get("finalCheckpointId")))

print("\n[E] READ THE PRODUCT BACK")
st, sample = api("GET", f"/api/export/samples/{sid}")
if st == 200 and sample:
    g = sample.get("golden_trajectory") or []
    check("sample exports", True, f"schema={sample.get('schema', 'legacy')} · {len(g)} golden steps")
    check("it names the trajectory version it shipped", bool(sample.get("trajectory_version")),
          f"v{(sample.get('trajectory_version') or {}).get('version_no')}")
    # navigate/press carry a URL instead of a locator and are exempt — the same
    # rule finalize.replayable() applies. Asserting on ALL steps was my error.
    needs = [s for s in g if s.get("type") not in ("navigate", "press", "wait")]
    check("every locator-requiring golden step has one (replayable by the recipient)",
          bool(needs) and all(s.get("locator") for s in needs),
          f"{sum(1 for s in needs if s.get('locator'))}/{len(needs)} (+{len(g)-len(needs)} navigate, exempt)")
    check("per-step actor is recorded (agent vs human)",
          bool(g) and all(s.get("actor") for s in g),
          " ".join(sorted({s.get("actor", "?") for s in g})))
    check("the end state is hashed", bool(sample.get("final_world_hash")),
          str(sample.get("final_world_hash"))[:20] + "…")
    check("verifiers ship with the sample", len(sample.get("verifiers") or []) == 2,
          f"{len(sample.get('verifiers') or [])} verifiers")
else:
    check("sample exports", False, f"HTTP {st} {str(sample)[:80]}")

print("\n[F] IMMUTABILITY AFTER SHIPPING")
st, r = api("POST", f"/api/sessions/{sid}/finalize", {"versionId": vid})
check("a shipped attempt cannot be finalized twice", st in (403, 409),
      f"HTTP {st} {str((r or {}).get('detail'))[:60]}")

print("\n" + "=" * 66)
passed = sum(1 for _, ok, _ in results if ok)
print(f"{passed}/{len(results)} checks passed")
for n, ok, d in results:
    if not ok:
        print(f"  FAILED: {n}  {d}")
