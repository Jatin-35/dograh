"""Topic: structure node prompts in sections; sequence multi-turn tasks."""

from __future__ import annotations

from api.services.voice_prompting_guide._base import (
    AuditCheck,
    Stage,
    StageLens,
    VoicePromptingTopic,
)

TOPIC = VoicePromptingTopic(
    id="call_flow_design",
    title="Structure node prompts; sequence multi-turn tasks; design conversation around variable extraction",
    severity="medium",
    applies_to_node_types=("agentNode", "startCall"),
    stages={
        Stage.plan: StageLens(
            relevant=True,
            lens=(
                "For each multi-turn node, sketch the step sequence (e.g. get name → "
                "get order ID → verify → call tool → read back). Decide what each "
                "node collects — one item per turn."
            ),
        ),
        Stage.create: StageLens(
            relevant=True,
            lens=(
                "Break the node prompt into 5-8 labeled sections in the documented "
                "order, state the stay/move exit conditions in the prompt itself, and "
                "close with a Rules block of short prohibitions. Write multi-turn "
                "tasks as a numbered sequence, collect one piece of information per "
                "turn, and keep variable-extraction instructions in the node's "
                "separate extraction_prompt field, not the main prompt."
            ),
        ),
        Stage.review: StageLens(
            relevant=True,
            lens=(
                "Check the node asks for one thing at a time, that it states when to "
                "stay and when to move on, and that it ends with a Rules block rather "
                "than trailing off after the call flow. Check extraction logic isn't "
                "tangled into the conversational prompt, and whether the nodes are "
                "created around variable extraction."
            ),
        ),
    },
    content="""\
A good node prompt is broken into clear sections — pick five to eight depending
on the use case rather than dumping one wall of text.

Use these section headings, in this order. Omit any the node does not need;
do not reorder the ones you keep.

  1. Main task at this node — one or two lines stating why this node exists.
  2. To-do list             — the checklist that must be satisfied before leaving.
  3. Call flow              — the narrative, or a numbered sequence for multi-turn work.
  4. Exit conditions        — when to stay, when to move, and to which node.
  5. Common objections      — only if this node actually attracts them.
  6. Knowledge base         — pointers to documents, never pasted reference data.
  7. Rules                  — the closing block; see below.

Sections are about ordering the agent's attention, not about length. A
well-sectioned node prompt is usually shorter than the wall of text it
replaces, because structure exposes the sentences that were saying the same
thing twice.

## State the exit conditions inside the prompt

Edge conditions decide routing, but the node prompt must also say it plainly
in its own words: "Stay in this node until X. Move to Y only when Z." The
duplication is deliberate — the edge condition is what the router evaluates,
while the prompt governs what the agent does while it is still here. Nodes
that leave this out hand off early, on the first plausible-sounding reply,
before the to-do list is actually complete.

Where a node should hold for more than one turn, say so explicitly — "use the
first one or two user turns to confirm you are speaking to the right person"
— because otherwise a single acknowledgement reads as completion.

## Close every node prompt with a Rules block

The final section is always the constraints, written as short imperative
bullets. Put them last: a model follows the end of a prompt more reliably than
its middle, so the things that must never happen belong at the bottom, not
buried under the call flow.

Cover whichever of these apply to the node:

  - Turn discipline — end the turn with either a question or a tool call,
    never both in the same output.
  - Tool-call hygiene — never mix prose and a tool call in one output; do not
    make a tool call when the last message was not from the user.
  - Repetition — do not ask again for something the user has already given.
  - Promises — never offer an email, callback, ticket number or timeline the
    workflow cannot actually deliver.
  - Invention — never invent a name, company, price, policy or prior
    interaction that was not supplied.

Write them as prohibitions, one per line. "Do not X" is followed more
reliably than "remember to Y", and a bullet list survives compaction better
than the same rules dissolved into prose.

For multi-turn tasks, break the work into a numbered sequence inside the call
flow. A refund-status flow looks like:
  1. Get the caller's name.
  2. Ask for the order ID.
  3. Verify the order ID character by character.
  4. Call get_order_details with orderId and name.
  5. Read back the order status.
  6. Ask if they need anything else.

Remember, the goal of this call is to collect information so design the questions
and flow which makese a coherent sense to a user.

Collect one thing at a time. Agents that ask "Can I get your name, date of
birth, and reason for calling?" almost always fail — the user gives one piece,
the agent has to chase the rest, and the flow falls apart. Sequencing one
question per turn is slower in theory but faster in practice because you never
have to recover from a half-answered batch.

Keep variable extraction out of the conversational prompt. Dograh gives each
agent/start/end node a separate `extraction_prompt` field — put the logic for
capturing a value there. The call flow can say "ask for the order ID"; the
rule for parsing and storing it belongs in extraction_prompt.

Generic, always-applicable material (persona, common objections, global
response style, anti-jailbreak rules) belongs in the global prompt, not in
each node prompt — a global node is reachable from anywhere in the call.
""",
    audit_checks=(
        AuditCheck(
            id="collects_one_thing_at_a_time",
            judge_question=(
                "When the node gathers multiple pieces of information, does the "
                "prompt instruct the agent to collect them one at a time rather than "
                "asking for several in a single turn?"
            ),
            expected="yes",
            quote=(
                "Prompt batches several asks in one turn — collect one item at a "
                "time, confirming as you go."
            ),
        ),
        AuditCheck(
            id="states_its_own_exit_conditions",
            judge_question=(
                "Does the node prompt say in its own words when to stay in this "
                "node and when to move on — not relying on the edge conditions "
                "alone to express that?"
            ),
            expected="yes",
            quote=(
                "Prompt never says when to stay or when to move on — the agent "
                "hands off on the first plausible reply. State it explicitly."
            ),
        ),
        AuditCheck(
            id="ends_with_a_rules_block",
            judge_question=(
                "Does the node prompt end with a short block of constraints or "
                "rules, written as prohibitions, rather than trailing off after "
                "the call flow?"
            ),
            expected="yes",
            quote=(
                "Prompt has no closing rules block — put the must-never-happen "
                "constraints last, where the model follows them most reliably."
            ),
        ),
        AuditCheck(
            id="extraction_kept_separate",
            judge_question=(
                "Is the main conversational prompt free of variable-extraction "
                "instructions (which belong in the separate extraction_prompt "
                "field)?"
            ),
            expected="yes",
            quote=(
                "Extraction logic is mixed into the main prompt — move it to the "
                "node's extraction_prompt field."
            ),
        ),
    ),
    cross_refs=("common_guidelines", "success_criteria", "tool_calls"),
)
