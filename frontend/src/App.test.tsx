import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, seriesColor } from "./App";

const series = [
  { id: "series-1", title: "Testserie", platform: "rtlplus", description: null },
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

  it("persists platform and series filters", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByRole("option", { name: "rtlplus" })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Plattform"), { target: { value: "rtlplus" } });
    fireEvent.click(screen.getByRole("button", { name: "Serie" }));
    fireEvent.click(screen.getByRole("option", { name: "Testserie" }));

    expect(localStorage.getItem("episode-calendar-platform")).toBe("rtlplus");
    expect(localStorage.getItem("episode-calendar-series")).toBe("series-1");
  });

  it("uses the same stable color in the series selector and legend", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByRole("option", { name: "rtlplus" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Serie" }));
    const picker = screen.getByRole("listbox", { name: "Serie" });
    const option = within(picker).getByRole("option", { name: "Testserie" });
    expect(option).toBeInTheDocument();
    expect(option.querySelector(".series-dot")).toHaveStyle({ backgroundColor: seriesColor("series-1") });
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

    expect(screen.getByRole("option", { name: "rtlplus" })).toBeInTheDocument();
    expect(screen.queryByText("Kalender wird geladen …")).not.toBeInTheDocument();
  });
});
