import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App, seriesColor } from "./App";

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

    await waitFor(() => expect(screen.getByRole("button", { name: "Serien" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Serien" }));
    const picker = screen.getByRole("listbox", { name: "Serie" });
    fireEvent.click(within(picker).getByRole("checkbox", { name: /Testserie/ }));

    expect(JSON.parse(localStorage.getItem("episode-calendar-series-selection") ?? "null")).toEqual(["series-2"]);
  });

  it("uses the same stable color in the series selector and legend", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Serien" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Serien" }));
    const picker = screen.getByRole("listbox", { name: "Serie" });
    const option = within(picker).getByRole("option", { name: /Testserie/ });
    expect(option).toHaveTextContent("Testserie (rtlplus)");
    expect(option.querySelector(".series-dot")).toHaveStyle({ backgroundColor: seriesColor("series-1") });
  });

  it("restores the saved series selection and sorts entries by title", async () => {
    localStorage.setItem("episode-calendar-series-selection", JSON.stringify(["series-2"]));
    render(<App />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Serien" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Serien" }));
    const options = within(screen.getByRole("listbox", { name: "Serie" })).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual(["Alle Serien", "Andere Serie (joyn)", "Testserie (rtlplus)"]);
    expect(within(options[1]).getByRole("checkbox")).toBeChecked();
    expect(within(options[2]).getByRole("checkbox")).not.toBeChecked();
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

    expect(screen.getByRole("button", { name: "Serien" })).toBeInTheDocument();
    expect(screen.queryByText("Kalender wird geladen …")).not.toBeInTheDocument();
  });
});
