import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.cookie
    .split("; ")
    .find((c) => c.startsWith("eca_csrf="))
    ?.slice("eca_csrf=".length);
}

// Double-submit CSRF (BACKEND_DESIGN.md §16.1): echo the readable cookie on unsafe methods.
const csrf: Middleware = {
  onRequest({ request }) {
    const token = csrfToken();
    if (token && UNSAFE.has(request.method)) request.headers.set("X-CSRF-Token", token);
    return request;
  },
};

export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });
api.use(csrf);

export class ApiError extends Error {
  constructor(
    public status: number,
    public problem: { code?: string; title?: string; detail?: string } | undefined,
  ) {
    super(problem?.detail ?? problem?.title ?? `HTTP ${status}`);
  }
}

export function unwrap<T>(result: { data?: unknown; error?: unknown; response: Response }): T {
  if (!result.response.ok) {
    throw new ApiError(result.response.status, result.error as ApiError["problem"]);
  }
  return result.data as T;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
