

## FINAL TURN - OUTPUT FORMAT

This is the final turn of the debate. Based on the discussion above, output your finalized hypothesis in JSON format with every required field of the schema. Write as a domain expert in the field of the research goal; do not force an engineering or product framing, and do not impose an artificial length limit.

The termination condition above is satisfied on this turn by the JSON below: the self-contained exposition of the finalized idea is the `hypothesis` field's value. Do not write the bare "HYPOTHESIS" token, or any other text, outside the JSON object.

### 1. hypothesis (required)
State the proposed mechanistic claim or relationship and the specific, testable prediction it makes. Use the natural language of the goal's scientific domain (molecular, cellular, physical, chemical, ecological, etc.). Name the entities, mechanism, direction of effect, and conditions precisely. Distinguish what is grounded in evidence from what is proposed. Do NOT begin with a fixed phrase such as "We want to develop"; write the claim directly. Be as long as the science requires — do not compress a mechanism into two sentences.

Example: "Partial inhibition of enzyme E in tissue T reduces the flux of metabolite M through pathway P, which in turn upregulates receptor R via the loss of feedback repression; the testable prediction is that E inhibition raises surface R density and downstream signaling in a dose-dependent manner, reversible by exogenous M."

### 2. explanation (required)
Mechanistic rationale for a scientific audience:
- The core problem or gap being addressed
- Why the proposed mechanism is plausible and how its components interact
- The boundary between established evidence and inference in this proposal

### 3. literature_grounding (required)
Explicit grounding with inline citation keys (2-4 sentences).
- If a Citation Reference List was provided above, use ONLY those `[C*]` keys (e.g. `[C1]`, `[C2]`, `[C3]`)
- Do NOT invent author-year citations — only use keys from the list
- If no Citation Reference List was provided, state: "This hypothesis is formulated without access to a literature review."

Example: "This mechanism builds on the reported activity of enzyme E [C1] and the feedback regulation of receptor R by metabolite M [C2]. It extends those findings to tissue T, where the interaction has not been directly measured [C3]."

### 4. experiment (required)
Return a structured pilot plan object, not a free-text paragraph. Put 2-5 ordered steps in `steps`, ending with the decisive Go/No-Go initial experiment. Name the model or system, methodology, controls, and quantitative readouts. Give the exact quantitative pass and fail thresholds for that final step in `go_criterion` and `no_go_criterion`; the latter is the explicit falsification criterion.

Also supply every other required schema field: a short noun-phrase `title`, scene-setting `introduction`, `recent_findings`, a short mechanism-family `category`, and the proposer's own `safety_and_toxicity` assessment.

---

Output exactly 1 hypothesis as valid JSON:
{
  "hypotheses": [
    {
      "title": "...",
      "introduction": "...",
      "recent_findings": "...",
      "hypothesis": "...",
      "explanation": "...",
      "literature_grounding": "...",
      "experiment": {
        "steps": ["Step 1: ...", "Step 2 (Go/No-Go initial experiment): ..."],
        "go_criterion": "...",
        "no_go_criterion": "..."
      },
      "category": "...",
      "safety_and_toxicity": "..."
    }
  ]
}

IMPORTANT: Use plain text with standard punctuation (no LaTeX, no decorative Unicode).
