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

Statically named stored-script calls are part of the reviewed program. Resolve their transitive
closure recursively, retaining each definition's source, parameter schema, content hash, and
provenance. Shared descendants and cycles are visited once. Scheduled firings use the same closure
for their weakest-definition provenance. Missing dependencies fail closed before execution.
Confirmations carry the resolved dependency content and hashes; a changed or deleted dependency
requires fresh preparation and review. Child execution checks the binding before using the loaded
source, so approval cannot silently follow a mutable name to different code.

Approval covers what the reviewed source spells out; it ends at executable content the source does
not contain. Persistence of executable definitions retains its own gate, with the enclosing source
and parent decision as context, so the write can be independently reviewed without borrowing the
parent verdict. A statically bound child shares program approval; other nested script invocations
receive their own source-aware review; delegated agents, callbacks, and future automation runs do
not inherit permission for their subsequent decisions. The approved script may initiate those
operations subject to existing policy, but their execution retains its own enforcement. Any review
still needed within a script should receive the enclosing source and parent decision as context, so
it does not recreate the original context-free failure.

What those boundaries return is data, and the approved program's continuation keeps its approval. A
delegation, an `llm()` call or a data-producing tool can be steered by what it reads, but its output
steers the program only through the data-dependent paths the program review was asked to assess --
the same paths any untrusted read feeds. The caller's own next step is still fixed by its source.

A general-purpose sandbox is a deterministic operation like any other when the code it runs is part
of the reviewed program. Code-execution calls therefore inherit when the operator has opted the tool
in and every string argument is a complete string literal of the reviewed source or its bound
closure. Text assembled at runtime -- interpolated, concatenated, taken from inputs or read from a
result -- is new executable content and receives its own review. That review covers the call only;
the program's approval continues to cover its other operations.

A program admitted without a review of its own -- a scheduled or event firing, or an
`execute_script` call no gate asked about -- is decided by the first blocking model review one of
its operations needs. That review is given the program as the thing being approved alongside the
call, and a model allow approves both. Any other outcome is recorded on the program and not asked
again, so a stochastic verdict is not retried until it allows. Scheduled firings thereby pass
through the same source-aware review boundary as an explicit `execute_script` call, paying for a
review only when an operation needs one.

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

Other script entry points gain inherited authorization only when they pass through the same
source-aware review boundary, which for scheduled and event firings is the first nested review they
need; merely using Monty confers no approval. No change to unrelated agent-review boundaries is proposed. Static
discovery recognizes literal stored-script names, rather than attempting to evaluate arbitrary
expressions. Dynamically selected code retains its independent gate. Tool metadata explicitly
identifies operations eligible to inherit deterministic execution approval; unclassified tools
retain their own enriched gates. Missing metadata therefore adds review rather than silently
widening approval.

Confirmation replay is an inline invocation of the pinned program and must satisfy current policy
for that invocation. It does not retain a permission that applies only to a live named lookup.
Observe-mode shadow reviews remain independent, so their counts are not a forecast of the number of
reviews an approved program would need under enforcement.

The literal rule for code execution is deliberately syntactic. It does not follow data into files: a
literal command that runs a file an earlier step wrote from runtime data is covered by the program
review, which sees that the program writes and then runs it. The rule reads every string argument of
the call, working directory included, because an operator-supplied tag does not say which argument
a remote server executes. Scripts that need runtime data in a sandbox pass it through a file written
by a separate step rather than interpolating it into the call, or accept one review per
runtime-built call. Observe-mode shadow reviews still do not
approve a program, so observe-mode review counts are not a forecast of enforce-mode counts.

