import json
import logging
from pathlib import Path
from typing import Any, Literal, cast

from Bio import Entrez

from mcp_server.cache_privacy import PUBLIC_PAPERS
from mcp_server.entrez import (
    MAX_ENTREZ_RESPONSE_BYTES,
    entrez_call,
    entrez_worker,
    read_entrez_bytes,
)
from mcp_server.log_privacy import failure_summary
from mcp_server.pmc_articles import split_pmc_articles, unavailable_pmc_ids
from mcp_server.pubmed_client import (
    MAX_FULLTEXT_PAPERS,
    MAX_PUBMED_PAPERS,
    _EntrezClient,
    validate_paper_count,
)
from mcp_server.pubmed_storage import (
    confined_path,
    validate_cache_identifier,
    write_metadata_cache_file,
)

logger = logging.getLogger(__name__)

# Marks a book record, which has no journal metadata but is not a failure.
_BOOK: Literal["book"] = "book"

# Full articles run to hundreds of kilobytes each; keep one response bounded.
PMC_BATCH_SIZE = 20


def _validate_pmc_id(metadata: dict[str, Any]) -> None:
    pmc_id = metadata.get("pmc_full_text_id")
    if pmc_id is not None:
        validate_cache_identifier(str(pmc_id), label="PMC ID", numeric=True)


def _is_valid_pubmed_id(value: str) -> bool:
    try:
        validate_cache_identifier(value, label="PubMed ID", numeric=True)
    except ValueError:
        return False
    return True


class PubmedSource(_EntrezClient):
    def _cached_metadata(self, paper_id: str, shared_dir: Path) -> dict[str, Any] | None:
        validate_cache_identifier(paper_id, label="PubMed ID", numeric=True)
        metadata_file = confined_path(shared_dir.parent, "shared", f"{paper_id}.metadata.json")
        if not metadata_file.exists():
            return None
        with metadata_file.open("rb") as stream:
            metadata = json.loads(read_entrez_bytes(stream))
        _validate_pmc_id(metadata)
        return cast(dict[str, Any], metadata)

    def _fetch_uncached_metadata(
        self, paper_ids: list[str], shared_dir: Path
    ) -> dict[str, dict[str, Any] | None | Literal["book"]]:
        try:
            fetched = self._fetch_papers_details(paper_ids)
        except Exception as exc:
            logger.warning("PubMed metadata fetch failed (%s)", failure_summary(exc))
            return dict.fromkeys(paper_ids)
        results: dict[str, dict[str, Any] | None | Literal["book"]] = {}
        for paper_id in paper_ids:
            if paper_id not in fetched:
                results[paper_id] = None
                continue
            metadata = fetched[paper_id]
            if metadata is None:
                results[paper_id] = _BOOK
                continue
            try:
                _validate_pmc_id(metadata)
                metadata_file = confined_path(
                    shared_dir.parent, "shared", f"{paper_id}.metadata.json"
                )
                write_metadata_cache_file(metadata_file, metadata)
                results[paper_id] = metadata
            except Exception as exc:
                logger.warning("PubMed metadata fetch failed (%s)", failure_summary(exc))
                results[paper_id] = None
        return results

    async def _gather_paper_metadata(
        self,
        paper_ids: list[str],
        shared_dir: Path,
    ) -> tuple[dict[str, Any], int]:
        """Uncached papers share one efetch and one ELink per batch instead of
        two requests each."""
        unique_ids = list(dict.fromkeys(paper_ids))
        results: dict[str, dict[str, Any] | None | Literal["book"]] = {}
        uncached: list[str] = []
        for paper_id in unique_ids:
            if (cached := self._cached_metadata(paper_id, shared_dir)) is not None:
                results[paper_id] = cached
            else:
                uncached.append(paper_id)
        if uncached:
            # Entrez's blocking HTTP calls and send gate must run off the
            # event loop.
            results.update(await entrez_worker(self._fetch_uncached_metadata, uncached, shared_dir))
        details = {
            paper_id: metadata
            for paper_id in unique_ids
            if isinstance(metadata := results[paper_id], dict)
        }
        return details, sum(results[paper_id] == _BOOK for paper_id in unique_ids)

    def _download_pmc_fulltext(self, pmc_id: str) -> str:
        """PMC can truncate documents across efetch responses, requiring
        pagination.
        """
        validate_cache_identifier(pmc_id, label="PMC ID", numeric=True)
        chunks = []
        cursor = 0
        remaining = MAX_ENTREZ_RESPONSE_BYTES
        for _ in range(4):
            response = entrez_call(
                Entrez.efetch,
                db="pmc",
                id=pmc_id,
                retstart=cursor,
                rettype="xml",
            )
            raw = read_entrez_bytes(response, remaining)
            remaining -= len(raw)
            body = raw.decode("utf-8")
            chunks.append(body)
            if "[truncated]" in body or "Result too long" in body:
                cursor += len(body)
            else:
                return "".join(chunks)
        raise ValueError("PMC pagination budget exceeded")

    def get_pubmed_fulltext(self, pmc_id: str, slug: str, run_id: str | None = None) -> str | None:
        try:
            shared_dir, _run_dir = self._prepare_run_directories(slug, run_id)
            validate_cache_identifier(pmc_id, label="PMC ID", numeric=True)
            fulltext_file = confined_path(shared_dir.parent, "shared", f"{pmc_id}.fulltext.html")
            if fulltext_file.exists():
                with fulltext_file.open("rb") as handle:
                    contents = read_entrez_bytes(handle).decode("utf-8")
            else:
                contents = self._download_pmc_fulltext(pmc_id)
                confined_path(shared_dir.parent, "shared", f"{pmc_id}.fulltext.html")
                fulltext_file.write_text(contents, encoding="utf-8")
            return contents
        except Exception as exc:
            logger.error("PMC fulltext download failed (%s)", failure_summary(exc))
            return None

    def _download_pmc_fulltexts(self, pmc_ids: list[str]) -> dict[str, str]:
        """One efetch per batch. An article the answer leaves out or cuts off
        is fetched alone; one PMC reports unavailable is not. A failed batch is
        logged and skipped, so an outage costs one request, not one per article."""
        if len(pmc_ids) > MAX_PUBMED_PAPERS:
            raise ValueError("PMC article budget exceeded")
        unique = list(dict.fromkeys(pmc_ids))
        for pmc_id in unique:
            validate_cache_identifier(pmc_id, label="PMC ID", numeric=True)
        found: dict[str, str] = {}
        for start in range(0, len(unique), PMC_BATCH_SIZE):
            batch = unique[start : start + PMC_BATCH_SIZE]
            try:
                response = entrez_call(Entrez.efetch, db="pmc", id=",".join(batch), rettype="xml")
                body = read_entrez_bytes(response).decode("utf-8")
            except Exception as exc:
                logger.error(
                    "PMC fulltext batch of %s failed (%s)", len(batch), failure_summary(exc)
                )
                continue
            articles = split_pmc_articles(body, batch)
            found.update(articles)
            unavailable = (unavailable_pmc_ids(body) & set(batch)) - articles.keys()
            if unavailable:
                logger.warning("PMC has no full text for %s articles", len(unavailable))
            missing = [p for p in batch if p not in articles and p not in unavailable]
            if missing:
                found.update(self._download_pmc_fulltexts_alone(missing))
        return found

    def _download_pmc_fulltexts_alone(self, pmc_ids: list[str]) -> dict[str, str]:
        logger.warning("PMC batch left %s articles out; fetching them alone", len(pmc_ids))
        found: dict[str, str] = {}
        for pmc_id in pmc_ids:
            try:
                found[pmc_id] = self._download_pmc_fulltext(pmc_id)
            except Exception as exc:
                logger.error("PMC fulltext download failed (%s)", failure_summary(exc))
        return found

    def _store_pmc_fulltexts(self, pmc_ids: list[str], slug: str, run_id: str | None) -> None:
        try:
            shared_dir, _run_dir = self._prepare_run_directories(slug, run_id)
            files: dict[str, Path] = {}
            for pmc_id in dict.fromkeys(pmc_ids):
                validate_cache_identifier(pmc_id, label="PMC ID", numeric=True)
                fulltext_file = confined_path(
                    shared_dir.parent, "shared", f"{pmc_id}.fulltext.html"
                )
                if not fulltext_file.exists():
                    files[pmc_id] = fulltext_file
            fetched = self._download_pmc_fulltexts(list(files)) if files else {}
        except Exception as exc:
            logger.error("PMC fulltext download failed (%s)", failure_summary(exc))
            return
        for pmc_id, contents in fetched.items():
            try:
                files[pmc_id].write_text(contents, encoding="utf-8")
            except Exception as exc:
                logger.error("PMC fulltext cache write failed (%s)", failure_summary(exc))

    async def _download_fulltexts_for_papers(
        self,
        paper_ids: list[str],
        all_details: dict[str, Any],
        slug: str,
        run_id: str | None,
    ) -> None:
        pmc_ids = [str(all_details[paper_id]["pmc_full_text_id"]) for paper_id in paper_ids]
        # Entrez's blocking HTTP calls and send gate must run off the event loop.
        await entrez_worker(self._store_pmc_fulltexts, pmc_ids, slug, run_id)

    def _prepare_run_directories(self, slug: str, run_id: str | None) -> tuple[Path, Path | None]:
        validate_cache_identifier(slug, label="slug")
        if run_id is not None:
            validate_cache_identifier(run_id, label="run ID")
        shared_dir = confined_path(self.qualified_path, PUBLIC_PAPERS, "shared")
        shared_dir.mkdir(parents=True, exist_ok=True)
        return shared_dir, None

    def _assemble_final_results(
        self,
        papers_to_use: list[str],
        all_details: dict[str, Any],
        max_papers: int,
    ) -> dict[str, Any]:
        selected_ids = list(dict.fromkeys(papers_to_use))
        selected_ids.extend(paper_id for paper_id in all_details if paper_id not in selected_ids)
        return {paper_id: all_details[paper_id] for paper_id in selected_ids[:max_papers]}

    async def pubmed_search(
        self,
        query: str,
        slug: str,
        max_papers: int = 10,
        recency_years: int = 0,
        run_id: str | None = None,
        *,
        include_fulltext: bool = True,
    ) -> dict[str, Any]:
        """Search extra candidates to cover missing full text; disabling
        downloads must not change selection.
        """
        validate_paper_count(max_papers, MAX_FULLTEXT_PAPERS)
        shared_dir, _run_dir = self._prepare_run_directories(slug, run_id)
        paper_ids = self.pubmed_search_ids(
            query,
            retmax=max_papers * 3,
            recency_years=recency_years,
        )
        paper_ids = list(
            dict.fromkeys(paper_id for paper_id in paper_ids if _is_valid_pubmed_id(paper_id))
        )
        all_details, books = await self._gather_paper_metadata(paper_ids, shared_dir)
        if paper_ids and len(all_details) + books != len(paper_ids):
            raise RuntimeError("PubMed metadata unavailable")
        papers_to_use = [
            paper_id
            for paper_id, metadata in all_details.items()
            if metadata.get("pmc_full_text_id") is not None
        ][:max_papers]
        if include_fulltext:
            await self._download_fulltexts_for_papers(papers_to_use, all_details, slug, run_id)
        return self._assemble_final_results(papers_to_use, all_details, max_papers)
