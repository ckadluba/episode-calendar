import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, episodeStartLabel, seriesColor } from "./App";

const series = [
  { id: "series-1", title: "Testserie", platform: "rtlplus", description: null },
  { id: "series-2", title: "Andere Serie", platform: "joyn", description: null },
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
    expect(screen.getByRole("heading", { name: "Joyn" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "RTL+" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Andere Serie" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Testserie" })).not.toBeChecked();
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
      "episode-calendar-cache-v1:http://localhost:8000:series",
      JSON.stringify({ timestamp: Date.now(), data: series }),
    );
    localStorage.setItem(
      "episode-calendar-cache-v1:http://localhost:8000:episodes:current:Europe/Vienna",
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
