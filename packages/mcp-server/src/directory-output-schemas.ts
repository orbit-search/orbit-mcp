/** Directory response families mirror 2020-api v2 handlers; preserve partial/new fields. */
import { z } from "zod";

// Fields may be omitted in partial/error responses. Present known fields remain
// typed; passthrough preserves profile evidence and future backend extensions.
const object = (shape: Record<string, z.ZodType>) => z.object(shape).partial().passthrough();
const text = z.string();
const nullableText = text.nullable();
const count = z.number();
const strings = z.array(text);
const record = z.record(z.string(), z.unknown());
const error = z.union([text, object({ code: text, message: text, retryable: z.boolean() })]).nullable();
const pagination = object({ limit: count, offset: count, total: count, has_more: z.boolean() });
const directory = object({ id: text, organization_id: text, name: text, description: nullableText, slug: nullableText,
  visibility: text, is_archived: z.boolean(), metadata: record.nullable(), can_edit: z.boolean(), is_member: z.boolean() });
const person = object({ person_id: text, profile_id: nullableText, orbit_id: nullableText, display_name: nullableText,
  source_ids: strings, source_count: count, generation_level: count.nullable(), match_status: nullableText });
const source = object({ id: text, directory_id: text, type: text, status: text, error: nullableText,
  row_counts: object({ total_rows: count, accepted_rows: count, invalid_rows: count, duplicate_rows: count, truncated_rows: count }),
  validation_errors: z.array(object({ row: count, reasons: strings })), warnings: strings, import_metadata: record });
const rowCounts = object({ totalRows: count, acceptedRows: count, invalidRows: count, duplicateRows: count, truncatedRows: count });
const readiness = object({ status: text, canSearch: z.boolean(), isArchived: z.boolean(), latestError: nullableText,
  sourceCounts: object({ total: count, queued: count, processing: count, complete: count, failed: count, canceled: count }),
  rowCounts, validationErrorCount: count });
const grant = object({ id: text, directory_id: text, organization_id: text, principal_id: text, principal_type: text, permission: text });
const refresh = object({ request_id: text, directory_id: text, status: text, error: nullableText,
  selection: object({ all: z.boolean(), selected_count: count.nullable() }), profile_count: count, unresolved_count: count,
  runs: object({ total: count, dispatched: count, completed: count, failed: count, skipped: count, updated: count }),
  credits_charged: count, skipped_reasons: z.record(z.string(), count), created_at: text, finished_at: nullableText });
const watcher = object({ id: text, orbit_id: text, enabled: z.boolean(), interval_seconds: count, phases: strings.nullable(), directory_person_id: nullableText });
const removed = object({ directory_id: text, person_id: text, removed: z.boolean(), removed_rows: count, profile_ids: strings });

export const directoryPayloadSchemas: Record<string, z.ZodType> = {
  list_directory_organizations: object({ organizations: z.array(object({ id: text, name: text })) }),
  list_directories: object({ directories: z.array(directory) }),
  get_directory: object({ directory, readiness }),
  create_directory: object({ directory }), update_directory: object({ directory }), archive_directory: object({ directory }),
  check_directory_slug: object({ slug: nullableText, available: z.boolean(), reason: text }),
  search_directory: z.union([object({ users: z.array(record), searchSummary: text, debugResponse: text }), z.array(record)]),
  list_directory_people: object({ directory_id: text, people: z.array(person), pagination, filters: object({ name: text, source_id: text }) }),
  get_directory_person: object({ directory_person: person }),
  count_directory_people: object({ directory_id: text, count }),
  list_pending_directory_people: object({ directory_id: text, in_progress: count, people: z.array(object({ row_id: text, source_id: text, status: text, reason: nullableText, error: nullableText, display_name: nullableText })) }),
  check_directory_membership: object({ directory_id: text, emails: strings, linkedin_urls: strings, profile_ids: strings }),
  add_directory_person: object({ person: object({ directory_id: text, organization_id: text, source_id: text, profile_id: text, added: z.boolean(), materialization: record.nullable() }) }),
  remove_directory_person: object({ person: removed }), leave_directory: object({ person: removed }),
  remove_directory_people: object({ directory_id: text, removed_count: count, person_ids: strings, profile_ids: strings, removal_source_id: nullableText, removed_at: text, restorable_until: text }),
  restore_directory_people: object({ directory_id: text, restored_count: count, person_ids: strings, profile_ids: strings, removal_source_id: nullableText }),
  get_directory_readiness: object({ readiness }),
  list_directory_sources: object({ sources: z.array(source) }), get_directory_source: object({ source }), update_directory_source_status: object({ source }),
  get_directory_source_status: object({ source_status: text, source, backfill_error: nullableText,
    backfills: z.array(object({ type: text, id: text, phase: text, fetched: count, completed: count, failed: count, pending: count, error: nullableText })) }),
  upload_directory_csv: object({ source_id: text, directory_id: text, source_status: text, row_counts: rowCounts, processing_queued: z.boolean(),
    validation_failures: z.array(object({ row: count, reasons: strings })), warnings: strings, idempotent: z.boolean() }),
  infer_directory_csv_mapping: object({ mapping: z.record(z.string(), z.union([text, strings])), mapping_status: text }),
  populate_directory_from_entities: object({ directory_id: text, queued: z.boolean(), source: source.nullable(), row_counts: rowCounts, truncated: z.boolean(), materialization: record.nullable(), backfill_error: nullableText }),
  remove_directory_source_people: object({ directory_id: text, source_id: text, removed_count: count, profile_ids: strings, source }),
  resync_directory_people: object({ directory_id: text, refreshed_count: count, person_ids: strings, profile_ids: strings, materialization: record.nullable() }),
  refresh_directory_profiles: object({ request_id: text, queued: z.boolean(), refresh }), list_directory_refreshes: object({ refreshes: z.array(refresh) }),
  list_directory_grants: object({ grants: z.array(grant) }), grant_directory_access: object({ grants: z.array(grant) }),
  revoke_directory_access: object({ grant, revoked: z.boolean() }),
  list_directory_watchers: object({ watchers: z.array(watcher) }), create_directory_watchers: object({ watchers: z.array(watcher) }),
  update_directory_watcher: watcher, delete_directory_watcher: object({ deleted: z.boolean() }),
  list_directory_activity: object({ events: z.array(object({ id: text, orbit_id: text, person_id: nullableText, title: text, date: nullableText, last_updated_at: nullableText,
    changes: z.array(object({ source_link: nullableText, source_name: nullableText, change_kind: nullableText, summary: nullableText })) })),
    total: count, limit: count, offset: count, directory: record, stats: object({ watched_profiles: count, active_watchers: count, events: count }) })
};

export function directoryOutputSchema(name: string) {
  const payload = directoryPayloadSchemas[name];
  if (!payload) throw new Error(`Missing directory response schema: ${name}`);
  return { data: object({ status: text, payload, error, message: text, code: text }) };
}

export function directoryResultHasFailure(envelope: Record<string, unknown>): boolean {
  const failed = (value: unknown) => value === "failed" || value === "error" || value === "completed_with_errors";
  // A successful list may contain historical failed runs; preserve them without
  // pretending the list request failed. Individual operation failures are errors.
  const payload = envelope.payload;
  return failed(envelope.status) || Boolean(payload && typeof payload === "object" && !Array.isArray(payload)
    && (failed((payload as Record<string, unknown>).status) || failed((payload as Record<string, unknown>).source_status)));
}
