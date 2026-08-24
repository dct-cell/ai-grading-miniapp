# Staged grading contract

This contract governs mathematical analysis and internal evidence. It does not
change the trusted service tier, competition rubric, output path, or conditional
web-search rule.

## General principles

- Do not begin with a desired total and retrofit reasons. Reconstruct what the
  student actually wrote, verify it, and then map verified achievements to a
  problem-specific marking scheme.
- Understand the problem target, hypotheses, necessary cases, and the student's
  route. A separate complete reference solution is not required. A public solution,
  when search is permitted, is an anchor rather than the only valid method.
- Preserve a mathematically reasonable alternative method even when it differs
  from a published solution, notation, order, or conventional presentation.
- Interpret unclear handwriting as the most reasonable mathematical reading in
  context. Record that reading under `interpretations`; do not deduct merely for
  ambiguity. If the chosen reading materially affects the score, mention it briefly
  in the public report as “按上下文理解为……”.
- Verify theorem hypotheses, definitions and object identity, point order,
  equality cases, sign conditions, computations, case coverage, and the final
  conclusion. Never silently repair the student's proof.
- Treat a suspected typo as `local` only when the intended expression is uniquely
  determined by nearby work, the correction adds no new idea, condition, or case,
  and later work consistently uses that intended form. Check both the literal and
  corrected readings; otherwise do not assume a typo.
- Finding one error does not end verification. Continue through all unaffected
  score-bearing steps, necessary conditions, and the final conclusion, and record
  every independent root error.
- Identify a root error and its downstream effects. Do not deduct again for each
  dependent line. Preserve credit for later work that remains independently valid.
- An absent solution receives zero and is described as `missing` / “未作答”, not as
  an erroneous proof.
- Internal JSON records evidence and decisions but is not copied wholesale into the
  report. The report keeps only decisive earned points, root errors, deduction
  reasons, and useful repair advice.

## Stage protocol

At the start of every stage, run exactly:

```bash
python .agents/skills/olympiad-grader/scripts/report_stage.py STAGE_ID
```

Use the following order. Do not go backwards.

1. `preparing`: render and inspect every submitted page; determine page ownership.
2. `understanding`: identify each problem, target, constraints, necessary cases,
   student route, missing work, and contextual interpretations. Write
   `output/internal/problem-analysis.json`.
3. `rubric`: build a method-independent scheme for the actual problem. Write
   `output/internal/marking-scheme.json`.
4. `decomposing`: faithfully represent the submitted proof as claims and
   dependencies. For `summary_report`, use location-free key mathematical units
   and group routine calculations or repeated equivalent deductions. For
   `annotated_review`, retain the source locations needed for selective page
   annotation. Write `output/internal/proof-map.json`.
5. `verifying`: test every recorded step and trace root errors. Write
   `output/internal/verification.json`.
6. `scoring`: map verified evidence to scoring checkpoints. Draft
   `output/internal/score-audit.json` with the initial judgment.
7. `auditing`: challenge high scores, recover valid alternative credit in low
   scores, remove duplicate credit/deductions, check caps and arithmetic, then
   update and freeze `score-audit.json` with the final judgment.
8. `reporting`: create `output/grading.json` only from the audited result. For
   `summary_report`, build `output/report.pdf`; for `annotated_review`, build
   `output/annotated.pdf`. The scoring judgment is shared; only public detail and
   report layout differ.
9. `validating`: render and inspect the report, correct layout/content defects,
   reopen the PDF, and return the manifest only after all files agree.

Write JSON as UTF-8. Every internal artifact has these root fields:

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null
}
```

For League profiles, `resolved_league_scope` is `full_paper` or `problem_set` and
must match the final grading file. For IMO and CMO it is `null`. Use stable ASCII
identifiers such as `p1`, `p1-u1-main`, `p1-s1`, and `p1-e1`.

## `problem-analysis.json`

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null,
  "problems": [
    {
      "id": "p1",
      "label": "第 1 题",
      "pages": [1, 2],
      "target": "需要证明或求出的精确目标",
      "constraints": ["题设条件", "不可忽略的定义或范围"],
      "student_route": "忠实概括学生实际采用的路线；未作答时写未作答",
      "interpretations": [
        {
          "reading": "按上下文理解为 x\\ge 0",
          "score_relevant": false
        }
      ],
      "submission_status": "answered"
    }
  ]
}
```

`submission_status` is `answered`, `partial`, or `missing`. List every input page
under at least one identified problem as appropriate. An empty `interpretations`
array is normal. A `summary_report` interpretation does not need a source
location. For `annotated_review`, add a nonempty `location` to every interpretation
so a score-relevant ambiguity can be marked selectively.

## `marking-scheme.json`

Create the scheme for this problem, not a generic prose rubric. A scoring slot is
one indivisible unit under the selected profile:

- IMO: seven slots, one point each.
- CMO: seven slots, three points each.
- League: four slots for a 40-point problem or five slots for a 50-point problem,
  ten points each.

Build the scheme from the problem's explicit obligations even when no complete
reference solution is available; absence of a reference must not stop grading.
Every slot must represent a concrete, independently checkable mathematical
achievement, not effort, length, a method name, a guessed answer, a few examples,
or a bound that is merely close. Separate genuinely independent obligations such
as necessity/sufficiency or bound/construction, but do not turn consecutive steps
of one dependency chain into freely additive parts. In each checkpoint description,
state what must minimally be established, allow stronger or functionally equivalent
results, and identify the common near-miss that is still insufficient.

Equivalent approaches may have different checkpoints with the same `slot_id`.
Only one checkpoint in a slot can earn that unit. Dependencies state mathematical
prerequisites and may reference either a specific checkpoint ID (`p1-u1-main`) or
the method-independent scoring slot ID (`p1-u1`). `exclusive_group` is `null`
unless two checkpoints cannot both be used. Shared or alternative checkpoints
must still map to an existing slot.

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null,
  "problems": [
    {
      "problem_id": "p1",
      "max_score": 7,
      "unit": 1,
      "base_units": 7,
      "checkpoints": [
        {
          "id": "p1-u1-main",
          "slot_id": "p1-u1",
          "points": 1,
          "description": "A method-independent mathematical achievement",
          "depends_on": [],
          "exclusive_group": null
        },
        {
          "id": "p1-u1-alt",
          "slot_id": "p1-u1",
          "points": 1,
          "description": "An equivalent alternative achievement",
          "depends_on": [],
          "exclusive_group": "p1-u1-route"
        }
      ],
      "zero_credit": [
        {"id": "p1-z1", "condition": "No relevant mathematical progress"}
      ],
      "deductions": [
        {
          "id": "p1-d1",
          "condition": "A root error invalidates named units",
          "withheld_slots": ["p1-u4", "p1-u5"],
          "root_error_once": true
        }
      ],
      "caps": [
        {
          "id": "p1-c1",
          "condition": "A specified global completeness failure",
          "max_score": 5
        }
      ]
    }
  ]
}
```

Include at least one checkpoint for every scoring slot. It is valid for alternative
checkpoints to make the checkpoint count exceed the number of slots. Use empty
arrays when no zero-credit rule, deduction rule, or cap is needed. Do not invent a
cap merely to force a preferred score.

## `proof-map.json`

Record the student's argument without correcting it or grading it here. Use the
evidence granularity selected by the trusted `service_tier`:

- For `summary_report`, create a location-free, minimally sufficient proof map.
  Record only score-bearing claims, decisive dependencies, necessary conditions,
  root errors, final conclusions, and the evidence needed by scoring checkpoints.
  Group consecutive routine calculations, algebraic expansions, and repeated
  equivalent deductions into one proof step. Do not locate individual steps by
  page, line, formula number, source quote, or coordinates. Compression must not
  hide an unsupported inference, missing case, invalid theorem application, or
  fatal gap. Do not group across a suspected typo, missing condition, or change in
  validity; split there so unaffected work can be verified and credited independently.
- For `annotated_review`, every substantive written step appears exactly once and
  retains the source page and location needed for selective annotation.

In both tiers, dependencies point only to earlier or logically prior recorded
steps. Every awarded checkpoint must still cite at least one proof step later
verified as `valid`, and every lost scoring unit must have an explicit
mathematical reason.

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null,
  "problems": [
    {
      "problem_id": "p1",
      "steps": [
        {
          "id": "p1-s1",
          "claim": "学生在此实际声称的结论",
          "category": "claim",
          "depends_on": []
        }
      ]
    }
  ]
}
```

The example above is the `summary_report` shape. For `annotated_review`, each
step additionally requires:

```json
{
  "page": 1,
  "location": "第 1 页第 3–5 行"
}
```

For a `missing` solution, `steps` is empty. Categories are descriptive (for
example `definition`, `construction`, `claim`, `calculation`, `case`, or
`conclusion`) and do not determine credit.

## `verification.json`

Give exactly one verification entry for every proof-map step. Verdicts are
`valid`, `invalid`, `unsupported`, or `ambiguous`. Repair scope is `none`, `local`,
or `global`. A root error may affect several later steps; those later entries point
to the same `root_error_id` rather than creating duplicate errors.

Use the following compact domain checks only where relevant; they are questions to
verify, not automatic deductions:

- Algebra: domain and sign conditions, reversible transformations, parameter
  boundaries, equality cases and attainability.
- Geometry: existence and branch of constructed objects, theorem hypotheses, and
  translation of coordinate/vector calculations back to the requested geometry;
  a diagram does not prove an unstated metric fact.
- Number theory: integrality and positivity, gcd conditions for cancellation or
  modular inverses, exceptional primes, and closure of necessity and sufficiency.
- Combinatorics: precise counted objects, no omission or double counting,
  construction constraints, and whether invariants or strategies cover every
  legal operation.

An omitted derivation remains `valid` only when it is routine and uniquely
recoverable from the written route. If completing it needs a new lemma, major case,
construction argument or technique, mark it `unsupported`; never supply that work
on the student's behalf.

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null,
  "problems": [
    {
      "problem_id": "p1",
      "root_errors": [
        {
          "id": "p1-e1",
          "step_id": "p1-s2",
          "description": "The first mathematically invalid inference",
          "affected_step_ids": ["p1-s2", "p1-s3"]
        }
      ],
      "steps": [
        {
          "step_id": "p1-s1",
          "verdict": "valid",
          "reason": "All required conditions are established.",
          "root_error_id": null,
          "impact": "Supports the first scoring unit.",
          "repair_scope": "none"
        }
      ]
    }
  ]
}
```

## `score-audit.json`

List every marking-scheme checkpoint exactly once. `checkpoint_results` is the
post-audit mapping: an awarded checkpoint earns exactly one profile unit and an
unawarded checkpoint earns zero. Never award two checkpoints sharing a slot.
Evidence step IDs must come from the proof map. A root error appears at most once
in `root_error_impacts`; its withheld slots must not also be awarded.

`initial_score` preserves the score before the skeptical review. The checkpoint
mapping and `final_score` contain the reviewed decision. If a listed cap applies,
the final score is the smaller of earned units and the strictest applied cap.

Before totaling a problem, distinguish two cases. If a core checkpoint or major
obligation is missing, award only slots supported by verified evidence. If the core
argument and all major obligations are present and every defect is repairable
locally along the student's existing route, reduce from the full applicable score
by the profile's scoring units. Do not combine these two calculations, and do not
remove independent valid work merely because another branch contains a root error.

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null,
  "total_score": 6,
  "max_score": 7,
  "problems": [
    {
      "problem_id": "p1",
      "submission_status": "answered",
      "unit": 1,
      "max_score": 7,
      "checkpoint_results": [
        {
          "checkpoint_id": "p1-u1-main",
          "awarded": true,
          "points_awarded": 1,
          "evidence_step_ids": ["p1-s1"],
          "reason": "The written step establishes this achievement."
        },
        {
          "checkpoint_id": "p1-u1-alt",
          "awarded": false,
          "points_awarded": 0,
          "evidence_step_ids": [],
          "reason": "Alternative checkpoint; the same slot is already earned."
        }
      ],
      "root_error_impacts": [
        {
          "root_error_id": "p1-e1",
          "withheld_slot_ids": ["p1-u7"],
          "reason": "One root error prevents this unit; downstream lines are not deducted again."
        }
      ],
      "caps_applied": [],
      "initial_score": 7,
      "final_score": 6,
      "review": {
        "high_score_challenge": "Checked whether any fatal gap was overlooked.",
        "low_score_credit_check": "Checked for an independently valid alternative route.",
        "double_count_check": "Checked every slot and root error once.",
        "band_and_total_check": "Checked the selected profile band and arithmetic.",
        "score_changed": true,
        "change_reason": "The skeptical review found one unsupported final unit."
      }
    }
  ]
}
```

When no score changes, set `score_changed` to `false` and `change_reason` to an
empty string. A missing solution has no proof steps, all checkpoints unawarded,
zero initial and final scores, and an explicit missing disposition.

## Final public files

After auditing, write `output/grading.json` using the existing format described in
`SKILL.md`. Its problem order, per-problem scores, maxima, standard, resolved scope,
and totals must exactly match `score-audit.json`. Public findings must be selective:
award evidence only where it helps explain real credit, mark root errors tightly,
and do not repeat downstream consequences as separate deductions.
