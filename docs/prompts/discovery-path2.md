# Agent prompt: decision tables with discovery (path 2)

A prompt to hand to an agent working in this repository. It has not been carried out yet.

Background: the "Discovery" section of https://claude.ai/artifact/LktuzxPw6hnFuxTbRhcMRd#discovery

```
In the quintessence repo, adopt path 2 (decision tables as the spec core) and extend it for the discovery use case, as described in the "Discovery" section of https://claude.ai/artifact/LktuzxPw6hnFuxTbRhcMRd#discovery.

Make these changes:
1. Add a discovery engine that generates finite checklists from the tables and turns each item into a question for the user: each condition asked of the other actions on the same entity, every state against every action, a way out of every state, paired opposites (e.g. lock/unlock), who may take each action, inputs that don't fit, and events that never arrive.
2. Give every table cell a provenance field (from the draft, confirmed by user, or inferred by the agent). Treat agent-inferred cells as open questions until the user confirms them.
3. Remove "unchanged" as a silent default. It must be an explicit answer the user gives.

Keep the existing completeness and consistency checks over the tables. Use an auth spec (login, password reset, lockout, unknown user) as the worked example and test. Open a PR with the changes.
```
