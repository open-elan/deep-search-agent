# Deep Research

A starter agent for the Elan platform that does in-depth research on any
topic. You give it a question; it agrees a research plan with you, searches
the web until its own critic is satisfied, and returns a structured report
with inline citations linking to every source.

Use it as is, or copy it as the starting point for your own multi-agent
research, analysis or due-diligence agents.

## How it works

```
interactive_planner_agent          talks to the user; never answers directly
 ├─ tool: plan_generator           drafts / refines a [RESEARCH] / [DELIVERABLE] plan
 └─ research_pipeline              runs only after the user approves the plan
     1. section_planner            plan -> outline of 4-6 report sections
     2. section_researcher         4-5 Google searches per goal, then builds deliverables
     3. iterative_refinement_loop  up to MAX_REFINEMENT_LOOPS rounds of:
          research_evaluator         strict critic: pass, or fail + follow-up queries
          escalation_checker         ends the loop on "pass"
          enhanced_search_executor   runs the follow-ups, merges the findings
     4. report_composer_with_citations
                                   writes the report with <cite source="src-N" /> tags,
                                   which a callback turns into markdown links
```

A typical conversation:

1. **You:** "How are small modular reactors changing nuclear economics?"
2. **Agent:** proposes a 5-goal research plan plus implied deliverables.
3. **You:** "Add a comparison of the top three vendors." → the plan is refined.
4. **You:** "Looks good, run it." → the pipeline runs and returns the cited report.

### Session state

| Key | Written by | Holds |
| --- | --- | --- |
| `research_plan` | `interactive_planner_agent` | the agreed plan |
| `report_sections` | `section_planner` | markdown outline of the report |
| `section_research_findings` | `section_researcher`, `enhanced_search_executor` | the latest full findings |
| `research_evaluation` | `research_evaluator` | `{grade, comment, follow_up_queries}` |
| `sources`, `url_to_short_id` | `collect_research_sources_callback` | every grounded web source, by `src-N` id |
| `final_cited_report` | `report_composer_with_citations` | report with `<cite>` tags |
| `final_report_with_citations` | `citation_replacement_callback` | report with markdown links |

Citations come from Gemini's search grounding metadata, so every link in the
report points at a page the model actually read.

## Files

```
elan.yaml                        deploy-time facts: tenant, model allowlist, resources
pyproject.toml                   extra dependencies beyond the runtime image
agents/deep_research/agent.py    MANIFEST, callbacks, and build(ctx) — the agent graph
agents/deep_research/prompts.py  every agent's instruction
```

## Run it locally

Put a Gemini API key in `.env` (git-ignored):

```bash
GOOGLE_API_KEY=your-key
```

Then:

```bash
elan env --runtime /path/to/elan-platform   # the local runtime; temporary
elan validate                               # checks elan.yaml against the MANIFEST
elan adk web                                # chat with it in the ADK dev UI
```

Use `elan adk`, not plain `adk`: Elan targets export `build(ctx)` rather than
`root_agent`, and `elan adk` generates the shim that wires them up with the
platform's plugins.

## Make it yours

- **Depth vs. cost.** `MAX_REFINEMENT_LOOPS` in `agent.py` caps the
  critic → search rounds (default 3). `Limits(max_llm_calls=150,
  timeout_s=1800)` in the `MANIFEST` caps a whole run; keep it in step with
  the loop count.
- **Tone and structure.** All behaviour lives in `prompts.py`. Change the
  number of plan goals, the report's sections or the critic's bar there.
- **Skip the approval step.** For a fully autonomous agent, have `build()`
  return `research_pipeline` instead and seed `research_plan` from the user's
  message (e.g. a `before_agent_callback`).
- **Add your own sources.** Give `section_researcher` and
  `enhanced_search_executor` extra tools, such as an internal search API or an
  MCP server declared as a `ToolBinding` in the `MANIFEST`. Add each host to
  `egress` in `elan.yaml`.
- **Change the model.** Update `model` in the `MANIFEST` and `llm.models` in
  `elan.yaml` together; `elan validate` refuses a mismatch. The built-in
  `google_search` tool needs a Gemini model.
- **Bill for it.** The starter is `Billing(unbilled=True)`. To charge your end
  users, set a `sku`, `deposit_micros` and token prices instead.

## Deploy

```bash
elan secret set llm default --from-file key.txt   # the Gemini key, once per tenant
# set source.repo in elan.yaml to your repository, then:
git push          # elan deploy builds the REMOTE commit, not your disk
elan deploy
elan invoke deep-research --text "Research the state of solid-state batteries"
```

## The two files, and why there are two

`elan.yaml` holds **deploy-time facts**: what to build, how big, what it may
reach. Each agent's Python `MANIFEST` holds **runtime behaviour**: its model,
its tools, what it may do without asking. No field appears in both. Where they
look like they overlap they are in a validation relationship: the MANIFEST
states what the agent asks for, `elan.yaml` states what the platform permits,
and a deploy is refused on a mismatch.

## Dependencies

The platform's runtime image already carries ADK and the agentkit packages.
`pyproject.toml` lists only what the image lacks. Never put `agentkit-*` in
it.
