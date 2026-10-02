"""Persons, identifiers, organizations and entity mentions.

Owns (single writer, BACKEND_DESIGN.md §5.1): persons, person_identifiers, organizations, entity_mentions.
Other modules import only from this package root.
"""

from eca.people.identity_rules import is_public_domain, normalize_alias, normalize_email
from eca.people.names import NameMatch, match_names
from eca.people.purge import purge_mentions, purge_mentions_for_sources, purge_user
from eca.people.queries import (
    PERSON_EDITABLE,
    OrganizationView,
    PersonDetail,
    PersonSummary,
    add_alias,
    edit_organization,
    edit_person,
    importance_of,
    list_organizations,
    list_people_page,
    person_detail,
)
from eca.people.service import (
    AliasEntry,
    MentionCount,
    MentionIn,
    PersonRef,
    alias_catalog,
    aliases_of,
    create_self_person,
    find_by_email,
    get_persons,
    get_self_person,
    mentions_of,
    merge_persons,
    merged_ids,
    record_interaction,
    record_mentions,
    replace_alias_mentions,
    resolve_address,
    resolve_name,
)

__all__ = [
    "PERSON_EDITABLE",
    "AliasEntry",
    "MentionCount",
    "MentionIn",
    "NameMatch",
    "OrganizationView",
    "PersonDetail",
    "PersonRef",
    "PersonSummary",
    "add_alias",
    "alias_catalog",
    "aliases_of",
    "create_self_person",
    "edit_organization",
    "edit_person",
    "find_by_email",
    "get_persons",
    "get_self_person",
    "importance_of",
    "is_public_domain",
    "list_organizations",
    "list_people_page",
    "match_names",
    "mentions_of",
    "merge_persons",
    "merged_ids",
    "normalize_alias",
    "normalize_email",
    "person_detail",
    "purge_mentions",
    "purge_mentions_for_sources",
    "purge_user",
    "record_interaction",
    "record_mentions",
    "replace_alias_mentions",
    "resolve_address",
    "resolve_name",
]
