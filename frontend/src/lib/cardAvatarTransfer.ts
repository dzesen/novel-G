import { ApiError } from "@/lib/api";

export class CardAvatarSourceUnavailable extends Error {}

export function isPermanentCardAvatarTransferFailure(
  cause: unknown,
): boolean {
  if (cause instanceof CardAvatarSourceUnavailable) return true;
  return (
    cause instanceof ApiError &&
    [400, 404, 409, 413, 415, 422].includes(cause.status)
  );
}
