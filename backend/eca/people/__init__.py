"""Persons, identifiers, organizations and entity mentions.

Owns (single writer, BACKEND_DESIGN.md §5.1): persons, person_identifiers, organizations, entity_mentions.
Other modules import only from this package root.
"""

from eca.people.identity_rules import is_public_domain, normalize_alias, normalize_email
from eca.people.purge import purge_mentions, purge_mentions_for_sources, purge_user
from eca.people.service import (
    MentionIn,
    PersonRef,
    aliases_of,
    create_self_person,
    find_by_email,
    get_persons,
    get_self_person,
    merge_persons,
    merged_ids,
    record_interaction,
    record_mentions,
    resolve_address,
    resolve_name,
)

__all__ = [
    "MentionIn",
    "PersonRef",
    "aliases_of",
    "create_self_person",
    "find_by_email",
    "get_persons",
    "get_self_person",
    "is_public_domain",
    "merge_persons",
    "merged_ids",
    "normalize_alias",
    "normalize_email",
    "purge_mentions",
    "purge_mentions_for_sources",
    "purge_user",
    "record_interaction",
    "record_mentions",
    "resolve_address",
    "resolve_name",
]
