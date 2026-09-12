import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Lock, Loader2, KeyRound, CheckCircle2 } from "lucide-react";
import { api } from "../api";
import BrandLogo from "./BrandLogo";

const MIN_PASSWORD_LENGTH = 8;

export default function ResetPasswordPage() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") || "";
  const navigate = useNavigate();

  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (!token) return setError("This reset link is missing its token — request a new one.");
    if (password.length < MIN_PASSWORD_LENGTH) return setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
    if (password !== confirmPassword) return setError("Passwords don't match.");

    setBusy(true);
    setError("");
    try {
      await api.resetPassword(token, password);
      setDone(true);
      setTimeout(() => navigate("/login", { replace: true }), 2500);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <Link to="/" className="mb-4" aria-label="Autogram home"><BrandLogo /></Link>
          <h2 className="text-xl font-semibold tracking-tight text-slate-900">Set a new password</h2>
          <p className="mt-1 text-sm text-slate-500">Choose a new password for your account.</p>
        </div>

        <div className="card animate-fade-up p-7">
          {!token && (
            <p role="alert" className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-2.5 text-xs text-amber-700">
              This link is missing its reset token. Request a new one from the login page.
            </p>
          )}

          {done ? (
            <div className="flex flex-col items-center gap-3 py-2 text-center">
              <div className="flex h-11 w-11 items-center justify-center rounded-full bg-emerald-50">
                <CheckCircle2 size={20} className="text-emerald-600" />
              </div>
              <p className="text-sm text-slate-600">
                Your password has been reset. Redirecting you to log in...
              </p>
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-4">
              <div className="relative">
                <Lock size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
                <input className="input !pl-10" type="password" aria-label="New password" required minLength={MIN_PASSWORD_LENGTH} disabled={busy}
                  placeholder={`New password (min ${MIN_PASSWORD_LENGTH} characters)`}
                  value={password} onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password" autoFocus />
              </div>
              <div className="relative">
                <Lock size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
                <input className="input !pl-10" type="password" aria-label="Confirm new password" required disabled={busy}
                  placeholder="Confirm new password"
                  value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)}
                  autoComplete="new-password" />
              </div>

              {error && (
                <p role="alert" className="animate-fade-up rounded-lg border border-red-200 bg-red-50 px-3.5 py-2.5 text-xs text-red-700">
                  {error}
                </p>
              )}

              <button type="submit" className="btn-primary w-full !py-3" disabled={busy || !token}>
                {busy ? <Loader2 size={16} className="animate-spin" /> : <KeyRound size={16} />}
                {busy ? "Resetting..." : "Reset password"}
              </button>
            </form>
          )}

          <p className="mt-5 text-center text-xs text-slate-500">
            <Link to="/login" className="font-semibold text-brand-600 hover:text-brand-700">
              Back to log in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
