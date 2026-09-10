# Directory workflows

Use directory tools only for the user's requested organization and collection.
Start with `list_directory_organizations` and `list_directories` to obtain IDs;
use `get_directory` before changes. If more than one organization or directory
fits, ask which one. Never assume the first organization is the intended target.

Directory tools require a connected user and the relevant `directories.read`
or `directories.write` permission. Follow an authentication challenge to
reconnect; never request credentials in chat. Write permission does not imply
read permission. The backend still enforces the user's organization role and
directory grants. Do not bypass a denial using another organization or global
search. A directory login can expire before the search connection expires.

## Organize and research

- Create a private directory with `create_directory` unless the user explicitly
  chooses public visibility. Confirm the target and visibility. Use
  `check_directory_slug` when a public slug is needed.
- Use `update_directory` for requested changes. Preserve unrelated metadata:
  it can hold directory settings, so read before replacing it.
- Use `list_directory_people` to browse members; it supports name filtering,
  not natural-language search. Follow returned pagination.
- Use `search_directory` for natural-language research confined to that
  directory. Do not silently run `search_people` globally instead.
- Read a member with `get_directory_person`; use its returned Orbit profile ID
  with `get_profile` before substantive person research. Directory-person IDs
  and Orbit profile IDs are different identifiers.
- Use `add_directory_person` with a resolved profile ID. Check membership with
  `check_directory_membership` when importing supplied identities.
- Confirm specific member IDs before `remove_directory_person` or
  `remove_directory_people`. Save removal source IDs and restoration deadlines;
  use `restore_directory_people` only within the backend's supported window.
- `archive_directory` archives; it does not permanently erase the directory.
  Confirm the exact target. `leave_directory` removes only the user's membership.

## Import and populate

Use `infer_directory_csv_mapping` for supplied headers if mapping is unclear.
Review the mapping and confirm the destination before `upload_directory_csv`.
Provide the user's CSV text unchanged; do not invent rows or fill unknown cells
from memory. Keep the same UUID `idempotency_key` and content on a retry.
The tool accepts at most 512 KiB of UTF-8 CSV text. Use the Orbit dashboard for
larger files or image uploads; do not invent upload URLs or silently truncate.

For known company or school IDs, use `populate_directory_from_entities` with
explicit AND/OR filters after confirming scope and research costs. Do not guess
entity IDs from company names. Read the source returned by the operation.

Track imports using `list_directory_sources`, `get_directory_source`,
`get_directory_source_status`, `list_pending_directory_people`, and
`get_directory_readiness`. Source reads can recover pending processing; they
are not strictly read-only. Report accepted, queued, importing, failed, and
completed accurately. Preserve partial successes and surface validation errors.
Do not call `update_directory_source_status` to make a failed import appear
successful. Use it only for an explicitly requested administrative correction
supported by actual processing evidence. Removing source members requires
confirmation before `remove_directory_source_people`.

## Share and follow changes

Read `list_directory_grants` before sharing. Confirm recipient type, exact ID,
and permissions (`search`, `upload`, `manage`) before `grant_directory_access`.
Use the returned grant ID for `revoke_directory_access`. Do not widen access
just because a search or import was denied.

For one-time research refreshes, confirm either specific directory-person IDs
or everyone, including organization credit usage. Call `refresh_directory_profiles`
with one selection and a stable UUID `request_id`. Track `list_directory_refreshes`;
acceptance is not completion. `resync_directory_people` instead rematerializes
existing data and is not equivalent to researching profiles again.

For ongoing updates, read `list_directory_watchers`, then confirm member Orbit
profile IDs, interval, phases, and recurring credits before
`create_directory_watchers` or `update_directory_watcher`. Stop selected
watchers with `delete_directory_watcher` after confirmation. Read updates with
`list_directory_activity`. Do not schedule updates during ordinary research.

Never blindly retry a timed-out mutation: it might already have succeeded.
Inspect the relevant list/status first. Honor rate limits and stop on denied
access, terminal failure, or an unresolved target.
