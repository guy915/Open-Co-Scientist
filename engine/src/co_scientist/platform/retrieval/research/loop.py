from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import Union

from co_scientist.platform.retrieval.research.artifacts import (
    CallStatus,
    Document,
    ExtractedFinding,
    Finding,
    Question,
    ResearchBudget,
    ResearchModelPort,
    ResearchResult,
    RetrievalPort,
    SearchCall,
    SourceHit,
    StopReason,
    ThreadRecord,
    ThreadStatus,
    dedupe_findings,
)

logger = logging.getLogger(__name__)

# Sources searched at once (this package imports nothing else in the repo, so
# it mirrors core.constants.SOURCE_FANOUT); unbounded fan-out bursts into
# upstream rate limits.
SOURCE_FANOUT = 3


def bind_findings(
    extracted: Sequence[ExtractedFinding],
    question: str,
    calls: Sequence[SearchCall],
) -> tuple[Finding, ...]:
    """Missing call provenance leaves an empty call_id; it must not discard
    the claim and quoted span.
    """
    call_by_locator = {hit.locator: call.id for call in calls for hit in call.hits}
    return tuple(
        Finding(
            text=item.text,
            question=question,
            locator=item.locator,
            span=item.span,
            call_id=call_by_locator.get(item.locator, ""),
        )
        for item in extracted
    )


def _claim_locators(
    calls: Sequence[SearchCall],
) -> list[tuple[SearchCall, list[SourceHit]]]:
    """Deduplicate before reserved/ordinary fills; the first source claims
    shared locators.
    """
    seen: set[str] = set()
    paired = []
    for call in calls:
        hits = [hit for hit in call.hits if hit.locator not in seen]
        seen.update(hit.locator for hit in hits)
        paired.append((call, hits))
    return paired


def _fill_reserved(
    paired: Sequence[tuple[SearchCall, list[SourceHit]]],
    budget: ResearchBudget,
) -> list[SourceHit]:
    """Reservations preserve source ranking, never pad missing hits or exceed
    the document budget.
    """
    quotas = dict(budget.reserved_slots)
    if not quotas:
        return []
    taken: list[SourceHit] = []
    for call, hits in paired:
        room = budget.hits_per_question - len(taken)
        places = min(quotas.get(call.source, 0), room)
        if places > 0:
            taken.extend(hits[:places])
    return taken


def admit_within_budget(
    calls: Sequence[SearchCall], budget: ResearchBudget
) -> tuple[list[SourceHit], list[SearchCall]]:
    """Reservations prevent configured-last sources being crowded out by
    indexed literature.
    """
    paired = _claim_locators(calls)
    admitted = _fill_reserved(paired, budget)
    admitted_locators = {hit.locator for hit in admitted}

    for _call, hits in paired:
        for hit in hits:
            if hit.locator in admitted_locators:
                continue
            if len(admitted) < budget.hits_per_question:
                admitted.append(hit)
                admitted_locators.add(hit.locator)

    recorded = [
        replace(
            call,
            admitted=tuple(hit.locator for hit in call.hits if hit.locator in admitted_locators),
            dropped=tuple(hit.locator for hit in call.hits if hit.locator not in admitted_locators),
        )
        for call in calls
    ]
    return admitted, recorded


SEED_STANCE = "seed"

_Continuation = Union[
    "tuple[list[Question], ResearchBudget]",
    StopReason,
]


async def conduct_research(
    *,
    goal: str,
    model: ResearchModelPort,
    retrieval: RetrievalPort,
    budget: ResearchBudget,
    seed_questions: Sequence[str] = (),
) -> ResearchResult:
    session = _Session(model=model, retrieval=retrieval, budget=budget)
    stances, questions = await session.open(goal, seed_questions)
    stop_reason, levels_run = await session.descend(questions)
    return ResearchResult(
        goal=goal,
        stances=stances,
        threads=tuple(session.threads),
        calls=tuple(session.calls),
        findings=dedupe_findings(session.findings),
        stop_reason=stop_reason,
        levels_run=levels_run,
    )


class _Session:
    def __init__(
        self,
        *,
        model: ResearchModelPort,
        retrieval: RetrievalPort,
        budget: ResearchBudget,
    ) -> None:
        self.model = model
        self.retrieval = retrieval
        self.budget = budget
        # Semaphores belong to the request's live loop, never a module shared by
        # worker cohorts.
        self.limiter = asyncio.Semaphore(budget.concurrency)
        self.fanout = asyncio.Semaphore(SOURCE_FANOUT)
        # Questions that reduce to one query share its search instead of
        # sending it again; each thread still records its own call.
        self.searches: dict[tuple[str, str], asyncio.Task[tuple[SourceHit, ...]]] = {}
        self.threads: list[ThreadRecord] = []
        self.calls: list[SearchCall] = []
        self.findings: list[Finding] = []

    async def open(
        self, goal: str, seed_questions: Sequence[str]
    ) -> tuple[tuple[str, ...], list[Question]]:
        """Plan stance coverage before questions so one attractive
        perspective cannot consume the first level.
        """
        if seed_questions:
            return (), [Question(text=text, stance=SEED_STANCE) for text in seed_questions]

        stances = tuple(await self.model.plan_stances(goal=goal, limit=self.budget.breadth))
        if not stances:
            return (), []

        asked = await asyncio.gather(
            *(self.model.ask_questions(goal=goal, stance=stance, limit=1) for stance in stances)
        )
        questions = [
            Question(text=text, stance=stance)
            for stance, texts in zip(stances, asked, strict=True)
            for text in texts
        ]
        return stances, questions

    async def descend(self, questions: list[Question]) -> tuple[StopReason, int]:
        levels_run = 0
        while questions:
            accepted = self._clamp(questions, levels_run + 1)
            if not accepted:
                return StopReason.NO_FOLLOW_UPS, levels_run
            levels_run += 1
            await asyncio.gather(*(self._run_thread(question, levels_run) for question in accepted))
            outcome = self._continue_from(levels_run)
            if isinstance(outcome, StopReason):
                return outcome, levels_run
            questions, self.budget = outcome
        return StopReason.NO_FOLLOW_UPS, levels_run

    def _continue_from(self, depth: int) -> _Continuation:
        level_threads = [thread for thread in self.threads if thread.depth == depth]
        if not any(thread.finding_ids for thread in level_threads):
            failed = sum(thread.status is ThreadStatus.FAILED for thread in level_threads)
            empty = sum(thread.status is ThreadStatus.EMPTY for thread in level_threads)
            logger.warning(
                "Research level %s produced no findings (%s failed, %s empty); stopping descent",
                depth,
                failed,
                empty,
            )
            return StopReason.NO_RESULTS
        next_budget = self.budget.descend()
        if next_budget is None:
            return StopReason.DEPTH_EXHAUSTED
        questions = self._follow_up_questions(depth)
        if not questions:
            return StopReason.NO_FOLLOW_UPS
        return questions, next_budget

    def _follow_up_questions(self, depth: int) -> list[Question]:
        """Deduplicate across levels as well as siblings to avoid paying
        again for already-answered questions.
        """
        seen: set[str] = {thread.question.text for thread in self.threads}
        questions: list[Question] = []
        for thread in self.threads:
            if thread.depth != depth:
                continue
            for text in thread.follow_ups:
                if text in seen:
                    continue
                seen.add(text)
                questions.append(
                    Question(
                        text=text,
                        stance=thread.question.stance,
                        parent_id=thread.question.id,
                    )
                )
        return questions

    def _clamp(self, questions: Sequence[Question], depth: int) -> list[Question]:
        """Preserve declined depth so refusal at different levels remains
        distinguishable.
        """
        breadth = self.budget.breadth
        for question in questions[breadth:]:
            self.threads.append(
                ThreadRecord(
                    question=question,
                    depth=depth,
                    status=ThreadStatus.DECLINED,
                    note=(f"Breadth budget spent: this level funds {breadth} questions"),
                    retry_breadth=len(questions),
                )
            )
        return list(questions[:breadth])

    async def _run_thread(self, question: Question, depth: int) -> None:
        async with self.limiter:
            try:
                record = await self._research_question(question, depth)
            except Exception as exc:
                logger.warning("Research thread failed (%s)", type(exc).__name__)
                record = ThreadRecord(
                    question=question,
                    depth=depth,
                    status=ThreadStatus.FAILED,
                    note=str(exc),
                )
            self.threads.append(record)

    async def _research_question(self, question: Question, depth: int) -> ThreadRecord:
        query = await self.model.to_query(question=question.text)
        calls = await self._search_sources(question.text, query)
        admitted, calls = admit_within_budget(calls, self.budget)
        self.calls.extend(calls)
        call_ids = tuple(call.id for call in calls)

        if not admitted:
            status = (
                ThreadStatus.FAILED
                if calls and all(call.status is CallStatus.FAILED for call in calls)
                else ThreadStatus.EMPTY
            )
            return ThreadRecord(
                question=question,
                depth=depth,
                status=status,
                call_ids=call_ids,
                note=(
                    "All sources failed"
                    if status is ThreadStatus.FAILED
                    else "No source returned a usable result"
                ),
            )
        return await self._read_for(question, depth, admitted, calls)

    async def _read_for(
        self,
        question: Question,
        depth: int,
        admitted: Sequence[SourceHit],
        calls: Sequence[SearchCall],
    ) -> ThreadRecord:
        call_ids = tuple(call.id for call in calls)
        documents = await self._read_documents(admitted)
        usable_documents = [document for document in documents if document.text.strip()]
        if not usable_documents:
            return ThreadRecord(
                question=question,
                depth=depth,
                status=ThreadStatus.FAILED,
                call_ids=call_ids,
                note="No admitted document had readable text or a snippet",
            )
        read_degradation = None
        if any(not document.full_text for document in documents):
            if any(document.full_text for document in documents):
                read_degradation = "Some full-text reads failed; available text was retained"
            else:
                read_degradation = "Full text was unavailable; snippets were used"

        extraction = await self.model.extract(
            question=question.text,
            documents=usable_documents,
        )
        findings = bind_findings(extraction.findings, question.text, calls)
        self.findings.extend(findings)

        if not findings:
            return ThreadRecord(
                question=question,
                depth=depth,
                status=ThreadStatus.EMPTY,
                call_ids=call_ids,
                follow_ups=extraction.follow_ups,
                note=(
                    "Nothing in the admitted documents answered it"
                    if read_degradation is None
                    else f"Nothing in the admitted documents answered it; {read_degradation}"
                ),
            )

        summary = await self.model.compress(question=question.text, findings=findings)
        return ThreadRecord(
            question=question,
            depth=depth,
            status=ThreadStatus.OK,
            call_ids=call_ids,
            finding_ids=tuple(finding.id for finding in findings),
            follow_ups=extraction.follow_ups,
            summary=summary,
            note=read_degradation,
        )

    async def _search_sources(self, question: str, query: str) -> list[SearchCall]:
        """A failed source must not veto healthy remote siblings or a local
        corpus.
        """

        async def one(source: str) -> SearchCall:
            started = time.monotonic()
            try:
                hits = await self._shared_search(query, source)
            except Exception as exc:
                logger.warning("Search failed (%s)", type(exc).__name__)
                return SearchCall(
                    question=question,
                    query=query,
                    source=source,
                    status=CallStatus.FAILED,
                    error=str(exc),
                    duration_seconds=time.monotonic() - started,
                )
            ordered = hits
            return SearchCall(
                question=question,
                query=query,
                source=source,
                status=CallStatus.OK if ordered else CallStatus.EMPTY,
                hits=ordered,
                duration_seconds=time.monotonic() - started,
            )

        gathered = await asyncio.gather(*(one(source) for source in self.budget.sources))
        return list(gathered)

    async def _shared_search(self, query: str, source: str) -> tuple[SourceHit, ...]:
        """Threads are cancelled only with their session, so a shared search
        may be cancelled with any of its awaiters."""
        key = (source, " ".join(query.split()))
        if key not in self.searches:
            self.searches[key] = asyncio.ensure_future(self._search(query, source))
        return await self.searches[key]

    async def _search(self, query: str, source: str) -> tuple[SourceHit, ...]:
        async with self.fanout:
            hits = await self.retrieval.search(
                query=query,
                source=source,
                limit=self.budget.hits_per_question,
            )
        return tuple(hits)

    async def _read_documents(self, hits: Sequence[SourceHit]) -> list[Document]:
        """Unreadable full text remains snippet-depth evidence rather than
        silently disappearing.
        """

        async def one(hit: SourceHit) -> Document:
            try:
                text = await self.retrieval.read(locator=hit.locator)
            except Exception as exc:
                logger.warning("Read failed (%s)", type(exc).__name__)
                text = None
            if text:
                return Document(hit=hit, text=text, full_text=True)
            return Document(hit=hit, text=hit.snippet, full_text=False)

        return list(await asyncio.gather(*(one(hit) for hit in hits)))
