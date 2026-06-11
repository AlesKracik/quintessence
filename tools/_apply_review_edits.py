# Temporary script: applies the reviewed Phase 1-3 edits to .claude/commands/*.md
# Deleted after use.
import io, sys

EDITS = {
    r".claude/commands/spec.md": [
        (
            "- **Purpose**: one sentence on what this area is for.\n- **Domain vocabulary**: entities (with states if applicable), actors, verbs.\n",
            "- **Purpose**: one sentence on what this area is for.\n- **Domain vocabulary**: entities (with states if applicable), actors, verbs.\n- **State machines**: any entity captured with `states[]` → run the state-machine beat (below) right away, not at resume. The early matrix pass needs `state_machines[]` to exist, and transition capture surfaces missing behaviors while the user is still describing the domain.\n",
        ),
        (
            "run `tools/spec-matrix.py <target>` and triage the `?` cells in this conversation. A gap found now,",
            "run `tools/spec-matrix.py <target>` and triage the `?` cells in this conversation — classify into the GAP / IMPOSSIBLE / OUT-OF-SCOPE buckets defined in `/spec-check` Step 4a; triage values written into `covered_by` survive regeneration. A gap found now,",
        ),
        (
            "Same tool, earlier moment.\n\nWrite to `specs/<target>.json` as you go.",
            "Same tool, earlier moment.\n\n**Closing gap sweep**: when the clusters are exhausted (before formalizing), run one short red-team moment while the user is still in the conversation. From `/spec-check` Step 4b's category list, keep only the categories this domain plausibly touches; ask at most ~8 questions whose answer would add or change a requirement, each citing the REQ/INV/entity it touches (or \"absent\"). Answers become REQs/INVs/CONs in the same exchange; what the user can't answer becomes a Q-NNN. Same logic as the early matrix: a gap found now is a requirement in one exchange. The full `--reality` pass at check time is for depth — it shouldn't be the first time these questions are asked.\n\nWrite to `specs/<target>.json` as you go.",
        ),
    ],
    r".claude/commands/spec-check.md": [
        (
            "Pass it: `entities[]`, `requirements[]`, `invariants[]`, `properties[]`, `glossary` from area JSON. Prompt:",
            "Pass it: `entities[]`, `requirements[]`, `invariants[]`, `properties[]`, `concepts` from area JSON. Prompt:",
        ),
        (
            "> You operate this product at a Fortune-500 customer. Read these requirements, invariants, and entity model. Generate 20–30 questions a production SRE / security reviewer / compliance auditor would ask that the spec does NOT answer. Cover at minimum: authentication & RBAC, multi-tenancy isolation, audit trail, observability (metrics/alerts/SLOs), idempotency of user actions, API/CLI surface beyond UX, quotas & rate limits, crash recovery of control plane mid-operation, time/timezone semantics, encryption at rest/transit, backward compatibility & migration, concurrent operators. For each question, categorize as `critical` / `important` / `nice-to-have` and cite which REQ/INV/entity the gap touches (or note \"no anchor — entirely absent\"). Return JSON array.",
            "> You operate this product for a demanding customer (SRE / security reviewer / compliance auditor). Read these requirements, invariants, and entity model. Category list: authentication & RBAC, multi-tenancy isolation, audit trail, observability (metrics/alerts/SLOs), idempotency of user actions, API/CLI surface beyond UX, quotas & rate limits, crash recovery of control plane mid-operation, time/timezone semantics, encryption at rest/transit, backward compatibility & migration, concurrent operators. First discard the categories this area plausibly never touches (one line each: category — why not). For the remaining categories, generate the questions the spec does NOT answer — scale to the spec, roughly one question per 3 requirements, max 20. Every question must cite which REQ/INV/entity the gap touches, or state \"no anchor — entirely absent\"; drop any question you can neither anchor nor justify as an absence. Categorize each as `critical` / `important` / `nice-to-have`. Return JSON array.",
        ),
    ],
    r".claude/commands/spec-readback.md": [
        (
            "No diff machinery needed; don't break this with timestamps outside the designated status fields or with reflowed prose.",
            "No diff machinery needed. The only timestamps in the document are the header's \"Last verified\" and the Reference section's verification history — per-entry blocks carry none, so a re-check that changes nothing semantic produces an empty diff. Don't break this with stray dates or reflowed prose.",
        ),
        (
            "**Status:** <status>  |  **Requirements:** <n verified>/<total> verified, <n witnessed>/<total> witnessed  |  **Invariants:** <n verified>/<total>  |  **Open questions:** <n>  |  **Last verified:** <verification_log[-1] date or \"never\">\n",
            "**Status:** <status>  |  **Requirements:** <n verified>/<total> verified, <n witnessed>/<total> witnessed  |  **Invariants:** <n verified>/<total>  |  **Open questions:** <n>  |  **Last verified:** <verification_log[-1] date or \"never\">\n\n*Legend: ✓ verified against code · ◐ witnessed in model only · ✗ no witness · ⏳ not checked · ⊘ skipped (justified)*\n",
        ),
        (
            "*While* the account is Unlocked, *when* the user submits invalid credentials, the system *shall* increment failedAttempts and lock the account at MAX_FAILED_ATTEMPTS.",
            "*While* the account is Unlocked, *when* the user submits invalid credentials, the system *shall* increment failedAttempts and lock the account on the 5th failed attempt (MAX_FAILED_ATTEMPTS = 5).",
        ),
        (
            "> **Witness:** <one-line `tools/itf_tools.py summarize` output — e.g. \"6 steps: 5× login_failed(alice) → account Locked\">. Found by Apalache <witness.checked_at>; code replay: <✓ green / not yet run>.",
            "> **Witness:** <one-line `tools/itf_tools.py summarize` output — e.g. \"6 steps: 5× login_failed(alice) → account Locked\">",
        ),
        (
            "Status marks: ✓ verified (witness replayed green against code) · ◐ witnessed, not yet verified · ✗ no witness · ⏳ not checked · ⊘ skipped (cite the `justification` and the enforcing INV inline).",
            "Status marks: ✓ verified (witness replayed green against code) · ◐ witnessed, not yet verified · ✗ no witness · ⏳ not checked · ⊘ skipped (cite the `justification` and the enforcing INV inline). The status mark on the heading is the single source of verified/witnessed state — no dates and no \"code replay\" repeat in the witness line. Emit the Legend line under the header status bar exactly as shown, so reviewers never need this instruction file to decode marks.",
        ),
    ],
}

root = sys.argv[1]
total = 0
for rel, edits in EDITS.items():
    path = root + "/" + rel
    with io.open(path, encoding="utf-8") as f:
        text = f.read()
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            print("FAIL %s: expected 1 occurrence, found %d: %r" % (rel, n, old[:80]))
            sys.exit(1)
        text = text.replace(old, new)
        total += 1
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print("OK  %s (%d edits)" % (rel, len(edits)))
print("Applied %d edits." % total)
