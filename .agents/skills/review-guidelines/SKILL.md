---
name: review-guidelines
description: Family Assistant's code review standards — severity levels, the project threat model, the proportionality gate, design-document review, and which comments not to post. Use when reviewing a pull request, diff, or design document in this repository, and when deciding how to answer review feedback on your own change.
---

# Review Guidelines

Read [references/review-guidelines.md](references/review-guidelines.md) in full before writing
review feedback. It defines what makes a comment actionable, the severity levels and the threat
model behind `SECURITY_RISK`, the cost/benefit gate for proportionality, how to review a design
document, and what not to comment on. The automated reviewers (`scripts/review-changes.py` and the
Gemini review workflow) load the same file, so a review you write and a bot's review apply one
standard.

## Answering review on your own change

Stop review-fix loops at the scope boundary. On rereview, distinguish defects in the original change
from defects introduced by earlier feedback. If repairing review-added code would require another
layer of state, validation, attestation, retries, or lifecycle machinery, prefer deletion,
narrowing, reuse of an existing chokepoint, or an accepted bounded residual unless the user
explicitly authorizes the expanded design. Record an accepted residual in the change or its design
doc, as the root `AGENTS.md` describes, so the next round does not reopen it.
