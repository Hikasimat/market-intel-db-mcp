"""Market Intel DB intelligent, evidence-based company and event research engine."""
from research.cache import ResearchCache, SQLiteResearchCache, make_company_key
from research.fetcher import SafeFetcher
from research.models import (
    CandidateContact,
    CompanyResearchResult,
    CompatibilityLevel,
    ConflictRecord,
    EventResearchResult,
    FetchedPage,
    FieldEvidence,
    ResearchBudget,
    SearchResult,
    SourceReliability,
    SourceType,
    TokenUsageRecord,
    UsageSummary,
    VerificationStatus,
    VortexenProfile,
    VortexenRelevance,
)
from research.provider_router import ProviderRouter
from research.search import SearchClient
from research.service import ResearchService
from research.vortexen_profile import VortexenProfileManager

__all__ = [
    "CandidateContact",
    "CompanyResearchResult",
    "CompatibilityLevel",
    "ConflictRecord",
    "EventResearchResult",
    "FetchedPage",
    "FieldEvidence",
    "ProviderRouter",
    "ResearchBudget",
    "ResearchCache",
    "ResearchService",
    "SQLiteResearchCache",
    "SafeFetcher",
    "SearchClient",
    "SearchResult",
    "SourceReliability",
    "SourceType",
    "TokenUsageRecord",
    "UsageSummary",
    "VerificationStatus",
    "VortexenProfile",
    "VortexenProfileManager",
    "VortexenRelevance",
    "make_company_key",
]
