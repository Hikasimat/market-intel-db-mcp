"""Prospect input schema.

Field names and enums mirror the Django ProspectSerializer / ProspectForm as
described in the project brief. UNVERIFIED (project source unavailable at build
time) and marked `# ASSUMED`: key-contact `prefix` values, the shape of
`acquiring_companies_data` and `shareholdings` items, `market_event_ids`.
Everything is `extra="forbid"`: unsupported fields are rejected, never dropped.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal, Optional
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

COMPANY_STRUCTURES = ("Parent", "Branch", "Subsidiary")
OPERATIONAL_STATUSES = ("Active", "Inactive", "Permanently Closed", "Acquired", "Merged")
OWNERSHIP_SECTORS = ("Private Company", "Government Company", "Semi Government Company")
PRIMARY_OFFERING_TYPES = ("Products", "Services", "Solutions", "Multiple")
OFFERING_TYPES = ("Product", "Service", "Solution")
OFFERING_FOR_PRIMARY = {"Products": "Product", "Services": "Service", "Solutions": "Solution"}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-().]{5,24}$")


def _blank_to_none(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _check_email(v: Optional[str]) -> Optional[str]:
    v = _blank_to_none(v)
    if v is None:
        return None
    if len(v) > 254 or not EMAIL_RE.match(v):
        raise ValueError("is not a valid email address")
    return v


def _check_url(v: Optional[str], host_suffix: str | None = None) -> Optional[str]:
    v = _blank_to_none(v)
    if v is None:
        return None
    p = urlparse(v)
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname or " " in v:
        raise ValueError("must be a valid http(s) URL")
    if host_suffix and not (p.hostname == host_suffix or p.hostname.endswith("." + host_suffix)):
        raise ValueError(f"must be a {host_suffix} URL")
    return v


def _check_phone(v: Optional[str]) -> Optional[str]:
    v = _blank_to_none(v)
    if v is None:
        return None
    if not PHONE_RE.match(v) or len(re.sub(r"\D", "", v)) < 7:
        raise ValueError("is not a valid phone number (digits, +, spaces, dashes, brackets; 7+ digits)")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Offering(_Strict):
    offering_type: Literal["Product", "Service", "Solution"]
    name: str = Field(min_length=1, max_length=255)


class KeyContact(_Strict):
    prefix: Optional[str] = Field(default=None, max_length=50)
    contact_name: str = Field(min_length=1, max_length=255)
    designation: Optional[str] = Field(default=None, max_length=255)
    official_email: Optional[str] = None
    phone_number: Optional[str] = None
    linkedin_profile: Optional[str] = None

    _prefix = field_validator("prefix", mode="before")(_blank_to_none)
    _designation = field_validator("designation", mode="before")(_blank_to_none)
    _email = field_validator("official_email")(_check_email)
    _phone = field_validator("phone_number")(_check_phone)

    @field_validator("linkedin_profile")
    @classmethod
    def _li(cls, v):
        return _check_url(v, "linkedin.com")


class AcquiringCompany(_Strict):
    acquiring_company: UUID
    acquired_shares: Optional[float] = Field(default=None, ge=0, le=100)


class Shareholding(_Strict):
    holder_type: Literal["Company", "Individual", "Government", "Other"]
    share_percentage: float = Field(gt=0, le=100)
    company: Optional[UUID] = None             # Company: existing prospect UUID
    is_self: bool = False                      # Company: holds shares in itself
    contact: Optional[UUID] = None             # Individual: existing contact UUID
    contact_index: Optional[int] = Field(default=None, ge=0)  # Individual: index in key_contacts
    government_entity: Optional[UUID] = None   # Government
    other_name: Optional[str] = None           # Other

    _name = field_validator("other_name", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _by_type(self):
        t = self.holder_type
        if t == "Company" and not (self.company or self.is_self):
            raise ValueError("Company shareholder needs company (existing prospect UUID) or is_self=true")
        if t == "Company" and self.company and self.is_self:
            raise ValueError("Company shareholder cannot set both company and is_self")
        if t == "Individual" and self.contact is None and self.contact_index is None:
            raise ValueError("Individual shareholder needs contact or contact_index (position in key_contacts)")
        if t == "Government" and not self.government_entity:
            raise ValueError("Government shareholder needs government_entity")
        if t == "Other" and not self.other_name:
            raise ValueError("Other shareholder needs other_name")
        return self


class ProspectInput(_Strict):
    company_name: str = Field(min_length=1, max_length=255)
    country_head_office: str = Field(min_length=1, max_length=100)
    complete_address: str = Field(min_length=1)
    primary_industries: str = Field(min_length=1)
    ownership_sector: Literal["Private Company", "Government Company", "Semi Government Company"]
    company_structure: Literal["Parent", "Branch", "Subsidiary"]
    operational_status: Literal["Active", "Inactive", "Permanently Closed", "Acquired", "Merged"]
    primary_offering_type: Literal["Products", "Services", "Solutions", "Multiple"]

    official_phone_number: Optional[str] = None
    official_email_address: Optional[str] = None
    official_website_url: Optional[str] = None
    linkedin_company_page: Optional[str] = None

    parent_companies: list[UUID] = Field(default_factory=list)
    merging_companies: list[UUID] = Field(default_factory=list)
    dissolved_companies: list[UUID] = Field(default_factory=list)
    acquiring_companies_data: list[AcquiringCompany] = Field(default_factory=list)
    offerings_data: list[Offering] = Field(default_factory=list)
    key_contacts: list[KeyContact] = Field(default_factory=list)
    shareholdings: list[Shareholding] = Field(default_factory=list)
    market_event_ids: list[UUID] = Field(default_factory=list)

    _phone = field_validator("official_phone_number")(_check_phone)
    _email = field_validator("official_email_address")(_check_email)

    @field_validator("official_website_url")
    @classmethod
    def _web(cls, v):
        return _check_url(v)

    @field_validator("linkedin_company_page")
    @classmethod
    def _li(cls, v):
        return _check_url(v, "linkedin.com")

    def to_api_payload(self) -> dict[str, Any]:
        """The exact JSON body sent to POST /api/prospects/ (blank optionals omitted)."""
        data = self.model_dump(mode="json", exclude_none=True)
        return {k: v for k, v in data.items() if v not in ([], "")}


class GovernmentEntityInput(_Strict):
    name: str = Field(min_length=1, max_length=255)
    country: str = Field(min_length=1, max_length=255)
    is_state_government: bool = False
    state_name: Optional[str] = Field(default=None, max_length=255)
    central_government: Optional[UUID] = None
    official_website: Optional[str] = None
    official_email: Optional[str] = None
    phone_number: Optional[str] = None
    complete_address: Optional[str] = None

    _state = field_validator("state_name", mode="before")(_blank_to_none)
    _website = field_validator("official_website")(_check_url)
    _email = field_validator("official_email")(_check_email)
    _phone = field_validator("phone_number")(_check_phone)
    _address = field_validator("complete_address", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _validate_hierarchy(self):
        if not self.is_state_government:
            self.state_name = None
            self.central_government = None
        return self

    def to_api_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True)
        return {k: v for k, v in data.items() if v not in ([], "")}


class MarketEventInput(_Strict):
    event_title: str = Field(min_length=1, max_length=255)
    host_country: str = Field(min_length=1, max_length=255)
    start_date: date = Field(default_factory=date.today)
    end_date: date = Field(default_factory=date.today)
    website_url: Optional[str] = None

    _url = field_validator("website_url")(_check_url)

    @model_validator(mode="after")
    def _check_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot be earlier than start_date")
        return self

    def to_api_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude_none=True)
        return {k: v for k, v in data.items() if v not in ([], "")}


def _format_errors(exc: ValidationError, root_name: str) -> list[str]:
    errors = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or root_name
        msg = e["msg"].removeprefix("Value error, ")
        if e["type"] == "missing":
            msg = "is required"
        elif e["type"] == "extra_forbidden":
            msg = "is not a supported field"
        elif e["type"] == "literal_error":
            msg = f"has an invalid value ({e['msg'].removeprefix('Input should be ')})"
        errors.append(f"{loc}: {msg}")
    return errors


def parse_prospect(raw: Any) -> tuple[Optional[ProspectInput], list[str]]:
    """Parse + normalise raw tool input. Returns (model, []) or (None, readable errors)."""
    if not isinstance(raw, dict):
        return None, ["prospect must be a JSON object."]
    try:
        return ProspectInput.model_validate(raw), []
    except ValidationError as exc:
        return None, _format_errors(exc, "prospect")


def parse_government_entity(raw: Any) -> tuple[Optional[GovernmentEntityInput], list[str]]:
    """Parse + normalise raw government entity input."""
    if not isinstance(raw, dict):
        return None, ["government_entity must be a JSON object."]
    try:
        return GovernmentEntityInput.model_validate(raw), []
    except ValidationError as exc:
        return None, _format_errors(exc, "government_entity")


def parse_market_event(raw: Any) -> tuple[Optional[MarketEventInput], list[str]]:
    """Parse + normalise raw market event input."""
    if not isinstance(raw, dict):
        return None, ["market_event must be a JSON object."]
    try:
        return MarketEventInput.model_validate(raw), []
    except ValidationError as exc:
        return None, _format_errors(exc, "market_event")


PROSPECT_BLUEPRINT: dict[str, Any] = {
    "module": "prospects",
    "version": "1.0",
    "description": (
        "Market Intel Default Prospect Blueprint. Research every applicable field prior to creation. "
        "All lookup fields (Parent Companies, Government Entities, Market Events) must reference real, "
        "verified database IDs. If missing, lookups must be searched and created before prospect creation."
    ),
    "sections": [
        {
            "section_id": "corporate_structure",
            "section_name": "Corporate Structure & Parent Auto-Linking",
            "title": "Corporate Structure & Parent Auto-Linking",
            "description": "Defines organization structure, operational status, and relationship hierarchy.",
            "fields": [
                {
                    "field_name": "company_structure",
                    "label": "Company Structure *",
                    "type": "string",
                    "required": True,
                    "choices": list(COMPANY_STRUCTURES),
                    "description": "Organizational structure. Branch and Subsidiary require parent_companies.",
                    "editable": True,
                },
                {
                    "field_name": "operational_status",
                    "label": "Operational Status *",
                    "type": "string",
                    "required": True,
                    "choices": list(OPERATIONAL_STATUSES),
                    "description": "Current status. 'Acquired' requires acquiring_companies_data. 'Merged' requires merging_companies.",
                    "editable": True,
                },
                {
                    "field_name": "ownership_sector",
                    "label": "Company Type / Ownership Sector *",
                    "type": "string",
                    "required": True,
                    "choices": list(OWNERSHIP_SECTORS),
                    "default": "Private Company",
                    "description": "Sector of ownership. 'Government Company' and 'Semi Government Company' require government entity association.",
                    "editable": True,
                },
                {
                    "field_name": "parent_companies",
                    "label": "Parent Companies * (Conditional)",
                    "type": "list[uuid]",
                    "required": False,
                    "conditional_requirement": "Mandatory for 'Branch' or 'Subsidiary'. Forbidden for 'Parent'.",
                    "lookup_module": "prospects",
                    "cardinality": "multiple",
                    "description": "List of existing Prospect UUIDs acting as parents. Resolved via search_existing_prospects.",
                    "editable": True,
                },
                {
                    "field_name": "merging_companies",
                    "label": "Merging Companies * (Conditional)",
                    "type": "list[uuid]",
                    "required": False,
                    "conditional_requirement": "Mandatory when operational_status is 'Merged'.",
                    "lookup_module": "prospects",
                    "cardinality": "multiple",
                    "description": "Existing Prospect UUIDs merging into this entity.",
                    "editable": True,
                },
                {
                    "field_name": "dissolved_companies",
                    "label": "Dissolved Companies (Conditional)",
                    "type": "list[uuid]",
                    "required": False,
                    "conditional_requirement": "Optional when operational_status is 'Merged'. Marked as Inactive by backend.",
                    "lookup_module": "prospects",
                    "cardinality": "multiple",
                    "description": "Companies dissolved due to merger. Must be a subset of merging_companies.",
                    "editable": True,
                },
                {
                    "field_name": "acquiring_companies_data",
                    "label": "Acquiring Companies * (Conditional)",
                    "type": "list[object]",
                    "required": False,
                    "conditional_requirement": "Mandatory when operational_status is 'Acquired'.",
                    "lookup_module": "prospects",
                    "cardinality": "multiple",
                    "nested_fields": [
                        {"field_name": "acquiring_company", "type": "uuid", "required": True, "description": "Existing Prospect UUID of acquirer."},
                        {"field_name": "acquired_shares", "type": "float", "required": False, "min": 0, "max": 100, "description": "Percentage acquired."},
                    ],
                    "editable": True,
                },
            ],
        },
        {
            "section_id": "general_information",
            "section_name": "General Information",
            "title": "General Information",
            "description": "Core identifying details and communication coordinates.",
            "fields": [
                {
                    "field_name": "company_name",
                    "label": "Company Name *",
                    "type": "string",
                    "required": True,
                    "min_length": 1,
                    "max_length": 255,
                    "description": "Official legal or commercial name. Exact duplicates are rejected.",
                    "editable": True,
                },
                {
                    "field_name": "country_head_office",
                    "label": "Country (Head Office) *",
                    "type": "string",
                    "required": True,
                    "min_length": 1,
                    "max_length": 100,
                    "description": "Primary country where the headquarters is located.",
                    "editable": True,
                },
                {
                    "field_name": "complete_address",
                    "label": "Complete Address *",
                    "type": "string",
                    "required": True,
                    "min_length": 1,
                    "description": "Full street address including city, state/province, postal code.",
                    "editable": True,
                },
                {
                    "field_name": "primary_industries",
                    "label": "Primary Industries *",
                    "type": "string",
                    "required": True,
                    "min_length": 1,
                    "max_length": 255,
                    "description": "Primary line of business or industry sector (e.g. Technology, Oil & Gas).",
                    "editable": True,
                },
                {
                    "field_name": "official_phone_number",
                    "label": "Official Phone Number",
                    "type": "string",
                    "required": False,
                    "description": "Corporate phone number with international country code (E.164 format preferred).",
                    "editable": True,
                },
                {
                    "field_name": "official_email_address",
                    "label": "Official Email Address",
                    "type": "email",
                    "required": False,
                    "validation_rules": ["Must be verified via instant-verify API and return VALID status. INVALID/UNKNOWN/TIMEOUT blocks creation."],
                    "description": "Corporate contact email.",
                    "editable": True,
                },
                {
                    "field_name": "official_website_url",
                    "label": "Official Website URL",
                    "type": "url",
                    "required": False,
                    "description": "Official company website (must begin with http:// or https://).",
                    "editable": True,
                },
                {
                    "field_name": "linkedin_company_page",
                    "label": "LinkedIn Company Page",
                    "type": "url",
                    "required": False,
                    "description": "Company profile URL on linkedin.com.",
                    "editable": True,
                },
            ],
        },
        {
            "section_id": "shareholding_ownership",
            "section_name": "Shareholding / Ownership",
            "title": "Shareholding / Ownership",
            "description": "Equity distribution and stakeholder structure.",
            "fields": [
                {
                    "field_name": "shareholdings",
                    "label": "Shareholders",
                    "type": "list[object]",
                    "required": False,
                    "description": "List of shareholders. Sum of share_percentage across all items must not exceed 100%.",
                    "nested_fields": [
                        {
                            "field_name": "holder_type",
                            "label": "Holder Type *",
                            "type": "string",
                            "required": True,
                            "choices": ["Company", "Individual", "Government", "Other"],
                        },
                        {
                            "field_name": "share_percentage",
                            "label": "Shares (%) *",
                            "type": "float",
                            "required": True,
                            "min": 0.01,
                            "max": 100.0,
                        },
                        {
                            "field_name": "company",
                            "label": "Company (when holder_type='Company')",
                            "type": "uuid",
                            "required": False,
                            "lookup_module": "prospects",
                            "description": "Existing Prospect UUID. Alternatively set is_self=true.",
                        },
                        {
                            "field_name": "is_self",
                            "label": "Self / Treasury Shareholding",
                            "type": "boolean",
                            "default": False,
                            "description": "Set to true if company owns shares in itself.",
                        },
                        {
                            "field_name": "contact",
                            "label": "Contact (when holder_type='Individual')",
                            "type": "uuid",
                            "required": False,
                            "description": "UUID of an existing contact, or use contact_index.",
                        },
                        {
                            "field_name": "contact_index",
                            "label": "Contact Index (when holder_type='Individual')",
                            "type": "integer",
                            "required": False,
                            "description": "0-based index referencing an entry in key_contacts list.",
                        },
                        {
                            "field_name": "government_entity",
                            "label": "Government Entity Name * (when holder_type='Government')",
                            "type": "uuid",
                            "required": False,
                            "lookup_module": "government_entities",
                            "description": "UUID of verified GovernmentEntity record. Mandatory when holder_type='Government'.",
                        },
                        {
                            "field_name": "other_name",
                            "label": "Stakeholder Name * (when holder_type='Other')",
                            "type": "string",
                            "required": False,
                            "description": "Name of individual, trust, or other entity.",
                        },
                    ],
                    "editable": True,
                },
            ],
        },
        {
            "section_id": "market_offerings",
            "section_name": "Market Offerings",
            "title": "Market Offerings",
            "description": "Products, services, and solutions offered.",
            "fields": [
                {
                    "field_name": "primary_offering_type",
                    "label": "Primary Offering Type *",
                    "type": "string",
                    "required": True,
                    "choices": list(PRIMARY_OFFERING_TYPES),
                    "description": "Determines allowed offering categories in offerings_data.",
                    "editable": True,
                },
                {
                    "field_name": "offerings_data",
                    "label": "Offerings List",
                    "type": "list[object]",
                    "required": False,
                    "description": "Specific offerings. Each offering_type must match primary_offering_type (or any type if 'Multiple').",
                    "nested_fields": [
                        {
                            "field_name": "offering_type",
                            "label": "Offering Type *",
                            "type": "string",
                            "required": True,
                            "choices": list(OFFERING_TYPES),
                        },
                        {
                            "field_name": "name",
                            "label": "Offering Name *",
                            "type": "string",
                            "required": True,
                            "min_length": 1,
                            "max_length": 255,
                        },
                    ],
                    "editable": True,
                },
            ],
        },
        {
            "section_id": "market_events",
            "section_name": "Market Events",
            "title": "Market Events",
            "description": "Trade shows, conferences, exhibitions, or market summits associated with the prospect.",
            "fields": [
                {
                    "field_name": "market_event_ids",
                    "label": "Associate Market Events",
                    "type": "list[uuid]",
                    "required": False,
                    "cardinality": "multiple",
                    "lookup_module": "market_events",
                    "description": (
                        "List of real, verified Market Event UUIDs. Claude must search existing events via "
                        "search_market_events and create any missing event via validate_market_event / "
                        "create_confirmed_market_event before prospect creation."
                    ),
                    "editable": True,
                },
            ],
        },
        {
            "section_id": "key_contacts",
            "section_name": "Key Contacts / Personnel",
            "title": "Key Contacts / Personnel",
            "description": "Decision makers, executives, and company officers.",
            "fields": [
                {
                    "field_name": "key_contacts",
                    "label": "Key Contacts",
                    "type": "list[object]",
                    "required": False,
                    "description": "Company personnel. Every supplied official_email must pass instant verification with VALID status.",
                    "nested_fields": [
                        {
                            "field_name": "prefix",
                            "label": "Prefix / Honorific",
                            "type": "string",
                            "required": False,
                            "choices": [
                                "Mr.", "Ms.", "Mrs.", "Dr.", "Prof.", "Sir", "Madam",
                                "His Highness", "Her Highness", "His Excellency", "Her Excellency", "Honorable"
                            ],
                        },
                        {
                            "field_name": "contact_name",
                            "label": "Contact Name *",
                            "type": "string",
                            "required": True,
                            "min_length": 1,
                            "max_length": 255,
                        },
                        {
                            "field_name": "designation",
                            "label": "Designation / Role",
                            "type": "string",
                            "required": False,
                            "max_length": 255,
                        },
                        {
                            "field_name": "official_email",
                            "label": "Official Email",
                            "type": "email",
                            "required": False,
                            "validation_rules": ["Must be verified via instant-verify API and return VALID status."],
                        },
                        {
                            "field_name": "phone_number",
                            "label": "Phone Number",
                            "type": "string",
                            "required": False,
                        },
                        {
                            "field_name": "linkedin_profile",
                            "label": "LinkedIn Profile",
                            "type": "url",
                            "required": False,
                            "validation_rules": ["Must be a valid linkedin.com URL."],
                        },
                    ],
                    "editable": True,
                },
            ],
        },
        {
            "section_id": "government_entity_association",
            "section_name": "Government Entity Association",
            "title": "Government Entity Association",
            "description": "Official government entity links for public sector, state-owned, or government-affiliated prospects.",
            "fields": [
                {
                    "field_name": "government_entity",
                    "label": "Government Entity Name *",
                    "type": "uuid",
                    "required": False,
                    "conditional_requirement": (
                        "Treated as required when ownership_sector is 'Government Company' or 'Semi Government Company', "
                        "or when shareholding holder_type is 'Government'. Formatted in API payload under "
                        "shareholdings with holder_type='Government' and government_entity=<UUID>."
                    ),
                    "lookup_module": "government_entities",
                    "cardinality": "single",
                    "description": (
                        "UUID of real, verified Government Entity record. Search with search_government_entities. "
                        "If missing, validate and create via validate_government_entity and create_confirmed_government_entity."
                    ),
                    "editable": True,
                },
            ],
        },
    ],
    "lookup_modules": {
        "government_entities": {
            "search_tool": "search_government_entities",
            "detail_tool": "get_record_details",
            "validate_tool": "validate_government_entity",
            "create_tool": "create_confirmed_government_entity",
            "required_fields": ["name", "country"],
            "optional_fields": ["is_state_government", "state_name", "central_government", "official_website", "official_email", "phone_number", "complete_address"],
        },
        "government_entity": {
            "search_tool": "search_government_entities",
            "detail_tool": "get_record_details",
            "validate_tool": "validate_government_entity",
            "create_tool": "create_confirmed_government_entity",
            "required_fields": ["name", "country"],
            "optional_fields": ["is_state_government", "state_name", "central_government", "official_website", "official_email", "phone_number", "complete_address"],
        },
        "market_events": {
            "search_tool": "search_market_events",
            "detail_tool": "get_record_details",
            "validate_tool": "validate_market_event",
            "create_tool": "create_confirmed_market_event",
            "required_fields": ["event_title", "host_country"],
            "optional_fields": ["start_date", "end_date", "website_url"],
        },
        "market_event": {
            "search_tool": "search_market_events",
            "detail_tool": "get_record_details",
            "validate_tool": "validate_market_event",
            "create_tool": "create_confirmed_market_event",
            "required_fields": ["event_title", "host_country"],
            "optional_fields": ["start_date", "end_date", "website_url"],
        },
        "prospects": {
            "search_tool": "search_existing_prospects",
            "detail_tool": "get_record_details",
            "validate_tool": "validate_prospect_data",
            "create_tool": "create_confirmed_prospect",
        },
        "prospect": {
            "search_tool": "search_existing_prospects",
            "detail_tool": "get_record_details",
            "validate_tool": "validate_prospect_data",
            "create_tool": "create_confirmed_prospect",
        },
    },
}
