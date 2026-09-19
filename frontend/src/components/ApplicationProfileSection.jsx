import { useEffect, useState } from "react";
import { Loader2, Save, ShieldCheck } from "lucide-react";
import { api } from "../api";

// The values below are exactly what the backend stores in `candidate_demographics`
// (see app/models/db_models.py: VALID_*_VALUES). Gender/disability/veteran/Hispanic
// are closed vocabularies the API validates; race is free text on the server and is
// stored worded the way application forms word it ("Asian"), which the option
// matcher then resolves against each form's own list.
const DECLINE = { value: "decline_to_answer", label: "Prefer not to say" };

export const APPLICATION_PROFILE_FIELDS = [
  {
    key: "gender", label: "Gender",
    options: [
      { value: "male", label: "Male" },
      { value: "female", label: "Female" },
      { value: "non_binary", label: "Non-binary" },
      { value: "self_described", label: "Prefer to self-describe" },
      DECLINE,
    ],
  },
  {
    key: "disability_status", label: "Disability status",
    options: [
      { value: "has_disability", label: "Yes, I have a disability (or have had one)" },
      { value: "no_disability", label: "No, I do not have a disability" },
      DECLINE,
    ],
  },
  {
    key: "veteran_status", label: "Veteran status",
    options: [
      { value: "veteran", label: "I am a protected veteran" },
      { value: "not_veteran", label: "I am not a protected veteran" },
      DECLINE,
    ],
  },
  {
    key: "race_ethnicity", label: "Race",
    options: [
      { value: "American Indian or Alaska Native", label: "American Indian or Alaska Native" },
      { value: "Asian", label: "Asian" },
      { value: "Black or African American", label: "Black or African American" },
      { value: "Native Hawaiian or Other Pacific Islander", label: "Native Hawaiian or Other Pacific Islander" },
      { value: "White", label: "White" },
      { value: "Two or More Races", label: "Two or more races" },
      DECLINE,
    ],
  },
  {
    key: "hispanic_latino", label: "Hispanic / Latino",
    options: [
      { value: "yes", label: "Yes" },
      { value: "no", label: "No" },
      DECLINE,
    ],
  },
];

const EMPTY = Object.fromEntries(APPLICATION_PROFILE_FIELDS.map((f) => [f.key, ""]));

function fromServer(data) {
  return Object.fromEntries(APPLICATION_PROFILE_FIELDS.map((f) => [f.key, data?.[f.key] ?? ""]));
}

/**
 * Voluntary EEO answers, saved once and reused on every application.
 *
 * Nothing here is ever guessed or defaulted: "Not set" is a real state (the
 * backend keeps NULL as "never asked") and is what a form question is left for a
 * human to answer under. Saving sends every field, so choosing "Not set" clears a
 * previously saved answer.
 */
export default function ApplicationProfileSection({ toast, profileExists }) {
  const [values, setValues] = useState(EMPTY);
  const [loading, setLoading] = useState(Boolean(profileExists));
  const [saving, setSaving] = useState(false);
  // Which account these answers belong to. They are stored per user, and an
  // application only ever reads the answers of the account that runs it — so
  // saving while signed in as a different account looks exactly like "not saved".
  const [email, setEmail] = useState(null);

  useEffect(() => {
    api.me().then((me) => setEmail(me?.email ?? null)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!profileExists) { setLoading(false); return undefined; }
    let cancelled = false;
    setLoading(true);
    api.getDemographics()
      .then((data) => { if (!cancelled) setValues(fromServer(data)); })
      .catch((error) => { if (!cancelled) toast(error.message, "error"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [profileExists]); // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    setSaving(true);
    try {
      const body = Object.fromEntries(
        APPLICATION_PROFILE_FIELDS.map((f) => [f.key, values[f.key] === "" ? null : values[f.key]]),
      );
      const saved = await api.putDemographics(body);
      setValues(fromServer(saved));
      toast("Application profile saved.", "success");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setSaving(false);
    }
  }

  const disabled = !profileExists || loading || saving;

  return (
    <div id="profile-application" className="card p-6">
      <h2 className="mb-1 flex items-center gap-2.5 font-semibold text-slate-900">
        <span className="flex h-7 w-7 items-center justify-center rounded-md bg-brand-50 text-brand-600">
          <ShieldCheck size={15} />
        </span>
        Application profile — voluntary self-identification
      </h2>
      <p className="mb-3 text-xs text-slate-500">
        Many applications ask these voluntary equal-opportunity questions. Answer once here and Autogram will
        use your answer on every form — including the Launch Agent. They are optional, never guessed, and never
        sent to an AI model. Leave a field as "Not set" and Autogram will ask you instead of answering.
      </p>
      {email && (
        <p className="mb-3 text-xs text-slate-600" data-testid="application-profile-account">
          Saved for account: <span className="font-semibold text-slate-800">{email}</span>
        </p>
      )}
      {!profileExists && (
        <p className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800" role="status">
          Save your profile above first — these answers are stored against it.
        </p>
      )}
      {loading ? (
        <div className="flex justify-center py-6"><Loader2 size={20} className="animate-spin text-brand-600" /></div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {APPLICATION_PROFILE_FIELDS.map((field) => (
            <label key={field.key} className="block">
              <span className="field-label">{field.label}</span>
              <select
                className="input"
                disabled={disabled}
                value={values[field.key]}
                onChange={(e) => setValues((v) => ({ ...v, [field.key]: e.target.value }))}
              >
                <option value="">Not set</option>
                {field.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
          ))}
        </div>
      )}
      <button className="btn-ghost mt-4 text-xs" disabled={disabled} onClick={save}>
        {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />} Save application profile
      </button>
    </div>
  );
}
