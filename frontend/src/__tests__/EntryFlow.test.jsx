import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import App from "../App";
import { api, auth } from "../api";

vi.mock("../pages/Home", () => ({ default: () => <h1>Your dashboard</h1> }));
vi.mock("../api", () => ({
  auth: { getToken: vi.fn(), setToken: vi.fn(), clear: vi.fn() },
  setUnauthorizedHandler: vi.fn(),
  api: { me: vi.fn(), health: vi.fn(), listResumes: vi.fn(), signup: vi.fn(), login: vi.fn() },
}));

function Location() { return <output data-testid="location">{useLocation().pathname}</output>; }
function open(path = "/") { render(<MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter>); }

beforeEach(() => {
  vi.clearAllMocks();
  auth.getToken.mockReturnValue(null);
  api.health.mockResolvedValue({ status: "ok" });
  api.listResumes.mockResolvedValue({ resumes: [] });
  window.scrollTo = vi.fn();
});

describe("public entry experience", () => {
  it("shows the landing page without requiring authentication or fetching private data", () => {
    open();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Your career deserves more than manual applications.");
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(api.me).not.toHaveBeenCalled();
    expect(api.listResumes).not.toHaveBeenCalled();
  });

  it("takes Get Started to signup and supports switching to login", async () => {
    open();
    await userEvent.click(screen.getAllByRole("link", { name: "Get Started" })[0]);
    expect(screen.getByTestId("location")).toHaveTextContent("/signup");
    expect(screen.getAllByRole("button", { name: "Create account", exact: true }).find(button => button.type === "submit")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Log in instead" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
  });

  it("keeps the dashboard protected and opens it after login", async () => {
    api.login.mockResolvedValue({ access_token: "test-token", email: "alex@example.com" });
    open("/dashboard");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/login"));
    await userEvent.type(screen.getByLabelText("Email address"), "alex@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "password123");
    await userEvent.click(screen.getAllByRole("button", { name: "Log in", exact: true }).find(button => button.type === "submit"));
    expect(await screen.findByRole("heading", { name: "Your dashboard" })).toBeInTheDocument();
    expect(api.login).toHaveBeenCalledWith("alex@example.com", "password123");
    expect(auth.setToken).toHaveBeenCalledWith("test-token");
  });

  it("keeps the landing public for signed-in visitors and offers their dashboard", async () => {
    auth.getToken.mockReturnValue("existing-token");
    api.me.mockResolvedValue({ email: "alex@example.com" });
    open();
    expect(await screen.findByRole("link", { name: "Open your dashboard" })).toHaveAttribute("href", "/dashboard");
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/);
  });

  it("creates an account from signup and opens the dashboard", async () => {
    api.signup.mockResolvedValue({ access_token: "new-token", email: "new@example.com" });
    open("/signup");
    await userEvent.type(screen.getByLabelText("Email address"), "new@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "password123");
    await userEvent.click(screen.getAllByRole("button", { name: "Create account", exact: true }).find(button => button.type === "submit"));
    expect(await screen.findByRole("heading", { name: "Your dashboard" })).toBeInTheDocument();
    expect(api.signup).toHaveBeenCalledWith("new@example.com", "password123");
    expect(auth.setToken).toHaveBeenCalledWith("new-token");
  });

  it("returns an expired session to login without exposing the dashboard", async () => {
    auth.getToken.mockReturnValue("expired-token");
    api.me.mockRejectedValue(new Error("Session expired"));
    open("/dashboard");
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
    expect(screen.queryByRole("heading", { name: "Your dashboard" })).not.toBeInTheDocument();
  });

  it("does not discard a valid session when the resume library is unavailable", async () => {
    auth.getToken.mockReturnValue("existing-token");
    api.me.mockResolvedValue({ email: "alex@example.com" });
    api.listResumes.mockRejectedValue(new Error("Temporarily unavailable"));
    open("/login");
    expect(await screen.findByRole("heading", { name: "Your dashboard" })).toBeInTheDocument();
    expect(auth.clear).not.toHaveBeenCalled();
  });
});
