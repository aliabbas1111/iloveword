"""
Hardening tests — rate limiting, the cleanup sweeper and the resource caps.

Kept apart from test_suite.py because it deliberately sets very low limits;
running both in one process would rate-limit the functional suite.

    python test_hardening.py
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("ILW_RATE_LIGHT", "5/minute")
os.environ.setdefault("ILW_RATE_HEAVY", "3/minute")
os.environ.setdefault("ILW_RATE_DEFAULT", "500/hour")
os.environ.setdefault("ILW_CLEANUP_INTERVAL_SECONDS", "1")
os.environ.setdefault("ILW_WORKSPACE_MAX_AGE_SECONDS", "0")

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

import core  # noqa: E402
import main  # noqa: E402

DOC = Path("/tmp/fixtures/report.docx")
results = []


def check(name, ok, detail=""):
    results.append((ok, name, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def main_():
    if not DOC.exists():
        print("Run test_suite.py first — it builds the fixtures."); return 1
    doc = DOC.read_bytes()
    client = TestClient(main.app)

    def post(path, **kw):
        return client.post(path, files={"file": ("a.docx", doc)}, **kw)

    # --- light tier --------------------------------------------------------
    codes = [post("/api/tools/extract-text").status_code for _ in range(8)]
    check("light tier: 5 allowed then 429", codes[:5] == [200] * 5 and codes[5:] == [429] * 3, str(codes))

    body = post("/api/tools/extract-text").json()
    check("429 body carries {detail, code, limit}",
          body.get("code") == "rate_limited" and "limit" in body, str(body))

    resp = post("/api/tools/extract-text")
    check("429 sends Retry-After", resp.headers.get("Retry-After") == "60",
          str(resp.headers.get("Retry-After")))

    # --- heavy tier --------------------------------------------------------
    codes = [post("/api/convert/word-to-pdf").status_code for _ in range(5)]
    check("heavy tier: 3 allowed then 429", codes == [200, 200, 200, 429, 429], str(codes))

    # --- an independent endpoint keeps its own budget ----------------------
    code = post("/api/tools/remove-metadata").status_code
    check("limits are per endpoint, not global", code == 200, str(code))

    # --- /health is exempt (the container healthcheck polls it) ------------
    codes = {client.get("/health").status_code for _ in range(15)}
    check("/health exempt from limits", codes == {200}, str(codes))

    # --- cleanup sweeper ---------------------------------------------------
    orphan = core.Workspace.create()
    (orphan.inp / "leftover.bin").write_bytes(b"x" * 1024)
    os.utime(orphan.root, (time.time() - 7200, time.time() - 7200))
    removed = core.purge_stale_workspaces(3600)
    check("sweeper removes aged workspaces",
          removed >= 1 and not orphan.root.exists(), f"removed={removed}")

    fresh = core.Workspace.create()
    core.purge_stale_workspaces(3600)
    check("sweeper spares in-flight workspaces", fresh.root.exists())
    fresh.dispose()

    # --- resource ceilings are actually configured -------------------------
    check("soffice memory ceiling set", core.SOFFICE_MEM_BYTES > 0,
          f"{core.SOFFICE_MEM_BYTES // 1024 // 1024} MB")
    check("cleanup interval configured", core.CLEANUP_INTERVAL > 0,
          f"{core.CLEANUP_INTERVAL}s")

    # --- no leaked workspaces after the whole run --------------------------
    leftovers = [p for p in core.TMP_ROOT.iterdir()] if core.TMP_ROOT.exists() else []
    check("no workspaces leaked", not leftovers, f"{len(leftovers)} left")

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
