/**
 * ApplicationProfileSection — where a user saves the voluntary EEO answers the
 * automation reuses. What is worth pinning:
 *
 *  - a saved answer is shown (not silently reset to "Not set"), because the
 *    Save button writes EVERY field and a blank-looking form would wipe them;
 *  - "Not set" is sent as null, which is how a user clears an answer, and is
 *    never turned into a default like "decline_to_answer";
 *  - with no profile the section can't save at all — the API is per-profile and
 *    would 404.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ApplicationProfileSection from "../components/ApplicationProfileSection";
import { api, auth } from "../api";

const SAVED = {
  candidate_id: "p1",
  gender: "male",
  disability_status: "no_disability",
  veteran_status: null,
  race_ethnicity: "Asian",
  hispanic_latino: "no",
};

beforeEach(() => {
  auth.setToken("tok");
  vi.restoreAllMocks();
  vi.spyOn(api, "me").mockResolvedValue({ user_id: "u1", email: "simran@example.com" });
});

describe("account", () => {
  it("shows which account the answers are saved for", async () => {
    vi.spyOn(api, "getDemographics").mockResolvedValue(SAVED);
    render(<ApplicationProfileSection toast={vi.fn()} profileExists />);

    // Answers are stored per user and only ever read for the account that runs an
    // application; saving while signed in as someone else looks exactly like "not saved".
    expect(await screen.findByTestId("application-profile-account")).toHaveTextContent("simran@example.com");
  });

  it("still works when the account cannot be looked up", async () => {
    api.me.mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "getDemographics").mockResolvedValue(SAVED);
    render(<ApplicationProfileSection toast={vi.fn()} profileExists />);

    await waitFor(() => expect(screen.getByLabelText("Gender")).toHaveValue("male"));
    expect(screen.queryByTestId("application-profile-account")).not.toBeInTheDocument();
  });
});

describe("loading", () => {
  it("shows the answers already saved for this user", async () => {
    vi.spyOn(api, "getDemographics").mockResolvedValue(SAVED);
    render(<ApplicationProfileSection toast={vi.fn()} profileExists />);

    await waitFor(() => expect(screen.getByLabelText("Gender")).toHaveValue("male"));
    expect(screen.getByLabelText("Disability status")).toHaveValue("no_disability");
    expect(screen.getByLabelText("Race")).toHaveValue("Asian");
    expect(screen.getByLabelText("Hispanic / Latino")).toHaveValue("no");
    // Never asked stays "Not set" rather than being defaulted.
    expect(screen.getByLabelText("Veteran status")).toHaveValue("");
  });

  it("offers every field the backend stores", async () => {
    vi.spyOn(api, "getDemographics").mockResolvedValue({ candidate_id: "p1" });
    render(<ApplicationProfileSection toast={vi.fn()} profileExists />);

    for (const label of ["Gender", "Disability status", "Veteran status", "Race", "Hispanic / Latino"]) {
      expect(await screen.findByLabelText(label)).toBeInTheDocument();
    }
  });
});

describe("saving", () => {
  it("sends every field, with unset ones as null", async () => {
    vi.spyOn(api, "getDemographics").mockResolvedValue(SAVED);
    const put = vi.spyOn(api, "putDemographics").mockResolvedValue({ ...SAVED, veteran_status: "not_veteran" });
    const toast = vi.fn();
    render(<ApplicationProfileSection toast={toast} profileExists />);
    await waitFor(() => expect(screen.getByLabelText("Gender")).toHaveValue("male"));

    await userEvent.selectOptions(screen.getByLabelText("Veteran status"), "not_veteran");
    await userEvent.click(screen.getByRole("button", { name: /save application profile/i }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put).toHaveBeenCalledWith({
      gender: "male",
      disability_status: "no_disability",
      veteran_status: "not_veteran",
      race_ethnicity: "Asian",
      hispanic_latino: "no",
    });
    expect(toast).toHaveBeenCalledWith("Application profile saved.", "success");
  });

  it("clears a saved answer by sending null", async () => {
    vi.spyOn(api, "getDemographics").mockResolvedValue(SAVED);
    const put = vi.spyOn(api, "putDemographics").mockResolvedValue({ ...SAVED, gender: null });
    render(<ApplicationProfileSection toast={vi.fn()} profileExists />);
    await waitFor(() => expect(screen.getByLabelText("Gender")).toHaveValue("male"));

    await userEvent.selectOptions(screen.getByLabelText("Gender"), "");
    await userEvent.click(screen.getByRole("button", { name: /save application profile/i }));

    await waitFor(() => expect(put).toHaveBeenCalled());
    expect(put.mock.calls[0][0].gender).toBeNull();
  });

  it("reports a save failure instead of swallowing it", async () => {
    vi.spyOn(api, "getDemographics").mockResolvedValue(SAVED);
    vi.spyOn(api, "putDemographics").mockRejectedValue(new Error("Invalid gender 'x'."));
    const toast = vi.fn();
    render(<ApplicationProfileSection toast={toast} profileExists />);
    await waitFor(() => expect(screen.getByLabelText("Gender")).toHaveValue("male"));

    await userEvent.click(screen.getByRole("button", { name: /save application profile/i }));

    await waitFor(() => expect(toast).toHaveBeenCalledWith("Invalid gender 'x'.", "error"));
  });
});

describe("without a profile", () => {
  it("does not call the API and cannot be saved", async () => {
    const get = vi.spyOn(api, "getDemographics").mockResolvedValue(SAVED);
    render(<ApplicationProfileSection toast={vi.fn()} profileExists={false} />);

    expect(await screen.findByText(/save your profile above first/i)).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Gender")).toBeDisabled();
    expect(screen.getByRole("button", { name: /save application profile/i })).toBeDisabled();
  });
});
