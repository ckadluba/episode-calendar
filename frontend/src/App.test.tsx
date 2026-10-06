import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, calendarItems, episodeStartLabel, selectCalendarRelease, seriesColor, sortCalendarItems, type Release } from "./App";

const series = [
  { id: "series-1", title: "Testserie", platform: "RTL+", platform_id: "rtlplus", description: null },
  { id: "series-2", title: "Andere Serie", platform: "Joyn.at", platform_id: "joyn", description: null },
];

function mockApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const data = url.endsWith("/series") ? series : [];
      return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }));
    }),
  );
}

describe("App preferences", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    mockApi();
  });

  it("restores the selected week after a reload", async () => {
    localStorage.setItem("episode-calendar-week", "next");

    render(<App />);

    expect(screen.getByRole("button", { name: "Nächste Woche" })).toHaveClass("active");
    await waitFor(() => expect(screen.queryByText("Kalender wird geladen …")).not.toBeInTheDocument());
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("includePreviews=all"));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("includeReleaseHistory=true"));
  });

  it("supports switching to last week", async () => {
    render(<App />);

    expect(screen.getByRole("button", { name: "Letzte Woche" })).not.toHaveClass("active");
    fireEvent.click(screen.getByRole("button", { name: "Letzte Woche" }));
    expect(screen.getByRole("button", { name: "Letzte Woche" })).toHaveClass("active");
    await waitFor(() => expect(screen.queryByText("Kalender wird geladen …")).not.toBeInTheDocument());
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/v1/episodes/last-week?"));
  });

  it("uses a future-oriented heading for next week", async () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "Nächste Woche" }));
    expect(screen.getByRole("heading", { name: "Was läuft nächste Woche?" })).toBeInTheDocument();
    expect(screen.getByText(/Neue Episoden deiner Serien.*Zukünftige Daten können unvollständig sein\./)).toBeInTheDocument();
  });

  it("uses abbreviated weekday labels in the calendar", async () => {
    render(<App />);

    await waitFor(() => expect(screen.queryByText("Kalender wird geladen …")).not.toBeInTheDocument());
    expect(screen.getAllByRole("heading", { level: 2 })[0]).toHaveTextContent(/^(Mo|Di|Mi|Do|Fr|Sa|So)\., \d{1,2}\. /);
  });

  it("persists an arbitrary series selection", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("2 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Testserie" }));
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));

    expect(JSON.parse(localStorage.getItem("episode-calendar-series-selection") ?? "null")).toEqual({ selected: ["series-2"], known: ["series-1", "series-2"] });
  });

  it("uses the same stable color in the series filter and legend", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("2 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    const checkbox = screen.getByRole("checkbox", { name: "Testserie" });
    expect(checkbox.parentElement?.querySelector(".series-dot")).toHaveStyle({ backgroundColor: seriesColor("series-1") });
  });

  it("restores the saved series selection and groups entries by platform", async () => {
    localStorage.setItem("episode-calendar-series-selection", JSON.stringify(["series-2"]));
    render(<App />);

    await waitFor(() => expect(screen.getByText("1 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    expect(screen.getByRole("heading", { name: /Joyn\.at/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /RTL\+/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Andere Serie" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Testserie" })).not.toBeChecked();
  });

  it("selects or clears all series for one provider", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("2 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    const providerCheckbox = screen.getByRole("checkbox", { name: "Alle Serien von Joyn.at auswählen" });

    fireEvent.click(providerCheckbox);
    expect(screen.getByRole("checkbox", { name: "Andere Serie" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Testserie" })).toBeChecked();

    fireEvent.click(providerCheckbox);
    expect(screen.getByRole("checkbox", { name: "Andere Serie" })).toBeChecked();
  });

  it("persists the preview setting next to a provider", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("2 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    const previewCheckbox = screen.getByRole("checkbox", { name: "Vorab-Releases für Joyn.at anzeigen" });
    expect(previewCheckbox).toBeChecked();
    const requestCount = vi.mocked(fetch).mock.calls.length;
    fireEvent.click(previewCheckbox);
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));

    expect(JSON.parse(localStorage.getItem("episode-calendar-preview-preferences") ?? "null")).toEqual({
      rtlplus: true,
      joyn: false,
    });
    expect(vi.mocked(fetch).mock.calls).toHaveLength(requestCount);
  });

  it("selects or clears all series globally", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("2 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    fireEvent.click(screen.getByRole("button", { name: "Keine auswählen" }));
    expect(screen.getByRole("checkbox", { name: "Testserie" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Andere Serie" })).not.toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Alle auswählen" }));
    expect(screen.getByRole("checkbox", { name: "Testserie" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Andere Serie" })).toBeChecked();
  });

  it("selects series added since the saved filter", async () => {
    localStorage.setItem("episode-calendar-series-selection", JSON.stringify({ selected: ["series-2"], known: ["series-2"] }));
    render(<App />);

    await waitFor(() => expect(screen.getByText("2 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    expect(screen.getByRole("checkbox", { name: "Testserie" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Andere Serie" })).toBeChecked();
  });

  it("discards changes made on the filter page", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("2 von 2 Serien ausgewählt")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Testserie" }));
    fireEvent.click(screen.getByRole("button", { name: "Verwerfen" }));
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    expect(screen.getByRole("checkbox", { name: "Testserie" })).toBeChecked();
  });

  it("renders cached API data without waiting for the network", () => {
    localStorage.setItem(
      "episode-calendar-cache-v2:http://localhost:8000:series",
      JSON.stringify({ timestamp: Date.now(), data: series }),
    );
    localStorage.setItem(
      "episode-calendar-cache-v2:http://localhost:8000:episodes:current:Europe/Vienna",
      JSON.stringify({ timestamp: Date.now(), data: [] }),
    );
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => undefined)));

    render(<App />);

    expect(screen.getByRole("button", { name: "Filter" })).toBeInTheDocument();
    expect(screen.queryByText("Kalender wird geladen …")).not.toBeInTheDocument();
  });
});

describe("episode start labels", () => {
  it("labels first episodes as new series or new seasons", () => {
    expect(episodeStartLabel({ season_number: 1, number: 1 })).toBe("Neue Serie");
    expect(episodeStartLabel({ season_number: null, number: 1 })).toBe("Neue Serie");
    expect(episodeStartLabel({ season_number: 2, number: 1 })).toBe("Neue Staffel");
    expect(episodeStartLabel({ season_number: 2, number: 2 })).toBeNull();
  });
});

describe("calendar releases", () => {
  const episodeWithReleases = (releases: Release[]) => ({
    id: "episode-1",
    series_id: "series-1",
    season_number: 1,
    number: 1,
    title: "Pilot",
    description: null,
    platform: "Joyn.at",
    platform_id: "joyn",
    releases,
  });

  it("shows exactly one preferred release per episode", () => {
    const preview = { release_type: "streaming", release_at: "2026-09-28T18:00:00Z", url: "https://example.test/preview", preview: true };
    const regular = { release_type: "streaming", release_at: "2026-10-01T18:00:00Z", url: "https://example.test/regular", preview: false };
    const tv = { release_type: "tv_broadcast", release_at: "2026-10-02T18:00:00Z", url: null, preview: false };

    expect(selectCalendarRelease(episodeWithReleases([preview, regular, tv]), true)).toBe(preview);
    expect(selectCalendarRelease(episodeWithReleases([preview, regular, tv]), false)).toBe(regular);
    expect(selectCalendarRelease(episodeWithReleases([preview, tv]), true)).toBe(preview);
    expect(selectCalendarRelease(episodeWithReleases([preview]), false)).toBeNull();
    expect(selectCalendarRelease(episodeWithReleases([preview]), true)).toBe(preview);
    expect(selectCalendarRelease(episodeWithReleases([tv]), false)).toBe(tv);
  });

  it("does not show the same episode again on its later TV release date", () => {
    const preview = { release_type: "streaming", release_at: "2026-09-29T22:00:00Z", url: "https://example.test/preview", preview: true };
    const tv = { release_type: "tv_broadcast", release_at: "2026-10-06T20:35:00Z", url: null, preview: false };
    const from = new Date("2026-10-05T00:00:00+02:00");
    const to = new Date("2026-10-12T00:00:00+02:00");

    expect(selectCalendarRelease(episodeWithReleases([preview, tv]), true, from, to)).toBeNull();
    expect(selectCalendarRelease(episodeWithReleases([preview, tv]), true, new Date("2026-09-28T00:00:00Z"), from)).toBe(preview);
    expect(selectCalendarRelease(episodeWithReleases([preview, tv]), false, from, to)).toBe(tv);
  });

  it("uses the later TV date when a week-earlier catalog release is a preview", () => {
    const preview = { release_type: "streaming", release_at: "2026-10-06T22:00:00Z", url: "https://example.test/preview", preview: true };
    const tv = { release_type: "tv_broadcast", release_at: "2026-10-13T18:15:00Z", url: null, preview: false };
    const episode = episodeWithReleases([preview, tv]);

    expect(selectCalendarRelease(episode, true, new Date("2026-10-05T00:00:00Z"), new Date("2026-10-12T00:00:00Z"))).toBe(preview);
    expect(selectCalendarRelease(episode, false, new Date("2026-10-05T00:00:00Z"), new Date("2026-10-12T00:00:00Z"))).toBeNull();
    expect(selectCalendarRelease(episode, false, new Date("2026-10-12T00:00:00Z"), new Date("2026-10-19T00:00:00Z"))).toBe(tv);
  });

  it("keeps every release date of an episode", () => {
    const episode = {
      id: "episode-1",
      series_id: "series-1",
      season_number: 1,
      number: 1,
      title: "Pilot",
      description: null,
      platform: "ARD Mediathek",
      platform_id: "ardmediathek",
      releases: [
        { release_type: "streaming", release_at: "2026-09-28T18:00:00Z", url: null, preview: false },
        { release_type: "tv_broadcast", release_at: "2026-10-01T17:15:00Z", url: null, preview: false },
      ],
    };

    expect(calendarItems([episode]).map((item) => item.release.release_at)).toEqual([
      "2026-09-28T18:00:00Z",
      "2026-10-01T17:15:00Z",
    ]);
  });

  it("sorts episodes by release time, series title, season, then episode", () => {
    const makeItem = (
      id: string,
      seriesId: string,
      releaseAt: string,
      seasonNumber: number | null,
      episodeNumber: number | null,
    ) => ({
      episode: {
        id,
        series_id: seriesId,
        season_number: seasonNumber,
        number: episodeNumber,
        title: id,
        description: null,
        platform: "Test",
        platform_id: "test",
        releases: [],
      },
      release: { release_type: "streaming", release_at: releaseAt, url: null, preview: false },
    });
    const items = [
      makeItem("later-time", "series-2", "2026-10-01T10:01:00Z", 1, 1),
      makeItem("series-b", "series-2", "2026-10-01T10:00:00Z", 1, 1),
      makeItem("season-2", "series-1", "2026-10-01T10:00:00Z", 2, 1),
      makeItem("episode-10", "series-1", "2026-10-01T10:00:00Z", 1, 10),
      makeItem("episode-2", "series-1", "2026-10-01T10:00:00Z", 1, 2),
    ];

    expect(sortCalendarItems(items, new Map(series.map((item) => [item.id, item]))).map(({ episode }) => episode.id))
      .toEqual(["series-b", "episode-2", "episode-10", "season-2", "later-time"]);
  });
});
