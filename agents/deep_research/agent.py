"""Deep Research: plan with the user, research the web, deliver a cited report.

The root agent drafts a research plan with the user. Once the user approves it,
it hands over to `research_pipeline`, which:

1. outlines the report's sections,
2. researches every goal in the plan with Google Search,
3. loops critic -> follow-up search until the critic passes the research (or
   MAX_REFINEMENT_LOOPS runs out),
4. writes the report and turns its `<cite>` tags into markdown links.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import AsyncGenerator
from typing import Literal

from agentkit_core.manifest import AgentManifest, Billing, Limits
from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event, EventActions
from google.adk.planners import BuiltInPlanner
from google.adk.tools import google_search
from google.adk.tools.agent_tool import AgentTool
from google.adk.utils.instructions_utils import inject_session_state
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from .prompts import (
    ENHANCED_SEARCH,
    INTERACTIVE_PLANNER,
    PLAN_GENERATOR,
    REPORT_COMPOSER,
    RESEARCH_EVALUATOR,
    SECTION_PLANNER,
    SECTION_RESEARCHER,
)

log = logging.getLogger(__name__)

MANIFEST = AgentManifest(
    slug="deep-search",
    # Bump this whenever you change this file — CI enforces it, so the version
    # shown in the portal and stamped on every event stays trustworthy.
    version="0.1.1",
    display_name="Deep Research",
    description=(
        "Researches any topic in depth: agrees a research plan with you, "
        "searches the web iteratively until a critic is satisfied, and "
        "delivers a structured report with inline citations."
    ),
    model="gemini-flash-latest",
    # One approved run is many model calls: section planner, researcher (one
    # call per search round), up to MAX_REFINEMENT_LOOPS rounds of critic +
    # follow-up search, then the composer. Size these for that, and lower
    # MAX_REFINEMENT_LOOPS before you lower them.
    limits=Limits(max_concurrent=4, max_llm_calls=150, timeout_s=1800),
    # A starter template: not billed to end users. To charge for it, replace
    # this with a sku, deposit_micros and token prices (see the Elan docs on
    # billing), and keep `include_plugins=False` on the AgentTool below.
    billing=Billing(unbilled=True),
)

# How many critic -> follow-up search rounds the pipeline may run before it
# writes the report with whatever it has.
MAX_REFINEMENT_LOOPS = 3


# --- Structured output ---


class SearchQuery(BaseModel):
    """One targeted web search query."""

    search_query: str = Field(
        description="A highly specific and targeted query for web search."
    )


class Feedback(BaseModel):
    """The critic's verdict on the research so far."""

    grade: Literal["pass", "fail"] = Field(
        description="'pass' if the research is sufficient, 'fail' if it needs another pass."
    )
    comment: str = Field(
        description="What is strong and what is missing in the research."
    )
    follow_up_queries: list[SearchQuery] | None = Field(
        default=None,
        description="Targeted queries that would close the gaps. Null or empty when the grade is 'pass'.",
    )


# --- Instructions ---


def _instruction(template: str):
    """An instruction that knows today's date and still reads session state.

    A callable instruction skips ADK's `{key}` templating, so this does it
    explicitly after filling `{current_date}`.
    """

    async def provider(ctx: ReadonlyContext) -> str:
        today = datetime.date.today().isoformat()
        return await inject_session_state(
            template.replace("{current_date}", today), ctx
        )

    return provider


# --- Citations ---


def collect_research_sources_callback(callback_context: CallbackContext) -> None:
    """Record every web source the searches grounded on, with a stable id.

    Walks the session's events for Gemini grounding metadata, gives each new URL
    an id (`src-1`, `src-2`, ...) and keeps the text segments it supports. The
    composer cites by these ids; `citation_replacement_callback` resolves them.
    """
    session = callback_context._invocation_context.session
    url_to_short_id = callback_context.state.get("url_to_short_id", {})
    sources = callback_context.state.get("sources", {})
    id_counter = len(url_to_short_id) + 1

    for event in session.events:
        grounding = event.grounding_metadata
        if not (grounding and grounding.grounding_chunks):
            continue
        chunk_ids = {}
        for idx, chunk in enumerate(grounding.grounding_chunks):
            if not chunk.web:
                continue
            url = chunk.web.uri
            if url not in url_to_short_id:
                short_id = f"src-{id_counter}"
                url_to_short_id[url] = short_id
                sources[short_id] = {
                    "short_id": short_id,
                    "title": chunk.web.title or chunk.web.domain,
                    "url": url,
                    "domain": chunk.web.domain,
                    "supported_claims": [],
                }
                id_counter += 1
            chunk_ids[idx] = url_to_short_id[url]

        for support in grounding.grounding_supports or []:
            scores = support.confidence_scores or []
            for i, chunk_idx in enumerate(support.grounding_chunk_indices or []):
                if chunk_idx not in chunk_ids:
                    continue
                sources[chunk_ids[chunk_idx]]["supported_claims"].append(
                    {
                        "text_segment": support.segment.text if support.segment else "",
                        "confidence": scores[i] if i < len(scores) else 0.5,
                    }
                )

    callback_context.state["url_to_short_id"] = url_to_short_id
    callback_context.state["sources"] = sources


_CITE_TAG = re.compile(r'\s*<cite\s+source\s*=\s*["\']?\s*(src-\d+)\s*["\']?\s*/>')


def citation_replacement_callback(
    callback_context: CallbackContext,
) -> genai_types.Content:
    """Turn `<cite source="src-N" />` tags into markdown links.

    Unknown ids are dropped with a warning. The returned content replaces the
    composer's own output, so the user sees the linked report.
    """
    report = callback_context.state.get("final_cited_report", "")
    sources = callback_context.state.get("sources", {})

    def link(match: re.Match) -> str:
        source = sources.get(match.group(1))
        if not source:
            log.warning("Dropping citation to an unknown source: %s", match.group(0))
            return ""
        title = source.get("title") or source.get("domain") or match.group(1)
        return f" [{title}]({source['url']})"

    linked = _CITE_TAG.sub(link, report)
    linked = re.sub(r"\s+([.,;:])", r"\1", linked)
    callback_context.state["final_report_with_citations"] = linked
    return genai_types.Content(parts=[genai_types.Part(text=linked)])


# --- Loop control ---


class EscalationChecker(BaseAgent):
    """Ends the refinement loop as soon as the critic grades the research 'pass'."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        evaluation = ctx.session.state.get("research_evaluation")
        if evaluation and evaluation.get("grade") == "pass":
            log.info("[%s] research passed; ending the loop", self.name)
            yield Event(author=self.name, actions=EventActions(escalate=True))
        else:
            yield Event(author=self.name)


# --- The agent ---


def _thinking() -> BuiltInPlanner:
    return BuiltInPlanner(
        thinking_config=genai_types.ThinkingConfig(include_thoughts=True)
    )


def build(ctx) -> BaseAgent:
    """Return the agent this container runs.

    Every sub-agent is built here rather than at import: an ADK agent may have
    only one parent, so module-level instances could not survive a second build.
    """
    plan_generator = LlmAgent(
        name="plan_generator",
        model=ctx.model,
        description="Drafts or refines an action-oriented research plan, searching only to identify the topic.",
        instruction=_instruction(PLAN_GENERATOR),
        tools=[google_search],
    )

    section_planner = LlmAgent(
        name="section_planner",
        model=ctx.model,
        description="Turns the approved plan into a markdown outline of report sections.",
        instruction=SECTION_PLANNER,
        output_key="report_sections",
    )

    section_researcher = LlmAgent(
        name="section_researcher",
        model=ctx.model,
        description="Runs the first full pass of web research over the plan.",
        planner=_thinking(),
        instruction=SECTION_RESEARCHER,
        tools=[google_search],
        output_key="section_research_findings",
        after_agent_callback=collect_research_sources_callback,
    )

    research_evaluator = LlmAgent(
        name="research_evaluator",
        model=ctx.model,
        description="Critically grades the research and proposes follow-up queries.",
        instruction=_instruction(RESEARCH_EVALUATOR),
        output_schema=Feedback,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        output_key="research_evaluation",
    )

    enhanced_search_executor = LlmAgent(
        name="enhanced_search_executor",
        model=ctx.model,
        description="Runs the critic's follow-up searches and merges the results.",
        planner=_thinking(),
        instruction=ENHANCED_SEARCH,
        tools=[google_search],
        output_key="section_research_findings",
        after_agent_callback=collect_research_sources_callback,
    )

    report_composer = LlmAgent(
        name="report_composer_with_citations",
        model=ctx.model,
        include_contents="none",
        description="Writes the final report from the findings, with inline citations.",
        instruction=REPORT_COMPOSER,
        output_key="final_cited_report",
        after_agent_callback=citation_replacement_callback,
    )

    research_pipeline = SequentialAgent(
        name="research_pipeline",
        description="Executes an approved research plan: researches, refines until it passes review, and writes a cited report.",
        sub_agents=[
            section_planner,
            section_researcher,
            LoopAgent(
                name="iterative_refinement_loop",
                max_iterations=MAX_REFINEMENT_LOOPS,
                sub_agents=[
                    research_evaluator,
                    EscalationChecker(name="escalation_checker"),
                    enhanced_search_executor,
                ],
            ),
            report_composer,
        ],
    )

    return LlmAgent(
        name="interactive_planner_agent",
        model=ctx.model,
        description=ctx.manifest.description,
        instruction=_instruction(INTERACTIVE_PLANNER),
        sub_agents=[research_pipeline],
        # include_plugins=False keeps the nested run out of the platform's
        # plugins; on a billed target it is required, or the ledger opens a
        # second hold that never settles.
        tools=[AgentTool(plan_generator, include_plugins=False)],
        output_key="research_plan",
    )
