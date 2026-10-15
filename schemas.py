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
    market_event_ids: list[str | int] = Field(default_factory=list)  # ASSUMED id type

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


def parse_prospect(raw: Any) -> tuple[Optional[ProspectInput], list[str]]:
    """Parse + normalise raw tool input. Returns (model, []) or (None, readable errors)."""
    if not isinstance(raw, dict):
        return None, ["prospect must be a JSON object."]
    try:
        return ProspectInput.model_validate(raw), []
    except ValidationError as exc:
        errors = []
        for e in exc.errors():
            loc = ".".join(str(p) for p in e["loc"]) or "prospect"
            msg = e["msg"].removeprefix("Value error, ")
            if e["type"] == "missing":
                msg = "is required"
            elif e["type"] == "extra_forbidden":
                msg = "is not a supported field"
            elif e["type"] == "literal_error":
                msg = f"has an invalid value ({e['msg'].removeprefix('Input should be ')})"
            errors.append(f"{loc}: {msg}")
        return None, errors
