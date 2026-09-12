import { useState } from "react";
import { Link } from "react-router-dom";
import { Mail, Loader2, Send, CheckCircle2 } from "lucide-react";
import { api } from "../api";
import BrandLogo from "./BrandLogo";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);

  async function submit(e) {
    e.preventDefault();
    if (!email) return setError("Enter your email address.");
    setBusy(true);
    setError("");
    try {
      await api.forgotPassword(email);
      // Generic confirmation regardless of whether the account exists —
      // the backend never reveals that either.
      setSent(true);
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
          <h2 className="text-xl font-semibold tracking-tight text-slate-900">Reset your password</h2>
          <p className="mt-1 text-sm text-slate-500">
            Enter your account email and we'll send you a link to reset it.
          </p>
        </div>

        <div className="card animate-fade-up p-7">
          {sent ? (
            <div className="flex flex-col items-center gap-3 py-2 text-center">
              <div className="flex h-11 w-11 items-center justify-center rounded-full bg-emerald-50">
                <CheckCircle2 size={20} className="text-emerald-600" />
              </div>
              <p className="text-sm text-slate-600">
                If an account exists for <span className="font-medium text-slate-900">{email}</span>,
                we've sent a password reset link. Check your inbox — it expires in 30 minutes.
              </p>
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-4">
              <div className="relative">
                <Mail size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
                <input className="input !pl-10" type="email" placeholder="you@example.com" aria-label="Email address" required disabled={busy}
                  value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" autoFocus />
              </div>

              {error && (
                <p role="alert" className="animate-fade-up rounded-lg border border-red-200 bg-red-50 px-3.5 py-2.5 text-xs text-red-700">
                  {error}
                </p>
              )}

              <button type="submit" className="btn-primary w-full !py-3" disabled={busy}>
                {busy ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
                {busy ? "Sending..." : "Send reset link"}
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
