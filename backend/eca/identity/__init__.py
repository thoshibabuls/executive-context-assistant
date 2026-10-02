"""Users, sign-in (Google OIDC), sessions, CSRF, preferences and checkpoints.

Owns (single writer, BACKEND_DESIGN.md §5.1): users, auth_sessions, user_preferences, user_checkpoints
(and the short-lived ``oauth_states``). Other modules import only from this package root.
"""

from eca.identity import events as _events  # registers event types
from eca.identity.checkpoints import SURFACE, check_surface, get_checkpoints, mark_seen
from eca.identity.events import USER_CREATED, USER_DELETION_REQUESTED, UserCreated, UserDeletionRequested
from eca.identity.oauth_state import (
    ConsumedState,
    NewState,
    consume_state,
    create_state,
    discard_user_states,
    pkce_challenge,
)
from eca.identity.oidc import (
    SIGNIN_SCOPES,
    GoogleIdentity,
    JwksCache,
    TokenResponse,
    authorization_url,
    exchange_code,
    verify_id_token,
)
from eca.identity.purge import delete_user_row, purge_sessions
from eca.identity.service import (
    UserProfile,
    UserSettings,
    create_user,
    find_signin_user,
    get_profile,
    get_user_settings,
    link_google_identity,
    list_active_user_ids,
    request_account_deletion,
)
from eca.identity.sessions import (
    NewSession,
    SessionInfo,
    check_csrf,
    create_session,
    load_session,
    revoke_all_sessions,
    revoke_session,
    session_user_id,
)

del _events

__all__ = [
    "SIGNIN_SCOPES",
    "SURFACE",
    "USER_CREATED",
    "USER_DELETION_REQUESTED",
    "ConsumedState",
    "GoogleIdentity",
    "JwksCache",
    "NewSession",
    "NewState",
    "SessionInfo",
    "TokenResponse",
    "UserCreated",
    "UserDeletionRequested",
    "UserProfile",
    "UserSettings",
    "authorization_url",
    "check_csrf",
    "check_surface",
    "consume_state",
    "create_session",
    "create_state",
    "create_user",
    "delete_user_row",
    "discard_user_states",
    "exchange_code",
    "find_signin_user",
    "get_checkpoints",
    "get_profile",
    "get_user_settings",
    "link_google_identity",
    "list_active_user_ids",
    "load_session",
    "mark_seen",
    "pkce_challenge",
    "purge_sessions",
    "request_account_deletion",
    "revoke_all_sessions",
    "revoke_session",
    "session_user_id",
    "verify_id_token",
]
