import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PendingActionCard } from "./PendingActionCard";

const base = {
  action_id: "action-1",
  action_type: "calendar.create",
  payload: { summary: "喝水" },
  result: {},
  error_code: "",
  error_message: "",
  created_at: 1,
} as const;

describe("PendingActionCard", () => {
  it("shows confirm while awaiting confirmation", () => {
    const onConfirm = vi.fn();
    render(
      <PendingActionCard
        action={{ ...base, status: "AWAITING_CONFIRMATION" }}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("确认"));
    expect(onConfirm).toHaveBeenCalledWith("action-1");
  });

  it("shows retry only for retryable failures", () => {
    render(
      <PendingActionCard
        action={{ ...base, status: "FAILED_RETRYABLE", error_message: "429" }}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("重试")).toBeInTheDocument();
    expect(screen.queryByText("确认")).not.toBeInTheDocument();
  });

  it.each([
    ["CONFIRMED", "CONFIRMED"],
    ["EXECUTING", "EXECUTING"],
    ["SUCCEEDED", "SUCCEEDED"],
  ] as const)("renders backend state %s without decision buttons", (status, label) => {
    render(
      <PendingActionCard
        action={{ ...base, status }}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.queryByText("确认")).not.toBeInTheDocument();
    expect(screen.queryByText("重试")).not.toBeInTheDocument();
  });

  it("shows final failure without retry", () => {
    render(
      <PendingActionCard
        action={{
          ...base,
          status: "FAILED_FINAL",
          error_message: "参数错误",
        }}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByText("参数错误")).toBeInTheDocument();
    expect(screen.queryByText("重试")).not.toBeInTheDocument();
  });
});
