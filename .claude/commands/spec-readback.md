# /spec-readback — put the current change in front of a person

The person reviews a generated page, never the Lean. This command produces that page for the change at hand and tells the person what to look at.

1. Find the revision the change started from: `git merge-base HEAD origin/main` (use the repository's default branch).
2. Run `lake exe check --since <that revision>`. The page `out/review.html` then marks what the change added, removed or altered against the ledger of that revision. Without `--since` the page shows everything with no comparison.
3. Tell the person, in a few plain sentences and in this order:
   - anything violated, failed, or drifting between code and model;
   - what the change does to the requirements: new, removed, reworded, or verified more weakly than before;
   - claims below their target, and why;
   - the open questions that are theirs to answer;
   - for an area extracted from code: that the binding under "what is compared" needs their confirmation.
4. Ask for corrections. A correction goes back through `/spec`; then run this command again.

You do not record approval. The change is approved when its pull request is approved, with the committed ledger as the record of what was verified.
