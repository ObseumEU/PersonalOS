/*
 * The push service's subscribe with one recovery (T-1431). Chrome answers "AbortError: Registration failed - push
 * service error" when it cannot register with its push service: a stale subscription left in a broken state (after a
 * deploy or a key change), a passing network error, or a browser without a push service at all (Brave with "Google
 * services for push messaging" off, Chromium without Google keys). The first two pass on a second try after the old
 * subscription is dropped; the last one needs the owner to change a setting, so it gets a message saying which.
 * No imports: `npm test` runs it with fakes (tests/pushsubscribe.test.ts).
 */

export type PushManagerLike = {
  subscribe(options: PushSubscriptionOptionsInit): Promise<PushSubscription>;
  getSubscription(): Promise<PushSubscription | null>;
};

/** The browser could not register with its push service (not a refused permission, not our server). */
export const isPushServiceError = (e: unknown) => (e as { name?: string } | null)?.name === "AbortError";

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** subscribe(); on a push service error, drop whatever subscription the browser still holds, wait and try once more. */
export async function subscribeWithRetry(
  pm: PushManagerLike,
  options: PushSubscriptionOptionsInit,
  pause: (ms: number) => Promise<void> = sleep,
): Promise<{ sub: PushSubscription; retried: boolean }> {
  try {
    return { sub: await pm.subscribe(options), retried: false };
  } catch (e) {
    if (!isPushServiceError(e)) throw e;
    const stale = await pm.getSubscription().catch(() => null);
    await stale?.unsubscribe().catch(() => undefined);
    await pause(1000);
    return { sub: await pm.subscribe(options), retried: true };
  }
}

/** What the owner can do when the push service refused twice. */
export function pushServiceMessage(brave: boolean): string {
  if (brave)
    return (
      "Brave nemá zapnutou službu pro oznámení. Otevři brave://settings/privacy, zapni „Používat služby Google pro zasílání push zpráv“, " +
      "restartuj prohlížeč a stiskni Zapnout notifikace znovu."
    );
  return (
    "Služba oznámení prohlížeče přihlášení odmítla (push service error). Zkus to za chvíli znovu; když to nepomůže, " +
    "zkontroluj, že síť neblokuje službu oznámení (Chrome: fcm.googleapis.com), nebo použij Chrome, Edge či Firefox, případně telefon."
  );
}
