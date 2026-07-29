import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { WorldBadge } from "./TaskReview";

describe("WorldBadge — what the annotator can tell about their world", () => {
  it("says nothing on the shared gym", () => {
    const { container } = render(<WorldBadge world="shared" onReset={() => {}} resetting={false} />);
    expect(container.textContent).toBe("");
  });

  it("shows a kept world as kept", () => {
    render(<WorldBadge world="preserved" onReset={() => {}} resetting={false} />);
    expect(screen.getByText("● World kept")).toBeTruthy();
  });

  it("shows a reseeded world as a fresh seed", () => {
    render(<WorldBadge world="seeded" onReset={() => {}} resetting={false} />);
    expect(screen.getByText("○ Fresh seed")).toBeTruthy();
  });

  it("reports a fully rebuilt fork prefix so the annotator knows the world is at the fork point", () => {
    render(<WorldBadge world="seeded" restore={{ done: 5, total: 5, partial: false, reason: "" }} onReset={() => {}} resetting={false} />);
    expect(screen.getByText("↺ Rebuilt to step 5")).toBeTruthy();
  });

  it("flags a partial rebuild — the world is SHORT of where the annotator thinks", () => {
    render(<WorldBadge world="seeded" restore={{ done: 2, total: 5, partial: true, reason: "step 3 could not be rebuilt" }} onReset={() => {}} resetting={false} />);
    const pill = screen.getByText("⚠ Rebuilt 2/5");
    expect(pill).toBeTruthy();
    expect(pill.getAttribute("title")).toContain("step 3 could not be rebuilt");
  });

  it("shows no rebuild pill for an unforked attempt (restore null)", () => {
    render(<WorldBadge world="seeded" restore={null} onReset={() => {}} resetting={false} />);
    expect(screen.queryByText(/Rebuilt/)).toBeNull();
  });

  it("offers the start-over escape hatch, and calls it", () => {
    const onReset = vi.fn();
    render(<WorldBadge world="preserved" onReset={onReset} resetting={false} />);
    fireEvent.click(screen.getByText("⟲ Reset world"));
    expect(onReset).toHaveBeenCalledOnce();
  });

  it("does not fire reset while a reset is already running", () => {
    const onReset = vi.fn();
    render(<WorldBadge world="preserved" onReset={onReset} resetting={true} />);
    fireEvent.click(screen.getByText("Resetting…"));
    expect(onReset).not.toHaveBeenCalled();
  });
});
