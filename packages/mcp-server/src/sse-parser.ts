import { createParser, type EventSourceMessage } from "eventsource-parser";
import type { RawSearchUser, SearchResult, SSEInitialPayload } from "./types.js";

function normalizeMatchReason(reason: unknown): string {
  if (typeof reason === "string") return reason;
  if (reason !== null && typeof reason === "object" && "reason" in reason) {
    return String((reason as { reason: unknown }).reason);
  }
  return "";
}

function toSearchResult(user: RawSearchUser): SearchResult {
  return {
    displayName: user.displayName ?? "",
    username: user.username ?? null,
    userId: user.userId,
    city: user.city ?? null,
    age: user.age ?? null,
    matchReason: normalizeMatchReason(user.matchReason),
    sourceCount: user.sourceCount ?? 0,
  };
}

export interface SSEParseResult {
  results: SearchResult[];
  timedOut: boolean;
}

export async function consumeSSEStream(
  response: Response,
  signal: AbortSignal,
): Promise<SSEParseResult> {
  const usersMap = new Map<string, SearchResult>();
  let timedOut = false;

  const body = response.body;
  if (!body) {
    return { results: [], timedOut: false };
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();

  const parser = createParser({
    onEvent(event: EventSourceMessage) {
      try {
        // eventsource-parser v3 exposes the SSE event-type field as either
        // `event` or `name` depending on minor version — access both defensively
        const eventName: string =
          (event as unknown as Record<string, unknown>)["event"] as string ??
          (event as unknown as Record<string, unknown>)["name"] as string ??
          "";
        const eventData: string = event.data;

        if (eventName === "initial") {
          const parsed: SSEInitialPayload = JSON.parse(eventData);
          const users = parsed?.payload?.users ?? [];
          for (const user of users) {
            if (user.userId) {
              usersMap.set(user.userId, toSearchResult(user));
            }
          }
        } else if (eventName === "update") {
          const parsed: RawSearchUser = JSON.parse(eventData);
          if (parsed.userId) {
            const existing = usersMap.get(parsed.userId);
            if (existing) {
              usersMap.set(parsed.userId, {
                ...existing,
                displayName: parsed.displayName ?? existing.displayName,
                username: parsed.username ?? existing.username,
                city: parsed.city ?? existing.city,
                age: parsed.age ?? existing.age,
                matchReason: parsed.matchReason
                  ? normalizeMatchReason(parsed.matchReason)
                  : existing.matchReason,
                sourceCount: parsed.sourceCount ?? existing.sourceCount,
              });
            } else {
              usersMap.set(parsed.userId, toSearchResult(parsed));
            }
          }
        }
      } catch {
        /* malformed event data — skip */
      }
    },
  });

  try {
    while (true) {
      if (signal.aborted) {
        timedOut = true;
        break;
      }
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
  } catch (err: unknown) {
    if (err instanceof Error && err.name === "AbortError") {
      timedOut = true;
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* reader may already be released */
    }
  }

  return {
    results: Array.from(usersMap.values()),
    timedOut,
  };
}
