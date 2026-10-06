"""Thin Exa client used for external incident research.

Kept dependency-free (plain ``httpx``) so the payload we send and the fields we
keep are both explicit — that matters for trace readability in Braintrust.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

EXA_SEARCH_URL = "https://api.exa.ai/search"

# Domains worth biasing toward when a responder is chasing an error signature or
# a third-party outage: provider status pages, upstream issue trackers, and the
# operational writeups where failure modes actually get documented.
OPERATIONS_DOMAINS = [
    "status.aws.amazon.com",
    "health.aws.amazon.com",
    "www.cloudflarestatus.com",
    "status.cloud.google.com",
    "status.stripe.com",
    "www.githubstatus.com",
    "status.datadoghq.com",
    "github.com",
    "kubernetes.io",
    "docs.aws.amazon.com",
    "www.postgresql.org",
    "redis.io",
    "stackoverflow.com",
    "sre.google",
    "aws.amazon.com",
    "engineering.fb.com",
]


@dataclass(frozen=True)
class SearchHit:
    """One normalized Exa result."""

    title: str
    url: str
    published_date: str | None
    author: str | None
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "published_date": self.published_date,
            "author": self.author,
            "excerpt": self.text,
        }


class ExaClient:
    """Minimal synchronous Exa search client."""

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def search(
        self,
        query: str,
        num_results: int = 5,
        max_characters: int = 1200,
        include_domains: list[str] | None = None,
        start_published_date: str | None = None,
    ) -> list[SearchHit]:
        """Run one Exa search and return normalized hits."""
        payload: dict[str, Any] = {
            "query": query,
            "numResults": num_results,
            "type": "auto",
            "contents": {"text": {"maxCharacters": max_characters}},
        }
        if include_domains:
            payload["includeDomains"] = include_domains
        if start_published_date:
            payload["startPublishedDate"] = start_published_date

        response = httpx.post(
            EXA_SEARCH_URL,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()
        body = response.json()

        hits: list[SearchHit] = []
        for item in body.get("results", []):
            hits.append(
                SearchHit(
                    title=item.get("title") or "(untitled)",
                    url=item.get("url", ""),
                    published_date=item.get("publishedDate"),
                    author=item.get("author"),
                    text=(item.get("text") or "").strip(),
                )
            )
        return hits
