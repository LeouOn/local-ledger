"""TAX-07: propose tax fields from stored documents using the local model.

The model proposes; the reviewer disposes. Everything in this module flows
through exactly one seam - ``TaxWorkspaceService.propose_field`` - and every
proposal lands as PROPOSED with model provenance. Nothing here verifies,
totals, or touches a cloud endpoint.

Design in one paragraph: per document, pages with text (only
``PageText.has_text``; scanned or unreadable pages are reported as notes,
never sent to the model) are chunked; each chunk gets one local-model call
whose output is parsed against a strict schema with bounded retries
(``llm.structured``); every candidate is then re-validated against the FULL
page text - the model never gets the last word on its own citations. Valid
candidates become :class:`ExtractionProposal` records with exact integer
cents parsed from the verbatim source text; everything else becomes an
explicitly-reasoned :class:`UnresolvedField` or a per-document
:class:`DocumentExtractionError`. Batch writes happen only after every model
call has completed, so an outage raises before anything is written.

Security posture:

- **Local-only transport.** Production wiring goes through
  :func:`local_client_and_model`, which reuses ``llm.providers.get_local_client``
  and therefore inherits the loopback-endpoint refusal and the Ollama-ready
  gate. There is no code path from this module to a cloud client.
- **Untrusted document contents.** Extracted text is interpolated only inside
  explicit data markers in the prompt, and its only escape hatches are
  validated citations. Embedded instructions cannot change the supported
  schema (a module constant), the workflow, or the transport; adversarial
  documents at worst produce rejections.
- **Citations must be true.** A page number that does not exist, a snippet
  that is not a verbatim substring of that page, or an amount text that is
  not verbatim on the page is a rejection with a reason - never a guess, and
  never a silent drop.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from local_ledger.ingest.amounts import AmbiguousAmountError, parse_amount_to_cents
from local_ledger.llm.structured import (
    StructuredOutputError,
    make_openai_completer,
)
from local_ledger.llm.structured import (
    extract as structured_extract,
)
from local_ledger.tax.extraction_prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from local_ledger.tax.investments import INCOME_FIELDS
from local_ledger.tax.pdf_text import PageStatus, PageText, extract_pages

__all__ = [
    "CHUNK_CHARS",
    "CHUNK_OVERLAP",
    "SUPPORTED_FIELD_NAMES",
    "DocumentExtractionError",
    "ExtractionProposal",
    "ExtractionProposalSet",
    "MalformedModelOutput",
    "ProposalRunReport",
    "UnresolvedField",
    "extract_document_fields",
    "local_client_and_model",
    "propose_for_documents",
]

# --------------------------------------------------------------------------
# Supported field schema (closed by design; anything else is "unsupported")
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SupportedField:
    """One field the extractor may propose, and the label the prompt shows."""

    name: str
    label: str
    source: str


#: Canonical name for each consolidated-1099 income key in
#: ``investments.INCOME_FIELDS``. Aliases of those keys (see the alias table
#: there) are accepted as input names but normalized to these.
_INCOME_KEY_CANONICAL: dict[str, str] = {
    "interest.box1": "box1_interest_income",
    "interest.box3": "box3_us_savings_bonds",
    "dividends.box1a": "box1a_total_ordinary_dividends",
    "dividends.box1b": "box1b_qualified_dividends",
    "dividends.box2a": "box2a_capital_gain_distributions",
    "dividends.box7": "box7_foreign_tax_paid",
    "summary.gross_proceeds": "1099b_gross_proceeds",
}

#: Consolidated-1099 boxes the fixtures carry that INCOME_FIELDS has no
#: reconciliation measure for yet. Proposable and verifiable; reconciliation
#: can adopt them later without any change here.
_FIXTURE_EXTRA_FIELDS: tuple[SupportedField, ...] = (
    SupportedField(
        "box2_early_withdrawal_penalty",
        "Early withdrawal penalty (1099-INT box 2)",
        "1099-INT",
    ),
    SupportedField(
        "box4_federal_tax_withheld",
        "Federal income tax withheld (1099-INT / 1099-DIV box 4)",
        "1099",
    ),
    SupportedField(
        "box8_tax_exempt_interest", "Tax-exempt interest (1099-INT box 8)", "1099-INT"
    ),
    SupportedField(
        "box3_nondividend_distributions",
        "Nondividend distributions (1099-DIV box 3)",
        "1099-DIV",
    ),
)

#: Rental annual statement lines, named after the fixture / measure
#: conventions (``docs/tax-filing/fixtures/rental-statement-property-a.json``).
_RENTAL_FIELDS: tuple[SupportedField, ...] = tuple(
    SupportedField(name, label, "RENTAL")
    for name, label in (
        ("gross_rents_received", "Gross rents received"),
        ("advertising", "Advertising"),
        ("cleaning_and_maintenance", "Cleaning and maintenance"),
        ("commissions", "Commissions"),
        ("insurance", "Insurance"),
        ("legal_and_professional", "Legal and professional fees"),
        ("management_fees", "Management fees"),
        ("mortgage_interest_1098_box1", "Mortgage interest paid (1098 box 1)"),
        ("repairs", "Repairs"),
        ("supplies", "Supplies"),
        ("property_taxes", "Property taxes"),
        ("utilities", "Utilities"),
        ("hoa_dues", "HOA dues"),
    )
)


def _build_supported_fields() -> tuple[SupportedField, ...]:
    fields: list[SupportedField] = []
    for key, canonical in _INCOME_KEY_CANONICAL.items():
        spec = INCOME_FIELDS[key]
        fields.append(SupportedField(canonical, spec.label, spec.tax_section))
    fields.extend(_FIXTURE_EXTRA_FIELDS)
    fields.extend(_RENTAL_FIELDS)
    return tuple(fields)


SUPPORTED_FIELDS: tuple[SupportedField, ...] = _build_supported_fields()
SUPPORTED_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in SUPPORTED_FIELDS)

#: Normalization map: any accepted spelling (lowercased) -> canonical name.
#: Accepts every INCOME_FIELDS alias, the canonical names themselves, and the
#: fixture-style ``<name>_cents`` variants; the canonical name is what is
#: recorded on the proposal.
_ALIASES_TO_CANONICAL: dict[str, str] = {}
for _field in SUPPORTED_FIELDS:
    _ALIASES_TO_CANONICAL[_field.name.lower()] = _field.name
    _ALIASES_TO_CANONICAL[f"{_field.name}_cents"] = _field.name
for _key, _canonical in _INCOME_KEY_CANONICAL.items():
    for _alias in INCOME_FIELDS[_key].aliases:
        _ALIASES_TO_CANONICAL.setdefault(_alias.lower(), _canonical)
del _field, _key, _canonical, _alias

# --------------------------------------------------------------------------
# Chunking bounds
# --------------------------------------------------------------------------

#: Characters of page text per model call. A statement page is rarely longer;
#: bigger chunks only dilute the model's attention.
CHUNK_CHARS = 6000
#: Overlap between consecutive chunks of one page, so a value whose line is
#: cut by the window boundary is still fully visible in (at least) one chunk.
CHUNK_OVERLAP = 200


@dataclass(frozen=True)
class _Chunk:
    page_number: int
    text: str
    is_partial_page: bool


def _chunk_page(page: PageText, chunk_chars: int, chunk_overlap: int) -> list[_Chunk]:
    text = page.text
    if len(text) <= chunk_chars:
        return [_Chunk(page.page_number, text, False)]
    chunks: list[_Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_chars, len(text))
        if end < len(text) and chunk_overlap > 0:
            # Prefer a line boundary inside the back-off window so the cut
            # lands between lines, not through a value.
            newline = text.rfind("\n", max(start, end - chunk_overlap), end)
            if newline > start:
                end = newline + 1
        end = max(end, start + 1)
        chunks.append(_Chunk(page.page_number, text[start:end], True))
        if end >= len(text):
            break
        start = max(end - chunk_overlap, start + 1)
    return chunks


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionProposal:
    """A validated, schema-bounded candidate ready for ``propose_field``."""

    field_name: str
    raw_text: str
    amount_cents: int
    page_number: int
    box_or_line: str
    snippet_text: str


@dataclass(frozen=True)
class UnresolvedField:
    """A candidate that was seen but could not be honestly proposed, and why."""

    field_name: str
    page_number: int | None
    reason: str


@dataclass(frozen=True)
class DocumentExtractionError:
    """One document failed structurally or at write time; others survive."""

    document_id: str
    reason: str


@dataclass(frozen=True)
class ExtractionProposalSet:
    """Everything extraction concluded about one document."""

    document_id: str
    prompt_version: str
    model: str
    proposals: tuple[ExtractionProposal, ...] = ()
    unresolved: tuple[UnresolvedField, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProposalRunReport:
    """Batch outcome: per-document sets, per-document errors, write count."""

    sets: tuple[ExtractionProposalSet, ...]
    errors: tuple[DocumentExtractionError, ...]
    written_count: int


class MalformedModelOutput(Exception):
    """The model violated the structural output contract after retries."""


# --------------------------------------------------------------------------
# Strict model-output schema (structure only; semantics checked below)
# --------------------------------------------------------------------------


class _ModelProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: str
    raw_text: str
    page_number: int
    box_or_line: str = ""
    snippet_text: str


class _ModelBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[_ModelProposal]


# --------------------------------------------------------------------------
# Candidate validation - citations must be true
# --------------------------------------------------------------------------


def _repeated_lines(pages: Sequence[PageText]) -> set[str]:
    """Lines (stripped) that appear verbatim on two or more pages.

    Repeated headers/footers are exactly this shape, and a proposal whose
    citation resolves only to such lines is rejected: page furniture must
    never become a field proposal.
    """
    pages_per_line: dict[str, set[int]] = {}
    for page in pages:
        for line in page.text.splitlines():
            key = line.strip()
            if key:
                pages_per_line.setdefault(key, set()).add(page.page_number)
    return {line for line, pgs in pages_per_line.items() if len(pgs) >= 2}


def _validate_candidate(
    candidate: _ModelProposal,
    *,
    pages_by_number: dict[int, PageText],
    repeated_lines: set[str],
    seen_fields: set[str],
) -> ExtractionProposal | UnresolvedField:
    canonical = _ALIASES_TO_CANONICAL.get(candidate.field_name.strip().lower())
    if canonical is None:
        return UnresolvedField(
            candidate.field_name,
            candidate.page_number,
            "unsupported field name (not in the supported schema); "
            "unsupported-by-design and reported, not guessed",
        )

    def unresolved(reason: str) -> UnresolvedField:
        return UnresolvedField(canonical, candidate.page_number, reason)

    page = pages_by_number.get(candidate.page_number)
    if page is None or not page.has_text:
        return unresolved(
            f"citation page {candidate.page_number} does not exist or "
            "produced no extractable text"
        )

    snippet = candidate.snippet_text
    if not snippet.strip() or snippet not in page.text:
        return unresolved(
            f"citation snippet is not a verbatim substring of page "
            f"{page.page_number} text (hallucinated locator)"
        )

    raw = candidate.raw_text
    if not raw.strip() or raw not in page.text:
        return unresolved(
            f"amount text {raw!r} is not found verbatim on page {page.page_number}"
        )
    if raw not in snippet:
        return unresolved("citation snippet does not contain the amount text")

    lines = [line.strip() for line in snippet.splitlines() if line.strip()]
    if lines and all(line in repeated_lines for line in lines):
        return unresolved(
            "citation resolves only to lines repeated across pages "
            "(header/footer furniture)"
        )

    try:
        amount_cents = parse_amount_to_cents(raw, locale="us")
    except AmbiguousAmountError as exc:
        return unresolved(f"amount {raw!r} could not be parsed exactly: {exc}")

    if canonical in seen_fields:
        return unresolved("duplicate proposal: this field was already proposed once")

    seen_fields.add(canonical)
    return ExtractionProposal(
        field_name=canonical,
        raw_text=raw,
        amount_cents=amount_cents,
        page_number=page.page_number,
        box_or_line=candidate.box_or_line.strip(),
        snippet_text=snippet,
    )


# --------------------------------------------------------------------------
# Pure entry point (TAX-10 can benchmark this without any PDF or DB)
# --------------------------------------------------------------------------


def extract_document_fields(
    page_texts: Sequence[PageText],
    *,
    client: Any,
    model: str,
    document_id: str = "",
    chunk_chars: int = CHUNK_CHARS,
    chunk_overlap: int = CHUNK_OVERLAP,
    max_retries: int = 1,
) -> ExtractionProposalSet:
    """Propose fields for one document from its per-page texts.

    ``client`` is any OpenAI-compatible chat client (in production, the local
    provider client; in tests, a fake - the unit environment blocks sockets).
    Only pages whose status says they actually produced text are used; every
    other page becomes a note. Malformed model output raises
    :class:`MalformedModelOutput` (per-document, callers catch); a transport
    or availability failure raises whatever the client raised - clean, and
    nothing has been written.
    """
    usable = [page for page in page_texts if page.has_text]
    notes: list[str] = [
        f"page {page.page_number} skipped ({page.status.value}): "
        "no extractable text; enter fields manually"
        for page in page_texts
        if not page.has_text and page.status is not PageStatus.SKIPPED_AFTER_LIMIT
    ]
    if not usable:
        return ExtractionProposalSet(
            document_id=document_id,
            prompt_version=PROMPT_VERSION,
            model=model,
            notes=(
                *notes,
                "no pages with extractable text were available to the model",
            ),
        )

    pages_by_number = {page.page_number: page for page in usable}
    repeated_lines = _repeated_lines(usable)

    chunks: list[_Chunk] = []
    for page in usable:
        page_chunks = _chunk_page(page, chunk_chars, chunk_overlap)
        if any(chunk.is_partial_page for chunk in page_chunks):
            notes.append(
                f"page {page.page_number} was split into {len(page_chunks)} chunks; "
                "a field split across a chunk boundary is left unresolved"
            )
        chunks.extend(page_chunks)

    completer = make_openai_completer(client)
    candidates: list[_ModelProposal] = []
    for chunk in chunks:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_prompt(
                    page_number=chunk.page_number,
                    page_text=chunk.text,
                    supported_fields=list(SUPPORTED_FIELD_NAMES),
                    is_partial_page=chunk.is_partial_page,
                ),
            },
        ]
        try:
            batch = structured_extract(
                completer,
                _ModelBatch,
                messages,
                model,
                max_retries=max_retries,
            )
        except StructuredOutputError as exc:
            raise MalformedModelOutput(
                f"model returned structurally invalid output: {exc}"
            ) from exc
        candidates.extend(batch.proposals)

    seen_fields: set[str] = set()
    proposals: list[ExtractionProposal] = []
    unresolved: list[UnresolvedField] = []
    for candidate in candidates:
        outcome = _validate_candidate(
            candidate,
            pages_by_number=pages_by_number,
            repeated_lines=repeated_lines,
            seen_fields=seen_fields,
        )
        if isinstance(outcome, ExtractionProposal):
            proposals.append(outcome)
        else:
            unresolved.append(outcome)

    return ExtractionProposalSet(
        document_id=document_id,
        prompt_version=PROMPT_VERSION,
        model=model,
        proposals=tuple(proposals),
        unresolved=tuple(unresolved),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# Batch wrapper: complete every model call first, then write via the seam
# --------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def propose_for_documents(
    service: Any,
    workspace_id: str,
    documents: Sequence[tuple[str, bytes]],
    *,
    client: Any,
    model: str,
    chunk_chars: int = CHUNK_CHARS,
    chunk_overlap: int = CHUNK_OVERLAP,
    clock: Callable[[], datetime] = _utc_now,
) -> ProposalRunReport:
    """Extract and propose fields for a batch of ``(document_id, pdf_bytes)``.

    Phase 1 runs every page extraction and model call first; a model outage
    (any non-:class:`MalformedModelOutput` exception) propagates before a
    single write happens. Malformed output for one document becomes a
    :class:`DocumentExtractionError` while the other documents continue.
    Phase 2 pushes each validated proposal through
    ``TaxWorkspaceService.propose_field`` with full model provenance.
    """
    sets: list[ExtractionProposalSet] = []
    errors: list[DocumentExtractionError] = []

    for document_id, pdf_bytes in documents:
        result = extract_pages(pdf_bytes)
        try:
            proposal_set = extract_document_fields(
                result.pages,
                client=client,
                model=model,
                document_id=document_id,
                chunk_chars=chunk_chars,
                chunk_overlap=chunk_overlap,
            )
        except MalformedModelOutput as exc:
            errors.append(DocumentExtractionError(document_id, str(exc)))
            continue
        sets.append(proposal_set)

    provenance = {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "transport": "local",
        "proposed_at": clock().isoformat(),
    }

    written = 0
    for proposal_set in sets:
        try:
            for proposal in proposal_set.proposals:
                service.propose_field(
                    workspace_id=workspace_id,
                    document_id=proposal_set.document_id,
                    field_name=proposal.field_name,
                    raw_text=proposal.raw_text,
                    amount_cents=proposal.amount_cents,
                    page_number=proposal.page_number,
                    box_or_line=proposal.box_or_line,
                    snippet_text=proposal.snippet_text,
                    model_provenance=dict(provenance),
                )
                written += 1
        except Exception as exc:  # workspace gone/locked etc. - keep the batch alive
            errors.append(
                DocumentExtractionError(
                    proposal_set.document_id, f"workspace write failed: {exc}"
                )
            )

    return ProposalRunReport(
        sets=tuple(sets), errors=tuple(errors), written_count=written
    )


# --------------------------------------------------------------------------
# Production wiring (local transport only)
# --------------------------------------------------------------------------


def local_client_and_model(settings: Any) -> tuple[Any, str]:
    """Build the local-only client from provider settings.

    Reuses the settings page's provider machinery: ``get_local_client``
    refuses any non-loopback base URL (:class:`NonLocalEndpointError`) and
    raises a typed, guidance-carrying error when Ollama is not READY - which
    is the model-outage path. There is deliberately no fallback.
    """
    from local_ledger.llm.providers import get_local_client

    client = get_local_client(settings)
    return client, settings.local_model
