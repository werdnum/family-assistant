# Script execution as the automatic review boundary

Status: implemented.

## Problem

An approved `execute_script` invocation can fail when its internal operations receive independent
automatic reviews. Those reviews see the individual operation and the original trigger, without the
executing script or its approval. Implementation steps consequently appear unrelated to the
authorized task and also consume additional review budget.

The original paths explain the behavior:

- `tools/infrastructure.py` constructs `ToolCallReviewInput` for each gated operation.
- `scripting/monty_engine.py` clears `taint_policy_snapshot` before dispatching nested tools. This
  obtains fresh taint state, but provides no parent execution authorization.
- `tools/execute_script.py` resolves stored source inside the tool, after the outer gate.
- `scripting/apis/keychute.py` authorizes brokered HTTP through the separate named-sink gate.

## Review boundary

Review the resolved script invocation as a program, including its source, effective inputs,
available capabilities, relevant policy constraints, and definition provenance. The reviewer
assesses the program's possible effects against the originating request, including loops,
data-dependent destinations, and calls that create durable behavior. Script text and inputs remain
untrusted review data, never reviewer instructions.

Resolve and validate inline or stored invocations before review without executing script code or
performing side effects. Execute the same resolved source and inputs that were reviewed; a stored
script name alone is insufficient. Incomplete source or unavailable review context cannot produce a
whole-script approval. Apply existing fail-closed review behavior rather than silently treating a
truncated program as approved. Durable confirmations store and render the resolved program and
effective inputs as the executable payload; replay does not look the source up again by name. Stored
identity and definition provenance provide review context, not a mutable execution reference.

An explicit successful review, or the human confirmation it requires, authorizes the deterministic
operations of that invocation. Carry this authorization in runtime-owned execution context through
the shared policy gates. Do not use `in_script`, a permissive policy result, an observe-mode
verdict, or a frozen taint snapshot as proof of approval. Unreviewed script executions retain
existing gates.

Nested operations covered by this approval do not repeat automatic intent adjudication. They still
pass tool availability, access control, hard policy denials, mandatory confirmation floors, argument
validation, and runtime resource limits. Approval does not widen the executing profile's powers.
Apply the same rule at tool dispatch and the named-sink authorization gate used by brokered HTTP.

Keep taint collection active throughout execution and propagate result provenance normally. An
approved program may read untrusted data and process it without triggering another intent review
merely because taint increased. Explicit hard taint restrictions still apply. The outer review must
assess how runtime data can influence effects; deterministic execution does not imply fixed or safe
destinations.

Approval ends at new executable code or a new model decision. Persistence of executable definitions
retains its own gate, with the enclosing source and parent decision as context, so the write can be
independently reviewed without borrowing the parent verdict. A nested script invocation receives its
own source-aware review; delegated agents, model-driven tools, callbacks, and future automation runs
do not inherit permission for their subsequent decisions. The approved script may initiate those
operations subject to existing policy, but their execution retains its own enforcement. A model
decision also ends inherited approval for the calling script's continuation. Any review still needed
within a script should receive the enclosing source and parent decision as context, so it does not
recreate the original context-free failure.

The execution authorization is local to the invocation and ends on completion, failure, timeout, or
cancellation. It must not leak to sibling calls or later turns. Persisted definitions retain their
existing provenance semantics: parent approval must not stamp arbitrary runtime-derived executable
content as human-authored or independently reviewed.

## Implementation milestones

1. **Make the invocation reviewable.** Share source resolution and input validation between review
   preparation and execution. Extend review input and rendering with the complete resolved program
   and execution capabilities. Verify inline and stored scripts show the actual executed source,
   including when a stored definition changes after preparation or while a durable confirmation is
   pending. Verify a script admitted without outer review supplies its full context when runtime
   taint causes the first nested review.
2. **Carry execution approval through the enforcement chokepoints.** Integrate scoped authorization
   into tool dispatch, Monty execution, and named-sink authorization. Preserve live taint tracking
   and policy floors. Verify an approved multi-tool script receives one automatic review and covered
   HTTP operations use that approval too.
3. **Verify boundary isolation and fallback.** Cover rejection before side effects, missing or
   failed review, observe mode, mandatory confirmation, hard denial, newly read untrusted data,
   nested code, model delegation, persisted definitions, and cleanup on failure/cancellation. Check
   concurrent sibling operations cannot borrow approval. Use functional tests with a recording
   reviewer and real policy/provider plumbing, supplemented by focused prompt-rendering tests.
4. **Make decisions explainable and document the behavior.** Link internal-operation audit records
   to the parent review and identify inherited authorization separately from independent approval.
   Count actual reviewer invocations against the review budget. Update the automatic-review design,
   scripting user documentation, and tool description. Add review-evaluation cases for legitimate
   intermediate steps and programs with unsafe data-driven effects, then run the relevant policy,
   script, review, and evaluation tests plus repository lint checks.

## Deliberate tradeoffs

This makes automatic judgment about the whole program, accepting that runtime values are not always
known at review time. Hard controls remain runtime-enforced. It does not attempt static effect
inference, enumerate every possible tool argument, or prove program safety. Programs whose effects
cannot be justified from the available context can still require confirmation or be denied.

The initial change targets explicit `execute_script` invocations. Other script entry points gain
inherited authorization only when they pass through the same source-aware review boundary; merely
using Monty confers no approval. No change to unrelated agent-review boundaries is proposed. Tool
metadata explicitly identifies operations eligible to inherit deterministic execution approval;
unclassified tools retain their own enriched gates. Missing metadata therefore adds review rather
than silently widening approval.

Confirmation replay is an inline invocation of the pinned program and must satisfy current policy
for that invocation. It does not retain a permission that applies only to a live named lookup.
Observe-mode shadow reviews remain independent, so their counts are not a forecast of the number of
reviews an approved program would need under enforcement.
