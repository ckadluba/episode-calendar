import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch, getApiActivitySnapshot, subscribeApiActivity } from "./api";

describe("apiFetch activity tracking", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps the activity active until every parallel request has settled", async () => {
    const resolveRequest: Array<(response: Response) => void> = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => resolveRequest.push(resolve))));
    const listener = vi.fn();
    const unsubscribe = subscribeApiActivity(listener);

    const first = apiFetch("/first");
    const second = apiFetch("/second");
    expect(getApiActivitySnapshot()).toBe(true);
    expect(listener).toHaveBeenCalledTimes(2);

    resolveRequest[0](new Response("first"));
    await first;
    expect(getApiActivitySnapshot()).toBe(true);

    resolveRequest[1](new Response("second"));
    await second;
    expect(getApiActivitySnapshot()).toBe(false);
    expect(listener).toHaveBeenCalledTimes(4);

    unsubscribe();
  });

  it("stops tracking when a request fails", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));

    await expect(apiFetch("/fail")).rejects.toThrow("offline");
    expect(getApiActivitySnapshot()).toBe(false);
  });
});
